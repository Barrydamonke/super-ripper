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
import os
import shlex
import signal
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from datetime import datetime, timedelta, timezone

from . import db, joblog, jobs, settings
from .clock import utc_now
from .config import AppConfig
from .events import Notifier
from .space import SpaceChecker

log = logging.getLogger(__name__)

PROGRESS_INTERVAL = 1.0
CANCEL_WAIT_SECONDS = 3.0
KILL_GRACE_SECONDS = 5.0
# Child processes started by jobs, so tests (and shutdown checks) can see none are left.
LIVE_PROCESSES: set[asyncio.subprocess.Process] = set()


class ProcessStalled(Exception):
    """A child printed nothing for `seconds` and was stopped."""

    def __init__(self, seconds: float):
        super().__init__(f"no output for {seconds:g} s")
        self.seconds = seconds


class JobContext:
    def __init__(self, runner: JobRunner, job_id: int, drive_id: str, params: dict):
        self.job_id = job_id
        self.drive_id = drive_id
        self.params = params
        self.config = runner.config
        self.services = runner.services
        self._runner = runner
        self._last_write: float | None = None
        self.log = joblog.JobLog(joblog.path_for(runner.config, job_id))

    async def report(self, progress: float, message: str, estimated_finish: datetime | None = None) -> None:
        """Progress 0.0-1.0. Written and pushed to browsers at most once a second."""
        self.log.progress(progress)
        now = time.monotonic()
        if progress < 1.0 and self._last_write is not None and now - self._last_write < PROGRESS_INTERVAL:
            return
        self._last_write = now
        finish = estimated_finish.isoformat(timespec="seconds") if estimated_finish else None
        await self._runner.db(jobs.set_progress, self.job_id, min(max(progress, 0.0), 1.0), message, finish)
        await self._runner.notify(self.drive_id)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds * self._runner.time_scale)

    async def db(self, fn, *args, **kwargs):
        return await self._runner.db(fn, *args, **kwargs)

    async def run_process(
        self, argv: list[str], *, cwd: Path, on_line: Callable[[str], Awaitable[None]],
        env: dict[str, str] | None = None, idle_timeout: float | None = None,
    ) -> int:
        """Run a child (fixed argv, no shell), feeding each output line to on_line.

        Lines end at '\n' or '\r' (progress bars rewrite with '\r'). If the job is
        cancelled or the worker stops, the child gets SIGTERM, then SIGKILL after
        KILL_GRACE_SECONDS, before the cancellation carries on. With idle_timeout, a
        child that prints nothing for that many seconds is stopped the same way and
        ProcessStalled is raised. The command, its output (less progress chatter) and
        its exit code go to the job's log.
        """
        from .cyanrip import LineSplitter

        posix = os.name == "posix"
        self.log.write(f"$ {shlex.join(str(a) for a in argv)}")
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env={**os.environ, **env} if env else None,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=posix,  # its own process group, so helpers die with it
        )
        LIVE_PROCESSES.add(proc)
        splitter = LineSplitter()
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(proc.stdout.read(4096), idle_timeout)
                except TimeoutError:
                    self.log.write(f"no output for {_duration(idle_timeout)}: stopping the process")
                    raise ProcessStalled(idle_timeout) from None
                if not chunk:
                    break
                for line in splitter.feed(chunk.decode("utf-8", errors="replace")):
                    self.log.output(line)
                    await on_line(line)
            for line in splitter.flush():
                self.log.output(line)
                await on_line(line)
            code = await proc.wait()
            self.log.write(f"exit code {code}")
            return code
        finally:
            if proc.returncode is None:
                # Cancelled, or on_line failed: stop the child. Shielded so a second
                # cancel (e.g. shutdown after a user cancel) can't skip the SIGKILL.
                await asyncio.shield(_terminate(proc, posix))
            LIVE_PROCESSES.discard(proc)


def _duration(seconds: float) -> str:
    if seconds >= 60 and seconds % 60 == 0:
        minutes = int(seconds // 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{seconds:g} s"


async def _terminate(proc: asyncio.subprocess.Process, posix: bool) -> None:
    if proc.returncode is not None:
        return

    def send(sig: int) -> None:
        try:
            if posix:
                os.killpg(proc.pid, sig)
            elif sig == signal.SIGTERM:
                proc.terminate()
            else:
                proc.kill()
        except ProcessLookupError:
            pass

    send(signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), KILL_GRACE_SECONDS)
    except TimeoutError:
        send(getattr(signal, "SIGKILL", signal.SIGTERM))
        await proc.wait()


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
        services=None,
    ):
        self.config = config
        self.services = services  # the app's Services (drive access, MusicBrainz...); None in some tests
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
            await self._ended(job_id)  # a queued job never reaches its handler
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
        ctx = JobContext(self, job_id, drive_id, json.loads(job["params"]))
        ctx.log.write(f"job #{job_id} ({job['kind']}) started on {drive_id}")
        ctx.log.write(f"params: {job['params']}")
        handler = self.handlers.get(job["kind"])
        if handler is None:
            await self._finish(drive_id, job_id, "failed", f"Unknown job kind: {job['kind']}")
            return
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
                ctx.log.write("finished: interrupted (restart or shutdown)")
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
        joblog.JobLog(joblog.path_for(self.config, job_id)).write(
            f"finished: {state}" + (f" — {error}" if error else ""))
        await self._ended(job_id)
        await self.notify(drive_id)

    async def _ended(self, job_id: int) -> None:
        """Tell the services a job reached a final state (e.g. to settle a video rip)."""
        hook = getattr(self.services, "job_ended", None)
        if hook is None:
            return
        try:
            await hook(job_id)
        except Exception:  # bookkeeping after a job must never stop the worker
            log.exception("jobs: after #%s ended", job_id)
