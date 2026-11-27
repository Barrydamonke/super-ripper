"""Encode presets as data: the settings model, validation and the rules for greying out options,
then the presets table (the database functions at the end)."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, fields

from . import settings

log = logging.getLogger(__name__)

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

# ISO 639-2 terminology (T) codes mapped to the bibliographic (B) code MakeMKV and ffprobe emit.
_LANGUAGE_ALIASES = {
    "fra": "fre", "deu": "ger", "zho": "chi", "nld": "dut", "ces": "cze", "ell": "gre", "fas": "per",
    "ron": "rum", "slk": "slo", "cym": "wel", "isl": "ice", "mkd": "mac", "msa": "may", "bod": "tib",
    "hye": "arm", "kat": "geo", "eus": "baq", "sqi": "alb", "mya": "bur",
}
_LANGUAGE_NAMES = {
    "eng": "English", "jpn": "Japanese", "fre": "French", "ger": "German",
    "spa": "Spanish", "ita": "Italian", "por": "Portuguese", "rus": "Russian",
    "kor": "Korean", "chi": "Chinese", "dut": "Dutch", "swe": "Swedish",
    "nor": "Norwegian", "dan": "Danish", "fin": "Finnish", "pol": "Polish", "hin": "Hindi",
    "cze": "Czech", "gre": "Greek", "per": "Persian", "rum": "Romanian", "slo": "Slovak",
    "wel": "Welsh", "ice": "Icelandic", "mac": "Macedonian", "may": "Malay", "tib": "Tibetan",
    "arm": "Armenian", "geo": "Georgian", "baq": "Basque", "alb": "Albanian", "bur": "Burmese",
    "und": "unlabelled",
}


def canonical_language(code: str) -> str:
    """One code per language: ISO 639-2 T codes (fra) become the B code (fre) the tools emit."""
    code = code.lower()
    return _LANGUAGE_ALIASES.get(code, code)


def _canonical_list(codes) -> list[str]:
    return list(dict.fromkeys(canonical_language(c) for c in codes))


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
                _int(key, v[key], 1 if v["mode"] == "hardware" else 0, 51)
            elif key in ("bitrate_surround", "bitrate_stereo"):
                top = 640 if v["audio_codec"] == "ac3" else 1536
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
        v["audio_languages"] = tuple(_canonical_list(v["audio_languages"]))
        v["subtitle_languages"] = tuple(_canonical_list(v["subtitle_languages"]))
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
    """A short hash of the settings that matter; greyed-out fields are reset to Standard's values first."""
    standard = STANDARD.to_dict()
    payload = {k: (standard[k] if k in disabled_fields(s) else v) for k, v in s.to_dict().items()}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


def language_name(code: str) -> str:
    code = canonical_language(code)
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


def _seed_audio() -> dict:
    return {"audio_codec": "eac3", "audio_channels": "max51", "bitrate_surround": 640,
            "bitrate_stereo": 224, "audio_languages": ["eng"], "audio_tracks": "first_per_language",
            "keep_commentary": False, "subtitles": "all", "subtitle_languages": ["eng"]}


def _standard_seed() -> dict:
    return {"engine": "handbrake", "mode": "software", "codec": "x265_10bit", "quality_dvd": 18,
            "quality_bluray": 20, "speed": "slow", "max_height": None, "deinterlace": "auto",
            "crop": "off", **_seed_audio()}


SEEDS: dict[int, tuple[str, dict]] = {
    1: ("Standard", _standard_seed()),
    2: ("Fast", {**_standard_seed(), "engine": "ffmpeg", "mode": "hardware", "codec": "hevc_vaapi",
                 "quality_dvd": 24, "quality_bluray": 24}),
    3: ("Copy only", {**_standard_seed(), "engine": "ffmpeg", "mode": "copy", "audio_codec": "copy",
                      "audio_tracks": "all"}),
}

STANDARD = PresetSettings.from_dict(SEEDS[1][1])


# -- the preview on the Presets page ----------------------------------------------------------------


@dataclass(frozen=True)
class Example:
    label: str
    disc_type: str
    seconds: int
    bytes: int
    width: int
    height: int
    sar: tuple[int, int]
    dar: float
    streams: tuple


