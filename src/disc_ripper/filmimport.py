"""Identify a ripped film and import its encode into Radarr (one flow per rip, resumable by step)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import shutil
from pathlib import Path, PurePosixPath

from . import db, encodes, film_imports, filmid, joblog, settings, videofiles
from .clock import utc_now
from .radarr import RadarrError, parse_resolution, parse_runtime, to_candidate

log = logging.getLogger(__name__)

POLL_SECONDS = 5.0
IMPORT_TIMEOUT_SECONDS = 600.0
BUSY = "Busy, try again in a moment."
NO_RADARR = "Set up Radarr in Settings to import films."
DEFAULT_ROOT = "Movies"
STEPS = ("identify", "claim", "add", "replace", "encode", "import", "unmonitor", "extras", "done")
_CHOOSABLE = {"identifying", "needs_identify", "waiting_other", "needs_replace_ok", "failed", "undone"}


class FlowError(Exception):
    """A step failed for a reason the owner can read; no traceback needed."""


class _Stop(Exception):
    """The flow ends here on purpose; the row already says why."""


class Flow:
    """One run of the steps for a rip. Radarr calls run in a thread; the database is used on the loop."""

    def __init__(self, services, client, rip_id: int):
        self.client, self.rip_id = client, rip_id
        self.conn = db.connect(services.config.db_path)
        self.services = services
        self.log = joblog.JobLog(services.config.config_dir / joblog.LOG_DIRNAME / f"import-{rip_id}.log")
        client.on_call = lambda method, path, outcome: self.log.write(f"{method} {path} -> {outcome}")
        self.movie: dict | None = None

    def close(self) -> None:
        self.client.on_call = None
        self.conn.close()

    # -- helpers ---------------------------------------------------------------------

    def row(self):
        return film_imports.get(self.conn, self.rip_id)

    def set(self, **fields) -> None:
        film_imports.update(self.conn, self.rip_id, utc_now(), **fields)

    async def call(self, name: str, *args, **kwargs):
        return await asyncio.to_thread(getattr(self.client, name), *args, **kwargs)

    # -- the flow ----------------------------------------------------------------------

    async def run(self, start: str = "identify") -> None:
        step = start
        try:
            for step in STEPS[STEPS.index(start):]:
                self.set(step=step)
                self.log.write(f"step: {step}")
                await getattr(self, f"_step_{step}")()
        except _Stop:
            return
        except Exception as exc:  # any failure stops the import with a reason; Retry resumes at this step
            reason = str(exc) or type(exc).__name__
            if not isinstance(exc, (RadarrError, FlowError, OSError)):
                log.exception("film import: rip #%s failed at %s", self.rip_id, step)
            self.log.write(f"FAILED at {step}: {reason}")
            self.set(state="failed", error=reason, step=step)

    async def _step_identify(self) -> None:
        info = self.conn.execute(
            "SELECT r.main_title, d.label, d.scan FROM video_rips r LEFT JOIN video_discs d "
            "ON d.fingerprint = r.fingerprint WHERE r.rip_id = ?", (self.rip_id,)).fetchone()
        if info is None or info["main_title"] is None:
            self.set(state="tv_skipped", message="TV: not imported yet")
            self.log.write("no main title: skipped (TV)")
            raise _Stop
        scan = json.loads(info["scan"] or "{}")
        names = [n for n in dict.fromkeys([scan.get("label") or "", filmid.clean_label(info["label"] or "")]) if n]
        seconds = next((t["seconds"] for t in scan.get("titles", []) if t.get("number") == info["main_title"]), 0)
        found: dict[int, filmid.Candidate] = {}
        for name in names:
            for item in await self.call("lookup", name):
                if item.get("tmdbId"):
                    cand = to_candidate(item)
                    found.setdefault(cand.tmdb_id, cand)
        scored = filmid.score(list(found.values()), names, seconds)
        top = filmid.confident(scored)
        self.log.write(f"searched {names}: {len(scored)} candidate(s), confident: {top.title if top else 'none'}")
        if top is None:
            shown = [{**dataclasses.asdict(s.candidate), "title_score": round(s.title_score, 3),
                      "runtime_ok": s.runtime_ok} for s in scored[:5]]
            self.set(state="needs_identify", candidates=shown, message="Pick the film.")
            raise _Stop
        self.set(tmdb_id=top.tmdb_id, title=top.title, year=top.year, root_folder=DEFAULT_ROOT, message="")

    async def _step_claim(self) -> None:
        if not film_imports.claim(self.conn, self.rip_id, self.row()["tmdb_id"], utc_now()):
            self.set(state="waiting_other", message="Another rip of this film is being imported.")
            self.log.write("another rip of this film is being imported: waiting")
            raise _Stop
        self.set(error=None, message="")

    async def _step_add(self) -> None:
        row = self.row()
        movie = await self.call("movie", row["movie_id"]) if row["movie_id"] else None
        if movie is None:
            movie = await self.call("movie_by_tmdb", row["tmdb_id"])
        added = movie is None
        if movie is None:
            roots = await self.call("root_folders")
            wanted = (row["root_folder"] or DEFAULT_ROOT).lower()
            root = next((r for r in roots if PurePosixPath(r["path"].replace("\\", "/")).name.lower() == wanted), None)
            if root is None:
                raise RadarrError(f"Radarr has no root folder named {row['root_folder'] or DEFAULT_ROOT}.")
            profiles = await self.call("quality_profiles")
            if not profiles:
                raise RadarrError("Radarr has no quality profile.")
            lookup = await self.call("lookup_tmdb", row["tmdb_id"])
            movie = await self.call("add_movie", lookup, root["path"], profiles[0]["id"])
            self.log.write(f"added movie #{movie['id']} to {root['path']}")
        self.movie = movie
        self.set(movie_id=movie["id"], **({"added_by_us": 1} if added else {}))

    async def _step_replace(self) -> None:
        if self.movie is None:
            self.movie = await self.call("movie", self.row()["movie_id"])
        await _replace_check(self)

    def pick_encode(self):
        """The encode to import: the one already imported, else the newest finished main-title encode."""
        row = self.row()
        chosen = encodes.get(self.conn, row["encode_id"]) if row["encode_id"] else None
        if chosen is None or not chosen["imported_at"]:
            main = self.conn.execute("SELECT main_title FROM video_rips WHERE rip_id = ?", (self.rip_id,)).fetchone()[0]
            chosen = next((e for e in reversed(encodes.for_rip(self.conn, self.rip_id))
                           if e["title"] == main and e["state"] == "done" and encodes.output_intact(e)), None)
        if chosen is None:
            raise FlowError("No finished encode of the film.")
        return chosen

    async def _step_encode(self) -> None:
        chosen = self.pick_encode()
        self.log.write(f"importing encode #{chosen['id']} ({chosen['preset']}): {chosen['output']}")
        self.set(encode_id=chosen["id"])

    async def _step_import(self) -> None:
        row = self.row()
        enc = encodes.get(self.conn, row["encode_id"])
        if enc["imported_at"]:
            self.log.write("already imported")
            return
        output = Path(enc["output"])
        movie = await self.call("movie", row["movie_id"])
        have = movie.get("movieFile") or {}
        if not output.exists() and have.get("path") and have.get("size") == enc["output_bytes"]:
            self.log.write("the file is already in Radarr (an earlier run got that far)")
            self._imported(enc, have["path"])
            return
        view = await self.call("manual_import_view", str(output.parent))
        seen = next((v for v in view if v.get("path") == str(output)), None)
        if seen is None:
            raise RadarrError("Radarr can't see the encoded file in its folder.")
        definitions = await self.call("quality_definitions")
        wanted = filmid.quality_name(enc["disc_type"], enc["preset"])
        definition = next((d for d in definitions if (d.get("quality") or {}).get("name") == wanted
                           or d.get("title") == wanted), None)
        if definition is None:
            raise RadarrError(f"Radarr has no quality named {wanted}.")
        quality = {"quality": definition["quality"], "revision": {"version": 1, "real": 0, "isRepack": False}}
        entry = {"path": str(output), "movieId": row["movie_id"], "quality": quality,
                 "languages": seen.get("languages") or []}
        bin_dir, before = None, set()
        if row["old_path"]:  # replacing: note what is in the recycle bin so the old file can be found after
            try:
                bin_path, visible = await _visible_bin(self)
                if visible:
                    bin_dir = Path(bin_path)
                    before = await asyncio.to_thread(_listing, bin_dir)
            except Exception as exc:  # best effort: the import itself goes on
                self.log.write(f"recycle bin could not be listed: {exc}")
        command_id = await self.call("command", "ManualImport", files=[entry], importMode="move")
        await self._wait_for(command_id)
        movie = await self.call("movie", row["movie_id"])
        have = movie.get("movieFile") or {}
        if not have.get("path") or (have.get("size") is not None and have["size"] != enc["output_bytes"]):
            raise RadarrError("Radarr finished, but the film's file in Radarr is not the new one.")
        if bin_dir is not None:
            try:
                arrived = (await asyncio.to_thread(_listing, bin_dir)) - before
                recycled = next((p for p in sorted(arrived, key=_mtime, reverse=True)
                                 if p.name == Path(row["old_path"]).name), None)
                self.set(recycled_path=str(recycled) if recycled else None)
                if recycled is None:
                    log.warning("film import: rip #%s: the old file is not in the recycle bin", self.rip_id)
                self.log.write(f"recycle bin: {recycled or 'WARNING: the old file was not found there'}")
            except Exception as exc:  # best effort: Radarr has already imported the file
                self.log.write(f"recycle bin could not be searched: {exc}")
        self._imported(enc, have["path"])

    def _imported(self, enc, path: str) -> None:
        now = utc_now()
        with self.conn:
            self.conn.execute("UPDATE encodes SET imported_path = ?, imported_at = ? WHERE id = ?",
                              (path, now, enc["id"]))
        self.set(new_path=path)
        self.log.write(f"imported as {path}")

    async def _wait_for(self, command_id: int) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + IMPORT_TIMEOUT_SECONDS
        while True:
            status = await self.call("command_status", command_id)
            if status == "completed":
                return
            if status in ("failed", "aborted", "cancelled", "orphaned"):
                raise RadarrError(f"Radarr's import {status}; check Radarr's activity log.")
            if loop.time() > deadline:
                raise RadarrError("Radarr took too long to import the file.")
            await asyncio.sleep(POLL_SECONDS)

    async def _step_unmonitor(self) -> None:
        movie = await self.call("movie", self.row()["movie_id"])
        if movie.get("monitored"):
            movie["monitored"] = False
            await self.call("update_movie", movie, move_files=False)
        await self._tidy_folder()

    async def _tidy_folder(self) -> None:
        """When replacing: rename the film's folder to Radarr's own name for it, and follow the file there."""
        row = self.row()
        if not row["old_path"]:
            return
        movie = await self.call("movie", row["movie_id"])
        if settings.get(self.conn, "tidy_folders_on_replace"):
            lookup = await self.call("lookup_tmdb", row["tmdb_id"])
            wanted = filmid.folder_name(to_candidate(lookup))
            if PurePosixPath(movie["path"].replace("\\", "/")).name != wanted:
                root = movie.get("rootFolderPath") or str(Path(movie["path"]).parent)
                target = str(Path(root) / wanted)
                await self.call("update_movie", {**movie, "path": target}, move_files=True)
                after = await self.call("movie", row["movie_id"])
                moved = (after.get("movieFile") or {}).get("path")
                if after["path"] == movie["path"] or not moved or not await asyncio.to_thread(Path(moved).exists):
                    raise RadarrError("Radarr didn't move the film's folder.")
                self.log.write(f"folder tidied: {movie['path']} -> {after['path']}")
                movie = after
        have = (movie.get("movieFile") or {}).get("path")
        if have and have != row["new_path"] and await asyncio.to_thread(Path(have).exists):
            with self.conn:
                self.conn.execute("UPDATE encodes SET imported_path = ? WHERE id = ?", (have, row["encode_id"]))
            self.set(new_path=have)

    async def _step_extras(self) -> None:
        row = self.row()
        folder = Path(row["new_path"]).parent / "extras"
        copied = []
        for src in self._extra_sources():
            dest = folder / src.name
            if dest.exists():
                self.log.write(f"extras: {dest.name} already exists, skipped")
                continue
            await asyncio.to_thread(self._copy, src, dest)
            self.log.write(f"extras: copied {src} -> {dest}")
            copied.append(str(dest))
        self.set(extras=copied)

    @staticmethod
    def _copy(src: Path, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)

    def _extra_sources(self) -> list[Path]:
        files = json.loads(self.conn.execute(
            "SELECT files FROM video_rips WHERE rip_id = ?", (self.rip_id,)).fetchone()[0] or "{}")
        done = encodes.for_rip(self.conn, self.rip_id)
        out = []
        for title, entry in sorted(files.items(), key=lambda kv: int(kv[0])):
            if entry.get("role") != "extra":
                continue
            best = next((e for e in reversed(done) if e["title"] == int(title) and e["state"] == "done"
                         and encodes.output_intact(e)), None)
            raw = Path(entry["path"])
            if best:
                out.append(Path(best["output"]))
            elif raw.is_file() and not entry.get("raw_deleted_at"):
                out.append(raw)
            else:
                self.log.write(f"extras: title {title} has no file left; skipped")
        return out

    async def _step_done(self) -> None:
        self.set(state="imported", imported_at=utc_now(), message="", error=None)
        self.log.write("done")
        if self.services.jellyfin() is not None:
            self.services.refresher.trigger()


def _listing(folder: Path) -> set[Path]:
    return {p for p in folder.rglob("*") if p.is_file()} if folder.is_dir() else set()


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


async def _visible_bin(flow: Flow) -> tuple[str, bool]:
    """Radarr's recycle bin path, and whether this app can see it as a folder."""
    path = (await flow.call("recycle_bin"))[0]
    return path, bool(path) and await asyncio.to_thread(Path(path).is_dir)


