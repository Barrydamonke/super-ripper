"""The rip_cd job: read an audio CD with cyanrip, then hand the files on.

Once the audio is safely ripped the job counts as done. Anything that goes wrong
afterwards (identifying, tagging, filing) puts the rip on the Music page's
"Needs a look" list instead of failing the job: the disc has already been ejected.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import cd_rips, cyanrip, jobs, runner, settings
from .cdrom import CdDrive
from .cdtoc import Toc, musicbrainz_disc_id
from .clock import utc_now
from .cyanrip import TrackCheck

log = logging.getLogger(__name__)

WORK_KEEP_DAYS = 7
DRIVE_LOCK_TIMEOUT = 10.0
_FINAL = ("done", "failed", "cancelled", "interrupted")


def running_process_count() -> int:
    return len(runner.LIVE_PROCESSES)


def _drive(ctx, sr_name: str):
    path = ctx.config.dev_root / sr_name
    factory = getattr(ctx.services, "drive_factory", None) if ctx.services else None
    return factory(path) if factory else CdDrive(path)


def _with_drive_lock(ctx, fn):
    """Run fn() holding the drive's lock (shared with the poller and eject)."""
    monitor = getattr(ctx.services, "monitor", None) if ctx.services else None
    if monitor is None:
        return fn()
    lock = monitor.drive_lock(ctx.drive_id)
    if not lock.acquire(timeout=DRIVE_LOCK_TIMEOUT):
        raise RuntimeError("The drive is busy.")
    try:
        return fn()
    finally:
        lock.release()


def _read_settings(conn) -> tuple[str, int]:
    return settings.get(conn, "cd_rip_mode"), settings.get(conn, "cd_retries")


async def run_rip_cd(ctx: runner.JobContext) -> None:
    params = ctx.params
    disc_id = params["disc_id"]
    toc = Toc.from_json(params["toc"])
    sr_name = params["sr_name"]

    # A retry may find another disc in the drive by now: never rip it under this disc's identity.
    current = await asyncio.to_thread(_with_drive_lock, ctx, lambda: _drive(ctx, sr_name).read_toc())
    if musicbrainz_disc_id(current) != disc_id:
        raise RuntimeError("A different disc is in the drive.")

    work = ctx.config.work_dir / str(ctx.job_id)
    for old in await ctx.db(cd_rips.earlier_unfinished, disc_id, ctx.job_id):
        shutil.rmtree(ctx.config.work_dir / str(old), ignore_errors=True)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)

    mode, retries = await ctx.db(_read_settings)
    argv = cyanrip.build_argv(device=ctx.config.dev_root / sr_name, offset=int(params.get("offset", 0)),
                              retries=retries, mode=mode)
    tracks = toc.audio_tracks
    parser = cyanrip.ProgressParser()
    tail: list[str] = []
    started = time.monotonic()

    async def on_line(line: str) -> None:
        if line.strip():
            tail.append(line.strip())
            del tail[:-5]
        progress = parser.feed(line)
        if progress is None:
            return
        number, fraction = progress
        index = tracks.index(number) if number in tracks else 0
        overall = (index + fraction) / len(tracks)
        finish = None
        if overall > 0.02:
            remaining = (time.monotonic() - started) * (1 - overall) / overall
            finish = datetime.now(timezone.utc) + timedelta(seconds=remaining)
        await ctx.report(overall * 0.99, f"Track {index + 1} of {len(tracks)} — {fraction:.0%}", finish)

    code = await ctx.run_process(argv, cwd=work, on_line=on_line)
    rip_dir = work / "rip"
    files = cyanrip.track_files(rip_dir) if rip_dir.is_dir() else {}
    if code != 0 or any(t not in files for t in tracks):
        detail = " / ".join(tail) or f"exit code {code}"
        raise RuntimeError(f"cyanrip failed: {detail}")

    logs = sorted(rip_dir.glob("*.log"))
    log_text = logs[0].read_text(encoding="utf-8", errors="replace") if logs else ""
    checks = cyanrip.parse_accuraterip(log_text, tracks)
    await ctx.db(cd_rips.update, ctx.job_id, accuraterip=[c.__dict__ for c in checks])
    await ctx.report(0.99, "Ripped — identifying…")

    try:
        await asyncio.to_thread(_with_drive_lock, ctx, lambda: _drive(ctx, sr_name).eject())
    except Exception as exc:  # a stuck tray mustn't lose a finished rip
        log.warning("cdrip: eject after rip #%s failed: %s", ctx.job_id, exc)

    try:
        await after_rip(ctx, disc_id=disc_id, toc=toc, checks=checks, rip_dir=rip_dir)
    except Exception as exc:
        log.exception("cdrip: after rip #%s failed", ctx.job_id)
        await ctx.db(cd_rips.update, ctx.job_id, state="needs_look",
                     reasons=[f"Couldn't finish after the rip: {exc}"])
    await ctx.report(1.0, "Ripped")


async def after_rip(ctx, *, disc_id: str, toc: Toc, checks: list[TrackCheck], rip_dir: Path) -> None:
    """Identify, tag, decide and file. Filled in by the identification step."""
    await ctx.db(cd_rips.update, ctx.job_id, state="needs_look", reasons=["not identified yet"])


def sweep_work(conn, work_dir: Path, now: datetime) -> None:
    """Remove work folders of rips that ended more than WORK_KEEP_DAYS ago."""
    cutoff = now - timedelta(days=WORK_KEEP_DAYS)
    if not work_dir.is_dir():
        return
    for folder in work_dir.iterdir():
        if not folder.is_dir() or not folder.name.isdigit():
            continue
        job = jobs.get(conn, int(folder.name))
        if job is None or job["state"] not in _FINAL or not job["finished_at"]:
            continue
        try:
            finished = datetime.fromisoformat(job["finished_at"])
        except ValueError:
            continue
        if finished.tzinfo is None:
            finished = finished.replace(tzinfo=timezone.utc)
        if finished < cutoff:
            shutil.rmtree(folder, ignore_errors=True)


runner.HANDLERS["rip_cd"] = run_rip_cd