def _streams(*specs) -> tuple:
    from .videofiles import Stream

    counts = {"audio": 0, "subtitle": 0}
    out = []
    for kind, lang, *rest in specs:
        extra = dict(rest[0]) if rest else {}
        out.append(Stream(kind=kind, index=counts[kind], language=lang, **extra))
        counts[kind] += 1
    return tuple(out)


DVD = Example("DVD", "dvd", 6600, 6_000_000_000, 720, 480, (32, 27), 720 * 32 / (480 * 27), _streams(
    ("audio", "eng", {"channels": 6, "codec": "ac3"}), ("audio", "eng", {"channels": 2, "codec": "ac3"}),
    ("audio", "fra", {"channels": 6, "codec": "ac3"}),
    ("audio", "eng", {"channels": 2, "codec": "ac3", "title": "Commentary", "comment": True}),
    ("subtitle", "eng"), ("subtitle", "fra"), ("subtitle", "eng", {"forced": True})))
BLURAY = Example("Blu-ray", "bluray", 7800, 30_000_000_000, 1920, 1080, (1, 1), 1920 / 1080, _streams(
    ("audio", "eng", {"channels": 8, "codec": "truehd"}), ("audio", "eng", {"channels": 6, "codec": "ac3"}),
    ("audio", "jpn", {"channels": 6, "codec": "dts"}),
    ("audio", "eng", {"channels": 2, "codec": "ac3", "title": "Commentary", "comment": True}),
    ("subtitle", "eng"), ("subtitle", "jpn")))
EXAMPLES = (DVD, BLURAY)

_PREVIEW_CROP = {"dvd": (704, 480, 8, 0), "bluray": (1920, 800, 0, 140)}
_CHANNEL_LABEL = {1: "mono", 2: "stereo", 6: "5.1", 7: "6.1", 8: "7.1"}


def _channels(n: int) -> str:
    return _CHANNEL_LABEL.get(n, f"{n}ch")


def estimate_text(conn: sqlite3.Connection, s: PresetSettings, disc_type: str, seconds: float, size: int) -> str:
    """"≈ 6.1 GB, 3 h 40 min (rough)" for a film of this length and size, using the preset's history."""
    from . import encodes, encoding

    speeds, sizes = encodes.history(conn, fingerprint(s), disc_type)
    est = encoding.estimate(s, disc_type, seconds, size, speeds, sizes)
    basis = "rough" if est.rough_time or est.rough_size else f"from {len(speeds)} encodes"
    return f"≈ {est.bytes / 1e9:.1f} GB, {encoding._hours_minutes(est.seconds)} ({basis})"


def _keeps(plan, s: PresetSettings) -> str:
    from . import encoding

    parts = []
    for t in plan.audio:
        who = language_name(t.language) + (" commentary" if encoding._is_commentary(t) else "")
        if s.mode == "copy" or s.audio_codec == "copy":
            what = f"{_channels(t.channels)}, copied"
        else:
            out, kbps = encoding.audio_out(t.channels, s)
            shown = f" {_channels(out)}" if out != t.channels else ""
            what = f"{_channels(t.channels)} → {AUDIO_LABEL[s.audio_codec]}{shown} {kbps} kb/s"
        parts.append(f"audio {t.index + 1} ({who}, {what})")
    audio = ", ".join(parts) or "no audio"
    subs = f"subtitles {', '.join(str(t.index + 1) for t in plan.subtitles)}" if plan.subtitles else "no subtitles"
    return f"{audio}; {subs}"


def preview(conn: sqlite3.Connection, s: PresetSettings, name: str) -> list[dict]:
    """For each example disc: the command this preset would run, what it keeps, notes and an estimate."""
    import shlex
    from pathlib import PurePosixPath

    from . import encoding

    out = []
    for ex in EXAMPLES:
        plan = encoding.pick_tracks(ex.streams, s)
        crop = _PREVIEW_CROP[ex.disc_type] if encoding.needs_cropdetect(s) else None
        argv = encoding.build_argv(
            s, PurePosixPath("/data/downloads/disc-ripper/1/title_t00.mkv"),
            PurePosixPath(f"/work/encode-1/title_t00 - {name}.mkv"), disc_type=ex.disc_type, plan=plan,
            source_height=ex.height, interlaced=False, crop=crop, sar=ex.sar)
        notes = list(plan.notes)
        if encoding.needs_idet(s, ex.disc_type):
            notes.append("deinterlaced if the check finds interlacing")
        if encoding.needs_cropdetect(s):
            notes.append("crop found by cropdetect")
        out.append({"label": ex.label, "command": shlex.join([*encoding.priority_prefix(), *argv]),
                    "keeps": _keeps(plan, s), "notes": notes,
                    "estimate": estimate_text(conn, s, ex.disc_type, ex.seconds, ex.bytes)})
    return out


