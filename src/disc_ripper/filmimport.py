"""Identify a ripped film and import its encode into Radarr (one flow per rip, resumable by step)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import shutil
from pathlib import Path, PurePosixPath

from . import db, encodes, film_imports, filmid, joblog, video_rips
from .clock import utc_now
from .radarr import RadarrError, to_candidate

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
        self.set(movie_id=movie["id"])

    async def _step_replace(self) -> None:
        if self.movie is None:
            self.movie = await self.call("movie", self.row()["movie_id"])
        await _replace_check(self)

    async def _step_encode(self) -> None:
        row = self.row()
        chosen = encodes.get(self.conn, row["encode_id"]) if row["encode_id"] else None
        if chosen is None or not chosen["imported_at"]:
            main = self.conn.execute("SELECT main_title FROM video_rips WHERE rip_id = ?", (self.rip_id,)).fetchone()[0]
            chosen = next((e for e in reversed(encodes.for_rip(self.conn, self.rip_id))
                           if e["title"] == main and e["state"] == "done" and encodes.output_intact(e)), None)
        if chosen is None:
            raise FlowError("No finished encode of the film.")
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
        command_id = await self.call("command", "ManualImport", files=[entry], importMode="move")
        await self._wait_for(command_id)
        movie = await self.call("movie", row["movie_id"])
        have = movie.get("movieFile") or {}
        if not have.get("path") or (have.get("size") is not None and have["size"] != enc["output_bytes"]):
            raise RadarrError("Radarr finished, but the film's file in Radarr is not the new one.")
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


async def _replace_check(flow: Flow) -> None:
    """Step 4. Placeholder until the replace flow exists: a film that already has a file waits."""
    if flow.movie and (flow.movie.get("hasFile") or flow.movie.get("movieFile")):
        flow.set(state="needs_replace_ok", message="This film is already in Radarr; replacing comes in a later step.")
        flow.log.write("the movie already has a file: waiting (replace not built yet)")
        raise _Stop


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
