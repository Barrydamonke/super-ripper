"""Encode presets as data: the settings model, validation and the rules for greying out options.

Pure: no I/O, no database.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, fields

ENGINES = ("handbrake", "ffmpeg")
MODES = ("software", "hardware", "copy")
SOFTWARE_CODECS = ("x265_10bit", "x265", "x264")
HARDWARE_CODECS = ("hevc_vaapi", "h264_vaapi")
SPEEDS = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow")
MAX_HEIGHTS = (None, 1080, 720, 576, 480)
DEINTERLACE = ("auto", "off", "always")
CROP = ("off", "auto")
AUDIO_CODECS = ("eac3", "ac3", "aac", "opus", "copy")
AUDIO_CHANNELS = ("max51", "stereo", "keep")
AUDIO_TRACKS = ("first_per_language", "all")
SUBTITLES = ("all", "forced_only", "none")

CODEC_LABEL = {"x265_10bit": "H.265 10-bit", "x265": "H.265", "x264": "H.264",
               "hevc_vaapi": "H.265 (VAAPI)", "h264_vaapi": "H.264 (VAAPI)"}
AUDIO_LABEL = {"eac3": "E-AC-3", "ac3": "AC-3", "aac": "AAC", "opus": "Opus", "copy": "Copy"}

_VIDEO_FIELDS = ("codec", "quality_dvd", "quality_bluray", "speed", "max_height", "deinterlace", "crop")
_AUDIO_ENCODE_FIELDS = ("audio_codec", "audio_channels", "bitrate_surround", "bitrate_stereo")
_NAME = re.compile(r"[A-Za-z0-9 \-_()+.]{1,40}")
_LANG = re.compile(r"[a-z]{3}")

_LANGUAGE_NAMES = {
    "eng": "English", "jpn": "Japanese", "fra": "French", "fre": "French", "deu": "German",
    "ger": "German", "spa": "Spanish", "ita": "Italian", "por": "Portuguese", "rus": "Russian",
    "kor": "Korean", "zho": "Chinese", "chi": "Chinese", "nld": "Dutch", "swe": "Swedish",
    "nor": "Norwegian", "dan": "Danish", "fin": "Finnish", "pol": "Polish", "hin": "Hindi",
    "und": "unlabelled",
}


class PresetError(ValueError):
    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


@dataclass(frozen=True)
class PresetSettings:
    engine: str
    mode: str
    codec: str
    quality_dvd: int
    quality_bluray: int
    speed: str
    max_height: int | None
    deinterlace: str
    crop: str
    audio_codec: str
    audio_channels: str
    bitrate_surround: int
    bitrate_stereo: int
    audio_languages: tuple[str, ...]
    audio_tracks: str
    keep_commentary: bool
    subtitles: str
    subtitle_languages: tuple[str, ...]

    @classmethod
    def from_dict(cls, d: dict) -> PresetSettings:
        names = [f.name for f in fields(cls)]
        for key in d:
            if key not in names:
                raise PresetError(key, f"Unknown setting {key!r}")
        for key in names:
            if key not in d:
                raise PresetError(key, f"Missing setting {key!r}")
        v = dict(d)
        _choice("engine", v["engine"], ENGINES)
        _choice("mode", v["mode"], MODES)
        if v["mode"] == "copy":
            v["audio_codec"] = "copy"
        _choice("audio_codec", v["audio_codec"], AUDIO_CODECS)
        off = _disabled_fields(v["engine"], v["mode"], v["audio_codec"])
        blocked = _disabled_choices(v["engine"], v["mode"], v["audio_codec"])
        for key in names:
            if key in off:
                continue
            if key in ("quality_dvd", "quality_bluray"):
                _int(key, v[key], 0, 51)
            elif key in ("bitrate_surround", "bitrate_stereo"):
                top = 640 if key == "bitrate_surround" and v["audio_codec"] == "ac3" else 1536
                _int(key, v[key], 64, top)
            elif key == "max_height":
                _choice(key, v[key], MAX_HEIGHTS)
            elif key == "keep_commentary":
                if not isinstance(v[key], bool):
                    raise PresetError(key, "Must be true or false")
            elif key in ("audio_languages", "subtitle_languages"):
                _languages(key, v[key], required=key == "audio_languages" or v["subtitles"] != "none")
            elif key in _OPTIONS:
                _choice(key, v[key], _OPTIONS[key])
            if key in blocked and v[key] in blocked[key]:
                raise PresetError(key, blocked[key][v[key]])
        v["audio_languages"] = tuple(v["audio_languages"])
        v["subtitle_languages"] = tuple(v["subtitle_languages"])
        return cls(**v)

    def to_dict(self) -> dict:
        out = {f.name: getattr(self, f.name) for f in fields(self)}
        out["audio_languages"] = list(self.audio_languages)
        out["subtitle_languages"] = list(self.subtitle_languages)
        return out


_OPTIONS = {"mode": MODES, "codec": SOFTWARE_CODECS + HARDWARE_CODECS, "speed": SPEEDS,
            "deinterlace": DEINTERLACE, "crop": CROP, "audio_channels": AUDIO_CHANNELS,
            "audio_tracks": AUDIO_TRACKS, "subtitles": SUBTITLES}


def _choice(field: str, value: object, options: tuple) -> None:
    if value not in options or isinstance(value, bool):
        raise PresetError(field, f"{value!r} is not an option")


def _int(field: str, value: object, low: int, high: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise PresetError(field, f"Must be a whole number from {low} to {high}")


def _languages(field: str, value: object, *, required: bool) -> None:
    if not isinstance(value, (list, tuple)) or any(not isinstance(c, str) or not _LANG.fullmatch(c) for c in value):
        raise PresetError(field, "Use three-letter language codes such as eng")
    if required and not value:
        raise PresetError(field, "Pick at least one language")


def _disabled_fields(engine: str, mode: str, audio_codec: str) -> dict[str, str]:
    out: dict[str, str] = {}
    if mode == "hardware":
        out["speed"] = "Hardware encoding has no speed setting"
    if mode == "copy":
        for name in _VIDEO_FIELDS:
            out[name] = "Copy mode keeps the video as it is"
        for name in _AUDIO_ENCODE_FIELDS:
            out[name] = "Copy mode keeps the audio as it is"
    elif audio_codec == "copy":
        for name in ("audio_channels", "bitrate_surround", "bitrate_stereo"):
            out[name] = "Copied audio is not re-encoded"
    return out


def _disabled_choices(engine: str, mode: str, audio_codec: str) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    if engine == "handbrake":
        out["mode"] = {m: "Needs ffmpeg" for m in ("hardware", "copy")}
    if mode == "software":
        out["codec"] = {c: "Needs hardware mode" for c in HARDWARE_CODECS}
    elif mode == "hardware":
        out["codec"] = {c: "Needs software mode" for c in SOFTWARE_CODECS}
    if audio_codec in ("eac3", "ac3"):
        out["audio_channels"] = {"keep": f"{AUDIO_LABEL[audio_codec]} stops at 5.1"}
    return out


def disabled_fields(s: PresetSettings) -> dict[str, str]:
    """Fields that do nothing for these settings, with the reason."""
    return _disabled_fields(s.engine, s.mode, s.audio_codec)


def disabled_choices(s: PresetSettings) -> dict[str, dict[str, str]]:
    """Choices that can't be picked for these settings: field -> {value: reason}."""
    return _disabled_choices(s.engine, s.mode, s.audio_codec)


