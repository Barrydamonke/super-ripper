"""The rip_cd job: read an audio CD with cyanrip, then hand the files on.

Once the audio is safely ripped the job counts as done. Anything that goes wrong
afterwards (identifying, tagging, filing) puts the rip on the Music page's
"Needs a look" list instead of failing the job: the disc has already been ejected.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import uuid
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

import json

from . import cd_rips, cyanrip, filing, jellyfin, jobs, library, musicbrainz, runner, settings, tagging
from .cdrom import CdDrive
from .cdtoc import Toc, musicbrainz_disc_id
from .clock import utc_now
from .cyanrip import TrackCheck
from .filing import AlbumPlan
from .musicbrainz import Candidate

log = logging.getLogger(__name__)

WORK_KEEP_DAYS = 7
DRIVE_LOCK_TIMEOUT = 10.0
# Only rips that never finished leave audio in /work; a done rip's files have moved on.
_SWEEPABLE = ("failed", "cancelled", "interrupted")


def running_process_count() -> int:
    return len(runner.LIVE_PROCESSES)


def _drive(ctx, sr_name: str):
    path = ctx.config.dev_root / sr_name
    factory = getattr(ctx.services, "drive_factory", None) if ctx.services else None
    return factory(path) if factory else CdDrive(path)


def _with_drive_lock(ctx, fn):
    """Run fn() holding the drive's lock (shared with the poller and eject)."""
    monitor = getattr(ctx.services, "monitor", None) if ctx.services else None
    if monitor is None:
        return fn()
    lock = monitor.drive_lock(ctx.drive_id)
    if not lock.acquire(timeout=DRIVE_LOCK_TIMEOUT):
        raise RuntimeError("The drive is busy.")
    try:
        return fn()
    finally:
        lock.release()


def _read_settings(conn) -> tuple[str, int]:
    return settings.get(conn, "cd_rip_mode"), settings.get(conn, "cd_retries")


async def run_rip_cd(ctx: runner.JobContext) -> None:
    params = ctx.params
    disc_id = params["disc_id"]
    toc = Toc.from_json(params["toc"])
    sr_name = params["sr_name"]
    await ctx.db(cd_rips.ensure, ctx.job_id, disc_id, utc_now())

    # A retry may find another disc in the drive by now: never rip it under this disc's identity.
    current = await asyncio.to_thread(_with_drive_lock, ctx, lambda: _drive(ctx, sr_name).read_toc())
    if musicbrainz_disc_id(current) != disc_id:
        raise RuntimeError("A different disc is in the drive.")

    work = ctx.config.work_dir / str(ctx.job_id)
    for old in await ctx.db(cd_rips.earlier_unfinished, disc_id, ctx.job_id):
        shutil.rmtree(ctx.config.work_dir / str(old), ignore_errors=True)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)

    mode, retries = await ctx.db(_read_settings)
    argv = cyanrip.build_argv(device=ctx.config.dev_root / sr_name, offset=int(params.get("offset", 0)),
                              retries=retries, mode=mode)
    tracks = toc.audio_tracks
    parser = cyanrip.ProgressParser()
    tail: list[str] = []
    started = time.monotonic()

    async def on_line(line: str) -> None:
        if line.strip():
            tail.append(line.strip())
            del tail[:-5]
        progress = parser.feed(line)
        if progress is None:
            return
        number, fraction = progress
        index = tracks.index(number) if number in tracks else 0
        overall = (index + fraction) / len(tracks)
        finish = None
        if overall > 0.02:
            remaining = (time.monotonic() - started) * (1 - overall) / overall
            finish = datetime.now(timezone.utc) + timedelta(seconds=remaining)
        await ctx.report(overall * 0.99, f"Track {index + 1} of {len(tracks)} — {fraction:.0%}", finish)

    code = await ctx.run_process(argv, cwd=work, on_line=on_line)
    rip_dir = work / "rip"
    files = cyanrip.track_files(rip_dir) if rip_dir.is_dir() else {}
    if code != 0 or any(t not in files for t in tracks):
        detail = " / ".join(tail) or f"exit code {code}"
        raise RuntimeError(f"cyanrip failed: {detail}")

    logs = sorted(rip_dir.glob("*.log"))
    log_text = logs[0].read_text(encoding="utf-8", errors="replace") if logs else ""
    checks = cyanrip.parse_accuraterip(log_text, tracks)
    await ctx.db(cd_rips.update, ctx.job_id, accuraterip=[c.__dict__ for c in checks])
    await ctx.report(0.99, "Ripped — identifying…")

    try:
        await asyncio.to_thread(_with_drive_lock, ctx, lambda: _drive(ctx, sr_name).eject())
    except Exception as exc:  # a stuck tray mustn't lose a finished rip
        log.warning("cdrip: eject after rip #%s failed: %s", ctx.job_id, exc)

    try:
        await after_rip(ctx, disc_id=disc_id, toc=toc, checks=checks, rip_dir=rip_dir)
    except asyncio.CancelledError:
        # The audio is safe in downloads; record it before the cancellation carries on.
        _record_sync(ctx, state="needs_look",
                     reasons=["identification was interrupted — choose the release or type the details in"])
        raise
    except Exception as exc:
        log.exception("cdrip: after rip #%s failed", ctx.job_id)
        await ctx.db(cd_rips.update, ctx.job_id, state="needs_look",
                     reasons=[f"Couldn't finish after the rip: {exc}"])
    await ctx.report(1.0, "Ripped")


