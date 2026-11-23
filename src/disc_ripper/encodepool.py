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
import json
import logging
import os
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db, encodes, encoding, joblog, jobs, presets, runner, schedule_source, settings, videofiles, videorip
from . import space as space_mod
from .clock import utc_now
from .config import AppConfig
from .events import Notifier
from .runner import CANCEL_WAIT_SECONDS, JobContext
from .space import SpaceChecker

log = logging.getLogger(__name__)


STALL_UNIT_SECONDS = 60  # the stall setting is in minutes; tests shrink this
ASPECT_TOLERANCE = 0.02
CHECK_FAILED = "The encode failed its check:"
SEE_LOG = "See the log for the encoder's messages."
TRACKS_WRONG = "The encode's tracks don't match the preset:"


def _read_settings(conn):
    return settings.get(conn, "encode_stall_minutes"), settings.get(conn, "cache_min_free_gb")


def _other_reserved(conn, work_dir: Path, encode_id: int) -> int:
    """What other running rips and encodes were admitted for but haven't written yet."""
    total = videorip.outstanding_bytes(conn, work_dir, None)
    for other in encodes.running(conn):
        if other["id"] != encode_id:
            written = space_mod.tree_size(work_dir / f"encode-{other['id']}")
            est = encodes.estimate_for(conn, other)
            total += max((est.bytes if est else 0) - written, 0)
    return total


def _stopped_message(minutes: int) -> str:
    unit = "minute" if minutes == 1 else "minutes"
    return (f"The encoder stopped responding: no output for {minutes} {unit}, so it was stopped. "
            "Press Retry; the log has the encoder's last messages.")


async def _run_tool(ctx, argv, on_line, stall_minutes: int, work: Path, log_filter=None) -> int:
    try:
        return await ctx.run_process(
            argv, cwd=work, on_line=on_line, idle_timeout=stall_minutes * STALL_UNIT_SECONDS,
            log_filter=log_filter)
    except runner.ProcessStalled:
        raise RuntimeError(_stopped_message(stall_minutes)) from None


async def _detect_interlaced(ctx, source: Path, seconds: float, stall_minutes: int, work: Path) -> bool:
    votes = 0
    for fraction in (0.2, 0.5, 0.8):
        lines: list[str] = []

        async def collect(line: str) -> None:
            lines.append(line)

        await _run_tool(ctx, encoding.idet_argv(source, seconds * fraction), collect, stall_minutes, work)
        votes += encoding.parse_idet("\n".join(lines))
    interlaced = votes >= 2
    ctx.log.write(f"idet: {votes} of 3 samples interlaced, so {'deinterlacing' if interlaced else 'progressive'}")
    return interlaced


async def _detect_crop(ctx, source: Path, seconds: float, stall_minutes: int, work: Path,
                       info) -> tuple[int, int, int, int] | None:
    found = []
    for fraction in (0.2, 0.5, 0.8):
        lines: list[str] = []

        async def collect(line: str) -> None:
            lines.append(line)

        await _run_tool(ctx, encoding.cropdetect_argv(source, seconds * fraction), collect, stall_minutes, work)
        found.append(encoding.parse_cropdetect("\n".join(lines)))
    crop = encoding.pick_crop(found, info.width, info.height)
    ctx.log.write(f"cropdetect: {crop[0]}x{crop[1]} at {crop[2]},{crop[3]}" if crop else "cropdetect: no crop")
    return crop


def _langs(streams, kind: str) -> list[str]:
    return [presets.canonical_language(t.language) for t in streams if t.kind == kind]


def _same_langs(expected: list[str], got: list[str]) -> bool:
    """Same count and, track by track, the same language; `und` on either side matches anything."""
    return len(expected) == len(got) and all(a == b or "und" in (a, b) for a, b in zip(expected, got))


def _track_check(plan: encoding.TrackPlan, out_streams, engine: str = "ffmpeg") -> str | None:
    """Raise when the output's tracks aren't the plan's; the note to keep when HandBrake dropped subtitles."""
    expected, got = _langs(plan.audio, "audio"), _langs(out_streams, "audio")
    if not _same_langs(expected, got):
        raise RuntimeError(f"{TRACKS_WRONG} expected audio {', '.join(expected) or 'none'}; "
                           f"got {', '.join(got) or 'none'}.")
    want_subs, got_subs = len(plan.subtitles), len(_langs(out_streams, "subtitle"))
    if engine == "handbrake" and got_subs < want_subs:
        return f"HandBrake kept {got_subs} of {want_subs} subtitle tracks"
    if want_subs != got_subs:
        raise RuntimeError(f"{TRACKS_WRONG} expected {want_subs} subtitle tracks; got {got_subs}.")
    return None


