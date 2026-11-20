"""The scan_video job: read a DVD/Blu-ray's titles with makemkvcon, then start or hold."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import makemkv, runner, settings, titles, video_rips, videofiles
from .cdrip import _drive, _with_drive_lock
from .clock import utc_now

log = logging.getLogger(__name__)


def write_settings_conf(home: Path, key: str, selection: str) -> None:
    path = home / ".MakeMKV" / "settings.conf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(makemkv.settings_conf(key, selection.strip() or settings.DEFAULT_VIDEO_SELECTION),
                    encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def scan_env(config) -> dict:
    return {"HOME": str(config.config_dir / "makemkv")}


def _read(conn):
    key = settings.get(conn, "makemkv_key")
    selection = settings.get(conn, "video_track_selection")
    minlength = settings.get(conn, "video_min_title_seconds")
    return key, selection, minlength


def _decide(conn, drive_id, disc_type, label, scan, minlength, from_insert, downloads_dir, work_dir, now):
    fp = makemkv.fingerprint(disc_type, label, scan.titles)
    blocking = video_rips.blocking_rip(conn, fp, downloads_dir, work_dir)
    hold = conn.execute("SELECT video_hold FROM drives WHERE id = ?", (drive_id,)).fetchone()
    reasons = titles.hold_reasons(
        scan.titles,
        auto_start=bool(settings.get(conn, "auto_start_rips")),
        hold_pressed=bool(hold and hold["video_hold"]),
        already_ripped=f"downloads/{blocking['rip_id']}/" if blocking is not None else None,
        from_insert=from_insert,
    )
    payload = {"label": scan.label, "titles": [dataclasses.asdict(t) for t in scan.titles]}
    video_rips.save_scan(conn, fp, disc_type, label, payload, minlength, reasons, drive_id, now)
    return fp, reasons


async def run_scan_video(ctx: runner.JobContext) -> None:
    params = ctx.params
    disc_type, label = params["disc_type"], params.get("label", "")
    key, selection, minlength = await ctx.db(_read)
    home = ctx.config.config_dir / "makemkv"
    write_settings_conf(home, key, selection)
    work = ctx.config.work_dir / str(ctx.job_id)
    work.mkdir(parents=True, exist_ok=True)

    parser = makemkv.RobotParser()

    async def on_line(line: str) -> None:
        fraction = parser.feed(line)
        if fraction is not None:
            await ctx.report(fraction * 0.99, f"Scanning disc… {fraction:.0%}")

    argv = makemkv.info_argv(ctx.config.dev_root / params["sr_name"], minlength)
    code = await ctx.run_process(argv, cwd=work, on_line=on_line, env=scan_env(ctx.config))
    scan = parser.result()
    if code != 0 or not scan.titles:
        last = scan.messages[-1] if scan.messages else f"makemkvcon exit code {code}"
        hint = makemkv.update_hint(scan.messages)
        raise RuntimeError(last + (" " + hint if hint else ""))

    fp, reasons = await ctx.db(
        _decide, ctx.drive_id, disc_type, label, scan, minlength, bool(params.get("from_insert")),
        ctx.config.downloads_dir, ctx.config.work_dir, utc_now(),
    )
    log.info("video: scanned %r on %s: %d titles, held=%s", label, ctx.drive_id, len(scan.titles), reasons)
    main = titles.main_title(scan.titles)
    if not reasons:
        await ctx.services.start_video_rip(ctx.drive_id, fp, main=main.number, extras=[], again=False,
                                          scan_job_id=ctx.job_id)
    await ctx.report(1.0, "Scanned")
    drive_changed = getattr(ctx.services, "drive_changed", None)
    if drive_changed is not None:
        await drive_changed(ctx.drive_id)


runner.HANDLERS["scan_video"] = run_scan_video


# --- the rip_title job ---------------------------------------------------------------

DIFFERENT_DISC = "A different disc is in the drive."


def _prepare(conn, params: dict, job_id: int, now: str):
    """Check the disc and read what the rip needs. Returns (settings, old job ids to clean up)."""
    drive = conn.execute(
        "SELECT disc_type, disc_label FROM drives WHERE id = (SELECT drive_id FROM jobs WHERE id = ?)",
        (job_id,)).fetchone()
    disc = conn.execute("SELECT disc_type, label FROM video_discs WHERE fingerprint = ?",
                        (params["fingerprint"],)).fetchone()
    if drive is None or disc is None or (drive["disc_type"], drive["disc_label"]) != (disc["disc_type"], disc["label"]):
        raise RuntimeError(DIFFERENT_DISC)
    rip = conn.execute("SELECT state FROM video_rips WHERE rip_id = ?", (params["rip_id"],)).fetchone()
    if rip is not None and rip["state"] == "failed":  # a retry
        video_rips.set_state(conn, params["rip_id"], "ripping", "", now)
    old = video_rips.earlier_unfinished_jobs(conn, params["rip_id"], params["title"], job_id)
    return (settings.get(conn, "makemkv_key"), settings.get(conn, "video_track_selection"),
            settings.get(conn, "cache_min_free_gb")), old


def _label(conn, params: dict) -> str:
    role = params.get("role", "title")
    if role == "main":
        return "Main title"
    k, m = video_rips.role_position(conn, params["rip_id"], role, params["title"])
    return f"{'Extra' if role == 'extra' else 'Title'} {k} of {m}"


def _after_move(conn, rip_id: int, job_id: int, title: int, path: Path | None, role: str, message: str, now: str):
    if path is None:
        video_rips.set_state(conn, rip_id, "move_failed", message, now)
        return
    video_rips.record_file(conn, rip_id, title, path, role, now)
    if video_rips.all_moved(conn, rip_id, job_id):
        video_rips.set_state(conn, rip_id, "ripped", "", now)


def _move(config, rip_id: int, role: str, src: Path, min_free_gb: float,
          cancel: threading.Event | None = None) -> tuple[Path | None, str]:
    try:
        return videofiles.move_to_downloads(config, rip_id, role, src, min_free_gb=min_free_gb,
                                           cancel=cancel), ""
    except OSError as exc:
        log.warning("video: moving rip #%s to downloads failed: %s", rip_id, exc)
        return None, f"Couldn't move to downloads: {exc}"


async def run_rip_title(ctx: runner.JobContext) -> None:
    params = ctx.params
    work = ctx.config.work_dir / str(ctx.job_id)
    try:
        await _rip_title(ctx, params, work)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        shutil.rmtree(work, ignore_errors=True)
        await ctx.db(video_rips.fail_if_nothing_done, params["rip_id"], ctx.job_id, str(exc), utc_now())
        raise


async def _rip_title(ctx, params: dict, work: Path) -> None:
    rip_id, title, role = params["rip_id"], params["title"], params.get("role", "title")
    (key, selection, min_free), old = await ctx.db(_prepare, params, ctx.job_id, utc_now())
    for job in old:  # a cancelled or interrupted attempt's partial file would sit in the budget for a week
        shutil.rmtree(ctx.config.work_dir / str(job), ignore_errors=True)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    write_settings_conf(ctx.config.config_dir / "makemkv", key, selection)
    label = await ctx.db(_label, params)

    parser = makemkv.RobotParser()
    started = time.monotonic()

    async def on_line(line: str) -> None:
        fraction = parser.feed(line)
        if fraction is None:
            return
        finish = None
        if fraction > 0.02:
            remaining = (time.monotonic() - started) * (1 - fraction) / fraction
            finish = datetime.now(timezone.utc) + timedelta(seconds=remaining)
        await ctx.report(fraction * 0.98, f"{label} — {fraction:.0%}", finish)

    argv = makemkv.mkv_argv(ctx.config.dev_root / params["sr_name"], title, params["minlength"], work)
    code = await ctx.run_process(argv, cwd=ctx.config.work_dir, on_line=on_line, env=scan_env(ctx.config))
    if code != 0:
        messages = parser.result().messages
        hint = makemkv.update_hint(messages)
        detail = " / ".join(messages[-3:]) or f"makemkvcon exit code {code}"
        raise RuntimeError(detail + (" " + hint if hint else ""))
    src = await asyncio.to_thread(videofiles.check_output, work, params["expected_seconds"])
    await ctx.report(0.99, "Moving to downloads…")
    stop = threading.Event()
    copying = asyncio.ensure_future(asyncio.to_thread(_move, ctx.config, rip_id, role, src, min_free, stop))
    try:
        moved, message = await asyncio.shield(copying)
    except asyncio.CancelledError:
        # The thread outlives the cancel: stop it between chunks so nothing lands in downloads.
        stop.set()
        await asyncio.gather(copying, return_exceptions=True)
        raise
    await ctx.db(_after_move, rip_id, ctx.job_id, title, moved, role, message, utc_now())
    if moved is not None:
        shutil.rmtree(work, ignore_errors=True)
    if await ctx.db(lambda conn: titles.should_eject(video_rips.settled(conn, rip_id, ctx.job_id))):
        try:
            await asyncio.to_thread(_with_drive_lock, ctx, lambda: _drive(ctx, params["sr_name"]).eject())
        except Exception as exc:  # a stuck tray mustn't lose a finished rip
            log.warning("video: eject after rip #%s failed: %s", rip_id, exc)
    await ctx.report(1.0, "Ripped" if moved is not None else message)


runner.HANDLERS["rip_title"] = run_rip_title


def move_again_sync(conn, config, rip_id: int) -> str | None:
    """Retry the move for each finished title whose file still sits in /work."""
    min_free = settings.get(conn, "cache_min_free_gb")
    error = None
    for title, job_id, state in video_rips.attempts(conn, rip_id):
        folder = config.work_dir / str(job_id)
        found = sorted(folder.rglob("*.mkv")) if state == "done" and folder.is_dir() else []
        if not found:
            continue
        role = conn.execute("SELECT json_extract(params, '$.role') FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]
        role = role or "title"
        moved, message = _move(config, rip_id, role, found[0], min_free)
        if moved is None:
            error = message
            video_rips.set_state(conn, rip_id, "move_failed", message, utc_now())
            continue
        shutil.rmtree(folder, ignore_errors=True)
        video_rips.record_file(conn, rip_id, title, moved, role, utc_now())
    if error is None and video_rips.all_moved(conn, rip_id):
        video_rips.set_state(conn, rip_id, "ripped", "", utc_now())
    return error