def _height(resolution: str | None) -> int | None:
    parsed = parse_resolution(resolution)
    return parsed[1] if parsed and parsed[1] > 0 else None


async def _replace_check(flow: Flow) -> None:
    """Step 4. A film that already has a file is compared with the new encode; it waits unless all is well."""
    movie = flow.movie
    if not movie or not (movie.get("hasFile") or movie.get("movieFile")):
        return
    have = movie.get("movieFile") or {}
    media = have.get("mediaInfo") or {}
    enc = flow.pick_encode()
    problem = ""
    try:
        info = await asyncio.to_thread(videofiles.PROBE, Path(enc["output"]))
    except Exception as exc:  # ffprobe missing or the file unreadable: the checks can't pass
        info, problem = None, f"The new file couldn't be examined ({exc})."
    new_height = info.height if info and info.height > 0 else None
    new_seconds = info.seconds if info and info.seconds > 0 else None
    old_seconds = parse_runtime(media.get("runTime")) or (int(movie.get("runtime") or 0) * 60) or None
    old_height = _height(media.get("resolution"))
    old = {"path": have.get("path"), "size": have.get("size"), "resolution": media.get("resolution"),
           "video_codec": media.get("videoCodec"), "audio_codec": media.get("audioCodec"),
           "audio_channels": media.get("audioChannels"), "seconds": old_seconds}
    new = {"path": enc["output"], "size": enc["output_bytes"],
           "resolution": f"{info.width}x{info.height}" if new_height else None,
           "video_codec": info.video_codec if info else None, "audio_codec": info.audio_codec if info else None,
           "audio_channels": info.audio_channels[0] if info and info.audio_channels else None,
           "seconds": new_seconds}
    reasons: dict[str, str] = {}
    if new_seconds is None or old_seconds is None:
        reasons["length"] = problem or "Radarr doesn't know the old file's length, so it can't be compared."
    elif abs(new_seconds - old_seconds) > max(0.02 * old_seconds, 300):
        reasons["length"] = (f"The new file is {new_seconds / 60:.0f} min long but the old one is "
                             f"{old_seconds / 60:.0f} min.")
    bin_path, visible = await _visible_bin(flow)
    if not bin_path:
        reasons["recycle_bin"] = "Radarr's recycle bin isn't set, so the old file could not be recovered."
    elif not visible:
        reasons["recycle_bin"] = (f"Radarr's recycle bin isn't visible to the ripper at {bin_path}, "
                                  "so Undo couldn't bring the old file back.")
    if new_height is None or old_height is None:
        reasons["resolution"] = problem or "Radarr doesn't know the old file's resolution, so it can't be compared."
    elif new_height < old_height:
        reasons["resolution"] = f"The new file is {new_height}p but the old one is {old_height}p (a downgrade)."
    checks = {name: name not in reasons for name in ("length", "recycle_bin", "resolution")}
    flow.set(replace={"old": old, "new": new, "checks": checks, "reasons": reasons},
             old_path=have.get("path"), old_folder=movie.get("path"))
    ask = settings.get(flow.conn, "ask_before_replace")
    flow.log.write(f"replace check: {checks}; ask before replacing: {ask}")
    if ask or reasons:
        message = " ".join(["This film is already in Radarr.", *reasons.values()]) if reasons else (
            "This film is already in Radarr; replace its file?")
        flow.set(state="needs_replace_ok", message=message)
        raise _Stop