# -- the presets table ------------------------------------------------------------------------------

UNKNOWN = "Unknown preset."
GONE = "That preset was deleted; choose another with Encode ▾."
INVALID = "Its preset's settings are no longer valid; choose another preset with Encode ▾."


@dataclass(frozen=True)
class Preset:
    id: int
    name: str
    settings: PresetSettings
    fingerprint: str


def _preset(row) -> Preset:
    s = PresetSettings.from_dict(json.loads(row["settings"]))
    return Preset(row["id"], row["name"], s, row["fingerprint"] or fingerprint(s))


def list_presets(conn: sqlite3.Connection) -> list[Preset]:
    found = []
    for r in conn.execute("SELECT * FROM presets ORDER BY id").fetchall():
        try:
            found.append(_preset(r))
        except (ValueError, TypeError, KeyError, AttributeError) as exc:  # PresetError is a ValueError
            log.warning("presets: skipping preset #%s (%s): its settings are not valid: %s", r["id"], r["name"], exc)
    return found


def get_preset(conn: sqlite3.Connection, preset_id: int) -> Preset | None:
    if not 0 < preset_id < 2**63:  # a URL can carry any integer; SQLite can't
        return None
    row = conn.execute("SELECT * FROM presets WHERE id = ?", (preset_id,)).fetchone()
    if row is None:
        return None
    try:
        return _preset(row)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:  # like list_presets: an invalid row is skipped
        log.warning("presets: preset #%s (%s) has settings that are not valid: %s", row["id"], row["name"], exc)
        return None


def exists(conn: sqlite3.Connection, preset_id: int) -> bool:
    """A row with this id is stored (its settings may still be invalid)."""
    return 0 < preset_id < 2**63 and conn.execute("SELECT 1 FROM presets WHERE id = ?", (preset_id,)).fetchone() is not None


def _taken(conn: sqlite3.Connection, name: str, own_id: int | None) -> bool:
    row = conn.execute("SELECT id FROM presets WHERE lower(name) = lower(?)", (name,)).fetchone()
    return row is not None and row["id"] != own_id


_DUPLICATE = PresetError("name", "A preset with that name already exists.")


def create_preset(conn: sqlite3.Connection, name: str, s: PresetSettings, now: str) -> int:
    name = validate_name(name)
    if _taken(conn, name, None):
        raise _DUPLICATE
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO presets (name, settings, fingerprint, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (name, json.dumps(s.to_dict()), fingerprint(s), now, now))
    except sqlite3.IntegrityError:  # the unique index: a concurrent create of the same name
        raise _DUPLICATE from None
    return cur.lastrowid


def update_preset(conn: sqlite3.Connection, preset_id: int, name: str, s: PresetSettings, now: str) -> None:
    name = validate_name(name)
    if _taken(conn, name, preset_id):
        raise _DUPLICATE
    try:
        with conn:
            conn.execute("UPDATE presets SET name = ?, settings = ?, fingerprint = ?, updated_at = ? WHERE id = ?",
                         (name, json.dumps(s.to_dict()), fingerprint(s), now, preset_id))
            # A queued encode shows its preset's current name; running and finished ones keep their snapshot.
            conn.execute("UPDATE encodes SET preset_name = ? WHERE preset_id = ? AND state = 'queued'",
                         (name, preset_id))
    except sqlite3.IntegrityError:
        raise _DUPLICATE from None