def _record_sync(ctx, **fields) -> None:
    """A cd_rips write that doesn't await (for use while being cancelled)."""
    from . import db

    conn = db.connect(ctx.config.db_path)
    try:
        cd_rips.update(conn, ctx.job_id, **fields)
    finally:
        conn.close()


# --- after the rip: identify, decide, file ------------------------------------------

REASON_NO_MATCH = "no MusicBrainz match"
REASON_SEVERAL = "several possible releases"
REASON_DUPLICATE = "already in the library"
REASON_OTHER_EDITION = "another edition of an album in the library"
REASON_MB_DOWN = "MusicBrainz unreachable"
REASON_ALWAYS = "you asked to check every album"
REASON_TRACKS = "MusicBrainz track list doesn't match the disc"
REASON_EXISTS = "a file with that name is already in the library"


def decide(
    *,
    chosen: Candidate | None,
    groups: int,
    checks: list[TrackCheck],
    duplicate: bool,
    other_edition: bool,
    waiting_sibling: int | None,
    approval: str,
    mb_error: bool,
    track_mismatch: bool = False,
) -> list[str]:
    """Why this rip needs a look. An empty list means: file it automatically."""
    reasons = []
    if mb_error:
        reasons.append(REASON_MB_DOWN)
    elif groups == 0:
        reasons.append(REASON_NO_MATCH)
    elif chosen is None:
        reasons.append(REASON_SEVERAL)
    if track_mismatch:
        reasons.append(REASON_TRACKS)
    unverified = sum(1 for c in checks if c.status != "verified")
    if unverified:
        reasons.append(f"{unverified} track(s) not verified")
    if duplicate:
        reasons.append(REASON_DUPLICATE)
    if other_edition:
        reasons.append(REASON_OTHER_EDITION)
    if waiting_sibling is not None:
        reasons.append(f"disc {waiting_sibling} of an album waiting for a look")
    if not reasons and approval == "always":
        reasons.append(REASON_ALWAYS)
    return reasons


_ALBUM_LEVEL = filing.ALBUM_LEVEL


def filed_groups(conn) -> dict[str, int]:
    """Release group -> disc count of albums already in the library."""
    rows = conn.execute("SELECT release_group_id, disc_total FROM albums WHERE release_group_id IS NOT NULL")
    return {r[0]: r[1] for r in rows}