async def approve_replace(services, rip_id: int, typed: str = "") -> str | None:
    """The owner agrees to replace the film's file. Without a recycle bin the word REPLACE must be typed."""
    async def work():
        async def before(flow: Flow):
            row = flow.row()
            if row is None or row["state"] != "needs_replace_ok":
                return "This rip isn't waiting to replace a film."
            if not (await _visible_bin(flow))[1] and typed != "REPLACE":
                return ("Radarr's recycle bin isn't set (or isn't visible to the ripper), so the old file can't be recovered. "
                        "Type REPLACE to replace it anyway.")
            if not film_imports.claim(flow.conn, rip_id, row["tmdb_id"], utc_now()):
                flow.set(state="waiting_other", message="Another rip of this film is being imported.")
                return "Another rip of this film is being imported."
            flow.set(error=None, message="")
            flow.log.write("replace approved")
            return None

        return await _run(services, rip_id, "encode", before)

    return await _locked(services, rip_id, work)


# -- entry points -------------------------------------------------------------------------


async def _locked(services, rip_id: int, work) -> str | None:
    lock = services.import_lock(rip_id)
    if lock.locked():
        return BUSY
    async with lock:
        return await work()


async def _run(services, rip_id: int, start: str, before=None) -> str | None:
    client = services.radarr()
    if client is None:
        return NO_RADARR
    flow = Flow(services, client, rip_id)
    woken: list[int] = []
    try:
        if before is None:
            film_imports.ensure(flow.conn, rip_id, utc_now())
        else:
            error = await before(flow)
            if error:
                return error
        await flow.run(start)
        row = flow.row()
        if row["tmdb_id"] and row["state"] not in ("importing", "waiting_other"):
            woken = [r["rip_id"] for r in film_imports.waiting_for(flow.conn, row["tmdb_id"])]
    finally:
        flow.close()
    for waiting in woken:  # this rip no longer holds the film: the rips waiting for it go on from the claim
        _spawn(services, _wake(services, waiting), f"film import {waiting}")
    return None


