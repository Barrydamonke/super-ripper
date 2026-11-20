"""Checking a ripped video file with ffprobe, and moving it to downloads."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

GIB = 1024**3
STATVFS = getattr(os, "statvfs", None)  # absent on Windows dev machines: the space check is skipped
MIN_LENGTH_FRACTION = 0.9


@dataclass
class MkvInfo:
    seconds: float
    video: int
    audio: int


def parse_ffprobe(text: str) -> MkvInfo:
    data = json.loads(text)
    streams = data.get("streams", [])
    return MkvInfo(
        seconds=float(data.get("format", {}).get("duration") or 0),
        video=sum(1 for s in streams if s.get("codec_type") == "video"),
        audio=sum(1 for s in streams if s.get("codec_type") == "audio"),
    )


def ffprobe(path: Path) -> MkvInfo:
    argv = ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    done = subprocess.run(argv, capture_output=True, text=True, timeout=300)  # noqa: S603 (fixed argv)
    if done.returncode != 0:
        raise RuntimeError(f"ffprobe couldn't read the file: {done.stderr.strip()[-200:] or done.returncode}")
    return parse_ffprobe(done.stdout)


PROBE: Callable[[Path], MkvInfo] = ffprobe  # tests replace this


def check_output(work: Path, expected_seconds: int) -> Path:
    """The one ripped file in work, if it looks right; RuntimeError says what's wrong."""
    files = sorted(work.rglob("*.mkv")) if work.is_dir() else []
    if not files:
        raise RuntimeError("MakeMKV finished but wrote no file.")
    path = files[0]
    info = PROBE(path)
    if info.video < 1:
        raise RuntimeError("The ripped file has no video.")
    if info.audio < 1:
        raise RuntimeError("The ripped file has no audio.")
    if expected_seconds and info.seconds < expected_seconds * MIN_LENGTH_FRACTION:
        raise RuntimeError(
            f"The ripped file's length is {info.seconds:.0f}s but the disc says {expected_seconds}s.")
    return path


def _free_name(dest: Path, name: str) -> Iterator[Path]:
    stem, suffix = Path(name).stem, Path(name).suffix
    yield dest / name
    n = 2
    while True:
        yield dest / f"{stem} ({n}){suffix}"
        n += 1


def move_to_downloads(config, rip_id: int, role: str, src: Path, statvfs=None, min_free_gb: float = 0) -> Path:
    """Copy src to downloads/<rip_id>/ (extras in extras/), never overwriting, then remove src."""
    statvfs = statvfs or STATVFS
    size = src.stat().st_size
    if statvfs is not None:
        st = statvfs(config.work_dir)
        free = st.f_bavail * st.f_frsize
        if free - size < min_free_gb * GIB:
            raise OSError(f"only {free / GIB:.1f} GB free, and the cache needs {min_free_gb:g} GB kept spare")
    dest = config.downloads_dir / str(rip_id) / ("extras" if role == "extra" else "")
    dest.mkdir(parents=True, exist_ok=True)
    part = dest / (src.name + ".part")
    try:
        with src.open("rb") as fin, part.open("wb") as fout:  # an old .part is overwritten
            shutil.copyfileobj(fin, fout, 8 * 1024 * 1024)
            fout.flush()
            os.fsync(fout.fileno())
        for target in _free_name(dest, src.name):
            try:
                os.link(part, target)  # atomic, and fails if the name is taken
            except FileExistsError:
                continue
            except OSError:  # no hard links here: fall back to check-then-rename
                if target.exists():
                    continue
                os.replace(part, target)
            break
        part.unlink(missing_ok=True)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    src.unlink()
    try:
        src.parent.rmdir()
    except OSError:
        pass
    return target