def joined_album(conn, cand: Candidate):
    """The library album this disc belongs to: same release, or same group and disc count."""
    row = conn.execute(
        "SELECT albums.* FROM albums JOIN album_releases USING (album_key) WHERE release_id = ?",
        (cand.release_id,),
    ).fetchone()
    if row is None and cand.release_group_id:
        row = conn.execute(
            "SELECT * FROM albums WHERE release_group_id = ? AND disc_total = ? ORDER BY filed_at LIMIT 1",
            (cand.release_group_id, cand.medium_count),
        ).fetchone()
    return row


def build_plan(conn, cand: Candidate, files: list[Path], disc_id: str) -> AlbumPlan:
    row = joined_album(conn, cand)
    first = musicbrainz.tags_for(cand, cand.tracks[0], disc_id) if cand.tracks else {}
    if row is not None:
        album_key = row["album_key"]
        album_tags = json.loads(row["album_tags"])
        album_artist, album, year = row["album_artist"], row["album"], row["year"]
    else:
        album_key = cand.release_id
        album_tags = {k: first.get(k, "") for k in _ALBUM_LEVEL}
        album_artist, album, year = cand.album_artist, cand.title, cand.year
    tracks = []
    for path, track in zip(files, cand.tracks, strict=True):
        tags = musicbrainz.tags_for(cand, track, disc_id)
        tags.update({k: v for k, v in album_tags.items() if k in _ALBUM_LEVEL})
        tracks.append((path, tags))
    return AlbumPlan(
        album_key=album_key,
        release_id=cand.release_id,
        release_group_id=cand.release_group_id or None,
        album_artist=album_artist,
        album=album,
        year=year,
        compilation=cand.compilation,
        disc=cand.medium if cand.medium_count > 1 else None,
        disc_total=cand.medium_count,
        disc_id=disc_id,
        album_tags=album_tags,
        tracks=tracks,
    )


def candidate_snapshot(cands: list[Candidate]) -> list[dict]:
    """Everything needed to tag from a candidate later, without asking MusicBrainz again."""
    return [dataclasses.asdict(c) for c in cands]


def waiting_sibling(conn, job_id: int, cands: list[Candidate]) -> int | None:
    """This disc's number, if another disc of the same album is waiting on the Music page."""
    mine = {(c.release_group_id, c.medium) for c in cands if c.release_group_id}
    if not mine:
        return None
    for row in cd_rips.needs_look(conn):
        if row["job_id"] == job_id:
            continue
        theirs = {(c.get("release_group_id"), c.get("medium")) for c in json.loads(row["candidates"] or "[]")}
        for group, medium in mine:
            if any(g == group and m != medium for g, m in theirs):
                return medium
    return None


def summary_for(album_artist: str, album: str, year: str) -> str:
    return f"{album_artist} — {album}" + (f" ({year})" if year else "")


def _move_to_downloads(rip_dir: Path, files: list[Path], dest: Path) -> list[Path]:
    """Move the ripped files (and the log) out of the work area as NN.flac.

    All or nothing: if a move fails, the files already moved go back, so the audio
    is always in exactly one place."""
    dest.mkdir(parents=True, exist_ok=True)
    moved: list[tuple[Path, Path]] = []
    try:
        for n, src in enumerate(files, start=1):
            target = dest / f"{n:02d}.flac"
            filing._move(src, target)
            moved.append((src, target))
    except BaseException:
        for src, target in reversed(moved):
            try:
                filing._move(target, src)
            except OSError:
                pass
        raise
    moved = [target for _src, target in moved]
    for log_file in rip_dir.glob("*.log"):
        filing._move(log_file, dest / "rip.log")
        break
    return moved