async def _wake(services, rip_id: int) -> None:
    try:
        await _locked(services, rip_id, lambda: _run(services, rip_id, "claim"))
    except Exception:
        log.exception("film import: rip #%s crashed", rip_id)


def _spawn(services, coro, name: str) -> None:
    task = asyncio.create_task(coro, name=name)
    services._import_tasks.add(task)
    task.add_done_callback(services._import_tasks.discard)


async def run_import(services, rip_id: int) -> None:
    """Identify the rip's film and import it, from the first step. Does nothing without Radarr."""
    if services.radarr() is None:
        return
    await _locked(services, rip_id, lambda: _run(services, rip_id, "identify"))


async def retry_import(services, rip_id: int) -> str | None:
    """Resume a failed import at the step that failed. Returns a message when it can't."""
    async def work():
        conn = db.connect(services.config.db_path)
        try:
            row = film_imports.get(conn, rip_id)
        finally:
            conn.close()
        if row is None or row["state"] not in ("failed", "waiting_other"):
            return "Nothing to retry."
        start = row["step"] if row["step"] in STEPS else "identify"
        state = "importing" if STEPS.index(start) > STEPS.index("claim") else "identifying"

        async def before(flow: Flow):
            flow.set(state=state, error=None)

        return await _run(services, rip_id, start, before)

    return await _locked(services, rip_id, work)