def validate_name(name: str) -> str:
    name = name.strip()
    if not _NAME.fullmatch(name):
        raise PresetError("name", "Use 1 to 40 letters, digits, spaces or - _ ( ) + .")
    return name


def fingerprint(s: PresetSettings) -> str:
    """A short hash of the settings that matter; greyed-out fields don't count."""
    off = disabled_fields(s)
    payload = {k: v for k, v in s.to_dict().items() if k not in off}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:12]


def language_name(code: str) -> str:
    return _LANGUAGE_NAMES.get(code, code)


def summary(s: PresetSettings) -> tuple[str, str]:
    """A video line and an audio line for the presets list."""
    engine = "HandBrake" if s.engine == "handbrake" else "ffmpeg"
    if s.mode == "copy":
        video = f"{engine} · copy, no re-encode"
    else:
        unit = "QP" if s.mode == "hardware" else ("RF" if s.engine == "handbrake" else "CRF")
        video = f"{engine} · {CODEC_LABEL[s.codec]} · {unit} {s.quality_dvd}/{s.quality_bluray}"
        if s.mode == "software":
            video += f" · {s.speed}"
        if s.max_height:
            video += f" · max {s.max_height}p"
    langs = ", ".join(language_name(c) for c in s.audio_languages)
    if s.audio_codec == "copy":
        audio = f"Copy · {langs}"
    else:
        chan = {"max51": "up to 5.1", "stereo": "stereo", "keep": "as is"}[s.audio_channels]
        audio = f"{AUDIO_LABEL[s.audio_codec]} · {chan} · {s.bitrate_surround}/{s.bitrate_stereo} kb/s · {langs}"
    return video, audio