def _aspect_check(source_dar: float | None, out_dar: float | None) -> None:
    if not source_dar or not out_dar:
        return
    if abs(out_dar - source_dar) / source_dar > ASPECT_TOLERANCE:
        raise RuntimeError(f"{CHECK_FAILED} the picture shape (DAR {out_dar:.2f}) doesn't match the rip's "
                           f"({source_dar:.2f}).")


def _move(config, rip_id, role, out, min_free, cancel, reserved) -> Path:
    try:
        return videofiles.move_to_downloads(config, rip_id, role, out, min_free_gb=min_free,
                                            cancel=cancel, reserved_bytes=reserved)
    except OSError as exc:
        raise RuntimeError(f"Couldn't move to downloads: {exc}") from exc


async def run_encode(ctx: JobContext, row) -> None:
    """Encode one file beside its raw. The raw is never modified or deleted.

    The work folder never outlives a failure.
    """
    work = ctx.config.work_dir / f"encode-{ctx.job_id}"
    try:
        await _encode(ctx, row, work)
    except BaseException:  # failure, cancel or shutdown: nothing of this attempt stays in /work
        shutil.rmtree(work, ignore_errors=True)
        raise


async def _clear_default_subtitles(ctx: JobContext, output: Path, stall_minutes: int, work: Path) -> None:
    """Rewrite a HandBrake encode with no default subtitle track (a copy: seconds, no re-encode)."""
    fixed = output.with_suffix(".tmp")  # not .mkv: the check wants exactly one .mkv in the folder
    ctx.log.write("clearing the default flag on subtitle tracks (forced flags kept)")

    async def ignore(_line: str) -> None:
        return None

    argv = encoding.priority_prefix() + encoding.clear_default_subtitles_argv(output, fixed)
    code = await _run_tool(ctx, argv, ignore, stall_minutes, work, log_filter=encoding.ProgressNoise())
    if code != 0 or not fixed.is_file():
        raise RuntimeError(f"Clearing the default subtitle flag on the encode failed. {SEE_LOG}")
    os.replace(fixed, output)


