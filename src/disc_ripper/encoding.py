"""Encode presets and their command lines. Pure: no I/O, nothing runs here."""

from __future__ import annotations

import json
import os
from pathlib import Path

PRESETS = ("standard", "fast", "copy")
PRESET_LABEL = {"standard": "Standard", "fast": "Fast", "copy": "Copy"}
RENDER_NODE = "/dev/dri/renderD128"


def output_name(raw: Path, preset: str) -> str:
    return f"{raw.stem} - {PRESET_LABEL[preset]}.mkv"


def audio_plan(channels: int) -> tuple[int, int]:
    """(output channels, kb/s): surround stays 5.1 at 640, anything smaller keeps its layout at 224."""
    if channels >= 6:
        return 6, 640
    return channels, 224


def _prefix(var: str, default: str) -> list[str]:
    value = os.environ.get(var, "").strip()
    if not value:
        return [default]
    if value.startswith("["):
        return [str(part) for part in json.loads(value)]
    return [value]


def handbrake_prefix() -> list[str]:
    """HandBrakeCLI, or a test stand-in from HANDBRAKE_BIN (a path, or a JSON argv list)."""
    return _prefix("HANDBRAKE_BIN", "HandBrakeCLI")


def ffmpeg_prefix() -> list[str]:
    """ffmpeg, or a test stand-in from FFMPEG_BIN (a path, or a JSON argv list)."""
    return _prefix("FFMPEG_BIN", "ffmpeg")


def priority_prefix() -> list[str]:
    return ["nice", "-n", "19", "ionice", "-c", "3"] if os.name == "posix" else []


def _standard(src: str, out: str, disc_type: str, audio_channels: int | None) -> list[str]:
    dvd = disc_type == "dvd"
    argv = [*handbrake_prefix(), "-i", src, "-o", out, "--format", "av_mkv", "--markers",
            "--encoder", "x265_10bit", "--encoder-preset", "slow",
            "--quality", "18" if dvd else "20", "--crop-mode", "none", "--auto-anamorphic"]
    if dvd:
        argv += ["--comb-detect", "--decomb", "--detelecine"]
    if audio_channels is None:
        argv += ["--audio", "none"]
    else:
        channels, kbps = audio_plan(audio_channels)
        mixdown = {6: "5point1", 2: "stereo", 1: "mono"}.get(channels, "stereo")
        argv += ["--audio", "1", "--aencoder", "eac3", "--ab", str(kbps), "--mixdown", mixdown]
    return argv + ["--all-subtitles", "--subtitle-burned=none", "--json"]


def _fast(src: str, out: str, audio_channels: int | None, interlaced: bool, quality: int,
          sar: tuple[int, int]) -> list[str]:
    argv = [*ffmpeg_prefix(), "-hide_banner", "-nostdin", "-y", "-vaapi_device", RENDER_NODE, "-i", src,
            "-map", "0:v:0"]
    if audio_channels is not None:
        argv += ["-map", "0:a:0?"]
    argv += ["-map", "0:s?", "-map_chapters", "0",
             "-vf", "format=nv12,hwupload" + (",deinterlace_vaapi" if interlaced else ""),
             "-c:v", "hevc_vaapi", "-rc_mode", "CQP", "-qp", str(quality)]
    if audio_channels is None:
        argv += ["-an"]
    else:
        channels, kbps = audio_plan(audio_channels)
        argv += ["-c:a", "eac3", "-b:a", f"{kbps}k", "-ac", str(channels)]
    argv += ["-c:s", "copy"]
    if sar != (1, 1):  # hevc_vaapi can drop the aspect ratio (FFmpeg tickets #8376, #6276)
        argv += ["-bsf:v", f"hevc_metadata=sample_aspect_ratio={sar[0]}/{sar[1]}"]
    return argv + ["-progress", "pipe:1", "-nostats", out]


def encode_argv(preset: str, source: Path, output: Path, *, disc_type: str, audio_channels: int | None,
                interlaced: bool, fast_quality: int, sar: tuple[int, int] = (1, 1)) -> list[str]:
    """The command for a preset, without the priority prefix."""
    src, out = str(source), str(output)
    if preset == "standard":
        return _standard(src, out, disc_type, audio_channels)
    if preset == "fast":
        return _fast(src, out, audio_channels, interlaced, fast_quality, sar)
    if preset == "copy":
        return [*ffmpeg_prefix(), "-hide_banner", "-nostdin", "-y", "-i", src, "-map", "0", "-c", "copy",
                "-progress", "pipe:1", "-nostats", out]
    raise ValueError(f"unknown preset {preset!r}")


def idet_argv(source: Path, start_seconds: float, seconds: float = 20) -> list[str]:
    return [*ffmpeg_prefix(), "-hide_banner", "-nostdin", "-ss", f"{start_seconds:.0f}", "-t", f"{seconds:.0f}",
            "-i", str(source), "-map", "0:v:0", "-vf", "idet", "-f", "null", "-"]
