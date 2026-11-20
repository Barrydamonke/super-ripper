"""The scan_video job: read a DVD/Blu-ray's titles with makemkvcon, then start or hold."""

from __future__ import annotations

import dataclasses
import logging
import os
from pathlib import Path

from . import makemkv, runner, settings, titles, video_rips
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