async def choose_film(services, rip_id: int, ref, root_folder: str = DEFAULT_ROOT) -> str | None:
    """The owner picked the film (a TMDb ID, an IMDb ID or a link): import from the claim step."""
    parsed = filmid.parse_ref(str(ref))
    if parsed is None:
        return "Enter an IMDb ID, a TMDb ID or a TMDb/IMDb link."
    main = await asyncio.to_thread(services._db, lambda conn: conn.execute(
        "SELECT main_title FROM video_rips WHERE rip_id = ?", (rip_id,)).fetchone())
    if main is None or main["main_title"] is None:
        return "Only films can be imported."

    async def before(flow: Flow):
        row = flow.row()
        if row is not None and row["state"] not in _CHOOSABLE:
            return "This rip can't be changed now."
        try:
            kind, value = parsed
            lookup = await flow.call("lookup_imdb" if kind == "imdb" else "lookup_tmdb",
                                     value if kind == "imdb" else int(value))
            cand = to_candidate(lookup)
        except RadarrError as exc:
            return str(exc)
        except (KeyError, TypeError, ValueError):
            return "Radarr doesn't know that film."
        film_imports.ensure(flow.conn, rip_id, utc_now())
        flow.set(tmdb_id=cand.tmdb_id, title=cand.title, year=cand.year, root_folder=root_folder,
                 state="identifying", movie_id=None, error=None, message="")
        flow.log.write(f"chosen: {cand.title} ({cand.year}) tmdb {cand.tmdb_id}")
        return None

    return await _locked(services, rip_id, lambda: _run(services, rip_id, "claim", before))


def import_rip_for(conn, encode_id: int) -> int | None:
    """The rip whose import this finished encode should start, or None."""
    enc = encodes.get(conn, encode_id)
    if enc is None:
        return None
    rip = conn.execute("SELECT main_title FROM video_rips WHERE rip_id = ?", (enc["rip_id"],)).fetchone()
    if rip is None or (rip["main_title"] is not None and rip["main_title"] != enc["title"]):
        return None
    row = film_imports.get(conn, enc["rip_id"])
    return enc["rip_id"] if row is None or row["state"] == "identifying" else None


async def encode_done(services, encode_id: int) -> None:
    """A finished encode of a rip's main title starts the import in the background."""
    if services.radarr() is None:
        return
    rip_id = await asyncio.to_thread(services._db, import_rip_for, encode_id)
    if rip_id is None or services.import_lock(rip_id).locked():
        return
    _spawn(services, _background(services, rip_id), f"film import {rip_id}")


async def _background(services, rip_id: int) -> None:
    try:
        await run_import(services, rip_id)
    except Exception:
        log.exception("film import: rip #%s crashed", rip_id)
