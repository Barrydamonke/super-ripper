"""Checking a ripped video file with ffprobe, and moving it to downloads."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

GIB = 1024**3
STATVFS = getattr(os, "statvfs", None)  # absent on Windows dev machines: the space check is skipped
CHUNK = 8 * 1024 * 1024


class MoveCancelled(Exception):
    """The copy was stopped by a cancel; src is untouched and nothing is left in downloads."""


@dataclass
class MkvInfo:
    seconds: float
    video: int
    audio: int
    audio_channels: tuple[int, ...] = ()
    sar: tuple[int, int] = (1, 1)
    dar: float | None = None


def _ratio(text) -> tuple[int, int] | None:
    try:
        n, d = (int(x) for x in str(text).split(":"))
    except ValueError:
        return None
    return (n, d) if n > 0 and d > 0 else None


def parse_ffprobe(text: str) -> MkvInfo:
    data = json.loads(text)
    streams = data.get("streams", [])
    video = [s for s in streams if s.get("codec_type") == "video"]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    dar = _ratio(video[0].get("display_aspect_ratio")) if video else None
    return MkvInfo(
        seconds=float(data.get("format", {}).get("duration") or 0),
        video=len(video),
        audio=len(audio),
        audio_channels=tuple(int(s.get("channels") or 0) for s in audio),
        sar=(_ratio(video[0].get("sample_aspect_ratio")) if video else None) or (1, 1),
        dar=dar[0] / dar[1] if dar else None,
    )


def ffprobe(path: Path) -> MkvInfo:
    argv = ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    done = subprocess.run(argv, capture_output=True, text=True, timeout=300)  # noqa: S603 (fixed argv)
    if done.returncode != 0:
        raise RuntimeError(f"ffprobe couldn't read the file: {done.stderr.strip()[-200:] or done.returncode}")
    return parse_ffprobe(done.stdout)


PROBE: Callable[[Path], MkvInfo] = ffprobe  # tests replace this


def check_output(work: Path, expected_seconds: int, expected_audio: int = -1, *, encoded: bool = False) -> Path:
    """The one ripped file in work, if it looks right; RuntimeError says what's wrong.

    expected_audio is how many audio streams the scan listed for the title: a title
    that is silent on the disc (0) may come out silent; unknown (-1) still needs audio.
    encoded=True words the problems for an encoder's output instead of a MakeMKV rip.
    """
    maker, noun, source = ("The encoder", "encoded file", "the source") if encoded else (
        "MakeMKV", "ripped file", "the disc")
    files = sorted(work.glob("*.mkv")) if work.is_dir() else []
    if not files:
        raise RuntimeError(f"{maker} finished but wrote no file.")
    if len(files) > 1:
        raise RuntimeError(f"{maker} wrote more than one file ({len(files)}); expected one.")
    path = files[0]
    info = PROBE(path)
    if info.video < 1:
        raise RuntimeError(f"The {noun} has no video.")
    if info.audio < 1 and expected_audio != 0:
        raise RuntimeError(f"The {noun} has no audio.")
    if expected_seconds and abs(info.seconds - expected_seconds) > max(0.02 * expected_seconds, 5):
        raise RuntimeError(
            f"The {noun}'s length is {info.seconds:.0f}s but {source} says {expected_seconds}s.")
    return path


def _free_name(dest: Path, name: str) -> Iterator[Path]:
    stem, suffix = Path(name).stem, Path(name).suffix
    yield dest / name
    n = 2
    while True:
        yield dest / f"{stem} ({n}){suffix}"
        n += 1


def move_to_downloads(config, rip_id: int, role: str, src: Path, statvfs=None, min_free_gb: float = 0,
                     cancel: threading.Event | None = None, reserved_bytes: int = 0) -> Path:
    """Copy src to downloads/<rip_id>/ (extras in extras/), never overwriting, then remove src.

    The copy needs its size free on the cache pool (statvfs of /work: /data is a FUSE share
    that reports the array) on top of the floor and reserved_bytes, the space other running
    rips were promised by the space check but haven't written yet."""
    statvfs = statvfs or STATVFS
    size = src.stat().st_size
    if statvfs is not None:
        st = statvfs(config.work_dir)
        free = st.f_bavail * st.f_frsize
        if free - reserved_bytes - size < min_free_gb * GIB:
            others = f" ({reserved_bytes / GIB:.1f} GB of it promised to other rips)" if reserved_bytes else ""
            raise OSError(f"only {free / GIB:.1f} GB free{others}, and the cache needs {min_free_gb:g} GB kept spare")
    dest = config.downloads_dir / str(rip_id) / ("extras" if role == "extra" else "")
    dest.mkdir(parents=True, exist_ok=True)
    part = dest / (src.name + ".part")
    try:
        with src.open("rb") as fin, part.open("wb") as fout:  # an old .part is overwritten
            while chunk := fin.read(CHUNK):
                if cancel is not None and cancel.is_set():
                    raise MoveCancelled()
                fout.write(chunk)
            fout.flush()
            os.fsync(fout.fileno())
        if cancel is not None and cancel.is_set():
            raise MoveCancelled()
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