def _split_languages(text: str) -> list[str]:
    return [p for p in re.split(r"[,\s]+", text.lower()) if p]


def from_form(form: Mapping[str, str], base: PresetSettings) -> tuple[str, PresetSettings]:
    """(name, settings) from posted form fields; fields left out (greyed inputs aren't posted) keep base's value."""
    name = validate_name(form.get("name", ""))
    d = base.to_dict()
    for key in d:
        if key not in form or key == "keep_commentary":
            continue
        raw = form[key]
        if key in ("audio_languages", "subtitle_languages"):
            d[key] = _split_languages(raw)
        elif key == "max_height":
            d[key] = None if raw.strip().lower() == "none" else _form_int(key, raw)
        elif key in ("quality_dvd", "quality_bluray", "bitrate_surround", "bitrate_stereo"):
            d[key] = _form_int(key, raw)
        else:
            d[key] = raw
    if "audio_tracks" in form:  # the audio section was posted; an unticked checkbox sends nothing
        d["keep_commentary"] = "keep_commentary" in form
    return name, PresetSettings.from_dict(d)


def _form_int(field: str, raw: str) -> int:
    try:
        return int(raw.strip())
    except ValueError:
        raise PresetError(field, "Must be a whole number") from None


_SEED_AUDIO = {"audio_codec": "eac3", "audio_channels": "max51", "bitrate_surround": 640, "bitrate_stereo": 224,
               "audio_languages": ["eng"], "audio_tracks": "first_per_language", "keep_commentary": False,
               "subtitles": "all", "subtitle_languages": ["eng"]}
_STANDARD_SEED = {"engine": "handbrake", "mode": "software", "codec": "x265_10bit", "quality_dvd": 18,
                  "quality_bluray": 20, "speed": "slow", "max_height": None, "deinterlace": "auto",
                  "crop": "off", **_SEED_AUDIO}

SEEDS: dict[int, tuple[str, dict]] = {
    1: ("Standard", _STANDARD_SEED),
    2: ("Fast", {**_STANDARD_SEED, "engine": "ffmpeg", "mode": "hardware", "codec": "hevc_vaapi",
                 "quality_dvd": 24, "quality_bluray": 24}),
    3: ("Copy only", {**_STANDARD_SEED, "engine": "ffmpeg", "mode": "copy", "audio_codec": "copy",
                      "audio_tracks": "all"}),
}

STANDARD = PresetSettings.from_dict(SEEDS[1][1])
