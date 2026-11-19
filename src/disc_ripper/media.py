"""Poll each drive for discs going in and out.

There is no udev inside the container, so every few seconds each enabled drive
is asked CDROM_DRIVE_STATUS. When a disc appears it is probed for its type and
label. Drives busy with a job that holds the drive are left alone.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from . import db, discprobe, drives, jobs
from .cdrom import CdDrive
from .clock import utc_now
from .config import AppConfig
from .events import Notifier

log = logging.getLogger(__name__)
_BUSY = object()


@dataclass(frozen=True)
class _Target:
    drive_id: str
    sr_name: str
    previous: str


class MediaMonitor:
    def __init__(
        self,
        config: AppConfig,
        notifier: Notifier,
        *,
        drive_factory: Callable[[Path], CdDrive] = CdDrive,
        interval: float = 3.0,
        probe_timeout: float = 30.0,
        status_timeout: float = 10.0,
        on_insert: Callable[[str], Awaitable[None]] | None = None,
    ):
        self.config = config
        self.notifier = notifier
        self.drive_factory = drive_factory
        self.interval = interval
        self.probe_timeout = probe_timeout
        self.status_timeout = status_timeout
        self.on_insert = on_insert
        self.probe = discprobe.probe
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def drive_lock(self, drive_id: str) -> threading.Lock:
        """One lock per drive, shared by polling, probing and eject."""
        with self._locks_guard:
            return self._locks.setdefault(drive_id, threading.Lock())

    def _targets(self) -> list[_Target]:
        conn = db.connect(self.config.db_path)
        try:
            return [
                _Target(row["id"], row["sr_name"], row["media_state"])
                for row in drives.list_drives(conn)
                if row["present"] and row["enabled"] and row["sr_name"]
                and not jobs.running_uses_drive(conn, row["id"])
            ]
        finally:
            conn.close()

    def _locked(self, target: _Target, fn):
        """Run fn(drive) under the drive's lock, or return _BUSY if it is held.

        Never wait for the lock: a probe stuck on a damaged disc (or an eject)
        would otherwise stall polling of every drive."""
        lock = self.drive_lock(target.drive_id)
        if not lock.acquire(blocking=False):
            return _BUSY
        try:
            return fn(self.drive_factory(self.config.dev_root / target.sr_name))
        finally:
            lock.release()

    def _status(self, target: _Target):
        return self._locked(target, lambda drive: drive.drive_status())

    def _probe(self, target: _Target):
        return self._locked(target, self.probe)

    def _store(self, drive_id: str, state: str, disc_type: str | None, label: str) -> bool:
        conn = db.connect(self.config.db_path)
        try:
            return drives.set_media(conn, drive_id, state, disc_type, label, utc_now())
        finally:
            conn.close()

    async def _poll_drive(self, target: _Target, first: bool) -> None:
        try:
            state = await asyncio.wait_for(asyncio.to_thread(self._status, target), self.status_timeout)
        except TimeoutError:
            log.warning("media: %s did not answer within %gs", target.drive_id, self.status_timeout)
            state = "unknown"
        except Exception:  # one bad drive must not stop the others
            log.exception("media: status of %s failed", target.drive_id)
            state = "unknown"
        if state is _BUSY:
            return  # someone else is using the drive; look again next round
        disc_type: str | None = None
        label = ""
        inserted = state == "disc" and (first or target.previous != "disc")
        if state == "disc" and not inserted:
            return  # already probed; nothing to do while the disc stays in
        if inserted:
            try:
                result = await asyncio.wait_for(asyncio.to_thread(self._probe, target), self.probe_timeout)
                if result is _BUSY:
                    return  # probe next round
                disc_type, label = result
            except Exception:
                log.exception("media: probing %s failed", target.drive_id)
                disc_type, label = "unknown", ""
        changed = await asyncio.to_thread(self._store, target.drive_id, state, disc_type, label)
        if changed:
            log.info("media: %s -> %s %s %r", target.drive_id, state, disc_type or "", label)
            await self.notifier.drive_changed(target.drive_id)
        if inserted and not first and target.previous != "disc" and self.on_insert:
            await self.on_insert(target.drive_id)

    async def poll_once(self, *, first: bool = False) -> None:
        for target in await asyncio.to_thread(self._targets):
            try:
                await self._poll_drive(target, first)
            except Exception:  # e.g. database locked, a render error: carry on with the next drive
                log.exception("media: polling %s failed", target.drive_id)

    async def run(self) -> None:
        try:
            await self.poll_once(first=True)
        except Exception:
            log.exception("media: first poll failed")
        while True:
            await asyncio.sleep(self.interval)
            try:
                await self.poll_once()
            except Exception:
                log.exception("media: poll failed")
