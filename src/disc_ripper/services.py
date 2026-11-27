"""Wires the background machinery together and owns its lifecycle.

Services is also the Notifier: when a drive or the schedule changes it renders
the matching HTML fragment and publishes it to every open /events stream.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import shutil
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import cd_rips, cdrip, coverart, db, drives, encodes, film_imports, filmimport, joblog, jobs, presets, schedule_source, settings, titles, video_rips, videorip  # cdrip, videorip register handlers
from .jellyfin import Jellyfin, RefreshDebouncer
from .musicbrainz import MusicBrainz
from .radarr import Radarr
from .cdrom import CdDrive
from .cdtoc import Toc, musicbrainz_disc_id
from .clock import utc_now
from .config import AppConfig
from .encodepool import EncodePool
from .events import Broker
from .media import MediaMonitor
from .runner import JobRunner
from .space import SpaceChecker

log = logging.getLogger(__name__)

SCHEDULE_REFRESH_SECONDS = 300
RAW_SWEEP_SECONDS = 600
FILM_CONFIRM_SECONDS = 300
SCAN_MISSING = "The rip's scan is missing, so its length can't be checked; can't encode."
GUARD_TICK_SECONDS = 60
CD_BYTES_PER_SECOND = 176_400  # 44.1 kHz, 16-bit stereo
FLAC_RATIO = 0.75  # generous: FLAC is usually 50-70% of the raw audio


def drive_key(drive_id: str) -> str:
    """Short stable name for a drive in HTML ids and SSE event names (its ID has ':' and spaces)."""
    return hashlib.sha1(drive_id.encode()).hexdigest()[:10]


def _parse_utc(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_APP_LOG = "app.log"


def _configure_logging(config_dir: Path | None = None) -> None:
    logger = logging.getLogger("disc_ripper")
    if not any(type(h) is logging.StreamHandler for h in logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    if config_dir is None:
        return
    path = Path(config_dir) / "logs" / _APP_LOG
    for h in list(logger.handlers):  # one app log at a time (a restart in-process swaps it)
        if isinstance(h, RotatingFileHandler):
            logger.removeHandler(h)
            h.close()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(path, maxBytes=2 * 1024 * 1024, backupCount=4, encoding="utf-8")
    except OSError as exc:
        log.warning("could not open the app log %s: %s; carrying on without it", path, exc)
        return
    file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    logger.addHandler(file_handler)


def _app_version() -> str:
    from . import __version__

    sha = os.environ.get("GIT_SHA", "").strip()
    return f"{__version__} ({sha[:12]})" if sha else __version__


AUTO_RETRY_SECONDS = 180  # a stalled MakeMKV job is tried again after this…
AUTO_RETRY_MAX = 2  # …this many times, before it waits for the owner's Retry
DVD_DECOY_WAIT_SECONDS = 300  # a DVD held only for two same-length titles rips its longest after this


def _encode_preset_for(conn, rip_id: int) -> str:
    """The rip's own encode choice (a preset id or "off"), else the after-rip default."""
    row = conn.execute("SELECT encode_preset FROM video_rips WHERE rip_id = ?", (rip_id,)).fetchone()
    if row is not None and row["encode_preset"] and presets.parse_choice(conn, row["encode_preset"]) is None:
        return row["encode_preset"]
    return presets.default_choice(conn)