async def after_rip(ctx, *, disc_id: str, toc: Toc, checks: list[TrackCheck], rip_dir: Path) -> None:
    """Move the audio out of the work area and record where it is, then identify and file.

    The move and the record come first, before any network call, so whatever
    happens later the files are kept and the Music page knows where they are.
    """
    files_by_track = cyanrip.track_files(rip_dir)
    files = [files_by_track[t] for t in toc.audio_tracks]
    dest = ctx.config.downloads_dir / str(ctx.job_id)
    # Record where the audio is at every moment: a stop or a failed move must never leave
    # the Music page looking in the wrong place.
    await ctx.db(cd_rips.update, ctx.job_id, downloads_path=str(rip_dir),
                 summary=f"Unknown album · {len(files)} tracks")
    try:
        files = await asyncio.to_thread(_move_to_downloads, rip_dir, files, dest)
    except Exception:
        log.exception("cdrip: #%s: moving the rip to %s failed; it stays in %s", ctx.job_id, dest, rip_dir)
        raise
    await ctx.db(cd_rips.update, ctx.job_id, downloads_path=str(dest))
    shutil.rmtree(rip_dir.parent, ignore_errors=True)  # the work folder is done with
    await _identify(ctx, disc_id=disc_id, files=files, dest=dest, checks=checks)


async def _identify(ctx, *, disc_id: str, files: list[Path], dest: Path, checks: list[TrackCheck]) -> None:
    services = ctx.services
    approval, cover_on = await ctx.db(lambda conn: (settings.get(conn, "cd_approval"), settings.get(conn, "cd_cover_art")))

    mb_error = False
    try:
        releases = await asyncio.to_thread(services.musicbrainz.lookup_discid, disc_id)
        cands = musicbrainz.candidates_for_disc(releases, disc_id)
    except Exception as exc:  # unreachable, or an answer we can't read: treat both as "no answer"
        log.warning("cdrip: #%s: MusicBrainz: %s", ctx.job_id, exc)
        cands, mb_error = [], True
    groups = musicbrainz.group_equivalent(cands)
    filed = await ctx.db(filed_groups)
    chosen = musicbrainz.choose(groups, filed_groups=filed)
    track_mismatch = chosen is not None and len(chosen.tracks) != len(files)
    if track_mismatch:
        chosen = None

    plan = await ctx.db(build_plan, chosen, files, disc_id) if chosen else None
    cover = None
    if chosen and cover_on:
        data = await asyncio.to_thread(services.cover_fetch, chosen.release_id)
        if data:
            cover = dest / "cover.jpg"
            cover.write_bytes(data)
    if plan:
        for path, tags in plan.tracks:
            await asyncio.to_thread(tagging.write_tags, path, tags)

    duplicate = False
    notes = []
    joining = False
    if plan:
        row = await ctx.db(lambda conn: conn.execute(
            "SELECT discs FROM albums WHERE album_key = ?", (plan.album_key,)).fetchone())
        joining = row is not None
        duplicate = joining and str(plan.disc or 1) in json.loads(row["discs"])
        # Jellyfin only catches albums this app didn't file. A disc joining an album
        # we filed would always match its own disc 1 there.
        client = await asyncio.to_thread(services.jellyfin) if not joining else None
        if client is not None:
            release = plan.album_tags.get("MUSICBRAINZ_ALBUMID") or chosen.release_id
            try:
                duplicate = await asyncio.to_thread(client.has_album, release, plan.album)
            except jellyfin.JellyfinError as exc:
                notes.append("Jellyfin check failed")
                log.warning("cdrip: #%s: Jellyfin check failed: %s", ctx.job_id, exc)
    # Same release group already filed with a different number of discs (e.g. a deluxe edition):
    # never join or file it automatically.
    other_edition = not joining and any(
        c.release_group_id in filed and filed[c.release_group_id] != c.medium_count for c in cands
    )
    sibling = await ctx.db(waiting_sibling, ctx.job_id, cands)

    reasons = decide(
        chosen=chosen, groups=len(groups), checks=checks, duplicate=duplicate, other_edition=other_edition,
        waiting_sibling=sibling, approval=approval, mb_error=mb_error, track_mismatch=track_mismatch,
    )
    summary = summary_for(plan.album_artist, plan.album, plan.year) if plan else f"Unknown album · {len(files)} tracks"
    if notes:
        summary += " · " + ", ".join(notes)
    fields = dict(
        candidates=candidate_snapshot(cands), release_id=chosen.release_id if chosen else None,
        medium=chosen.medium if chosen else None, downloads_path=str(dest), summary=summary,
    )

    if not reasons:
        try:
            folder = await ctx.db(
                lambda conn: filing.file_album(conn, plan, ctx.config.music_dir, cover=cover, now=utc_now())
            )
        except filing.FileExists:
            reasons = [REASON_EXISTS]
        else:
            shutil.rmtree(dest, ignore_errors=True)
            await ctx.db(cd_rips.update, ctx.job_id, state="filed", reasons=[], filed_path=str(folder),
                         album_key=plan.album_key, **fields)
            services.refresher.trigger()
            log.info("cdrip: #%s filed %s", ctx.job_id, summary)
            return
    await ctx.db(cd_rips.update, ctx.job_id, state="needs_look", reasons=reasons, **fields)
    log.info("cdrip: #%s needs a look: %s", ctx.job_id, "; ".join(reasons))


