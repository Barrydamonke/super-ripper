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
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from . import cd_rips, cdrip, coverart, db, drives, jobs, schedule_source, settings, video_rips, videorip  # cdrip, videorip register handlers
from .jellyfin import Jellyfin, RefreshDebouncer
from .musicbrainz import MusicBrainz
from .cdrom import CdDrive
from .cdtoc import Toc, musicbrainz_disc_id
from .clock import utc_now
from .config import AppConfig
from .events import Broker
from .media import MediaMonitor
from .runner import JobRunner
from .space import SpaceChecker

log = logging.getLogger(__name__)

SCHEDULE_REFRESH_SECONDS = 300
GUARD_TICK_SECONDS = 60
CD_BYTES_PER_SECOND = 176_400  # 44.1 kHz, 16-bit stereo
FLAC_RATIO = 0.75  # generous: FLAC is usually 50-70% of the raw audio


def drive_key(drive_id: str) -> str:
    """Short stable name for a drive in HTML ids and SSE event names (its ID has ':' and spaces)."""
    return hashlib.sha1(drive_id.encode()).hexdigest()[:10]


def _configure_logging() -> None:
    logger = logging.getLogger("disc_ripper")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)


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
            on_present=self.on_present,
        )
        self.runner = JobRunner(config, self, self.space, services=self)
        self.musicbrainz = MusicBrainz()
        self.refresher = RefreshDebouncer(self.jellyfin)
        self.cover_fetch = coverart.fetch_front
        self._tasks: list[asyncio.Task] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._schedule_error: str | None = None

    def jellyfin(self) -> Jellyfin | None:
        """A Jellyfin client from the current settings, or None if not configured."""
        conn = db.connect(self.config.db_path)
        try:
            url, key = settings.get(conn, "jellyfin_url"), settings.get(conn, "jellyfin_api_key")
        finally:
            conn.close()
        return Jellyfin(url, key) if url and key else None

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

    def _start_video_rip(self, conn, drive_id: str, fingerprint: str, main: int,
                         scan_job_id: int | None = None) -> int | None:
        disc = conn.execute("SELECT * FROM video_discs WHERE fingerprint = ?", (fingerprint,)).fetchone()
        drive = conn.execute("SELECT sr_name FROM drives WHERE id = ?", (drive_id,)).fetchone()
        title = next(t for t in json.loads(disc["scan"])["titles"] if t["number"] == main)
        params = {"rip_id": 0, "fingerprint": fingerprint, "title": main, "role": "main",
                  "minlength": disc["minlength"], "expected_seconds": title["seconds"],
                  "sr_name": drive["sr_name"]}
        now = utc_now()
        conn.execute("BEGIN IMMEDIATE")
        try:
            if scan_job_id is not None:  # an auto-start: Hold or a cancel may have come since the scan decided
                hold = conn.execute("SELECT video_hold FROM drives WHERE id = ?", (drive_id,)).fetchone()
                scan = conn.execute("SELECT state FROM jobs WHERE id = ?", (scan_job_id,)).fetchone()
                if (hold is not None and hold["video_hold"]) or scan is None or scan["state"] != "running":
                    conn.rollback()
                    return None
            cur = conn.execute(
                "INSERT INTO jobs (drive_id, kind, state, params, needs_bytes, uses_drive, created_at) "
                "VALUES (?, 'rip_title', 'queued', ?, ?, 1, ?)",
                (drive_id, json.dumps(params), math.ceil(title["size"] * 1.02), now),
            )
            job_id = cur.lastrowid
            conn.execute("UPDATE jobs SET params = json_set(params, '$.rip_id', ?) WHERE id = ?", (job_id, job_id))
            conn.execute(
                "INSERT INTO video_rips (rip_id, fingerprint, state, main_title, created_at, updated_at) "
                "VALUES (?, ?, 'ripping', ?, ?, ?)", (job_id, fingerprint, main, now, now))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return job_id

    async def start_video_rip(self, drive_id: str, fingerprint: str, *, main: int, extras: list[int],
                              again: bool, scan_job_id: int | None = None) -> int | None:
        """Queue the main title's rip. (Extras, TV titles and the duplicate guard come later.)"""
        rip_id = await asyncio.to_thread(self._db, self._start_video_rip, drive_id, fingerprint, main,
                                         scan_job_id)
        if rip_id is None:
            return None
        log.info("video: queued rip #%s on %s", rip_id, drive_id)
        self.wake(drive_id)
        await self.drive_changed(drive_id)
        return rip_id

    def _hold_video(self, conn, drive_id: str) -> list[int]:
        with conn:
            conn.execute("UPDATE drives SET video_hold = 1 WHERE id = ?", (drive_id,))
        disc = video_rips.disc_in_drive(conn, drive_id)
        if disc is None:
            return []
        rows = conn.execute(
            "SELECT id FROM jobs WHERE kind = 'rip_title' AND state IN ('queued', 'waiting_space') "
            "AND json_extract(params, '$.fingerprint') = ? AND json_extract(params, '$.role') = 'main'",
            (disc["fingerprint"],)).fetchall()
        cancelled = []
        for row in rows:
            if jobs.cancel_queued(conn, row["id"], utc_now()) == "cancelled":
                cancelled.append(row["id"])
                with conn:  # a held rip never started: drop it (video_rips cascades with its job)
                    conn.execute("DELETE FROM jobs WHERE id = ?", (row["id"],))
        return cancelled

    async def hold_video(self, drive_id: str) -> None:
        """Hold the disc: no automatic rip, and a queued (not yet running) main rip is cancelled."""
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
            return render_drive_card(conn, row, datetime.now().astimezone())
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
        _configure_logging()
        self._loop = asyncio.get_running_loop()
        conn = db.connect(self.config.db_path)
        try:
            count = jobs.mark_interrupted(conn, utc_now())
            cd_rips.reconcile(conn, self.config.work_dir)
            cdrip.sweep_work(conn, self.config.work_dir, datetime.now(timezone.utc))
        finally:
            conn.close()
        if count:
            log.info("jobs: %d job(s) were running at the last stop; marked interrupted", count)
        await self._refresh_drives()
        for coro, name in (
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
        self._loop = None
