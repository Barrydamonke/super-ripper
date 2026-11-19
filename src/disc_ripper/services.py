"""Wires the background machinery together and owns its lifecycle.

Services is also the Notifier: when a drive or the schedule changes it renders
the matching HTML fragment and publishes it to every open /events stream.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from . import cdrip, coverart, db, drives, jobs, schedule_source, settings  # cdrip registers rip_cd
from .jellyfin import Jellyfin, RefreshDebouncer
from .musicbrainz import MusicBrainz
from .cdrom import CdDrive
from .clock import utc_now
from .config import AppConfig
from .events import Broker
from .media import MediaMonitor
from .runner import JobRunner
from .space import SpaceChecker

log = logging.getLogger(__name__)

SCHEDULE_REFRESH_SECONDS = 300
GUARD_TICK_SECONDS = 60


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
        self.monitor = MediaMonitor(config, self, drive_factory=lambda path: self.drive_factory(path))
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