def sweep_work(conn, work_dir: Path, now: datetime) -> None:
    """Remove work folders of rips that ended more than WORK_KEEP_DAYS ago."""
    cutoff = now - timedelta(days=WORK_KEEP_DAYS)
    if not work_dir.is_dir():
        return
    for folder in work_dir.iterdir():
        if not folder.is_dir() or not folder.name.isdigit():
            continue
        job = jobs.get(conn, int(folder.name))
        if job is None or job["state"] not in _SWEEPABLE or not job["finished_at"]:
            continue
        try:
            finished = datetime.fromisoformat(job["finished_at"])
        except ValueError:
            continue
        if finished.tzinfo is None:
            finished = finished.replace(tzinfo=timezone.utc)
        if finished < cutoff:
            shutil.rmtree(folder, ignore_errors=True)


runner.HANDLERS["rip_cd"] = run_rip_cd


# --- resolving a rip from the Music page --------------------------------------------


class ResolveError(Exception):
    """The request can't be done as asked; the message is shown to the user."""


def rip_files(rip) -> list[Path]:
    """The rip's audio, in track order: NN.flac in downloads, or cyanrip's names in /work."""
    if not rip["downloads_path"]:
        return []
    folder = Path(rip["downloads_path"])
    return list(cyanrip.track_files(folder).values()) if folder.is_dir() else []


def stored_candidates(rip) -> list[Candidate]:
    return [musicbrainz.candidate_from_dict(d) for d in json.loads(rip["candidates"] or "[]")]


def plan_for_candidate(conn, rip, cand: Candidate) -> AlbumPlan:
    files = rip_files(rip)
    if len(files) != len(cand.tracks):
        raise ResolveError(f"That release has {len(cand.tracks)} tracks on this disc; the rip has {len(files)}.")
    return build_plan(conn, cand, files, rip["disc_id"])


def plan_manual(rip, *, album: str, album_artist: str, year: str, disc: int, disc_total: int,
                titles: list[str], artists: list[str]) -> AlbumPlan:
    files = rip_files(rip)
    compilation = album_artist.strip().casefold() == library.VARIOUS_ARTISTS.casefold()
    album_tags = {"ALBUM": album, "ALBUMARTIST": album_artist, "DATE": year}
    tracks = []
    for n, (path, title, artist) in enumerate(zip(files, titles, artists, strict=True), start=1):
        tags = {
            **album_tags, "TITLE": title, "ARTIST": artist or album_artist, "TRACKNUMBER": str(n),
            "TRACKTOTAL": str(len(files)), "DISCNUMBER": str(disc), "DISCTOTAL": str(disc_total),
            "MUSICBRAINZ_DISCID": rip["disc_id"],
        }
        tracks.append((path, tags))
    return AlbumPlan(
        album_key=f"manual:{uuid.uuid4()}", release_id=None, release_group_id=None, album_artist=album_artist,
        album=album, year=year, compilation=compilation, disc=disc if disc_total > 1 else None,
        disc_total=disc_total, disc_id=rip["disc_id"], album_tags=album_tags, tracks=tracks,
    )


