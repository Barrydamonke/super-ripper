"""Encode presets and their command lines. Pure: no I/O, nothing runs here."""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .presets import PresetSettings, canonical_language, language_name
from .schedule import GuardState
from .videofiles import Stream

RENDER_NODE = "/dev/dri/renderD128"


def output_name(raw: Path, preset_name: str) -> str:
    return f"{raw.stem} - {preset_name}.mkv"


def audio_plan(channels: int) -> tuple[int, int]:
    """(output channels, kb/s): mono stays mono, stereo or unknown is stereo, 3+ channels become 5.1 at 640."""
    if channels < 1:
        return 2, 224
    if channels <= 2:
        return channels, 224
    return 6, 640


def audio_out(channels: int, s: PresetSettings) -> tuple[int, int]:
    """(output channels, kb/s) for one audio track under the preset's channel rule."""
    if s.audio_channels == "stereo":
        out = 2 if channels < 1 else min(channels, 2)
    elif s.audio_channels == "keep":
        out = {3: 6, 4: 6, 5: 6, 7: 8}.get(channels, channels or 2)
    else:  # max51
        out = 2 if channels < 1 else min(channels, 6) if channels <= 2 else 6
    return out, s.bitrate_surround if out >= 3 else s.bitrate_stereo


@dataclass(frozen=True)
class TrackPlan:
    audio: tuple[Stream, ...]
    subtitles: tuple[Stream, ...]
    notes: tuple[str, ...] = ()


def _is_commentary(t: Stream) -> bool:
    return t.comment or "commentary" in t.title.lower()


def _lang(t: Stream) -> str:
    return canonical_language(t.language)


def pick_tracks(streams: Sequence[Stream], s: PresetSettings) -> TrackPlan:
    """Which audio and subtitle tracks the preset keeps, in output order."""
    audio = [t for t in streams if t.kind == "audio"]
    langs = s.audio_languages
    normal = [t for t in audio if not _is_commentary(t)]
    picked: list[Stream] = []
    if s.audio_tracks == "all":
        for lang in langs:
            picked += [t for t in normal if _lang(t) == lang]
        picked += [t for t in normal if _lang(t) == "und"]
    else:
        for lang in langs:
            first = next((t for t in normal if _lang(t) == lang), None)
            if first:
                picked.append(first)
        if not picked:
            first = next((t for t in normal if _lang(t) == "und"), None)
            if first:
                picked.append(first)
    if s.keep_commentary:
        picked += [t for t in audio if _is_commentary(t) and (_lang(t) in langs or _lang(t) == "und")]
    notes: tuple[str, ...] = ()
    if audio and not picked:
        first_listed = language_name(langs[0]) if langs else "listed-language"
        picked = [audio[0]]
        notes = (f"No {first_listed} audio in this rip; kept {language_name(audio[0].language)}.",)
    subs = [t for t in streams if t.kind == "subtitle" and (_lang(t) in s.subtitle_languages or _lang(t) == "und")]
    if s.subtitles == "none":
        subs = []
    elif s.subtitles == "forced_only":
        subs = [t for t in subs if t.forced]
    return TrackPlan(tuple(picked), tuple(subs), notes)


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


_HB_AUDIO = {"eac3": "eac3", "ac3": "ac3", "aac": "av_aac", "opus": "opus", "copy": "copy"}
_FF_AUDIO = {"eac3": "eac3", "ac3": "ac3", "aac": "aac", "opus": "libopus", "copy": "copy"}
_HB_MIXDOWN = {1: "mono", 2: "stereo", 6: "5point1", 7: "6point1", 8: "7point1"}
_FF_SOFT = {"x265_10bit": "libx265", "x265": "libx265", "x264": "libx264"}


def _hb_argv(s: PresetSettings, src: str, out: str, disc_type: str, plan: TrackPlan,
             window: tuple[float, float] | None) -> list[str]:
    dvd = disc_type == "dvd"
    quality = s.quality_dvd if dvd else s.quality_bluray
    argv = [*handbrake_prefix(), "-i", src, "-o", out, "--format", "av_mkv", "--markers",
            "--encoder", s.codec, "--encoder-preset", s.speed, "--quality", str(quality),
            "--crop-mode", "auto" if s.crop == "auto" else "none", "--auto-anamorphic"]
    if s.deinterlace == "always":
        argv += ["--decomb"]
    elif s.deinterlace == "auto" and dvd:
        argv += ["--comb-detect", "--decomb", "--detelecine"]
    if s.max_height:
        argv += ["--maxHeight", str(s.max_height)]
    if not plan.audio:
        argv += ["--audio", "none"]
    else:
        argv += ["--audio", ",".join(str(t.index + 1) for t in plan.audio),
                 "--aencoder", ",".join([_HB_AUDIO[s.audio_codec]] * len(plan.audio))]
        if s.audio_codec != "copy":
            outs = [audio_out(t.channels, s) for t in plan.audio]
            argv += ["--ab", ",".join(str(k) for _, k in outs),
                     "--mixdown", ",".join(_HB_MIXDOWN.get(c, "5point1") for c, _ in outs)]
    argv += ["--subtitle", ",".join(str(t.index + 1) for t in plan.subtitles) if plan.subtitles else "none",
             "--subtitle-burned=none"]
    if window:
        argv += ["--start-at", f"seconds:{window[0]:g}", "--stop-at", f"seconds:{window[1]:g}"]
    return argv + ["--json"]