def uses(conn: sqlite3.Connection, preset_id: int) -> list[str]:
    """Human phrases for what still uses this preset (empty: nothing does)."""
    key = str(preset_id)
    out = []
    for state in ("queued", "running", "interrupted"):
        if conn.execute("SELECT 1 FROM encodes WHERE preset_id = ? AND state = ?", (preset_id, state)).fetchone():
            out.append(f"{'an' if state == 'interrupted' else 'a'} {state} encode")
    for r in conn.execute("SELECT label, model FROM drives WHERE encode_choice = ? ORDER BY id", (key,)):
        out.append(f"the {r['label'] or r['model']} dropdown")
    if conn.execute("SELECT 1 FROM video_rips WHERE encode_preset = ? AND state = 'ripping'", (key,)).fetchone():
        out.append("a rip in progress")
    if conn.execute("SELECT 1 FROM video_rips WHERE encode_preset = ? AND state = 'move_failed'", (key,)).fetchone():
        out.append("a rip waiting to be moved (Move again or Remove it on the Films page)")
    if default_choice(conn) == key:
        out.append("After ripping")
    return out


def _join(phrases: list[str]) -> str:
    return phrases[0] if len(phrases) == 1 else ", ".join(phrases[:-1]) + " and " + phrases[-1]


def delete_preset(conn: sqlite3.Connection, preset_id: int) -> str | None:
    """The reason it can't be deleted, or None once it is.

    Call with no open transaction (it runs BEGIN IMMEDIATE).
    """
    conn.execute("BEGIN IMMEDIATE")  # the checks and the delete are one step: an encode can't be queued between
    try:
        preset = get_preset(conn, preset_id)
        if preset is None:
            conn.rollback()
            return UNKNOWN
        if conn.execute("SELECT count(*) FROM presets").fetchone()[0] <= 1:
            conn.rollback()
            return "The last preset can't be deleted."
        used = uses(conn, preset_id)
        if used:
            conn.rollback()
            return f"{preset.name} is in use by {_join(used)}; choose another preset there first."
        conn.execute("DELETE FROM presets WHERE id = ?", (preset_id,))  # finished encodes keep their name
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    return None


_DEFAULT_KEY = "internal.encode_default"


def default_choice(conn: sqlite3.Connection) -> str:
    return str(settings.get_internal(conn, _DEFAULT_KEY, "1"))


def parse_choice(conn: sqlite3.Connection, value: str, *, allow_off: bool = True) -> str | None:
    """None when value is `off` (if allowed) or an existing preset id; else the error text."""
    if value == "off" and allow_off:
        return None
    if re.fullmatch(r"[1-9][0-9]{0,9}", value or "") and get_preset(conn, int(value)) is not None:
        return None
    return UNKNOWN


def set_default_choice(conn: sqlite3.Connection, choice: str) -> str | None:
    error = parse_choice(conn, choice)
    if error is None:
        settings.set_internal(conn, _DEFAULT_KEY, choice)
    return error


def choices(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """(value, label) pairs for the dropdowns: every preset, then Off."""
    return [(str(p.id), p.name) for p in list_presets(conn)] + [("off", "Off")]


def for_encode(conn: sqlite3.Connection, row) -> tuple[str, PresetSettings, str] | None:
    """(name, settings, fingerprint) for an encode row: its snapshot, else its live preset, else None."""
    if row["preset_settings"]:
        s = PresetSettings.from_dict(json.loads(row["preset_settings"]))
        return row["preset_name"], s, row["preset_fingerprint"] or fingerprint(s)
    stored = conn.execute("SELECT * FROM presets WHERE id = ?", (row["preset_id"],)).fetchone()         if row["preset_id"] is not None else None
    live = _preset(stored) if stored else None  # raises on invalid settings: the encode fails with that reason
    return (live.name, live.settings, live.fingerprint) if live else None


def fill_fingerprints(conn: sqlite3.Connection) -> None:
    """Compute any fingerprint the migration left NULL (it can't run Python)."""
    with conn:
        for r in conn.execute("SELECT id, settings FROM presets WHERE fingerprint IS NULL").fetchall():
            conn.execute("UPDATE presets SET fingerprint = ? WHERE id = ?",
                         (fingerprint(PresetSettings.from_dict(json.loads(r["settings"]))), r["id"]))
        for r in conn.execute("SELECT id, preset_settings FROM encodes "
                              "WHERE preset_fingerprint IS NULL AND preset_settings IS NOT NULL").fetchall():
            conn.execute("UPDATE encodes SET preset_fingerprint = ? WHERE id = ?",
                         (fingerprint(PresetSettings.from_dict(json.loads(r["preset_settings"]))), r["id"]))
