"""Per-drive job workers.

Each enabled drive has one worker task that runs its queue in order, one job at
a time, so a drive can never do two things at once. A job that doesn't fit the
cache budget waits in place (and holds up the jobs behind it, so the order
stays predictable). SQLite work runs in threads with short-lived connections so
a busy database never stalls the event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from . import db, jobs, settings
from .clock import utc_now
from .config import AppConfig
from .events import Notifier
from .space import SpaceChecker

log = logging.getLogger(__name__)

PROGRESS_INTERVAL = 1.0
CANCEL_WAIT_SECONDS = 3.0


class JobContext:
    def __init__(self, runner: JobRunner, job_id: int, drive_id: str, params: dict):
        self.job_id = job_id
        self.drive_id = drive_id
        self.params = params
        self._runner = runner
        self._last_write: float | None = None

    async def report(self, progress: float, message: str, estimated_finish: datetime | None = None) -> None:
        """Progress 0.0-1.0. Written and pushed to browsers at most once a second."""
        now = time.monotonic()
        if progress < 1.0 and self._last_write is not None and now - self._last_write < PROGRESS_INTERVAL:
            return
        self._last_write = now
        finish = estimated_finish.isoformat(timespec="seconds") if estimated_finish else None
        await self._runner.db(jobs.set_progress, self.job_id, min(max(progress, 0.0), 1.0), message, finish)
        await self._runner.notify(self.drive_id)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds * self._runner.time_scale)


Handler = Callable[[JobContext], Awaitable[None]]


def _left(seconds: int) -> str:
    minutes, secs = divmod(max(seconds, 0), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {secs}s"


async def run_test_job(ctx: JobContext) -> None:
    """Pretend work: runs for duration_s, reporting progress every second."""
    duration = max(int(ctx.params.get("duration_s", 60)), 1)
    finish = datetime.now(timezone.utc) + timedelta(seconds=duration)
    for elapsed in range(duration):
        await ctx.report(elapsed / duration, f"Test job: {_left(duration - elapsed)} left", finish)
        await ctx.sleep(1)
    await ctx.report(1.0, "Test job finished", finish)


HANDLERS: dict[str, Handler] = {"test": run_test_job}


class JobRunner:
    def __init__(
        self,
        config: AppConfig,
        notifier: Notifier,
        space: SpaceChecker,
        *,
        handlers: dict[str, Handler] = HANDLERS,
        time_scale: float = 1.0,
        recheck_seconds: float = 30.0,
    ):
        self.config = config
        self.notifier = notifier
        self.space = space
        self.handlers = handlers
        self.time_scale = time_scale
        self.recheck_seconds = recheck_seconds
        self._workers: dict[str, asyncio.Task] = {}
        self._wakeups: dict[str, asyncio.Event] = {}
        self._running: dict[int, asyncio.Task] = {}
        self._user_cancelled: set[int] = set()
        self._finished: dict[int, asyncio.Event] = {}
        # Space check + start as one step, so two drives can't both claim the same space.
        self._start_lock = asyncio.Lock()

    async def notify(self, drive_id: str) -> None:
        """Push a drive's card to browsers. A rendering problem must never stop a job."""
        try:
            await self.notifier.drive_changed(drive_id)
        except Exception:
            log.exception("jobs: notifying %s failed", drive_id)

    def _call(self, fn, *args, **kwargs):
        conn = db.connect(self.config.db_path)
        try:
            return fn(conn, *args, **kwargs)
        finally:
            conn.close()

    async def db(self, fn, *args, **kwargs):
        """Run fn(conn, *args) in a thread with its own connection."""
        return await asyncio.to_thread(self._call, fn, *args, **kwargs)

    # -- worker management -------------------------------------------------

    def sync_workers(self, drive_ids: set[str]) -> None:
        for drive_id in list(self._workers):
            if drive_id not in drive_ids:
                self._workers.pop(drive_id).cancel()
                self._wakeups.pop(drive_id, None)
        for drive_id in drive_ids:
            if drive_id not in self._workers:
                self._wakeups[drive_id] = asyncio.Event()
                self._workers[drive_id] = asyncio.create_task(self._worker(drive_id), name=f"worker {drive_id}")

    def wake(self, drive_id: str) -> None:
        event = self._wakeups.get(drive_id)
        if event is not None:
            event.set()

    async def cancel(self, job_id: int) -> bool:
        result = await self.db(jobs.cancel_queued, job_id, utc_now())
        if result == "cancelled":
            row = await self.db(jobs.get, job_id)
            await self.notify(row["drive_id"])
            self.wake(row["drive_id"])
            return True
        if result != "running":
            return False
        # Running: its handler task may not exist yet (the worker is between start
        # and creating it); _run checks this set once it does.
        self._user_cancelled.add(job_id)
        task = self._running.get(job_id)
        if task is not None and not task.done():
            task.cancel()
        finished = self._finished.get(job_id)
        if finished is not None:
            # Let the caller (the cancel button) render the card after the state is written.
            try:
                await asyncio.wait_for(finished.wait(), CANCEL_WAIT_SECONDS)
            except TimeoutError:
                pass
        return True

    async def stop(self) -> None:
        workers = list(self._workers.values())
        self._workers.clear()
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        # Anything a cancelled worker couldn't record (e.g. cancelled mid-write).
        await self.db(jobs.mark_interrupted, utc_now())

    # -- the worker --------------------------------------------------------

    async def _wait(self, drive_id: str, timeout: float | None) -> None:
        event = self._wakeups[drive_id]
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except TimeoutError:
            pass
        event.clear()

    def _space_check(self, conn, needs_bytes: int) -> str | None:
        return self.space.check(
            needs_bytes,
            jobs.running_bytes(conn),
            settings.get(conn, "work_budget_gb"),
            settings.get(conn, "cache_min_free_gb"),
        )

    async def _worker(self, drive_id: str) -> None:
        while True:
            try:
                await self._step(drive_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # a flaky disk or locked database must not end the queue for good
                log.exception("jobs: worker for %s hit an error; retrying", drive_id)
                await self._wait(drive_id, self.recheck_seconds)

    async def _step(self, drive_id: str) -> None:
        self._wakeups[drive_id].clear()
        job = await self.db(jobs.next_job, drive_id)
        if job is None:
            await self._wait(drive_id, None)
            return
        async with self._start_lock:
            reason = await self.db(self._space_check, job["needs_bytes"])
            started = reason is None and await self.db(jobs.start, job["id"], utc_now())
        if reason is not None:
            if job["state"] != "waiting_space" or job["message"] != reason:
                await self.db(jobs.wait_space, job["id"], reason)
                await self.notify(drive_id)
            await self._wait(drive_id, self.recheck_seconds)
            return
        if not started:
            return  # cancelled while we were checking; take the next one
        await self._run(drive_id, job)

    async def _run(self, drive_id: str, job) -> None:
        job_id = job["id"]
        self._finished[job_id] = asyncio.Event()
        try:
            await self._run_job(drive_id, job)
        finally:
            self._finished.pop(job_id).set()

    async def _run_job(self, drive_id: str, job) -> None:
        job_id = job["id"]
        self.space.invalidate()  # the job is about to write into /work
        await self.notify(drive_id)
        log.info("jobs: started #%s (%s) on %s", job_id, job["kind"], drive_id)
        handler = self.handlers.get(job["kind"])
        if handler is None:
            await self._finish(drive_id, job_id, "failed", f"Unknown job kind: {job['kind']}")
            return
        ctx = JobContext(self, job_id, drive_id, json.loads(job["params"]))
        task = asyncio.create_task(handler(ctx), name=f"job {job_id}")
        self._running[job_id] = task
        if job_id in self._user_cancelled:
            task.cancel()  # cancelled before the task existed
        try:
            await task
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                # The worker itself is being stopped: shutdown or the drive went away.
                task.cancel()
                # Written synchronously: awaiting here could be cancelled again.
                self._call(jobs.finish, job_id, "interrupted", utc_now(), message=jobs.INTERRUPTED_MESSAGE)
                raise
            await self._finish(drive_id, job_id, "cancelled", None)
        except Exception as exc:  # a job failing must not kill the drive's worker
            log.exception("jobs: #%s failed", job_id)
            await self._finish(drive_id, job_id, "failed", str(exc) or type(exc).__name__)
        else:
            await self._finish(drive_id, job_id, "done", None)
        finally:
            self._running.pop(job_id, None)
            self._user_cancelled.discard(job_id)

    async def _finish(self, drive_id: str, job_id: int, state: jobs.FinalState, error: str | None) -> None:
        await self.db(jobs.finish, job_id, state, utc_now(), error=error)
        log.info("jobs: #%s %s%s", job_id, state, f": {error}" if error else "")
        await self.notify(drive_id)