class Services:
    def __init__(
        self,
        config: AppConfig,
        *,
        drive_factory: Callable[[Path], CdDrive] = CdDrive,
        fetch: schedule_source.Fetcher = schedule_source.http_get,
    ):
        self.config = config
        self.drive_factory = drive_factory
        self.fetch = fetch
        self.broker = Broker()
        self.space = SpaceChecker(config.work_dir)
        # Indirect, so drive_factory can be swapped after construction (tests).
        self.monitor = MediaMonitor(
            config, self, drive_factory=lambda path: self.drive_factory(path), on_insert=self.on_insert,
            on_present=self.on_present, on_remove=self.on_remove,
        )
        self.runner = JobRunner(config, self, self.space, services=self)
        self.encoder = EncodePool(config, self, self.space, services=self)
        self.musicbrainz = MusicBrainz()
        self.refresher = RefreshDebouncer(self.jellyfin)
        self.cover_fetch = coverart.fetch_front
        self._tasks: list[asyncio.Task] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._schedule_error: str | None = None
        self._import_locks: dict[int, asyncio.Lock] = {}
        self._import_tasks: set[asyncio.Task] = set()
        self.dvd_decoy_wait: float = DVD_DECOY_WAIT_SECONDS
        self._dvd_waits: dict[str, asyncio.Task] = {}  # drive id -> its pending DVD wait
        self.auto_retry_wait: float = AUTO_RETRY_SECONDS
        self._auto_retries: set[asyncio.Task] = set()
        self._retry_rips: dict[int, int] = {}  # job id of a pending automatic retry -> its rip
        self._films_push: asyncio.Task | None = None
        self._films_dirty = False

    def jellyfin(self) -> Jellyfin | None:
        """A Jellyfin client from the current settings, or None if not configured."""
        conn = db.connect(self.config.db_path)
        try:
            url, key = settings.get(conn, "jellyfin_url"), settings.get(conn, "jellyfin_api_key")
        finally:
            conn.close()
        return Jellyfin(url, key) if url and key else None

    def radarr(self) -> Radarr | None:
        """A Radarr client from the current settings, or None if not configured."""
        conn = db.connect(self.config.db_path)
        try:
            url, key = settings.get(conn, "radarr_url"), settings.get(conn, "radarr_api_key")
        finally:
            conn.close()
        return Radarr(url, key) if url and key else None

    def import_lock(self, rip_id: int) -> asyncio.Lock:
        """The lock that lets one film-import flow at a time run for a rip."""
        return self._import_locks.setdefault(rip_id, asyncio.Lock())

    def retry_pending(self, rip_id: int) -> bool:
        """An automatic retry of one of this rip's titles is waiting to happen."""
        return rip_id in self._retry_rips.values()

    async def encode_done(self, encode_id: int) -> None:
        """An encode ended (done, failed or cancelled): delete raws that are due (straight away by default), and start the
        film import once the whole disc is ripped and encoded."""
        try:
            if await asyncio.to_thread(self._db, self._sweep_raw):
                await self.encodes_changed()
        except Exception:
            log.exception("encode: deleting raw rips after encode #%s failed", encode_id)
        await filmimport.encode_done(self, encode_id)

    # -- audio CDs -------------------------------------------------------------

    def music_waiting(self) -> int:
        """How many rips wait on the Music page (shown in the nav)."""
        return self._db(lambda conn: conn.execute("SELECT COUNT(*) FROM cd_rips WHERE state = 'needs_look'").fetchone()[0])

    def _db(self, fn, *args):
        conn = db.connect(self.config.db_path)
        try:
            return fn(conn, *args)
        finally:
            conn.close()

    def read_toc(self, drive_id: str) -> Toc:
        """The TOC of the disc in a drive, read under the drive's lock (no waiting on a busy drive)."""
        row = self._db(lambda conn: conn.execute("SELECT sr_name FROM drives WHERE id = ?", (drive_id,)).fetchone())
        if row is None or not row["sr_name"]:
            raise OSError("This drive has no /dev/sr node.")
        lock = self.monitor.drive_lock(drive_id)
        if not lock.acquire(timeout=5.0):
            raise OSError("The drive is busy.")
        try:
            toc = self.drive_factory(self.config.dev_root / row["sr_name"]).read_toc()
        finally:
            lock.release()
        disc_id = musicbrainz_disc_id(toc)
        self._db(cd_rips.remember_disc, disc_id, toc.to_json(), len(toc.audio_tracks), drive_id, utc_now())
        return toc

    def _enqueue_cd_rip(self, conn, drive_id: str, toc: Toc, again: bool) -> int | None:
        """Check and queue in one write transaction, so the Rip button and an insert
        (or a double-click) can't both queue the same disc."""
        drive = conn.execute("SELECT sr_name, read_offset FROM drives WHERE id = ?", (drive_id,)).fetchone()
        disc_id = musicbrainz_disc_id(toc)
        params = {"disc_id": disc_id, "toc": toc.to_json(), "offset": drive["read_offset"], "sr_name": drive["sr_name"]}
        now = utc_now()
        conn.execute("BEGIN IMMEDIATE")
        try:
            blocking = cd_rips.blocking_rip_for_disc(conn, disc_id)
            active = blocking is not None and blocking["job_state"] in ("queued", "waiting_space", "running")
            if active or (blocking is not None and not again):
                conn.rollback()
                return None
            cur = conn.execute(
                """
                INSERT INTO jobs (drive_id, kind, state, params, needs_bytes, uses_drive, created_at)
                VALUES (?, 'rip_cd', 'queued', ?, ?, 1, ?)
                """,
                (drive_id, json.dumps(params), math.ceil(toc.audio_seconds * CD_BYTES_PER_SECOND * FLAC_RATIO), now),
            )
            job_id = cur.lastrowid
            conn.execute(
                "INSERT INTO cd_rips (job_id, disc_id, state, updated_at) VALUES (?, ?, 'ripping', ?)",
                (job_id, disc_id, now),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return job_id

    async def enqueue_cd_rip(self, drive_id: str, toc: Toc, *, again: bool = False) -> int | None:
        """Queue a rip of the disc. None if this disc is already queued or ripping, or
        (without again) was already ripped and filed or is waiting for a look."""
        job_id = await asyncio.to_thread(self._db, self._enqueue_cd_rip, drive_id, toc, again)
        if job_id is not None:
            log.info("cd: queued rip #%s on %s", job_id, drive_id)
            self.wake(drive_id)
        await self.drive_changed(drive_id)
        return job_id

    async def on_present(self, drive_id: str) -> None:
        """A disc already in the drive at start-up: remember it (for the card), don't rip it."""
        row = await asyncio.to_thread(
            self._db, lambda conn: conn.execute("SELECT disc_type FROM drives WHERE id = ?", (drive_id,)).fetchone()
        )
        if row is not None and row["disc_type"] in ("dvd", "bluray"):
            await self.enqueue_video_scan(drive_id, from_insert=False)
            return
        if row is None or row["disc_type"] not in ("audio", "mixed"):
            return
        try:
            await asyncio.to_thread(self.read_toc, drive_id)
        except OSError as exc:
            log.warning("cd: reading the TOC on %s failed: %s", drive_id, exc)
            return
        await self.drive_changed(drive_id)

    async def on_insert(self, drive_id: str) -> None:
        """An audio CD went in: remember it and, with auto-start on, queue its rip
        unless this disc was already ripped (filed or waiting for a look)."""
        row = await asyncio.to_thread(
            self._db, lambda conn: conn.execute("SELECT disc_type FROM drives WHERE id = ?", (drive_id,)).fetchone()
        )
        if row is not None and row["disc_type"] in ("dvd", "bluray"):
            await self.enqueue_video_scan(drive_id, from_insert=True)
            return
        if row is None or row["disc_type"] not in ("audio", "mixed"):
            return
        try:
            toc = await asyncio.to_thread(self.read_toc, drive_id)
        except OSError as exc:
            log.warning("cd: reading the TOC on %s failed: %s", drive_id, exc)
            return
        disc_id = musicbrainz_disc_id(toc)

        def check(conn):
            return settings.get(conn, "auto_start_rips"), cd_rips.blocking_rip_for_disc(conn, disc_id)

        auto, blocking = await asyncio.to_thread(self._db, check)
        if not auto or blocking is not None:
            await self.drive_changed(drive_id)  # show Rip / "Already ripped" on the card
            return
        await self.enqueue_cd_rip(drive_id, toc)

    # -- DVD / Blu-ray ---------------------------------------------------------

    def _enqueue_video_scan(self, conn, drive_id: str, from_insert: bool) -> int | None:
        conn.execute("BEGIN IMMEDIATE")
        try:
            drive = conn.execute(
                "SELECT sr_name, disc_type, disc_label FROM drives WHERE id = ?", (drive_id,)).fetchone()
            busy = conn.execute(
                "SELECT 1 FROM jobs WHERE drive_id = ? AND kind = 'scan_video' "
                "AND state IN ('queued', 'waiting_space', 'running')", (drive_id,)).fetchone()
            if drive is None or not drive["sr_name"] or busy:
                conn.rollback()
                return None
            params = {"sr_name": drive["sr_name"], "disc_type": drive["disc_type"],
                      "label": drive["disc_label"] or "", "from_insert": from_insert}
            cur = conn.execute(
                "INSERT INTO jobs (drive_id, kind, state, params, needs_bytes, uses_drive, created_at) "
                "VALUES (?, 'scan_video', 'queued', ?, 0, 1, ?)",
                (drive_id, json.dumps(params), utc_now()),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return cur.lastrowid

    async def enqueue_video_scan(self, drive_id: str, *, from_insert: bool) -> int | None:
        """Queue a scan of the disc in a drive. None while one is already queued or running."""
        job_id = await asyncio.to_thread(self._db, self._enqueue_video_scan, drive_id, from_insert)
        if job_id is not None:
            log.info("video: queued scan #%s on %s", job_id, drive_id)
            self.wake(drive_id)
        await self.drive_changed(drive_id)
        return job_id

    @staticmethod
    def _rip_params(disc, drive, fingerprint: str, title: dict, role: str) -> dict:
        return {"rip_id": 0, "fingerprint": fingerprint, "title": title["number"], "role": role,
                "minlength": disc["minlength"], "expected_seconds": title["seconds"],
                "expected_audio": title.get("audio", -1),
                "sr_name": drive["sr_name"]}

    @staticmethod
    def _insert_rip_job(conn, drive_id: str, params: dict, title: dict, now: str) -> int:
        cur = conn.execute(
            "INSERT INTO jobs (drive_id, kind, state, params, needs_bytes, uses_drive, created_at) "
            "VALUES (?, 'rip_title', 'queued', ?, ?, 1, ?)",
            (drive_id, json.dumps(params), math.ceil(title["size"] * 1.02), now),
        )
        return cur.lastrowid

    def _start_video_rip(self, conn, drive_id: str, fingerprint: str, main: int | None,
                         scan_job_id: int | None = None, extras: list[int] | None = None,
                         again: bool = False, after_wait: bool = False) -> int | None:
        conn.execute("BEGIN IMMEDIATE")
        try:
            disc = conn.execute("SELECT * FROM video_discs WHERE fingerprint = ?", (fingerprint,)).fetchone()
            drive = conn.execute(
                "SELECT sr_name, video_hold, encode_choice, media_state FROM drives WHERE id = ?",
                (drive_id,)).fetchone()
            if disc is None or drive is None:
                conn.rollback()
                return None
            if after_wait:  # the DVD wait is over: only if the same disc is still in and not held
                current = video_rips.disc_in_drive(conn, drive_id)
                if (drive["video_hold"] or drive["media_state"] != "disc" or current is None
                        or current["fingerprint"] != fingerprint):
                    conn.rollback()
                    return None
            by_number = {t["number"]: t for t in json.loads(disc["scan"])["titles"]}
            wanted = [] if main is None else [main]
            if main is not None and main in (extras or []):
                conn.rollback()
                return None
            for number in extras or []:
                if number not in wanted:
                    wanted.append(number)
            if not wanted or any(n not in by_number for n in wanted):
                conn.rollback()
                return None
            if scan_job_id is not None:  # an auto-start: Hold or a cancel may have come since the scan decided
                scan = conn.execute("SELECT state FROM jobs WHERE id = ?", (scan_job_id,)).fetchone()
                if drive["video_hold"] or scan is None or scan["state"] != "running":
                    conn.rollback()
                    return None
            blocking = video_rips.blocking_rip(conn, fingerprint, self.config.downloads_dir, self.config.work_dir)
            if blocking is not None and (blocking["state"] == "ripping" or not again):
                conn.rollback()
                return None
            now = utc_now()
            ids = []
            for number in wanted:
                role = "title" if main is None else ("main" if number == main else "extra")
                params = self._rip_params(disc, drive, fingerprint, by_number[number], role)
                ids.append(self._insert_rip_job(conn, drive_id, params, by_number[number], now))
            rip_id = ids[0]
            conn.execute("UPDATE jobs SET params = json_set(params, '$.rip_id', ?) WHERE id IN (%s)"
                         % ",".join("?" * len(ids)), (rip_id, *ids))
            conn.execute(
                "INSERT INTO video_rips (rip_id, fingerprint, state, main_title, encode_preset, created_at, updated_at) "
                "VALUES (?, ?, 'ripping', ?, ?, ?, ?)", (rip_id, fingerprint, main, drive["encode_choice"], now, now))
            if scan_job_id is None and not after_wait:  # a manual start lifts the hold
                conn.execute("UPDATE drives SET video_hold = 0 WHERE id = ?", (drive_id,))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return rip_id

    def rip_dvd_later(self, drive_id: str, fingerprint: str, main: int) -> None:
        """After dvd_decoy_wait, rip the main title of a DVD held only for same-length titles,
        unless the owner started a rip, pressed Hold or took the disc out in the meantime."""
        async def later():
            await asyncio.sleep(self.dvd_decoy_wait)
            rip_id = await self.start_video_rip(drive_id, fingerprint, main=main, extras=[], again=False,
                                                after_wait=True)
            outcome = f"queued rip #{rip_id}" if rip_id else "nothing started (held, ejected or already ripping)"
            log.info("video: DVD wait over on %s: %s", drive_id, outcome)

        task = asyncio.get_running_loop().create_task(later())
        old = self._dvd_waits.get(drive_id)
        if old is not None:
            old.cancel()
        self._dvd_waits[drive_id] = task

        def forget(done, drive_id=drive_id):
            if self._dvd_waits.get(drive_id) is done:
                del self._dvd_waits[drive_id]

        task.add_done_callback(forget)

    def dvd_wait_pending(self, drive_id: str) -> bool:
        """A DVD on this drive is waiting out its decoy delay before ripping by itself."""
        task = self._dvd_waits.get(drive_id)
        return task is not None and not task.done()

    async def start_video_rip(self, drive_id: str, fingerprint: str, *, main: int | None, extras: list[int],
                              again: bool, scan_job_id: int | None = None, after_wait: bool = False) -> int | None:
        """Queue the main title then the extras (or every ticked title as a peer when there is no
        main). None when refused: a rip is active, the disc is already ripped (without again),
        or a title isn't on the disc."""
        rip_id = await asyncio.to_thread(self._db, self._start_video_rip, drive_id, fingerprint, main,
                                         scan_job_id, extras, again, after_wait)
        if rip_id is None:
            return None
        log.info("video: queued rip #%s on %s", rip_id, drive_id)
        self.wake(drive_id)
        await self.drive_changed(drive_id)
        return rip_id

    def _add_video_extra(self, conn, rip_id: int, title: int) -> tuple[int | None, str | None]:
        conn.execute("BEGIN IMMEDIATE")
        try:
            rip = conn.execute("SELECT * FROM video_rips WHERE rip_id = ?", (rip_id,)).fetchone()
            first = conn.execute("SELECT drive_id FROM jobs WHERE id = ?", (rip_id,)).fetchone()
            if rip is None or first is None or first["drive_id"] is None:
                conn.rollback()
                return None, None
            drive_id, fingerprint = first["drive_id"], rip["fingerprint"]
            disc = conn.execute("SELECT * FROM video_discs WHERE fingerprint = ?", (fingerprint,)).fetchone()
            drive = conn.execute("SELECT sr_name FROM drives WHERE id = ?", (drive_id,)).fetchone()
            by_number = {t["number"]: t for t in json.loads(disc["scan"])["titles"]} if disc else {}
            if title not in by_number or drive is None:
                conn.rollback()
                return None, None
            active = conn.execute(
                "SELECT 1 FROM jobs WHERE kind = 'rip_title' AND drive_id = ? "
                "AND state IN ('queued', 'waiting_space', 'running') LIMIT 1", (drive_id,)).fetchone()
            here = video_rips.disc_in_drive(conn, drive_id)
            if active is None and (here is None or here["fingerprint"] != fingerprint):
                conn.rollback()  # the extra has to rip from this disc
                return None, None
            if any(t == title and s in ("queued", "waiting_space", "running", "done")
                   for t, _j, s in video_rips.attempts(conn, rip_id)):
                conn.rollback()
                return None, None
            role = "title" if rip["main_title"] is None else "extra"
            params = self._rip_params(disc, drive, fingerprint, by_number[title], role)
            params["rip_id"] = rip_id
            now = utc_now()
            job_id = self._insert_rip_job(conn, drive_id, params, by_number[title], now)
            if rip["state"] == "ripped":  # reopen; a move_failed rip stays so until Move again clears it
                conn.execute("UPDATE video_rips SET state = 'ripping', updated_at = ? WHERE rip_id = ?",
                             (now, rip_id))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return job_id, drive_id

    async def add_video_extra(self, rip_id: int, title: int) -> int | None:
        """Queue one more title for a rip (an extra, or a peer when the rip has no main).
        None when refused, e.g. the title already has a queued, running or finished attempt."""
        job_id, drive_id = await asyncio.to_thread(self._db, self._add_video_extra, rip_id, title)
        if job_id is None:
            return None
        log.info("video: queued title %s as job #%s of rip #%s", title, job_id, rip_id)
        self.wake(drive_id)
        await self.drive_changed(drive_id)
        return job_id

    async def move_again(self, rip_id: int) -> str | None:
        """Retry moving a rip's files to downloads. Returns an error message, or None."""
        from . import videorip

        recorded: list[tuple[int, int]] = []

        def run(conn):
            row = conn.execute("SELECT drive_id FROM jobs WHERE id = ?", (rip_id,)).fetchone()
            error = videorip.move_again_sync(conn, self.config, rip_id,
                                             on_recorded=lambda r, t: recorded.append((r, t)))
            return error, row["drive_id"] if row else None

        error, drive_id = await asyncio.to_thread(self._db, run)
        for rec_rip, rec_title in recorded:
            await self.file_recorded(rec_rip, rec_title)
        if drive_id:
            await self.drive_changed(drive_id)
        return error

    # -- removing a rip ----------------------------------------------------------

    def _remove_prepare(self, conn, rip_id: int) -> tuple[str | None, list[int], list[int]]:
        """(refusal, the rip's rip_title job ids, its encode ids); queued jobs and encodes are cancelled."""
        if conn.execute("SELECT 1 FROM video_rips WHERE rip_id = ?", (rip_id,)).fetchone() is None:
            return "Unknown rip.", [], []
        imp = film_imports.get(conn, rip_id)
        if imp is not None and imp["state"] in ("importing", "identifying"):
            return f"Rip {rip_id} is being imported; wait for it to finish.", [], []
        job_rows = conn.execute("SELECT id, state FROM jobs WHERE kind = 'rip_title' "
                                "AND json_extract(params, '$.rip_id') = ? ORDER BY id", (rip_id,)).fetchall()
        if any(r["state"] == "running" for r in job_rows):
            return f"Rip {rip_id} is still ripping; cancel it on the drive's card first.", [], []
        enc_rows = encodes.for_rip(conn, rip_id)
        if any(r["state"] == "running" for r in enc_rows):
            return f"Rip {rip_id} is still encoding; cancel the encode first.", [], []
        now = utc_now()
        for row in enc_rows:
            if row["state"] == "queued" and encodes.cancel_queued(conn, row["id"], now) == "running":
                return f"Rip {rip_id} is still encoding; cancel the encode first.", [], []
        for row in job_rows:
            if row["state"] in ("queued", "waiting_space") and jobs.cancel_queued(conn, row["id"], now) == "running":
                return f"Rip {rip_id} is still ripping; cancel it on the drive's card first.", [], []
        return None, [r["id"] for r in job_rows], [r["id"] for r in enc_rows]

    def _remove_files(self, rip_id: int, job_ids: list[int], encode_ids: list[int]) -> str | None:
        paths = [self.config.downloads_dir / str(rip_id)]
        paths += [self.config.work_dir / str(i) for i in job_ids]
        paths += [self.config.work_dir / f"encode-{i}" for i in encode_ids]
        for path in paths:
            try:
                if path.exists():
                    shutil.rmtree(path)
            except OSError as exc:
                return f"Couldn't delete {path}: {exc}"
        return None

    def _remove_rows(self, conn, rip_id: int) -> list[str]:
        """Delete the rip's rows (jobs and logs stay as history); returns the drives its jobs ran on."""
        drives_ = self._rip_drives(conn, rip_id)
        with conn:
            conn.execute("DELETE FROM encodes WHERE rip_id = ?", (rip_id,))
            conn.execute("DELETE FROM film_imports WHERE rip_id = ?", (rip_id,))
            conn.execute("DELETE FROM video_rips WHERE rip_id = ?", (rip_id,))
        return drives_

    async def remove_rip(self, rip_id: int) -> str | None:
        """Remove a rip from the Films page: its downloads folder, work leftovers and rows. The library
        and Radarr are never touched. Returns why it can't be removed, or None."""
        lock = self.import_lock(rip_id)
        if lock.locked():
            return f"Rip {rip_id} is being imported; wait for it to finish."
        async with lock:  # also keeps a film import from starting meanwhile
            refusal, job_ids, encode_ids = await asyncio.to_thread(self._db, self._remove_prepare, rip_id)
            if refusal is None:
                refusal = await asyncio.to_thread(self._remove_files, rip_id, job_ids, encode_ids)
            drive_ids = []
            if refusal is None:
                drive_ids = await asyncio.to_thread(self._db, self._remove_rows, rip_id)
                log.info("films: removed rip #%s", rip_id)
        # Always publish: a refusal may follow cancelling some queued work.
        self.films_changed()
        await self.encodes_changed()
        for drive_id in drive_ids or await asyncio.to_thread(self._db, self._rip_drives, rip_id):
            await self.drive_changed(drive_id)
        return refusal

    @staticmethod
    def _rip_drives(conn, rip_id: int) -> list[str]:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT drive_id FROM jobs WHERE kind = 'rip_title' "
            "AND json_extract(params, '$.rip_id') = ? AND drive_id IS NOT NULL", (rip_id,))]

    # -- encodes ---------------------------------------------------------------

    def _queue_encode(self, conn, rip_id: int, title: int, choice: str) -> tuple[int | None, str | None]:
        """(encode id, None) or (None, why it was refused). choice is a preset id as a string."""
        error = presets.parse_choice(conn, choice, allow_off=False)
        preset = presets.get_preset(conn, int(choice)) if error is None else None
        if preset is None:
            return None, presets.UNKNOWN
        rip = conn.execute("SELECT * FROM video_rips WHERE rip_id = ?", (rip_id,)).fetchone()
        entry = json.loads(rip["files"] or "{}").get(str(title)) if rip else None
        if entry is None:
            return None, "That file isn't part of this rip."
        if entry.get("raw_deleted_at"):
            return None, "The raw rip has been deleted, so it can't be encoded."
        source = Path(entry["path"])
        try:
            size = source.stat().st_size
        except OSError:  # the raw file is gone
            return None, "The raw rip is no longer on disk."
        disc = conn.execute("SELECT disc_type, scan FROM video_discs WHERE fingerprint = ?",
                            (rip["fingerprint"],)).fetchone()
        scanned = {}
        if disc is not None:
            try:
                scanned = {t["number"]: t for t in json.loads(disc["scan"] or "{}").get("titles", [])}.get(title, {})
            except (ValueError, KeyError, TypeError, AttributeError):
                scanned = {}
        seconds = scanned.get("seconds") if isinstance(scanned, dict) else None
        if disc is None or not isinstance(seconds, (int, float)) or seconds <= 0:
            return None, SCAN_MISSING
        encode_id = encodes.create(
            conn, rip_id=rip_id, title=title, role=entry.get("role", "title"), source=source,
            disc_type=disc["disc_type"], source_seconds=float(seconds), source_bytes=size,
            source_audio=int(scanned.get("audio", -1)), preset_id=preset.id, preset_name=preset.name,
            now=utc_now(),
            check=lambda c: not video_rips.raw_deleted(c, rip_id, title))  # same transaction as the insert
        if encode_id is None:
            return None, "This file already has an encode waiting or running, or its raw rip was just deleted."
        return encode_id, None

    async def queue_encode_result(self, rip_id: int, title: int, choice: str) -> tuple[int | None, str | None]:
        """Like queue_encode, but also says why it was refused (for the page)."""
        encode_id, error = await asyncio.to_thread(self._db, self._queue_encode, rip_id, title, choice)
        if encode_id is not None:
            self.encoder.wake()
            await self.encodes_changed()
        return encode_id, error

    async def queue_encode(self, rip_id: int, title: int, choice: str) -> int | None:
        """Queue an encode of one ripped file. None when refused (queue_encode_result says why):
        the raw is deleted or unknown, the scan is missing, or the file already has an encode active."""
        return (await self.queue_encode_result(rip_id, title, choice))[0]

    def _queue_all(self, conn, rip_id: int, choice: str) -> list[int]:
        row = conn.execute("SELECT files FROM video_rips WHERE rip_id = ?", (rip_id,)).fetchone()
        numbers = sorted(int(t) for t in json.loads(row["files"] or "{}")) if row else []
        ids = [self._queue_encode(conn, rip_id, t, choice)[0] for t in numbers]
        return [i for i in ids if i is not None]

    async def encode_all(self, rip_id: int, choice: str) -> list[int]:
        ids = await asyncio.to_thread(self._db, self._queue_all, rip_id, choice)
        if ids:
            self.encoder.wake()
            await self.encodes_changed()
        return ids

    def _retry_encode(self, conn, encode_id: int) -> tuple[int | None, str | None]:
        """(new encode id, None) or (None, why it was refused)."""
        row = encodes.get(conn, encode_id)
        if row is None:
            return None, "Unknown encode."
        if row["state"] not in ("failed", "cancelled", "interrupted"):
            return None, "Only an encode that failed, was cancelled or was interrupted can be retried."
        rip = conn.execute("SELECT files FROM video_rips WHERE rip_id = ?", (row["rip_id"],)).fetchone()
        entry = json.loads(rip["files"] or "{}").get(str(row["title"])) if rip else None
        if entry is None:
            return None, "That file isn't part of this rip any more."
        if entry.get("raw_deleted_at"):
            return None, "The raw rip has been deleted, so it can't be encoded."
        if not Path(row["source"]).is_file():
            return None, "The raw rip is no longer on disk."
        if row["preset_id"] is None or presets.get_preset(conn, row["preset_id"]) is None:
            gone = row["preset_id"] is None or not presets.exists(conn, row["preset_id"])
            return None, presets.GONE if gone else presets.INVALID
        try:
            new_id = encodes.retry(conn, encode_id, utc_now())
        except ValueError:  # PresetError: the preset went bad between the check and the retry
            return None, presets.INVALID
        if new_id is None:
            return None, "This file already has an encode waiting or running."
        return new_id, None

    async def retry_encode_result(self, encode_id: int) -> tuple[int | None, str | None]:
        """Retry a failed, cancelled or interrupted encode: (new id, None) or (None, why it was refused)."""
        new_id, error = await asyncio.to_thread(self._db, self._retry_encode, encode_id)
        if new_id is not None:
            self.encoder.wake()
            await self.encodes_changed()
        return new_id, error

    async def retry_encode(self, encode_id: int) -> int | None:
        return (await self.retry_encode_result(encode_id))[0]

    async def cancel_encode(self, encode_id: int) -> bool:
        return await self.encoder.cancel(encode_id)

    def _set_encode_choice(self, conn, drive_id: str, preset: str) -> str | None:
        if (error := presets.parse_choice(conn, preset)) is not None:
            return error
        drive = conn.execute("SELECT media_state, disc_type FROM drives WHERE id = ?", (drive_id,)).fetchone()
        if drive is None:
            return "Unknown drive."
        if drive["media_state"] != "disc" or drive["disc_type"] not in ("dvd", "bluray"):
            return "No DVD or Blu-ray in the drive."
        with conn:
            conn.execute("UPDATE drives SET encode_choice = ? WHERE id = ?", (preset, drive_id))
            # The latest rip of the disc in this drive, if it's under way: files not yet recorded follow it.
            conn.execute(
                "UPDATE video_rips SET encode_preset = ? WHERE rip_id = ("
                "SELECT MAX(rip_id) FROM video_rips WHERE fingerprint IN "
                "(SELECT fingerprint FROM video_discs WHERE drive_id = ?)) AND state = 'ripping'",
                (preset, drive_id))
        return None

    async def set_encode_choice(self, drive_id: str, preset: str) -> str | None:
        """Choose how the disc in a drive is encoded (a preset id or "off"). Returns an error, or None."""
        error = await asyncio.to_thread(self._db, self._set_encode_choice, drive_id, preset)
        if error is None:
            await self.drive_changed(drive_id)
            await self.encodes_changed()
        return error

    async def file_recorded(self, rip_id: int, title: int) -> None:
        """A ripped file reached downloads: queue the automatic encode, unless that is off."""
        try:
            preset = await asyncio.to_thread(self._db, _encode_preset_for, rip_id)
            if preset != "off":
                await self.queue_encode(rip_id, title, preset)
        except Exception:  # a failed queueing must not fail the rip that just succeeded
            log.exception("encode: queueing after rip #%s title %s failed", rip_id, title)

    def delete_raw_sync(self, conn, rip_id: int, title: int, *, now: datetime,
                        keep_hours: float | None) -> str | None:
        """Delete the raw rip of a title once a finished encode's output is safely on disk.

        Returns why it was refused, or None when the raw is deleted. keep_hours None means "now";
        otherwise the first good encode must have finished that long ago. Only the raw file goes:
        never an encode's output, never a folder."""
        conn.execute("BEGIN IMMEDIATE")  # the checks and the flag are one step: a queue can't slip between
        try:
            error, good, raw = self._raw_checks(conn, rip_id, title, now, keep_hours)
            if error is not None:
                conn.rollback()
                return error
            # Unlink first, then flag, all under the write lock: a failed unlink leaves the record alone,
            # and a crash in between leaves a file the next delete simply finds already gone.
            try:
                self._unlink_raw(raw)
            except OSError as exc:
                conn.rollback()
                return f"Couldn't delete the raw rip: {exc.strerror or exc}."
            video_rips.mark_raw_deleted(conn, rip_id, title, now.isoformat(timespec="seconds"))
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        joblog.JobLog(self.config.config_dir / "logs" / f"encode-{good[-1]['id']}.log").write(
            f"raw rip deleted: {raw}")
        return None

    @staticmethod
    def _unlink_raw(raw: Path) -> None:
        raw.unlink(missing_ok=True)

    @staticmethod
    def _raw_checks(conn, rip_id: int, title: int, now: datetime, keep_hours: float | None):
        """(error, good encodes, raw path)."""
        if rip_id in film_imports.active_rip_ids(conn):
            return ("This rip is being imported or was imported; it's cleaned up once Jellyfin has the film."), [], None
        rip = conn.execute("SELECT files FROM video_rips WHERE rip_id = ?", (rip_id,)).fetchone()
        entry = json.loads(rip["files"] or "{}").get(str(title)) if rip else None
        if entry is None:
            return "That file isn't part of this rip.", [], None
        if entry.get("raw_deleted_at"):
            return "The raw rip has already been deleted.", [], None
        rows = conn.execute("SELECT * FROM encodes WHERE rip_id = ? AND title = ? ORDER BY id",
                            (rip_id, title)).fetchall()
        if any(r["state"] in ("queued", "running") for r in rows):
            return "Another encode of this file is waiting or running, so the raw rip is kept.", [], None
        raw = Path(entry["path"])
        good = encodes.good_encodes(rows, raw)  # intact, and made from this very raw file
        if not good:
            return ("The raw rip is kept until an encode has finished and its file is still there "
                    "and complete."), [], None
        for (output,) in conn.execute("SELECT output FROM encodes WHERE output IS NOT NULL").fetchall():
            if encodes.same_file(raw, Path(output)):
                return "The raw file is also an encode's output, so it is kept.", [], None
        if keep_hours is not None:
            stamps = [_parse_utc(r["finished_at"]) for r in good if r["finished_at"]]
            if not stamps:
                return "The raw rip is kept: it isn't recorded when its encode finished.", [], None
            if now < min(stamps) + timedelta(hours=keep_hours):
                return "The raw rip is kept for a while after its encode.", [], None
        return None, good, raw

    async def delete_raw_now(self, rip_id: int, title: int) -> str | None:
        """"Delete raw now". Returns why it was refused, or None."""
        error = await asyncio.to_thread(
            self._db, lambda conn: self.delete_raw_sync(conn, rip_id, title, now=datetime.now(timezone.utc),
                                                        keep_hours=None))
        if error is None:
            await self.encodes_changed()
        return error

    def _sweep_raw(self, conn) -> int:
        keep = settings.get(conn, "raw_keep_hours")
        now = datetime.now(timezone.utc)
        pairs = conn.execute("SELECT DISTINCT rip_id, title FROM encodes WHERE state = 'done'").fetchall()
        removed = 0
        for p in pairs:
            rows = encodes.for_rip(conn, p["rip_id"])
            if encodes.lost_subtitles([r for r in rows if r["title"] == p["title"]]):
                continue  # the raw has the subtitles the encode lost: only Delete raw now lets it go
            if film_imports.jellyfin_hold(conn, p["rip_id"]) is not None:
                continue  # the Jellyfin clean-up removes the whole folder once the film shows
            # refusals are normal (not yet, or already gone)
            removed += self.delete_raw_sync(conn, p["rip_id"], p["title"], now=now, keep_hours=keep) is None
        return removed

    def _cancel_orphaned_disc_jobs(self, conn) -> None:
        """Start-up: queued disc jobs whose disc was taken out while the app was off can never run."""
        ids = [r[0] for r in conn.execute(
            "SELECT jobs.id FROM jobs JOIN drives ON drives.id = jobs.drive_id "
            "WHERE jobs.kind IN ('rip_title', 'scan_video') AND jobs.state IN ('queued', 'waiting_space') "
            "AND drives.media_state IN ('empty', 'tray_open')")]
        if not ids:
            return
        with conn:
            conn.executemany(
                "UPDATE jobs SET state = 'cancelled', finished_at = ?, message = ? WHERE id = ?",
                [(utc_now(), "The disc was taken out while the app was off.", i) for i in ids])
        for job_id in ids:
            self._settle_for_job(conn, job_id)
        log.info("jobs: cancelled %d queued disc job(s): their disc was taken out while the app was off", len(ids))

    def _sweep_partials(self, conn) -> int:
        removed = video_rips.sweep_partials(conn, self.config.downloads_dir)
        for rip_id, job_id in removed:
            log.info("video: removed the leftover partial folder of job #%s (rip #%s)", job_id, rip_id)
        return len(removed)

    async def _raw_sweep_loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self._db, self._sweep_partials)
            except Exception:
                log.exception("video: removing leftover partial folders failed")
            try:
                if await asyncio.to_thread(self._db, self._sweep_raw):
                    await self.encodes_changed()
            except Exception:  # never let the sweep die
                log.exception("encode: deleting old raw rips failed")
            await asyncio.sleep(RAW_SWEEP_SECONDS)

    async def _film_confirm_loop(self) -> None:
        while True:
            try:
                await filmimport.confirm_and_clean(self, datetime.now(timezone.utc))
            except Exception:  # never let the loop die
                log.exception("film import: confirming imports failed")
            try:
                await filmimport.sweep_ready(self)
            except Exception:
                log.exception("film import: looking for rips ready to import failed")
            await asyncio.sleep(FILM_CONFIRM_SECONDS)

    def _render_films(self) -> str | None:
        from .web.films_routes import render_films

        return self._db(render_films)

    def _render_encodes(self) -> str | None:
        from .web.films_routes import render_encodes

        return self._db(render_encodes)

    def films_changed(self) -> None:
        """A film import changed: publish the Films list soon (changes close together are sent once)."""
        self._films_dirty = True
        if self._films_push is not None and not self._films_push.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._films_push = loop.create_task(self._push_films())

    async def _push_films(self) -> None:
        while self._films_dirty:
            self._films_dirty = False
            await asyncio.sleep(0.1)
            if not self.broker.subscriber_count:
                continue
            try:
                html = await asyncio.to_thread(self._render_films)
            except Exception:  # a failed render must not stop later pushes
                log.exception("films: publishing the Films list failed")
                continue
            if html is not None:
                self.broker.publish("films", html)

    async def encodes_changed(self, progress_only: bool = False) -> None:
        """Publish the Films list and the Encoding panel. A progress tick refreshes the panel only:
        re-rendering the Films page every second would reset a menu the user is choosing from."""
        views = (("encodes", self._render_encodes),) if progress_only else (
            ("films", self._render_films), ("encodes", self._render_encodes))
        for name, render in views:
            html = await asyncio.to_thread(render)
            if html is not None:
                self.broker.publish(name, html)

    def _settle_for_job(self, conn, job_id: int) -> int | None:
        row = jobs.get(conn, job_id)
        if row is None or row["kind"] != "rip_title":
            return None
        rip_id = json.loads(row["params"]).get("rip_id")
        video_rips.settle(conn, rip_id, self.config.work_dir, utc_now(), downloads_dir=self.config.downloads_dir)
        return rip_id

    async def job_ended(self, job_id: int) -> None:
        """The runner's hook: a job finished, or a queued one was cancelled. A rip_title job
        settles its rip once nothing of it is pending (ripped, move_failed or failed)."""
        rip_id = await asyncio.to_thread(self._db, self._settle_for_job, job_id)
        await self._maybe_auto_retry(job_id)  # first: a pending retry keeps the import from starting
        if rip_id is not None:  # the disc may be complete now (its encodes finished before it)
            await filmimport.maybe_start(self, rip_id)

    def _plan_auto_retry(self, conn, job_id: int) -> bool:
        """A scan or rip that failed because MakeMKV went silent gets another go (up to
        AUTO_RETRY_MAX): note it on the failed job so the card says when."""
        row = jobs.get(conn, job_id)
        if row is None or row["state"] != "failed" or row["kind"] not in ("scan_video", "rip_title"):
            return False
        if not (row["error"] or "").startswith(videorip.STALLED_PREFIX):
            return False
        tries = int(json.loads(row["params"]).get("auto_retry", 0))
        if tries >= AUTO_RETRY_MAX:
            return False
        at = (datetime.now().astimezone() + timedelta(seconds=self.auto_retry_wait)).strftime("%H:%M")
        note = f" Retrying automatically at {at} (try {tries + 2} of {AUTO_RETRY_MAX + 1})."
        with conn:
            conn.execute("UPDATE jobs SET error = error || ? WHERE id = ?", (note, job_id))
        return True

    def _do_auto_retry(self, conn, job_id: int) -> int | None:
        """Queue the retry, unless the owner acted on the drive meanwhile or the disc came out."""
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = jobs.get(conn, job_id)
            params = json.loads(row["params"])
            # Anything queued on the drive since the failure (Retry pressed, another disc): leave it.
            newer = conn.execute("SELECT 1 FROM jobs WHERE drive_id = ? AND id > ? AND created_at > ?",
                                 (row["drive_id"], job_id, row["finished_at"])).fetchone()
            drive = conn.execute("SELECT media_state, disc_label FROM drives WHERE id = ?",
                                 (row["drive_id"],)).fetchone()
            same_disc = drive is not None and drive["media_state"] == "disc"
            if same_disc and row["kind"] == "rip_title":
                current = video_rips.disc_in_drive(conn, row["drive_id"])
                same_disc = current is not None and current["fingerprint"] == params.get("fingerprint")
            elif same_disc:
                same_disc = drive["disc_label"] == params.get("label", "")
            gone = row["kind"] == "rip_title" and conn.execute(
                "SELECT 1 FROM video_rips WHERE rip_id = ?", (params.get("rip_id"),)).fetchone() is None
            if newer or not same_disc or gone:  # (gone: the rip was removed while the retry waited)
                conn.rollback()
                return None
            params["auto_retry"] = int(params.get("auto_retry", 0)) + 1
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return jobs.enqueue(conn, row["drive_id"], row["kind"], params, needs_bytes=row["needs_bytes"],
                            uses_drive=bool(row["uses_drive"]), now=utc_now())

    async def _maybe_auto_retry(self, job_id: int) -> None:
        if not await asyncio.to_thread(self._db, self._plan_auto_retry, job_id):
            return
        row = await asyncio.to_thread(self._db, jobs.get, job_id)
        rip_id = json.loads(row["params"]).get("rip_id") if row["kind"] == "rip_title" else None
        if rip_id is not None:  # before any await: a concurrent readiness check must see the pending retry
            self._retry_rips[job_id] = rip_id
        await self.drive_changed(row["drive_id"])

        async def later():
            try:
                await asyncio.sleep(self.auto_retry_wait)
                new_id = await asyncio.to_thread(self._db, self._do_auto_retry, job_id)
            finally:
                self._retry_rips.pop(job_id, None)
            log.info("jobs: automatic retry of #%s: %s", job_id,
                     f"queued #{new_id}" if new_id else "skipped (the drive was used or the disc came out)")
            if new_id is not None:
                self.wake(row["drive_id"])
                await self.drive_changed(row["drive_id"])
            elif rip_id is not None:  # the retry was dropped: the disc may be ready after all
                await filmimport.maybe_start(self, rip_id)

        task = asyncio.get_running_loop().create_task(later())
        self._auto_retries.add(task)
        task.add_done_callback(self._auto_retries.discard)

    def _eject_target(self, conn, job_id: int) -> tuple[str, str] | None:
        """(drive_id, sr_name) if cancelling this job left its disc's rip finished: eject then."""
        row = jobs.get(conn, job_id)
        if row is None or row["kind"] != "rip_title" or row["state"] != "cancelled":
            return None
        rip_id = json.loads(row["params"]).get("rip_id")
        drive = conn.execute("SELECT sr_name, media_state FROM drives WHERE id = ?", (row["drive_id"],)).fetchone()
        if drive is None or not drive["sr_name"] or drive["media_state"] != "disc":
            return None
        if not titles.should_eject(video_rips.attempts(conn, rip_id)):
            return None
        return row["drive_id"], drive["sr_name"]

    def _eject(self, drive_id: str, sr_name: str) -> None:
        lock = self.monitor.drive_lock(drive_id)
        if not lock.acquire(timeout=5.0):
            raise OSError("the drive is busy")
        try:
            self.drive_factory(self.config.dev_root / sr_name).eject()
        finally:
            lock.release()

    async def cancel_job(self, job_id: int) -> bool:
        """The Cancel button. Cancelling the last pending title of a rip whose other titles
        are done (an unwanted extra) ejects the disc, as a finished rip would."""
        cancelled = await self.runner.cancel(job_id)
        if cancelled:
            target = await asyncio.to_thread(self._db, self._eject_target, job_id)
            if target is not None:
                try:
                    await asyncio.to_thread(self._eject, *target)
                except Exception as exc:  # a stuck tray mustn't fail the cancel
                    log.warning("video: eject after cancelling #%s failed: %s", job_id, exc)
                await self.drive_changed(target[0])
        return cancelled

    def _cancel_disc_jobs(self, conn, drive_id: str) -> list[int]:
        """Cancel the drive's queued and waiting scans and title rips, in one transaction so the
        worker can't start one of them in between; then settle the rips they belonged to."""
        now = utc_now()
        conn.execute("BEGIN IMMEDIATE")
        try:
            ids = [r[0] for r in conn.execute(
                "SELECT id FROM jobs WHERE drive_id = ? AND kind IN ('rip_title', 'scan_video') "
                "AND state IN ('queued', 'waiting_space')", (drive_id,))]
            conn.executemany("UPDATE jobs SET state = 'cancelled', finished_at = ? WHERE id = ?",
                             [(now, i) for i in ids])
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        for job_id in ids:
            self._settle_for_job(conn, job_id)
        return ids

    async def cancel_disc_jobs(self, drive_id: str) -> list[int]:
        ids = await asyncio.to_thread(self._db, self._cancel_disc_jobs, drive_id)
        if ids:
            log.info("video: cancelled queued job(s) %s on %s: the disc is leaving", ids, drive_id)
            self.wake(drive_id)
            await self.drive_changed(drive_id)
        return ids

    async def cancel_for_eject(self, drive_id: str) -> None:
        """Before an eject: the disc's queued jobs first (so the worker doesn't start the next
        one), then the running job that holds the drive."""
        await self.cancel_disc_jobs(drive_id)
        grouped = await asyncio.to_thread(self._db, jobs.drive_jobs, drive_id)
        if grouped.running is not None and grouped.running["uses_drive"]:
            await self.runner.cancel(grouped.running["id"])

    async def on_remove(self, drive_id: str) -> None:
        """The disc left the drive (ejected, or the media monitor saw it go)."""
        if self.runner.clear_wedged(drive_id):
            await self.drive_changed(drive_id)  # the "drive is stuck" note goes
        await self.cancel_disc_jobs(drive_id)

    def _hold_video(self, conn, drive_id: str) -> list[int]:
        with conn:
            conn.execute("UPDATE drives SET video_hold = 1 WHERE id = ?", (drive_id,))
        disc = video_rips.disc_in_drive(conn, drive_id)
        if disc is None:
            return []
        held = json.loads(disc["held"])
        if any(r.startswith(titles.DECOY_REASON + ":") for r in held):  # the wait is over: drop "rips at HH:MM"
            held = [titles.DECOY_REASON if r.startswith(titles.DECOY_REASON + ":") else r for r in held]
            held.append("Hold pressed")
            with conn:
                conn.execute("UPDATE video_discs SET held = ? WHERE fingerprint = ?",
                             (json.dumps(held), disc["fingerprint"]))
        # One write transaction, so the worker can't start the main title or an extra in between.
        conn.execute("BEGIN IMMEDIATE")
        try:
            mains = [r[0] for r in conn.execute(
                "SELECT id FROM jobs WHERE kind = 'rip_title' AND state IN ('queued', 'waiting_space') "
                "AND json_extract(params, '$.fingerprint') = ? AND json_extract(params, '$.role') = 'main'",
                (disc["fingerprint"],))]
            cancelled = []
            for main in mains:  # a held rip never started: drop it and its queued extras
                cancelled += [r[0] for r in conn.execute(
                    "SELECT id FROM jobs WHERE kind = 'rip_title' AND state IN ('queued', 'waiting_space') "
                    "AND (id = ? OR json_extract(params, '$.rip_id') = ?)", (main, main))]
            # video_rips cascades with the main job
            conn.executemany("DELETE FROM jobs WHERE id = ?", [(i,) for i in cancelled])
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return cancelled

    async def hold_video(self, drive_id: str) -> None:
        """Hold the disc: no automatic rip, and a queued (not yet running) main rip is cancelled."""
        task = self._dvd_waits.pop(drive_id, None)
        if task is not None:  # Hold during the DVD's wait: it must not rip when the wait ends
            task.cancel()
        await asyncio.to_thread(self._db, self._hold_video, drive_id)
        self.wake(drive_id)
        await self.drive_changed(drive_id)

    # -- Notifier ------------------------------------------------------------

    def _render_drive(self, drive_id: str) -> str | None:
        from .web.dashboard_routes import render_drive_card

        conn = db.connect(self.config.db_path)
        try:
            row = conn.execute("SELECT * FROM drives WHERE id = ?", (drive_id,)).fetchone()
            if row is None:
                return None
            return render_drive_card(conn, row, datetime.now().astimezone(), config=self.config,
                                     dvd_wait_pending=self.dvd_wait_pending,
                                     drive_wedged=self.runner.is_wedged)
        finally:
            conn.close()

    def _render_guard(self) -> str:
        from .web.dashboard_routes import render_guard

        conn = db.connect(self.config.db_path)
        try:
            return render_guard(conn, datetime.now().astimezone())
        finally:
            conn.close()

    async def drive_changed(self, drive_id: str) -> None:
        html = await asyncio.to_thread(self._render_drive, drive_id)
        if html is not None:
            self.broker.publish(f"drive-{drive_key(drive_id)}", html)

    async def guard_changed(self) -> None:
        self.broker.publish("guard", await asyncio.to_thread(self._render_guard))

    async def _guard_changed_safely(self) -> None:
        try:
            await self.guard_changed()
        except Exception:  # a render error must not end the loop that keeps the panel current
            log.exception("schedule: updating the schedule panel failed")

    # -- drives and workers ----------------------------------------------------

    def _active_drive_ids(self) -> set[str]:
        conn = db.connect(self.config.db_path)
        try:
            return {r["id"] for r in drives.list_drives(conn) if r["present"] and r["enabled"]}
        finally:
            conn.close()

    async def _refresh_drives(self) -> None:
        self.runner.sync_workers(await asyncio.to_thread(self._active_drive_ids))

    def refresh_drives(self) -> None:
        """Start/stop workers after a rescan or enable/disable. Safe from any thread."""
        self.in_loop(self._refresh_drives())

    def wake(self, drive_id: str) -> None:
        """Tell a drive's worker to look at its queue. Safe from any thread."""
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self.runner.wake, drive_id)

    def in_loop(self, coro) -> None:
        """Run a coroutine on the services' loop, from that loop or another thread."""
        if self._loop is None:  # background services not running (tests)
            coro.close()
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            self._tasks.append(self._loop.create_task(coro))
        else:
            asyncio.run_coroutine_threadsafe(coro, self._loop)

    # -- background loops ------------------------------------------------------

    async def _schedule_loop(self) -> None:
        while True:
            try:
                error = await schedule_source.refresh(self.config.db_path, self.fetch)
            except Exception as exc:  # never let the refresher die
                error = f"unexpected error: {exc!r}"
            if error and error != self._schedule_error:
                log.warning("schedule: %s (keeping the last good copy)", error)
            elif not error and self._schedule_error:
                log.info("schedule: fetched again")
            self._schedule_error = error
            await self._guard_changed_safely()
            await asyncio.sleep(SCHEDULE_REFRESH_SECONDS)

    async def _guard_loop(self) -> None:
        # Phases change with the clock, not only when the schedule is fetched.
        while True:
            await asyncio.sleep(GUARD_TICK_SECONDS)
            await self._guard_changed_safely()

    async def start(self) -> None:
        _configure_logging(self.config.config_dir)
        log.info("disc-ripper %s starting", _app_version())
        self._loop = asyncio.get_running_loop()
        count = 0
        conn = db.connect(self.config.db_path)
        try:
            def guard(label, fn):
                try:
                    return fn()
                except Exception:  # one bad step must not keep the app from starting
                    log.exception("start-up: %s failed; carrying on", label)

            count = guard("marking interrupted jobs", lambda: jobs.mark_interrupted(conn, utc_now())) or 0
            # a stale running row can survive a stop race
            guard("marking interrupted encodes", lambda: encodes.mark_interrupted(conn, utc_now()))
            guard("requeueing interrupted encodes", lambda: encodes.requeue_interrupted(conn, utc_now()))
            guard("filling preset fingerprints", lambda: presets.fill_fingerprints(conn))
            guard("reconciling CD rips", lambda: cd_rips.reconcile(conn, self.config.work_dir))
            unsettled = guard("listing unsettled rips", lambda: list(video_rips.unsettled(conn))) or []
            for rip_id in unsettled:  # rips whose last job ended in the last run
                guard(f"settling rip #{rip_id}", lambda rip_id=rip_id: video_rips.settle(
                    conn, rip_id, self.config.work_dir, utc_now(), downloads_dir=self.config.downloads_dir))
            guard("cancelling orphaned disc jobs", lambda: self._cancel_orphaned_disc_jobs(conn))
            guard("sweeping partial files", lambda: self._sweep_partials(conn))
            guard("sweeping old work folders",
                  lambda: cdrip.sweep_work(conn, self.config.work_dir, datetime.now(timezone.utc)))
        finally:
            conn.close()
        if count:
            log.info("jobs: %d job(s) were running at the last stop; marked interrupted", count)
        await self._refresh_drives()
        self.encoder.start()
        try:
            await filmimport.resume_after_restart(self)
        except Exception:  # the loops must still start
            log.exception("film import: resuming after the restart failed")
        try:
            await filmimport.sweep_ready(self)
        except Exception:
            log.exception("film import: looking for rips ready to import failed")
        for coro, name in (
            (self._raw_sweep_loop(), "raw sweep"),
            (self._film_confirm_loop(), "film import confirm"),
            (self.monitor.run(), "media monitor"),
            (self._schedule_loop(), "schedule refresh"),
            (self._guard_loop(), "guard tick"),
        ):
            self._tasks.append(asyncio.create_task(coro, name=name))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await self.runner.stop()
        await self.encoder.stop()
        self._loop = None
