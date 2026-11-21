"""The global encode pool: `encode_slots` workers taking queued encodes oldest first.

Like the per-drive runner, but encodes belong to no drive. Before an encode starts
it must fit the shutdown schedule (guard) and the work budget (space); otherwise it
waits in the queue with a reason (and holds up the ones behind it, so the order stays
predictable), or fails at once if it could never fit one session. The number of
slots is read once at start(); changing the setting applies after a restart.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from . import db, encodes, encoding, joblog, jobs, schedule_source, settings
from .clock import utc_now
from .config import AppConfig
from .events import Notifier
from .runner import CANCEL_WAIT_SECONDS, JobContext
from .space import SpaceChecker

log = logging.getLogger(__name__)


async def run_encode(ctx: JobContext, row) -> None:
    """Encode one file (Task 6). The pool calls it through this module so tests can patch it."""
    raise NotImplementedError("Task 6")


class EncodePool:
    time_scale = 1.0

    def __init__(
        self,
        config: AppConfig,
        notifier: Notifier,
        space: SpaceChecker,
        *,
        services=None,
        recheck_seconds: float = 30.0,
        now=lambda: datetime.now().astimezone(),
    ):
        self.config = config
        self.notifier = notifier
        self.space = space
        self.services = services
        self.recheck_seconds = recheck_seconds
        self.now = now
        self.set_progress = encodes.set_progress  # what JobContext.report writes through
        self._workers: list[asyncio.Task] = []
        self._wakeups: list[asyncio.Event] = []
        self._running: dict[int, asyncio.Task] = {}
        self._user_cancelled: set[int] = set()
        self._finished: dict[int, asyncio.Event] = {}
        # Pick + checks + start as one step, so two slots can't take the same encode or claim the same space.
        self._start_lock = asyncio.Lock()

    # -- what JobContext needs ----------------------------------------------

    async def notify(self, _drive_id: str = "") -> None:
        """Tell the services the encode list changed. Must never stop an encode."""
        hook = getattr(self.services, "encodes_changed", None)
        if hook is None:
            return
        try:
            result = hook()
            if inspect.isawaitable(result):
                await result
        except Exception:
            log.exception("encodes: notifying failed")

    def _call(self, fn, *args, **kwargs):
        conn = db.connect(self.config.db_path)
        try:
            return fn(conn, *args, **kwargs)
        finally:
            conn.close()

    async def db(self, fn, *args, **kwargs):
        return await asyncio.to_thread(self._call, fn, *args, **kwargs)

    # -- management -----------------------------------------------------------

    def start(self) -> None:
        if self._workers:
            return
        slots = max(int(self._call(settings.get, "encode_slots")), 1)
        for n in range(slots):
            self._wakeups.append(asyncio.Event())
            self._workers.append(asyncio.create_task(self._worker(n), name=f"encode slot {n}"))

    def wake(self) -> None:
        for event in self._wakeups:
            event.set()

    async def cancel(self, encode_id: int) -> bool:
        result = await self.db(encodes.cancel_queued, encode_id, utc_now())
        if result == "cancelled":
            await self.notify()
            self.wake()
            return True
        if result != "running":
            return False
        self._user_cancelled.add(encode_id)  # _run_encode checks this once its task exists
        task = self._running.get(encode_id)
        if task is not None and not task.done():
            task.cancel()
        finished = self._finished.get(encode_id)
        if finished is not None:
            try:
                await asyncio.wait_for(finished.wait(), CANCEL_WAIT_SECONDS)
            except TimeoutError:
                pass
        return True

    async def stop(self) -> None:
        workers, self._workers = self._workers, []
        self._wakeups = []
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await self.db(encodes.mark_interrupted, utc_now())

    # -- the worker ---------------------------------------------------------------

    async def _wait(self, slot: int, timeout: float | None) -> None:
        event = self._wakeups[slot]
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except TimeoutError:
            pass
        event.clear()

    async def _worker(self, slot: int) -> None:
        while True:
            try:
                await self._step(slot)
            except asyncio.CancelledError:
                raise
            except Exception:  # a bad row, flaky disk or locked database must not end the queue for good
                log.exception("encodes: slot %s hit an error; retrying", slot)
                await self._wait(slot, self.recheck_seconds)

    def _decide(self, conn, row) -> tuple[str, str]:
        """("start" | "wait" | "too_long", message) from the schedule guard, then the work space."""
        recent = encodes.recent_speeds(conn, row["preset"], row["disc_type"])
        seconds, rough = encoding.estimate_seconds(row["source_seconds"], row["preset"], row["disc_type"], recent)
        now = self.now()
        verdict, message = encoding.guard_decision(schedule_source.current_guard(conn, now), now, seconds, rough)
        if verdict != "start":
            return verdict, message
        reason = self.space.check(
            encoding.estimate_size(row["source_bytes"], row["preset"]),
            jobs.running_bytes(conn) + encodes.reserved_bytes(conn),
            settings.get(conn, "work_budget_gb"),
            settings.get(conn, "cache_min_free_gb"),
        )
        return ("wait", reason) if reason else ("start", "")

    def _try_start(self, conn: sqlite3.Connection) -> tuple[sqlite3.Row | None, str, str]:
        row = encodes.next_queued(conn)
        if row is None:
            return None, "idle", ""
        verdict, message = self._decide(conn, row)
        if verdict == "start":
            if not encodes.start(conn, row["id"], utc_now()):
                return None, "retry", ""  # cancelled while we were checking
            return row, "start", ""
        if verdict == "too_long":
            encodes.start(conn, row["id"], utc_now())
            encodes.finish(conn, row["id"], "failed", utc_now(), error=message)
            return row, "too_long", message
        if row["message"] != message:
            encodes.wait(conn, row["id"], message)
            return row, "wait_changed", message
        return row, "wait", message

    async def _step(self, slot: int) -> None:
        self._wakeups[slot].clear()
        async with self._start_lock:
            row, outcome, message = await self.db(self._try_start)
        if outcome == "idle":
            await self._wait(slot, None)
        elif outcome == "retry":
            return
        elif outcome == "too_long":
            joblog.JobLog(self._log_path(row["id"])).write(f"finished: failed — {message}")
            await self.notify()
        elif outcome in ("wait", "wait_changed"):
            if outcome == "wait_changed":
                await self.notify()
            await self._wait(slot, self.recheck_seconds)  # the schedule and free space change with time
        else:
            await self._run(row)

    def _log_path(self, encode_id: int) -> Path:
        return self.config.config_dir / joblog.LOG_DIRNAME / f"encode-{encode_id}.log"

    async def _run(self, row) -> None:
        encode_id = row["id"]
        self._finished[encode_id] = asyncio.Event()
        try:
            await self._run_encode(row)
        finally:
            self._finished.pop(encode_id).set()

    async def _run_encode(self, row) -> None:
        encode_id = row["id"]
        self.space.invalidate()  # it is about to write into /work
        await self.notify()
        log.info("encodes: started #%s (%s)", encode_id, row["preset"])
        ctx = JobContext(self, encode_id, "", {}, log=joblog.JobLog(self._log_path(encode_id)))
        ctx.log.write(f"encode #{encode_id} ({row['preset']}) started: {row['source']}")
        # Looked up at call time so tests can patch it.
        task = asyncio.create_task(globals()["run_encode"](ctx, row), name=f"encode {encode_id}")
        self._running[encode_id] = task
        if encode_id in self._user_cancelled:
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                # The worker is being stopped. Make sure the task is finished (its child
                # process terminated) before going on.
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self._call(encodes.mark_interrupted, utc_now())  # synchronous: we may be cancelled again
                ctx.log.write("finished: interrupted (restart or shutdown)")
                raise
            await self._finish(encode_id, "cancelled", None)
        except Exception as exc:  # an encode failing must not kill the slot
            log.exception("encodes: #%s failed", encode_id)
            await self._finish(encode_id, "failed", str(exc) or type(exc).__name__)
        else:
            await self._finish(encode_id, "done", None)
        finally:
            self._running.pop(encode_id, None)
            self._user_cancelled.discard(encode_id)

    async def _finish(self, encode_id: int, state: str, error: str | None) -> None:
        await self.db(encodes.finish, encode_id, state, utc_now(), error=error)
        log.info("encodes: #%s %s%s", encode_id, state, f": {error}" if error else "")
        joblog.JobLog(self._log_path(encode_id)).write(f"finished: {state}" + (f" — {error}" if error else ""))
        await self.notify()
