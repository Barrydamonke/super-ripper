"""The rip_cd job: read an audio CD with cyanrip, then hand the files on.

Once the audio is safely ripped the job counts as done. Anything that goes wrong
afterwards (identifying, tagging, filing) puts the rip on the Music page's
"Needs a look" list instead of failing the job: the disc has already been ejected.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import json

from . import cd_rips, cyanrip, filing, jellyfin, jobs, musicbrainz, runner, settings, tagging
from .cdrom import CdDrive
from .cdtoc import Toc, musicbrainz_disc_id
from .clock import utc_now
from .cyanrip import TrackCheck
from .filing import AlbumPlan
from .musicbrainz import Candidate

log = logging.getLogger(__name__)

WORK_KEEP_DAYS = 7
DRIVE_LOCK_TIMEOUT = 10.0
_FINAL = ("done", "failed", "cancelled", "interrupted")


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
    except Exception as exc:
        log.exception("cdrip: after rip #%s failed", ctx.job_id)
        await ctx.db(cd_rips.update, ctx.job_id, state="needs_look",
                     reasons=[f"Couldn't finish after the rip: {exc}"])
    await ctx.report(1.0, "Ripped")


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


_ALBUM_LEVEL = ("ALBUM", "ALBUMARTIST", "DATE", "ORIGINALDATE", "LABEL", "BARCODE", "MUSICBRAINZ_ALBUMID",
                "MUSICBRAINZ_RELEASEGROUPID", "MUSICBRAINZ_ALBUMARTISTID")


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


def _candidate_snapshot(cands: list[Candidate]) -> list[dict]:
    return [
        {
            "release_id": c.release_id, "release_group_id": c.release_group_id, "title": c.title,
            "album_artist": c.album_artist, "date": c.date, "country": c.country, "label": c.label,
            "barcode": c.barcode, "medium": c.medium, "medium_count": c.medium_count,
            "tracks": [{"number": t.number, "title": t.title, "artist": t.artist} for t in c.tracks],
        }
        for c in cands
    ]


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
    """Move the ripped files (and the log) out of the work area as NN.flac."""
    dest.mkdir(parents=True, exist_ok=True)
    moved = []
    for n, src in enumerate(files, start=1):
        target = dest / f"{n:02d}.flac"
        filing._move(src, target)
        moved.append(target)
    for log_file in rip_dir.glob("*.log"):
        filing._move(log_file, dest / "rip.log")
        break
    return moved


async def after_rip(ctx, *, disc_id: str, toc: Toc, checks: list[TrackCheck], rip_dir: Path) -> None:
    services = ctx.services
    files_by_track = cyanrip.track_files(rip_dir)
    files = [files_by_track[t] for t in toc.audio_tracks]
    approval, cover_on = await ctx.db(lambda conn: (settings.get(conn, "cd_approval"), settings.get(conn, "cd_cover_art")))

    mb_error = False
    try:
        releases = await asyncio.to_thread(services.musicbrainz.lookup_discid, disc_id)
    except musicbrainz.MusicBrainzUnavailable as exc:
        log.warning("cdrip: #%s: %s", ctx.job_id, exc)
        releases, mb_error = [], True
    cands = musicbrainz.candidates_for_disc(releases, disc_id)
    groups = musicbrainz.group_equivalent(cands)
    filed = await ctx.db(filed_groups)
    chosen = musicbrainz.choose(groups, filed_groups=filed)
    track_mismatch = chosen is not None and len(chosen.tracks) != len(files)
    if track_mismatch:
        chosen = None

    dest = ctx.config.downloads_dir / str(ctx.job_id)
    files = await asyncio.to_thread(_move_to_downloads, rip_dir, files, dest)
    shutil.rmtree(rip_dir.parent, ignore_errors=True)  # the work folder is done with

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
    if plan:
        row = await ctx.db(lambda conn: conn.execute(
            "SELECT discs FROM albums WHERE album_key = ?", (plan.album_key,)).fetchone())
        duplicate = row is not None and str(plan.disc or 1) in json.loads(row["discs"])
        client = services.jellyfin()
        if client is not None and not duplicate:
            release = plan.album_tags.get("MUSICBRAINZ_ALBUMID") or chosen.release_id
            try:
                duplicate = await asyncio.to_thread(client.has_album, release, plan.album)
            except jellyfin.JellyfinError as exc:
                notes.append("Jellyfin check failed")
                log.warning("cdrip: #%s: Jellyfin check failed: %s", ctx.job_id, exc)
    other_edition = any(
        c.release_group_id in filed and filed[c.release_group_id] != c.medium_count for c in cands
    ) and plan is None
    sibling = await ctx.db(waiting_sibling, ctx.job_id, cands)

    reasons = decide(
        chosen=chosen, groups=len(groups), checks=checks, duplicate=duplicate, other_edition=other_edition,
        waiting_sibling=sibling, approval=approval, mb_error=mb_error, track_mismatch=track_mismatch,
    )
    summary = summary_for(plan.album_artist, plan.album, plan.year) if plan else f"Unknown album · {len(files)} tracks"
    if notes:
        summary += " · " + ", ".join(notes)
    fields = dict(
        candidates=_candidate_snapshot(cands), release_id=chosen.release_id if chosen else None,
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
        if job is None or job["state"] not in _FINAL or not job["finished_at"]:
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