def _ff_video(s: PresetSettings, disc_type: str, source_height: int, interlaced: bool,
              crop: tuple[int, int, int, int] | None) -> list[str]:
    vaapi = s.mode == "hardware"
    quality = s.quality_dvd if disc_type == "dvd" else s.quality_bluray
    deint = s.deinterlace == "always" or (s.deinterlace == "auto" and interlaced)
    cap = s.max_height if s.max_height and source_height > s.max_height else None
    cropf = f"crop={crop[0]}:{crop[1]}:{crop[2]}:{crop[3]}" if crop else None
    if vaapi:
        chain = [cropf, "format=nv12,hwupload", "deinterlace_vaapi" if deint else None,
                 f"scale_vaapi=w=-2:h={cap}" if cap else None]
    else:
        chain = ["bwdif" if deint else None, cropf, f"scale=-2:{cap}" if cap else None]
    vf = ",".join(c for c in chain if c)
    argv = ["-vf", vf] if vf else []
    if vaapi:
        return argv + ["-c:v", s.codec, "-rc_mode", "CQP", "-qp", str(quality)]
    argv += ["-c:v", _FF_SOFT[s.codec], "-preset", s.speed, "-crf", str(quality)]
    if s.codec == "x265_10bit":
        argv += ["-pix_fmt", "yuv420p10le"]
    if s.codec != "x264":
        argv += ["-x265-params", "log-level=error"]
    return argv


def _ff_argv(s: PresetSettings, src: str, out: str, disc_type: str, plan: TrackPlan, source_height: int,
             interlaced: bool, crop: tuple[int, int, int, int] | None, sar: tuple[int, int],
             window: tuple[float, float] | None) -> list[str]:
    copy = s.mode == "copy"
    vaapi = s.mode == "hardware"
    argv = [*ffmpeg_prefix(), "-hide_banner", "-nostdin", "-y"]
    if vaapi:
        argv += ["-vaapi_device", RENDER_NODE]
    if window:
        argv += ["-ss", f"{window[0]:g}", "-t", f"{window[1]:g}"]
    argv += ["-i", src, "-map", "0:v:0"]
    for t in plan.audio:
        argv += ["-map", f"0:a:{t.index}"]
    for t in plan.subtitles:
        argv += ["-map", f"0:s:{t.index}"]
    argv += ["-map_chapters", "0"]
    argv += ["-c", "copy"] if copy else _ff_video(s, disc_type, source_height, interlaced, crop)
    if not plan.audio:
        argv += ["-an"]
    elif not copy:
        argv += ["-c:a", _FF_AUDIO[s.audio_codec]]
        if s.audio_codec != "copy":
            for i, t in enumerate(plan.audio):
                channels, kbps = audio_out(t.channels, s)
                argv += [f"-b:a:{i}", f"{kbps}k", f"-ac:a:{i}", str(channels)]
    for i in range(len(plan.audio)):
        argv += [f"-disposition:a:{i}", "default" if i == 0 else "0"]
    if not plan.subtitles:
        argv += ["-sn"]
    else:
        if not copy:
            argv += ["-c:s", "copy"]
        argv += ["-disposition:s", "-default"]  # keep "forced", drop "default"
    if vaapi and sar != (1, 1):  # VAAPI encoders can drop the aspect ratio (FFmpeg tickets #8376, #6276)
        kind = "h264" if s.codec == "h264_vaapi" else "hevc"
        argv += ["-bsf:v", f"{kind}_metadata=sample_aspect_ratio={sar[0]}/{sar[1]}"]
    return argv + ["-progress", "pipe:1", "-nostats", out]


def build_argv(s: PresetSettings, source: Path, output: Path, *, disc_type: str, plan: TrackPlan,
               source_height: int, interlaced: bool = False, crop: tuple[int, int, int, int] | None = None,
               sar: tuple[int, int] = (1, 1), window: tuple[float, float] | None = None) -> list[str]:
    """The command for a preset's settings, without the priority prefix."""
    src, out = str(source), str(output)
    if s.engine == "handbrake":
        return _hb_argv(s, src, out, disc_type, plan, window)
    return _ff_argv(s, src, out, disc_type, plan, source_height, interlaced, crop, sar, window)


def needs_idet(s: PresetSettings, disc_type: str) -> bool:
    return s.engine == "ffmpeg" and s.mode != "copy" and s.deinterlace == "auto" and disc_type == "dvd"


def forced_interlaced(s: PresetSettings) -> bool:
    return s.deinterlace == "always"