def plan_add_to(conn, rip, album_key: str, disc: int, titles: list[str] | None) -> AlbumPlan:
    """Put this disc into an album already in the library."""
    row = conn.execute("SELECT * FROM albums WHERE album_key = ?", (album_key,)).fetchone()
    if row is None:
        raise ResolveError("That album isn't in the library any more.")
    files = rip_files(rip)
    cands = [c for c in stored_candidates(rip) if c.release_id == rip["release_id"]]
    if titles is None:
        if not cands or len(cands[0].tracks) != len(files):
            raise ResolveError("Type a title for every track.")
        titles = [t.title for t in cands[0].tracks]
        artists = [t.artist for t in cands[0].tracks]
    else:
        artists = [""] * len(titles)
    album_tags = json.loads(row["album_tags"])
    disc_total = max(int(row["disc_total"]), disc)
    compilation = PurePosixPath(row["folder"]).parts[0] == library.VARIOUS_ARTISTS
    tracks = []
    for n, (path, title, artist) in enumerate(zip(files, titles, artists, strict=True), start=1):
        tags = {
            **album_tags, "TITLE": title, "ARTIST": artist or row["album_artist"], "TRACKNUMBER": str(n),
            "TRACKTOTAL": str(len(files)), "DISCNUMBER": str(disc), "DISCTOTAL": str(disc_total),
            "MUSICBRAINZ_DISCID": rip["disc_id"],
        }
        tracks.append((path, tags))
    return AlbumPlan(
        album_key=album_key, release_id=None, release_group_id=row["release_group_id"],
        album_artist=row["album_artist"], album=row["album"], year=row["year"], compilation=compilation,
        disc=disc if disc_total > 1 else None, disc_total=disc_total, disc_id=rip["disc_id"],
        album_tags=album_tags, tracks=tracks,
    )


def file_resolved(conn, job_id: int, plan: AlbumPlan, music_dir: Path, *, allow_duplicate: bool = False) -> Path:
    """File a resolved rip into the library and mark it filed."""
    rip = cd_rips.get(conn, job_id)
    dest = Path(rip["downloads_path"])
    cover = dest / "cover.jpg"
    folder = filing.file_album(conn, plan, music_dir, cover=cover if cover.is_file() else None, now=utc_now(),
                               allow_duplicate_folder=allow_duplicate)
    shutil.rmtree(dest, ignore_errors=True)
    cd_rips.update(conn, job_id, state="filed", reasons=[], filed_path=str(folder), album_key=plan.album_key,
                   summary=summary_for(plan.album_artist, plan.album, plan.year))
    return folder


def claim(conn, job_id: int) -> bool:
    """Take a waiting rip for one request (two tabs or a double-click can't both file it)."""
    with conn:
        cur = conn.execute(
            "UPDATE cd_rips SET state = 'filing' WHERE job_id = ? AND state = 'needs_look'", (job_id,)
        )
    return cur.rowcount == 1


def release_claim(conn, job_id: int) -> None:
    with conn:
        conn.execute("UPDATE cd_rips SET state = 'needs_look' WHERE job_id = ? AND state = 'filing'", (job_id,))


def discard(conn, job_id: int) -> bool:
    """Delete a waiting rip's files. False if it isn't waiting (filed, ripping...)."""
    with conn:
        cur = conn.execute(
            "UPDATE cd_rips SET state = 'discarded' WHERE job_id = ? AND state = 'needs_look'", (job_id,)
        )
    if cur.rowcount != 1:
        return False
    rip = cd_rips.get(conn, job_id)
    if rip["downloads_path"]:
        shutil.rmtree(rip["downloads_path"], ignore_errors=True)
    return True
