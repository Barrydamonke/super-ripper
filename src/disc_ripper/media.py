"""Poll each drive for discs going in and out.

There is no udev inside the container, so every few seconds each enabled drive
is asked CDROM_DRIVE_STATUS. When a disc appears it is probed for its type and
label. Drives busy with a job that holds the drive are left alone.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
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
_BEFORE_INSERT = ("empty", "tray_open", "not_ready")
# Readings that may be a drive busy or spinning, not a removal: they must repeat to count.
_TRANSIENT = ("not_ready", "empty")
LOSS_WINDOW_SECONDS = 10.0  # a second transient reading within this long of the first is a removal


@dataclass(frozen=True)
class _Target:
    drive_id: str
    sr_name: str
    previous: str
    disc_type: str | None = None
    label: str = ""


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
        on_present: Callable[[str], Awaitable[None]] | None = None,
        on_remove: Callable[[str], Awaitable[None]] | None = None,
    ):
        self.config = config
        self.notifier = notifier
        self.drive_factory = drive_factory
        self.interval = interval
        self.probe_timeout = probe_timeout
        self.status_timeout = status_timeout
        self.on_insert = on_insert
        self.on_present = on_present  # a disc already in at start-up: remember it, don't rip
        self.on_remove = on_remove  # the disc left: its queued jobs must not outlive it
        self.probe = discprobe.probe
        self._locks: dict[str, threading.Lock] = {}
        self._lost_since: dict[str, float] = {}  # drive -> time of its first transient reading
        self._clock = time.monotonic
        self._locks_guard = threading.Lock()

    def drive_lock(self, drive_id: str) -> threading.Lock:
        """One lock per drive, shared by polling, probing and eject."""
        with self._locks_guard:
            return self._locks.setdefault(drive_id, threading.Lock())

    def _targets(self) -> list[_Target]:
        conn = db.connect(self.config.db_path)
        try:
            return [
                _Target(row["id"], row["sr_name"], row["media_state"], row["disc_type"], row["disc_label"] or "")
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
            self._lost_since.pop(target.drive_id, None)
            return  # someone else is using the drive; look again next round
        if state == "unknown" and target.previous == "disc" and not first:
            self._lost_since.pop(target.drive_id, None)  # not a reading of the tray: the run is broken
            # A timeout or a failed status call says nothing about the disc: it is almost always
            # still in. Treating it as a removal cancelled the drive's queued rips.
            log.info("media: %s did not report a state; keeping 'disc'", target.drive_id)
            return
        if state in _TRANSIENT and target.previous == "disc" and not first:
            # One not_ready/empty between title jobs is not a removal; it must persist.
            now = self._clock()
            earlier = self._lost_since.get(target.drive_id)
            if earlier is None or now - earlier > LOSS_WINDOW_SECONDS:
                self._lost_since[target.drive_id] = now
                log.info("media: %s reports %s with a disc in; waiting for another reading",
                         target.drive_id, state)
                return
            self._lost_since.pop(target.drive_id, None)  # confirmed: falls through as a removal
        elif state != "disc" or target.previous != "disc":
            self._lost_since.pop(target.drive_id, None)
        swap_check = False
        if state == "disc" and target.previous == "disc" and not first:
            # A disc is back after a transient reading: the same disc, or was it swapped?
            swap_check = target.drive_id in self._lost_since  # kept until a probe has run
        disc_type: str | None = None
        label = ""
        inserted = state == "disc" and (first or target.previous != "disc")
        if swap_check:
            await self._check_swap(target)
            return
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
        # Only a real insert: the drive was empty, open or spinning up. "unknown → disc"
        # is a read glitch with the same disc still in, and must not start a second rip.
        if inserted and first and self.on_present:
            await self.on_present(target.drive_id)
        if inserted and not first and target.previous in _BEFORE_INSERT and self.on_insert:
            await self.on_insert(target.drive_id)
        if changed and state != "disc" and target.previous == "disc" and self.on_remove:
            await self.on_remove(target.drive_id)

    async def _check_swap(self, target: _Target) -> None:
        """The disc went away and a disc is back within the debounce window: same one, or a swap?"""
        try:
            result = await asyncio.wait_for(asyncio.to_thread(self._probe, target), self.probe_timeout)
        except Exception:
            log.exception("media: probing %s failed", target.drive_id)
            self._lost_since.pop(target.drive_id, None)
            return  # can't tell: assume the same disc
        if result is _BUSY:
            return  # keep the mark; check again next poll
        self._lost_since.pop(target.drive_id, None)
        disc_type, label = result
        # Only a positive difference counts. discprobe degrades to ("unknown", "") while a drive
        # spins up, which says nothing. Audio CDs all have label "", so a CD-to-CD swap inside the
        # debounce window on a slot drive goes unnoticed (tray drives report tray_open, which acts at once).
        known = ("unknown", "data", None)
        type_differs = target.disc_type not in known and disc_type not in known and disc_type != target.disc_type
        label_differs = bool(target.label) and bool(label) and label != target.label
        if not (type_differs or label_differs):
            return
        log.info("media: %s: disc swapped (%r -> %r)", target.drive_id, target.label, label)
        await asyncio.to_thread(self._store, target.drive_id, "empty", None, "")
        if self.on_remove:
            await self.on_remove(target.drive_id)
        await asyncio.to_thread(self._store, target.drive_id, "disc", disc_type, label)
        await self.notifier.drive_changed(target.drive_id)
        if self.on_insert:
            await self.on_insert(target.drive_id)

    async def poll_once(self, *, first: bool = False) -> None:
        targets = await asyncio.to_thread(self._targets)
        ids = {t.drive_id for t in targets}
        for drive_id in [d for d in self._lost_since if d not in ids]:
            del self._lost_since[drive_id]  # skipped (a job holds the drive): not consecutive
        for target in targets:
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
