"""Encode presets and their command lines. Pure: no I/O, nothing runs here."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from .schedule import GuardState

PRESETS = ("standard", "fast", "copy")
PRESET_LABEL = {"standard": "Standard", "fast": "Fast", "copy": "Copy"}
RENDER_NODE = "/dev/dri/renderD128"


def output_name(raw: Path, preset: str) -> str:
    return f"{raw.stem} - {PRESET_LABEL[preset]}.mkv"


def audio_plan(channels: int) -> tuple[int, int]:
    """(output channels, kb/s): mono stays mono, stereo or unknown is stereo, 3+ channels become 5.1 at 640."""
    if channels < 1:
        return 2, 224
    if channels <= 2:
        return channels, 224
    return 6, 640


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
        mixdown = {6: "5point1", 2: "stereo", 1: "mono"}[channels]
        argv += ["--audio", "1", "--aencoder", "eac3", "--ab", str(kbps), "--mixdown", mixdown]
    # No subtitle track is flagged default, so players don't switch one on (and burn it in) by themselves.
    return argv + ["--all-subtitles", "--subtitle-burned=none", "--subtitle-default=none", "--json"]


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
    argv += ["-c:s", "copy", "-disposition:s", "-default"]  # keep "forced", drop "default"
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
                "-disposition:s", "-default", "-progress", "pipe:1", "-nostats", out]
    raise ValueError(f"unknown preset {preset!r}")


def idet_argv(source: Path, start_seconds: float, seconds: float = 20) -> list[str]:
    return [*ffmpeg_prefix(), "-hide_banner", "-nostdin", "-ss", f"{start_seconds:.0f}", "-t", f"{seconds:.0f}",
            "-i", str(source), "-map", "0:v:0", "-vf", "idet", "-f", "null", "-"]


class HandBrakeProgress:
    """Encode progress (0..1) from HandBrake's --json blocks; scan blocks don't count.

    A block's "State" normally precedes its "Progress", but either order works.
    """

    _PROGRESS = re.compile(r'^\s*"Progress"\s*:\s*([0-9.eE+-]+)')
    _STATE = re.compile(r'^\s*"State"\s*:\s*"(\w+)"')

    def __init__(self) -> None:
        self._state: str | None = None
        self._pending: float | None = None
        self._last: float | None = None

    def feed(self, line: str) -> float | None:
        if line.startswith("Progress:"):
            self._state, self._pending = None, None
            return None
        state = self._STATE.match(line)
        if state:
            self._state = state.group(1)
            if self._state == "WORKDONE":
                self._last = 1.0
                return 1.0
            pending, self._pending = self._pending, None
            return self._accept(pending) if self._state == "WORKING" else None
        match = self._PROGRESS.match(line)
        if match:
            value = float(match.group(1))
            if self._state == "WORKING":
                return self._accept(value)
            if self._state is None:
                self._pending = value
        return None

    def _accept(self, value: float | None) -> float | None:
        if value is None:
            return None
        value = min(max(value, 0.0), 1.0)
        if value == self._last:
            return None
        self._last = value
        return value


class FfmpegProgress:
    """Progress (0..1) from ffmpeg -progress key=value lines, tolerant of erratic times."""

    def __init__(self, source_seconds: float) -> None:
        self.source_seconds = source_seconds
        self._best = 0.0

    def feed(self, line: str) -> float | None:
        line = line.strip()
        if line == "progress=end":
            self._best = 1.0
            return 1.0
        if not line.startswith("out_time_us=") or self.source_seconds <= 0:
            return None
        try:
            micro = int(line.split("=", 1)[1])
        except ValueError:  # N/A
            return None
        if micro < 0:
            return None
        value = min(micro / 1e6 / self.source_seconds, 1.0)
        if value > self._best + 0.10:  # subtitle timestamps can make the time jump
            value = self._best + 0.01
        if value <= self._best:
            return None
        self._best = value
        return value


_FFMPEG_NOISE = ("frame=", "fps=", "stream_", "bitrate=", "total_size=", "out_time", "dup_frames=",
                 "drop_frames=", "speed=", "progress=continue")


class ProgressNoise:
    """True for progress chatter that would flood a log. Tracks HandBrake's JSON blocks by brace depth."""

    def __init__(self) -> None:
        self._depth = 0

    def __call__(self, line: str) -> bool:
        text = line.strip()
        if self._depth == 0:
            if text.startswith("Progress:") and text.endswith("{"):
                self._depth = 1
                return True
            return text.startswith(_FFMPEG_NOISE)
        self._depth += text.count("{") - text.count("}")
        self._depth = max(self._depth, 0)
        return True


def is_progress_noise(line: str) -> bool:
    """Stateless check for ffmpeg progress keys only; use ProgressNoise for HandBrake blocks."""
    return line.strip().startswith(_FFMPEG_NOISE)


_IDET = re.compile(r"Multi frame detection:\s*TFF:\s*(\d+)\s*BFF:\s*(\d+)\s*Progressive:\s*(\d+)")


def parse_idet(text: str) -> bool:
    """Interlaced when the last idet summary has more TFF+BFF frames than progressive."""
    found = _IDET.findall(text)
    if not found:
        return False
    tff, bff, progressive = (int(n) for n in found[-1])
    return tff + bff > progressive


# Seconds of film encoded per second: (DVD, Blu-ray).
DEFAULT_SPEED = {"standard": (1.0, 0.4), "fast": (6.0, 2.0), "copy": (30.0, 15.0)}
SIZE_FACTOR = {"standard": 0.35, "fast": 0.5, "copy": 1.0}


def estimate_seconds(source_seconds: float, preset: str, disc_type: str,
                     recent: list[float]) -> tuple[float, bool]:
    """(seconds, rough): from the mean recent speed when there is history, else the default table."""
    if recent:
        speed = sum(recent) / len(recent)
    else:
        dvd_speed, bluray_speed = DEFAULT_SPEED[preset]
        speed = dvd_speed if disc_type == "dvd" else bluray_speed
    return source_seconds / speed, not recent


def estimate_size(source_bytes: int, preset: str) -> int:
    return int(source_bytes * SIZE_FACTOR[preset])


def _hours_minutes(seconds: float) -> str:
    minutes = int(round(seconds / 60))
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours} h {minutes} min"
    return f"{hours} h" if hours else f"{minutes} min"


def guard_decision(guard: GuardState, now: datetime, estimate_s: float, rough: bool) -> tuple[str, str]:
    """("start" | "wait" | "too_long", message) for a job against the shutdown schedule."""
    if guard.phase == "draining":
        return "wait", "Waiting for the next session: the server is shutting down"
    if guard.phase != "in_window" or guard.grace_ends_at is None:
        return "start", ""
    if rough:
        estimate_s *= 1.25
    if guard.window_start is not None:
        session = (guard.grace_ends_at - guard.window_start).total_seconds()
        if estimate_s > session:
            hours = int(round(estimate_s / 3600))
            return "too_long", (f"Too long to finish in one session (about {hours} h); use Fast, "
                                "or run it with the server on outside the schedule")
    if now + timedelta(seconds=estimate_s) <= guard.grace_ends_at:
        return "start", ""
    ends = guard.grace_ends_at.astimezone(now.tzinfo).strftime("%H:%M")
    return "wait", (f"Waiting for the next session: needs about {_hours_minutes(estimate_s)}, "
                    f"the grace period ends at {ends}")