def needs_cropdetect(s: PresetSettings) -> bool:
    return s.engine == "ffmpeg" and s.mode != "copy" and s.crop == "auto"


def cropdetect_argv(source: Path, start_seconds: float, seconds: float = 20) -> list[str]:
    return [*ffmpeg_prefix(), "-hide_banner", "-nostdin", "-ss", f"{start_seconds:.0f}", "-t", f"{seconds:.0f}",
            "-i", str(source), "-map", "0:v:0", "-vf", "cropdetect=round=2", "-f", "null", "-"]


_CROP = re.compile(r"crop=(\d+):(\d+):(\d+):(\d+)")


def parse_cropdetect(text: str) -> tuple[int, int, int, int] | None:
    found = _CROP.findall(text)
    return tuple(int(n) for n in found[-1]) if found else None  # type: ignore[return-value]


def pick_crop(found: list[tuple | None], width: int, height: int) -> tuple | None:
    """The box the samples agree on (within 8 px), or None when they disagree or it is the full frame."""
    if not found or any(f is None for f in found):
        return None
    for a in found:
        for b in found:
            if any(abs(x - y) > 8 for x, y in zip(a, b)):
                return None
    winner = Counter(found).most_common(1)[0][0]
    return None if winner == (width, height, 0, 0) else winner


def expected_dar(source_dar: float, width: int, height: int, crop: tuple | None) -> float:
    if not crop:
        return source_dar
    return source_dar * (crop[0] / crop[1]) / (width / height)


def progress_parser(s: PresetSettings, source_seconds: float):
    return HandBrakeProgress() if s.engine == "handbrake" else FfmpegProgress(source_seconds)


def clear_default_subtitles_argv(source: Path, output: Path) -> list[str]:
    """A quick copy of a HandBrake encode that clears the subtitle "default" flag and keeps "forced",
    so players don't switch a subtitle track on (and burn it in) by themselves."""
    return [*ffmpeg_prefix(), "-hide_banner", "-nostdin", "-y", "-i", str(source), "-map", "0", "-c", "copy",
            "-disposition:s", "-default", "-f", "matroska", str(output)]


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


# Defaults used until there is history. Speed is seconds of film per second.
BASE_SPEED = {"software": (1.0, 0.4), "hardware": (6.0, 2.0), "copy": (30.0, 15.0)}  # (DVD, Blu-ray)
SPEED_FACTOR = {"ultrafast": 6, "superfast": 5, "veryfast": 4, "faster": 3, "fast": 2.2,
                "medium": 1.6, "slow": 1, "slower": 0.5, "veryslow": 0.25}
CODEC_SPEED = {"x264": 2.5, "x265": 1.1, "x265_10bit": 1.0}
# Output bytes / source bytes at the reference quality: (factor, DVD quality, Blu-ray quality).
BASE_SIZE = {"software": (0.35, 18, 20), "hardware": (0.5, 24, 24)}
H264_SIZE = 1.5
SMALL_CAP_HEIGHT = 720
SMALL_CAP_SPEED = 2.0
SMALL_CAP_SIZE = 0.5


@dataclass(frozen=True)
class Estimate:
    seconds: float
    bytes: int
    rough_time: bool
    rough_size: bool


def _small_cap(s: PresetSettings, disc_type: str) -> bool:
    return disc_type != "dvd" and s.max_height is not None and s.max_height <= SMALL_CAP_HEIGHT


def _default_speed(s: PresetSettings, disc_type: str) -> float:
    dvd, bluray = BASE_SPEED[s.mode]
    speed = dvd if disc_type == "dvd" else bluray
    if s.mode == "software":
        speed *= SPEED_FACTOR[s.speed] * CODEC_SPEED[s.codec]
    if s.mode != "copy" and _small_cap(s, disc_type):
        speed *= SMALL_CAP_SPEED
    return speed


def _default_size_factor(s: PresetSettings, disc_type: str) -> float:
    if s.mode == "copy":
        return 1.0
    factor, ref_dvd, ref_bluray = BASE_SIZE[s.mode]
    ref, q = (ref_dvd, s.quality_dvd) if disc_type == "dvd" else (ref_bluray, s.quality_bluray)
    factor *= 2 ** ((ref - q) / 6)
    if s.codec in ("x264", "h264_vaapi"):
        factor *= H264_SIZE
    if _small_cap(s, disc_type):
        factor *= SMALL_CAP_SIZE
    return min(factor, 1.0)


def estimate(s: PresetSettings, disc_type: str, source_seconds: float, source_bytes: int,
             speeds: list[float], sizes: list[float]) -> Estimate:
    """Time and size for one encode: from history when there is any, else from the settings."""
    speed = sum(speeds) / len(speeds) if speeds else _default_speed(s, disc_type)
    size = (int(source_seconds * sum(sizes) / len(sizes)) if sizes
            else int(source_bytes * _default_size_factor(s, disc_type)))
    return Estimate(source_seconds / speed, size, not speeds, not sizes)


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