async def _encode(ctx: JobContext, row, work: Path) -> None:
    source = Path(row["source"])
    if not source.is_file():
        raise RuntimeError("The ripped file is no longer there.")
    seconds = float(row["source_seconds"])
    found = await ctx.db(presets.for_encode, row)
    if found is None:
        raise RuntimeError(presets.GONE)
    name, s, fingerprint = found
    settings_json = json.dumps(s.to_dict())
    await ctx.db(encodes.snapshot, ctx.job_id, name, settings_json, fingerprint)  # before any work
    ctx.log.write(f"preset: {name} {settings_json}")
    stall_minutes, min_free = await ctx.db(_read_settings)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)

    info = await asyncio.to_thread(videofiles.PROBE, source)
    plan = encoding.pick_tracks(info.streams, s)
    for note in plan.notes:
        ctx.log.write(note)
    interlaced = encoding.forced_interlaced(s)
    if encoding.needs_idet(s, row["disc_type"]):
        interlaced = await _detect_interlaced(ctx, source, seconds, stall_minutes, work)
    crop = None
    if encoding.needs_cropdetect(s):
        crop = await _detect_crop(ctx, source, seconds, stall_minutes, work, info)
    output = work / encoding.output_name(source, name)
    argv = encoding.priority_prefix() + encoding.build_argv(
        s, source, output, disc_type=row["disc_type"], plan=plan, source_height=info.height,
        interlaced=interlaced, crop=crop, sar=info.sar)

    parser = encoding.progress_parser(s, seconds)
    label = name
    tail: list[str] = []
    noise = encoding.ProgressNoise()
    quiet = [False]

    def log_filter(line: str) -> bool:  # run_process asks this just before on_line, per line
        quiet[0] = noise(line)
        return quiet[0]

    started = time.monotonic()

    async def on_line(line: str) -> None:
        if line.strip() and not quiet[0]:
            tail.append(line.strip())
            del tail[:-3]
        fraction = parser.feed(line)
        if fraction is None:
            return
        finish = None
        if fraction > 0.02:
            remaining = (time.monotonic() - started) * (1 - fraction) / fraction
            finish = datetime.now(timezone.utc) + timedelta(seconds=remaining)
        await ctx.report(fraction * 0.98, f"{label} — {fraction:.0%}", finish)

    code = await _run_tool(ctx, argv, on_line, stall_minutes, work, log_filter=log_filter)
    elapsed = time.monotonic() - started
    if code != 0:
        said = " / ".join(tail) or f"The encoder exited with code {code}"
        raise RuntimeError(f"{said}{'' if said.endswith(('.', '!', '?')) else '.'} {SEE_LOG}")

    if s.engine == "handbrake":
        await _clear_default_subtitles(ctx, output, stall_minutes, work)

    try:
        out = await asyncio.to_thread(videofiles.check_output, work, round(seconds), row["source_audio"],
                                      encoded=True)
    except RuntimeError as exc:
        raise RuntimeError(f"{CHECK_FAILED} {exc}") from None
    out_info = await asyncio.to_thread(videofiles.PROBE, out)
    if s.engine == "handbrake" and s.crop == "auto":
        ctx.log.write("aspect check skipped: HandBrake chooses its own crop")
    else:
        _aspect_check(encoding.expected_dar(info.dar, info.width, info.height, crop) if info.dar else None,
                      out_info.dar)
    sub_note = _track_check(plan, out_info.streams, s.engine)
    if sub_note:
        ctx.log.write(sub_note)

    await ctx.report(0.99, "Moving to downloads…")
    stop = threading.Event()
    reserved = await ctx.db(_other_reserved, ctx.config.work_dir, ctx.job_id)
    copying = asyncio.ensure_future(asyncio.to_thread(
        _move, ctx.config, row["rip_id"], row["role"], out, min_free, stop, reserved))
    try:
        landed = await asyncio.shield(copying)
    except asyncio.CancelledError:
        # The thread outlives the cancel: stop it between chunks so nothing lands in downloads.
        stop.set()
        await asyncio.gather(copying, return_exceptions=True)
        raise
    shutil.rmtree(work, ignore_errors=True)
    await ctx.db(encodes.finish, ctx.job_id, "done", utc_now(), output=str(landed),
                 output_bytes=landed.stat().st_size, speed=seconds / max(elapsed, 0.001),
                 message=" ".join((*plan.notes, *([sub_note] if sub_note else []))) or None)
    await ctx.report(1.0, "Done")


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
        """Tell the services an encode changed state. Must never stop an encode."""
        await self._tell()

    async def notify_progress(self, _drive_id: str = "") -> None:
        """A progress tick (JobContext.report): only the Encoding panel needs refreshing."""
        await self._tell(progress_only=True)

    async def _tell(self, **kwargs) -> None:
        hook = getattr(self.services, "encodes_changed", None)
        if hook is None:
            return
        try:
            result = hook(**kwargs)
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
        est = encodes.estimate_for(conn, row)
        if est is None:
            return "start", ""  # its preset is gone: let the encode fail with the reason
        now = self.now()
        verdict, message = encoding.guard_decision(schedule_source.current_guard(conn, now), now, est.seconds,
                                                   est.rough_time)
        if verdict != "start":
            return verdict, message
        reason = self.space.check(
            est.bytes,
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
            self.wake()  # a slot waiting on space or the schedule can recheck at once

    async def _run_encode(self, row) -> None:
        encode_id = row["id"]
        self.space.invalidate()  # it is about to write into /work
        await self.notify()
        log.info("encodes: started #%s (%s)", encode_id, row["preset_name"])
        ctx = JobContext(self, encode_id, "", {}, log=joblog.JobLog(self._log_path(encode_id)))
        ctx.log.write(f"encode #{encode_id} ({row['preset_name']}) started: {row['source']}")
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
        if state == "done":
            await self._tell_done(encode_id)

    async def _tell_done(self, encode_id: int) -> None:
        """Let the services start the film import. Must never stop an encode."""
        hook = getattr(self.services, "encode_done", None)
        if hook is None:
            return
        try:
            result = hook(encode_id)
            if inspect.isawaitable(result):
                await result
        except Exception:
            log.exception("encodes: starting the film import failed")
