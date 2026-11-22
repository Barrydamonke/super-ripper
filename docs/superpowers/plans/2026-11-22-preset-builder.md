# Preset Builder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the three presets fixed in code with presets the owner creates and edits on a
Presets page. It offers every option from the main plan, greying of unsupported options, a
command preview and history-based estimates. Rips keep every language and each preset picks its
tracks.

**Architecture:**
- **Pure core:**
  - `presets.py`: the settings model, validation, greying rules, seeds, form parsing and
    examples. It also holds the database functions: the presets table, the default choice and
    the encode snapshots.
  - `encoding.py`: track picking, the command renderer, crop detection and estimates.
- **The switch-over task** (Task 5):
  - Migration 0008 seeds presets with fixed ids 1–3, rebuilds `encodes` and maps every old
    preset name to an id.
  - The encode pool, services, routes and Radarr quality all read presets from the database.
- **Task 6** adds the Presets page.
- **Task 7** adds the image smoke checks and the README.

**Tech Stack:** Python 3.12, SQLite (JSON1), FastAPI/Jinja/htmx (existing), HandBrakeCLI,
ffmpeg.

**Spec:** `docs/superpowers/specs/2026-11-22-preset-builder-design.md`

## Global Constraints

- **No new pip dependencies.** Commands are fixed argv lists, never a shell. Every preset field
  is validated, and there are no free-text encoder options.
- **Lowest priority:** every encode keeps `nice -n 19 ionice -c 3` (`encoding.priority_prefix()`).
- **Preset name:** 1–40 chars of letters, digits, space and `-_()+.`. It is unique ignoring case,
  and leading and trailing spaces are stripped.
- **Seeded ids are fixed:** 1 Standard, 2 Fast, 3 Copy only. Old values map `standard`→`1`,
  `fast`→`2`, `copy`→`3`, `off`→`off`.
- **What a preset choice is:** everywhere one is stored or posted, it is a preset id as a
  decimal string, or `off`. This covers `drives.encode_choice`, `video_rips.encode_preset`,
  `internal.encode_default` and form fields named `preset`.
- **Snapshot at start:** a queued encode uses its preset's current row. A running or finished
  encode uses its snapshot (`preset_settings`, `preset_name`, `preset_fingerprint`).
- **Deletion is refused** while the preset is used by any of these:
  - a queued or running encode
  - a drive's choice
  - a rip in progress
  - the after-rip default

  The last remaining preset can't be deleted.
- **Fingerprint:** the first 12 hex chars of the SHA-256 of the canonical JSON (sorted keys, no
  spaces) of the settings. Greyed-out fields are reset to the Standard seed's values first. It
  never includes the name.
- **New default MakeMKV selection:**
  `-sel:all,+sel:video,+sel:audio,+sel:subtitle,-sel:mvcvideo,=100:all,-10:eng`. The old
  default it replaces is
  `-sel:all,+sel:video,+sel:(eng|nolang),+sel:(audio&single),-sel:(audio&special),+sel:(subtitle&forced),-sel:mvcvideo,=100:all,-10:eng`.
- **Settings removed from the Settings page:** `encode_after_rip` and `fast_quality`.
- **Copy text:**
  - track fallback: "No {Language} audio in this rip; kept {Language}."
  - track check: "The encode's tracks don't match the preset: expected audio {a}; got {b}."
  - deletion refused: "{name} is in use by {what}; choose another preset there first."
  - retry with a deleted preset: "That preset was deleted; choose another with Encode ▾."
- **Tests:** `.venv/Scripts/python -m pytest -q` (Windows) and the whole suite stay green
  after every task.
- **Commits** end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Never
  commit `radarr-check/` or `status.json`.

## Review Focus

1. **Retrying a failed encode whose preset was since deleted:** a clear refusal with the copy
   text, never a 500 (Task 5).
2. **A source with no subtitles, or no audio:** valid argv (`--subtitle none` / `-sn`,
   `--audio none` / `-an`), and the track check passes (Tasks 3 and 5).
3. **A form post that leaves out disabled fields** (browsers don't submit disabled inputs): the
   saved preset keeps its earlier values for them, and saving doesn't fail (Tasks 1 and 6).
4. **Editing a preset while one of its encodes is running:** the running encode keeps its
   snapshot. History for the new fingerprint starts empty, so the estimate is marked rough
   (Task 5).
5. **An old English-only rip with a Japanese-only preset:** the encode keeps the first audio
   track. The finished encode's message carries the fallback note (Task 5).

---

### Task 1: The preset model (`presets.py`, pure part)

**Files:**
- Create: `src/disc_ripper/presets.py`
- Test: `tests/test_presets.py`

**Interfaces:**
- Produces:
  - **Option tuples:**
    - `ENGINES = ("handbrake", "ffmpeg")`
    - `MODES = ("software", "hardware", "copy")`
    - `SOFTWARE_CODECS = ("x265_10bit", "x265", "x264")`
    - `HARDWARE_CODECS = ("hevc_vaapi", "h264_vaapi")`
    - `SPEEDS = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow")`
    - `MAX_HEIGHTS = (None, 1080, 720, 576, 480)`
    - `DEINTERLACE = ("auto", "off", "always")`
    - `CROP = ("off", "auto")`
    - `AUDIO_CODECS = ("eac3", "ac3", "aac", "opus", "copy")`
    - `AUDIO_CHANNELS = ("max51", "stereo", "keep")`
    - `AUDIO_TRACKS = ("first_per_language", "all")`
    - `SUBTITLES = ("all", "forced_only", "none")`
  - **Labels:** `CODEC_LABEL`, with "H.265 10-bit", "H.265", "H.264", "H.265 (VAAPI)" and
    "H.264 (VAAPI)".
  - **`class PresetError(ValueError)`,** with `.field: str` and `.message: str`.
  - **`@dataclass(frozen=True) class PresetSettings`:**
    - `engine, mode, codec: str`
    - `quality_dvd, quality_bluray: int`
    - `speed: str`
    - `max_height: int | None`
    - `deinterlace, crop, audio_codec, audio_channels: str`
    - `bitrate_surround, bitrate_stereo: int`
    - `audio_languages: tuple[str, ...]`
    - `audio_tracks: str`
    - `keep_commentary: bool`
    - `subtitles: str`
    - `subtitle_languages: tuple[str, ...]`
    - Methods: `from_dict(d: dict) -> PresetSettings` (classmethod, raises PresetError) and
      `to_dict() -> dict` (tuples become lists).
  - **Functions:**
    - `validate_name(name: str) -> str`
    - `fingerprint(s: PresetSettings) -> str`
    - `disabled_fields(s) -> dict[str, str]` (field → reason)
    - `disabled_choices(s) -> dict[str, dict[str, str]]` (field → {value: reason})
    - `summary(s) -> tuple[str, str]` (a video line and an audio line)
    - `language_name(code: str) -> str`
    - `from_form(form: Mapping[str, str], base: PresetSettings) -> tuple[str, PresetSettings]`
  - **`SEEDS: dict[int, tuple[str, dict]]`** for ids 1–3, with the spec's values. These exact
    dicts are also written literally into migration 0008 in Task 5.
  - **`STANDARD = PresetSettings.from_dict(SEEDS[1][1])`.**

- [ ] **Step 1: Write the failing tests** in `tests/test_presets.py`:

```python
def test_seeds_round_trip():
    for pid, (name, d) in SEEDS.items():
        assert PresetSettings.from_dict(d).to_dict() == d
    assert SEEDS[1][0] == "Standard" and SEEDS[2][0] == "Fast" and SEEDS[3][0] == "Copy only"
    std = PresetSettings.from_dict(SEEDS[1][1])
    assert (std.engine, std.mode, std.codec, std.speed) == ("handbrake", "software", "x265_10bit", "slow")
    assert (std.quality_dvd, std.quality_bluray, std.crop, std.deinterlace) == (18, 20, "off", "auto")
    assert (std.audio_codec, std.audio_channels, std.bitrate_surround, std.bitrate_stereo) == ("eac3", "max51", 640, 224)
    assert std.audio_languages == ("eng",) and std.audio_tracks == "first_per_language"
    assert std.subtitles == "all" and std.subtitle_languages == ("eng",) and not std.keep_commentary
    fast = PresetSettings.from_dict(SEEDS[2][1])
    assert (fast.engine, fast.mode, fast.codec, fast.quality_dvd, fast.quality_bluray) == ("ffmpeg", "hardware", "hevc_vaapi", 24, 24)
    copy = PresetSettings.from_dict(SEEDS[3][1])
    assert (copy.engine, copy.mode, copy.audio_codec, copy.audio_tracks) == ("ffmpeg", "copy", "copy", "all")

@pytest.mark.parametrize("change,field", [
    ({"engine": "handbrake", "mode": "hardware"}, "mode"),
    ({"engine": "handbrake", "mode": "copy"}, "mode"),
    ({"mode": "software", "codec": "hevc_vaapi"}, "codec"),
    ({"quality_bluray": 52}, "quality_bluray"),
    ({"speed": "placebo"}, "speed"),
    ({"max_height": 900}, "max_height"),
    ({"audio_codec": "eac3", "audio_channels": "keep"}, "audio_channels"),
    ({"audio_codec": "ac3", "bitrate_surround": 768}, "bitrate_surround"),
    ({"bitrate_stereo": 32}, "bitrate_stereo"),
    ({"audio_languages": []}, "audio_languages"),
    ({"audio_languages": ["english"]}, "audio_languages"),
    ({"subtitles": "all", "subtitle_languages": []}, "subtitle_languages"),
    ({"bogus": 1}, "bogus"),
])
def test_bad_combinations_rejected(change, field):
    d = {**SEEDS[1][1], **change}
    with pytest.raises(PresetError) as err:
        PresetSettings.from_dict(d)
    assert err.value.field == field

def test_copy_mode_forces_audio_copy():
    d = {**SEEDS[2][1], "mode": "copy", "audio_codec": "eac3"}
    assert PresetSettings.from_dict(d).audio_codec == "copy"

def test_names():
    assert validate_name("  Anime (JPN)+ ") == "Anime (JPN)+"
    for bad in ("", "a" * 41, "x/y", "a:b"):
        with pytest.raises(PresetError):
            validate_name(bad)

def test_fingerprint_ignores_greyed_fields_and_changes_on_edit():
    copy = PresetSettings.from_dict(SEEDS[3][1])
    other = PresetSettings.from_dict({**SEEDS[3][1], "quality_bluray": 30, "speed": "fast"})
    assert fingerprint(copy) == fingerprint(other) and len(fingerprint(copy)) == 12
    std = PresetSettings.from_dict(SEEDS[1][1])
    assert fingerprint(std) != fingerprint(PresetSettings.from_dict({**SEEDS[1][1], "quality_bluray": 21}))

def test_greying():
    fast = PresetSettings.from_dict(SEEDS[2][1])
    assert "speed" in disabled_fields(fast)
    copy = PresetSettings.from_dict(SEEDS[3][1])
    assert {"codec", "quality_dvd", "quality_bluray", "speed", "max_height", "deinterlace", "crop",
            "audio_codec", "audio_channels", "bitrate_surround", "bitrate_stereo"} <= set(disabled_fields(copy))
    std = PresetSettings.from_dict(SEEDS[1][1])
    assert set(disabled_choices(std)["mode"]) == {"hardware", "copy"}
    assert "keep" in disabled_choices(std)["audio_channels"]
    assert set(disabled_choices(std)["codec"]) == set(HARDWARE_CODECS)

def test_from_form_keeps_omitted_disabled_fields():
    base = PresetSettings.from_dict({**SEEDS[2][1], "quality_dvd": 22})
    form = {"name": "Fast 2", "engine": "ffmpeg", "mode": "hardware", "codec": "h264_vaapi",
            "quality_bluray": "26", "audio_codec": "aac", "audio_channels": "keep",
            "bitrate_surround": "384", "bitrate_stereo": "160", "audio_languages": "eng, jpn",
            "audio_tracks": "all", "subtitles": "forced_only", "subtitle_languages": "eng",
            "deinterlace": "auto", "crop": "auto", "max_height": "720"}
    name, s = from_form(form, base)
    assert name == "Fast 2" and s.codec == "h264_vaapi" and s.quality_dvd == 22 and s.speed == base.speed
    assert s.audio_languages == ("eng", "jpn") and s.max_height == 720 and s.keep_commentary is False

def test_language_name():
    assert language_name("jpn") == "Japanese" and language_name("und") == "unlabelled" and language_name("xho") == "xho"
```

- [ ] **Step 2:** Run `.venv/Scripts/python -m pytest tests/test_presets.py -q`. It should fail:
  the module doesn't exist yet.

- [ ] **Step 3: Implement `presets.py` (pure part).**

  **Ranges:**
  - quality is 0–51 (VAAPI QP 1–51)
  - bitrates are 64–1536 kb/s; for AC-3, the surround bitrate is at most 640
  - a language code is exactly three lowercase letters (`und` allowed)
  - `audio_languages` needs at least one code
  - `subtitle_languages` needs at least one code unless `subtitles == "none"`

  **The greying rules:**
  - Mode:
    - `engine == "handbrake"` disables the `hardware` and `copy` modes.
    - The software codecs are disabled in hardware mode, and the hardware codecs in software
      mode.
    - Hardware mode disables `speed`.
    - Copy mode disables every video field and every audio field except languages, tracks and
      commentary.
  - Audio:
    - `audio_codec == "copy"` disables channels and both bitrates.
    - `keep` is disabled for `eac3` and `ac3`.
  - Selecting a disabled choice raises `PresetError` on that field.
  - Fields that are disabled are not validated, and are kept as given.

  **`from_form`:**
  - It starts from `base.to_dict()` and overlays only the keys present in the form.
    - Languages are split on commas and whitespace and lower-cased.
    - `max_height` "none" means None.
    - The `keep_commentary` checkbox is true when present and false when absent. Absent counts
      only if `audio_tracks` is in the form, i.e. the audio section was posted.
  - The name goes through `validate_name`.

  **`language_name`** covers eng, jpn, fra, fre, deu, ger, spa, ita, por, rus, kor, zho, chi,
  nld, swe, nor, dan, fin, pol, hin and und.

  **Seed dicts:**
  - All three seeds carry every field.
  - Copy only uses Standard's values for its greyed fields. Its other values are:
    - `engine "ffmpeg"`, `mode "copy"`, `codec "x265_10bit"`
    - `audio_codec "copy"`, `audio_tracks "all"`
    - `audio_languages ["eng"]`, `subtitles "all"`, `subtitle_languages ["eng"]`

- [ ] **Step 4:** Run the test file and then the full suite. Both should pass.

- [ ] **Step 5: Commit:** `feat: preset settings model, validation and greying`.

---

### Task 2: Tracks — probing languages and picking tracks

**Files:**
- Modify: `src/disc_ripper/videofiles.py` (`MkvInfo`, `parse_ffprobe`)
- Modify: `src/disc_ripper/encoding.py` (add `TrackPlan`, `pick_tracks` and `audio_out`)
- Test: `tests/test_encoding_tracks.py`

**Interfaces:**
- Consumes: `presets.PresetSettings` and `presets.language_name` (Task 1).
- Produces:
  - **`videofiles.Stream`:** `@dataclass(frozen=True)` with these fields:
    - `kind: str` (`"audio"` or `"subtitle"`)
    - `index: int` (0-based within its kind, in file order)
    - `language: str` (lower-case; `"und"` when missing)
    - `title: str = ""`
    - `comment: bool = False`
    - `forced: bool = False`
    - `channels: int = 0`
    - `codec: str = ""`
  - **`MkvInfo.streams: tuple[Stream, ...] = ()`,** filled by `parse_ffprobe` from `tags.language`,
    `tags.title`, `disposition.comment` and `disposition.forced`.
  - **`encoding.TrackPlan`:** `@dataclass(frozen=True)` with `audio: tuple[Stream, ...]`,
    `subtitles: tuple[Stream, ...]` and `notes: tuple[str, ...] = ()`.
  - **`encoding.pick_tracks(streams: Sequence[Stream], s: PresetSettings) -> TrackPlan`.**
  - **`encoding.audio_out(channels: int, s: PresetSettings) -> tuple[int, int]`:** the output
    channels and kb/s.

- [ ] **Step 1: Write the failing tests.** The helper `A(i, lang, ch=6, title="", comment=False)`
  builds audio Streams, and `S(i, lang, forced=False)` builds subtitle Streams.

```python
def test_parse_ffprobe_streams():  # a JSON with eng 6ch audio, jpn commentary audio (disposition.comment=1), eng forced sub, untagged sub
    info = videofiles.parse_ffprobe(TEXT)
    a = [x for x in info.streams if x.kind == "audio"]; s = [x for x in info.streams if x.kind == "subtitle"]
    assert (a[0].index, a[0].language, a[0].channels) == (0, "eng", 6)
    assert a[1].comment and a[1].language == "jpn"
    assert s[0].forced and s[1].language == "und"

def test_first_per_language_in_list_order():
    st = [A(0, "eng"), A(1, "eng", 2), A(2, "jpn"), A(3, "fra")]
    plan = pick_tracks(st, settings(audio_languages=("jpn", "eng")))
    assert [t.index for t in plan.audio] == [2, 0]

def test_all_tracks_and_untagged_last():
    st = [A(0, "und"), A(1, "eng"), A(2, "eng", 2), A(3, "fra")]
    plan = pick_tracks(st, settings(audio_tracks="all"))
    assert [t.index for t in plan.audio] == [1, 2, 0]

def test_untagged_used_when_nothing_tagged_matches():
    plan = pick_tracks([A(0, "und"), A(1, "fra")], settings())
    assert [t.index for t in plan.audio] == [0] and plan.notes == ()

def test_commentary_dropped_or_kept():
    st = [A(0, "eng"), A(1, "eng", 2, title="Director's Commentary"), A(2, "eng", 2, comment=True)]
    assert [t.index for t in pick_tracks(st, settings(audio_tracks="all")).audio] == [0]
    assert [t.index for t in pick_tracks(st, settings(keep_commentary=True)).audio] == [0, 1, 2]

def test_fallback_keeps_first_track_with_note():
    plan = pick_tracks([A(0, "eng"), A(1, "eng", 2)], settings(audio_languages=("jpn",)))
    assert [t.index for t in plan.audio] == [0]
    assert plan.notes == ("No Japanese audio in this rip; kept English.",)

def test_no_audio_at_all():
    assert pick_tracks([S(0, "eng")], settings()).audio == ()

def test_subtitles():
    st = [S(0, "eng"), S(1, "fra"), S(2, "eng", forced=True), S(3, "und")]
    assert [t.index for t in pick_tracks(st, settings()).subtitles] == [0, 2, 3]
    assert [t.index for t in pick_tracks(st, settings(subtitles="forced_only")).subtitles] == [2]
    assert pick_tracks(st, settings(subtitles="none", subtitle_languages=())).subtitles == ()

@pytest.mark.parametrize("mode,ch,expected", [("max51", 8, (6, 640)), ("max51", 2, (2, 224)), ("max51", 1, (1, 224)),
    ("max51", 0, (2, 224)), ("stereo", 6, (2, 224)), ("keep", 8, (8, 640)), ("keep", 2, (2, 224))])
def test_audio_out(mode, ch, expected):
    assert audio_out(ch, settings(audio_codec="aac", audio_channels=mode)) == expected
```

  Here `settings(**kw)` is `PresetSettings.from_dict({**SEEDS[1][1], **kw})`, with tuples
  converted to lists.

- [ ] **Step 2:** Run it. It should fail.

- [ ] **Step 3: Implement.**

  **Audio picking (`pick_tracks`):**
  - Commentary is `comment` or "commentary" in the title, ignoring case. With
    `keep_commentary` off, commentary is ineligible.
  - With `keep_commentary` on, commentary tracks of listed or untagged languages are appended
    after the normal picks, in file order.
  - **`first_per_language`:** for each listed language in order, the first eligible track
    tagged with it. If nothing was picked, the first eligible untagged track.
  - **`all`:** eligible tracks of listed languages, grouped in list order and then file order,
    followed by eligible untagged tracks.
  - **Fallback:** if the source has audio and nothing was picked, keep audio index 0, with the
    note "No {first listed language} audio in this rip; kept {language_name(track 0)}.".

  **Subtitles:** listed languages plus untagged tracks, in file order. `forced_only` keeps
  forced ones only, and `none` keeps nothing.

  **`audio_out`:**
  - `max51`: 3+ channels → (6, surround); 1–2 channels kept with the stereo bitrate; 0 → (2,
    stereo).
  - `stereo`: (min(max(ch, 1), 2), stereo), with 0 → 2.
  - `keep`: (ch or 2), with the surround bitrate if 3+ channels, else stereo.

- [ ] **Step 4:** Run the tests and the full suite. Both should pass.

- [ ] **Step 5: Commit:** `feat: per-stream languages and preset track picking`.

---

### Task 3: The command renderer, crop detection and the source window

**Files:**
- Modify: `src/disc_ripper/encoding.py`
- Test: `tests/test_encoding_argv.py`

**Interfaces:**
- Consumes: `PresetSettings` and `SEEDS` (Task 1); `TrackPlan`, `Stream` and `audio_out` (Task
  2).
- Produces:
  - `build_argv(s: PresetSettings, source: Path, output: Path, *, disc_type: str, plan: TrackPlan, source_height: int, interlaced: bool = False, crop: tuple[int, int, int, int] | None = None, sar: tuple[int, int] = (1, 1), window: tuple[float, float] | None = None) -> list[str]`.
    It returns the command without the priority prefix.
  - `needs_idet(s, disc_type) -> bool`: ffmpeg, not copy, deinterlace `auto`, and DVD.
  - `forced_interlaced(s) -> bool`: deinterlace `always`.
  - `needs_cropdetect(s) -> bool`: ffmpeg, not copy, and crop `auto`.
  - `cropdetect_argv(source: Path, start_seconds: float, seconds: float = 20) -> list[str]`.
  - `parse_cropdetect(text: str) -> tuple[int, int, int, int] | None`: the last `crop=W:H:X:Y`
    in the text.
  - `pick_crop(found: list[tuple | None], width: int, height: int) -> tuple | None`.
  - `expected_dar(source_dar: float, width: int, height: int, crop: tuple | None) -> float`.
  - `progress_parser(s, source_seconds)`: `HandBrakeProgress()` for HandBrake, else
    `FfmpegProgress(source_seconds)`.

  The old `encode_argv`, `PRESETS` and `PRESET_LABEL` stay until Task 5 deletes them.

- [ ] **Step 1: Write the failing tests.** These are exact lists. `HB` and `FF` are the
  prefixes, `src` and `out` are `str` paths, and the plan helpers come from Task 2.

```python
BD_PLAN = TrackPlan(audio=(A(0, "eng", 6),), subtitles=(S(0, "eng"), S(1, "eng", forced=True)))

def test_standard_bluray_exact():
    assert build_argv(seed(1), SRC, OUT, disc_type="bluray", plan=BD_PLAN, source_height=1080) == [
        *HB, "-i", src, "-o", out, "--format", "av_mkv", "--markers", "--encoder", "x265_10bit",
        "--encoder-preset", "slow", "--quality", "20", "--crop-mode", "none", "--auto-anamorphic",
        "--audio", "1", "--aencoder", "eac3", "--ab", "640", "--mixdown", "5point1",
        "--subtitle", "1,2", "--subtitle-burned=none", "--json"]

def test_standard_dvd_decombs():
    argv = build_argv(seed(1), SRC, OUT, disc_type="dvd", plan=BD_PLAN, source_height=480)
    assert argv[argv.index("--quality") + 1] == "18"
    i = argv.index("--auto-anamorphic")
    assert argv[i + 1:i + 4] == ["--comb-detect", "--decomb", "--detelecine"]

def test_fast_dvd_interlaced_exact():
    assert build_argv(seed(2), SRC, OUT, disc_type="dvd", plan=BD_PLAN, source_height=480, interlaced=True) == [
        *FF, "-hide_banner", "-nostdin", "-y", "-vaapi_device", "/dev/dri/renderD128", "-i", src,
        "-map", "0:v:0", "-map", "0:a:0", "-map", "0:s:0", "-map", "0:s:1", "-map_chapters", "0",
        "-vf", "format=nv12,hwupload,deinterlace_vaapi", "-c:v", "hevc_vaapi", "-rc_mode", "CQP", "-qp", "24",
        "-c:a", "eac3", "-b:a:0", "640k", "-ac:a:0", "6", "-disposition:a:0", "default",
        "-c:s", "copy", "-disposition:s", "-default", "-progress", "pipe:1", "-nostats", out]

def test_fast_anamorphic_sar_and_h264_metadata():
    argv = build_argv(seed(2), SRC, OUT, disc_type="dvd", plan=BD_PLAN, source_height=480, sar=(32, 27))
    assert argv[argv.index("-bsf:v") + 1] == "hevc_metadata=sample_aspect_ratio=32/27"
    h264 = settings_from(2, codec="h264_vaapi")
    argv = build_argv(h264, SRC, OUT, disc_type="dvd", plan=BD_PLAN, source_height=480, sar=(32, 27))
    assert argv[argv.index("-bsf:v") + 1] == "h264_metadata=sample_aspect_ratio=32/27"

def test_copy_exact():
    assert build_argv(seed(3), SRC, OUT, disc_type="bluray", plan=BD_PLAN, source_height=1080) == [
        *FF, "-hide_banner", "-nostdin", "-y", "-i", src, "-map", "0:v:0", "-map", "0:a:0",
        "-map", "0:s:0", "-map", "0:s:1", "-map_chapters", "0", "-c", "copy",
        "-disposition:a:0", "default", "-disposition:s", "-default", "-progress", "pipe:1", "-nostats", out]

def test_two_audio_tracks_second_not_default():
    plan = TrackPlan(audio=(A(2, "jpn", 6), A(0, "eng", 2)), subtitles=())
    hb = build_argv(seed(1), SRC, OUT, disc_type="bluray", plan=plan, source_height=1080)
    assert hb[hb.index("--audio") + 1:hb.index("--audio") + 8] == ["3,1", "--aencoder", "eac3,eac3", "--ab", "640,224", "--mixdown", "5point1,stereo"]
    assert hb[hb.index("--subtitle") + 1] == "none"
    ff = build_argv(seed(2), SRC, OUT, disc_type="bluray", plan=plan, source_height=1080)
    assert ["-map", "0:a:2", "-map", "0:a:0"] == ff[ff.index("0:v:0") + 1:ff.index("0:v:0") + 5]
    assert "-disposition:a:1" in ff and ff[ff.index("-disposition:a:1") + 1] == "0"
    assert "-sn" in ff

def test_no_audio():
    plan = TrackPlan(audio=(), subtitles=())
    assert "none" == (hb := build_argv(seed(1), SRC, OUT, disc_type="bluray", plan=plan, source_height=1080))[hb.index("--audio") + 1]
    assert "-an" in build_argv(seed(2), SRC, OUT, disc_type="bluray", plan=plan, source_height=1080)

def test_audio_copy_and_aac_opus_names():
    hb = build_argv(settings_from(1, audio_codec="copy"), SRC, OUT, disc_type="bluray", plan=BD_PLAN, source_height=1080)
    assert hb[hb.index("--aencoder") + 1] == "copy" and "--ab" not in hb and "--mixdown" not in hb
    assert "av_aac" in build_argv(settings_from(1, audio_codec="aac"), SRC, OUT, disc_type="bluray", plan=BD_PLAN, source_height=1080)
    ff = build_argv(settings_from(2, audio_codec="opus"), SRC, OUT, disc_type="bluray", plan=BD_PLAN, source_height=1080)
    assert ff[ff.index("-c:a") + 1] == "libopus"

def test_software_ffmpeg_filters_in_order():
    s = settings_from(1, engine="ffmpeg", codec="x265_10bit", max_height=720, crop="auto", deinterlace="always", speed="medium", quality_bluray=22)
    argv = build_argv(s, SRC, OUT, disc_type="bluray", plan=BD_PLAN, source_height=1080, interlaced=True, crop=(1920, 800, 0, 140))
    assert argv[argv.index("-vf") + 1] == "bwdif,crop=1920:800:0:140,scale=-2:720"
    i = argv.index("-c:v")
    assert argv[i:i + 9] == ["-c:v", "libx265", "-preset", "medium", "-crf", "22", "-pix_fmt", "yuv420p10le", "-x265-params"]

def test_vaapi_crop_and_scale():
    s = settings_from(2, max_height=720)
    argv = build_argv(s, SRC, OUT, disc_type="bluray", plan=BD_PLAN, source_height=1080, crop=(1920, 800, 0, 140))
    assert argv[argv.index("-vf") + 1] == "crop=1920:800:0:140,format=nv12,hwupload,scale_vaapi=w=-2:h=720"

def test_no_scale_when_source_not_taller():
    argv = build_argv(settings_from(2, max_height=720), SRC, OUT, disc_type="dvd", plan=BD_PLAN, source_height=480)
    assert "scale_vaapi" not in argv[argv.index("-vf") + 1]

def test_handbrake_cap_crop_always_deinterlace():
    argv = build_argv(settings_from(1, max_height=720, crop="auto", deinterlace="always"), SRC, OUT,
                      disc_type="bluray", plan=BD_PLAN, source_height=1080)
    assert argv[argv.index("--crop-mode") + 1] == "auto"
    assert argv[argv.index("--maxHeight") + 1] == "720" and "--decomb" in argv and "--comb-detect" not in argv

def test_window():
    hb = build_argv(seed(1), SRC, OUT, disc_type="bluray", plan=BD_PLAN, source_height=1080, window=(600, 30))
    assert hb[hb.index("--start-at") + 1] == "seconds:600" and hb[hb.index("--stop-at") + 1] == "seconds:30"
    ff = build_argv(seed(2), SRC, OUT, disc_type="bluray", plan=BD_PLAN, source_height=1080, window=(600, 30))
    assert ff[ff.index("-ss") + 1] == "600" and ff[ff.index("-t") + 1] == "30" and ff.index("-ss") < ff.index("-i")

def test_crop_helpers():
    assert parse_cropdetect("[Parsed_cropdetect_0] x1:0 crop=1920:800:0:140\n... crop=1920:804:0:138") == (1920, 804, 0, 138)
    assert pick_crop([(1920, 800, 0, 140)] * 2 + [(1920, 804, 0, 138)], 1920, 1080) == (1920, 800, 0, 140)
    assert pick_crop([(1920, 800, 0, 140), (1920, 1080, 0, 0), None], 1920, 1080) is None
    assert pick_crop([(1920, 1080, 0, 0)] * 3, 1920, 1080) is None
    assert expected_dar(16 / 9, 1920, 1080, (1920, 800, 0, 140)) == pytest.approx(2.4)

def test_detection_rules():
    assert needs_idet(seed(2), "dvd") and not needs_idet(seed(2), "bluray") and not needs_idet(seed(1), "dvd")
    assert needs_cropdetect(settings_from(2, crop="auto")) and not needs_cropdetect(settings_from(1, crop="auto"))
```

  `seed(n)` is `PresetSettings.from_dict(SEEDS[n][1])`. `settings_from(n, **kw)` overlays
  `kw` on seed n.

- [ ] **Step 2:** Run it. It should fail.

- [ ] **Step 3: Implement `build_argv` and the helpers.**

  **Codec names:**

  | Codec | HandBrake | ffmpeg |
  | --- | --- | --- |
  | `x265_10bit` | `x265_10bit` | `libx265` plus `-pix_fmt yuv420p10le` |
  | `x265` | `x265` | `libx265` |
  | `x264` | `x264` | `libx264` |

  - ffmpeg libx265 also gets `-x265-params log-level=error`.
  - ffmpeg software encodes use `-preset <speed> -crf <q>`.
  - Audio encoder names: HandBrake `eac3`, `ac3`, `av_aac`, `opus` and `copy`; ffmpeg `eac3`,
    `ac3`, `aac`, `libopus` and `copy`.
  - HandBrake mixdown names by channel count: 1 mono, 2 stereo, 6 5point1, 7 6point1,
    8 7point1. Any other count is rendered as 6 channels, 5point1.

  **The ffmpeg filter chain** is built in this order:
  1. the software deinterlace (`bwdif`), or VAAPI's `deinterlace_vaapi` placed after
     `hwupload`
  2. `crop=W:H:X:Y`
  3. software `scale=-2:N`, or `scale_vaapi=w=-2:h=N`, added only when `source_height > N`

  For VAAPI, `crop` comes before `format=nv12,hwupload`. If the software chain is empty, there
  is no `-vf`.

  **`pick_crop`:** returns None if any result is None, if any two results differ by more than
  8 px in any component, or if the winner is the full frame. Otherwise it returns the most
  common box, with ties going to the first.

  **cropdetect:** `cropdetect_argv` mirrors `idet_argv`, using `-vf cropdetect=round=2`.

- [ ] **Step 4:** Run the tests and the full suite. Both should pass.

- [ ] **Step 5: Commit:** `feat: build encode commands from preset settings`.

---

### Task 4: Estimates and Radarr quality from settings

**Files:**
- Modify: `src/disc_ripper/encoding.py` (add `Estimate`, `estimate` and the default tables)
- Modify: `src/disc_ripper/filmid.py` (add `quality_for`)
- Test: `tests/test_encoding_estimate.py`, `tests/test_filmid.py`

**Interfaces:**
- Produces:
  - `encoding.Estimate`: `@dataclass(frozen=True)` with `seconds: float`, `bytes: int`,
    `rough_time: bool` and `rough_size: bool`.
  - `encoding.estimate(s: PresetSettings, disc_type: str, source_seconds: float, source_bytes: int, speeds: list[float], sizes: list[float]) -> Estimate`.
    - `speeds` are seconds of film per second.
    - `sizes` are output bytes per second of source.
  - `filmid.quality_for(disc_type: str, s: PresetSettings) -> str`.
  - The old `estimate_seconds`, `estimate_size` and `quality_name` stay until Task 5.

- [ ] **Step 1: Write the failing tests.**

```python
def test_defaults_match_step6_for_seeds():
    e = estimate(seed(1), "bluray", 7200, 25_000_000_000, [], [])
    assert e.seconds == pytest.approx(7200 / 0.4) and e.bytes == int(25e9 * 0.35) and e.rough_time and e.rough_size
    assert estimate(seed(1), "dvd", 7200, 6e9, [], []).seconds == pytest.approx(7200 / 1.0)
    assert estimate(seed(2), "bluray", 7200, 25e9, [], []).seconds == pytest.approx(7200 / 2.0)
    assert estimate(seed(2), "bluray", 7200, 25e9, [], []).bytes == int(25e9 * 0.5)
    assert estimate(seed(3), "dvd", 7200, 6e9, [], []).seconds == pytest.approx(7200 / 30.0)

def test_defaults_move_with_settings():
    base = estimate(seed(1), "bluray", 7200, 25e9, [], [])
    faster = estimate(settings_from(1, speed="medium"), "bluray", 7200, 25e9, [], [])
    smaller = estimate(settings_from(1, quality_bluray=26), "bluray", 7200, 25e9, [], [])
    capped = estimate(settings_from(1, max_height=720), "bluray", 7200, 25e9, [], [])
    assert faster.seconds < base.seconds and smaller.bytes == pytest.approx(base.bytes / 2, rel=0.01)
    assert capped.bytes < base.bytes and capped.seconds < base.seconds

def test_history_wins():
    e = estimate(seed(1), "bluray", 7200, 25e9, [0.5, 0.3], [400_000.0])
    assert e.seconds == pytest.approx(7200 / 0.4) and e.bytes == int(7200 * 400_000) and not e.rough_time and not e.rough_size

def test_quality_for():
    assert quality_for("dvd", seed(1)) == "DVD"
    assert quality_for("bluray", seed(1)) == "Bluray-1080p"
    assert quality_for("bluray", seed(3)) == "Remux-1080p"
    assert quality_for("bluray", settings_from(1, max_height=720)) == "Bluray-720p"
    assert quality_for("bluray", settings_from(1, max_height=576)) == "Bluray-576p"
    assert quality_for("bluray", settings_from(1, max_height=480)) == "Bluray-480p"
```

- [ ] **Step 2:** Run it. It should fail.

- [ ] **Step 3: Implement.** All the default numbers live in one table at the top of the
  estimate code.

  **Speed** (seconds of film per second):

  | Preset type | DVD | Blu-ray |
  | --- | --- | --- |
  | Software, at the `slow` reference | 1.0 | 0.4 |
  | Hardware | 6.0 | 2.0 |
  | Copy | 30.0 | 15.0 |

  - Software speed multipliers by `speed`: ultrafast 6, superfast 5, veryfast 4, faster 3,
    fast 2.2, medium 1.6, slow 1, slower 0.5, veryslow 0.25.
  - Codec multipliers: x264 ×2.5, x265 ×1.1, x265_10bit ×1.
  - A Blu-ray whose cap is ≤720 goes ×2.

  **Size factor** (output bytes ÷ source bytes):

  - Reference factors: software 0.35 at RF 18 (DVD) / RF 20 (Blu-ray); hardware 0.5 at QP 24.
  - Scale by `2 ** ((ref - q) / 6)`, where `ref` is the reference quality above.
  - Then ×1.5 for x264 or h264_vaapi, and ×0.5 for a Blu-ray capped at ≤720. Cap the result
    at 1.0.
  - Copy is 1.0.

  **History:** when there is history, use the mean of `speeds` and the mean of `sizes` ×
  `source_seconds`.

- [ ] **Step 4:** Run the tests and the full suite. Both should pass.

- [ ] **Step 5: Commit:** `feat: preset-aware estimates and Radarr quality`.

---

### Task 5: The switch-over — migration 0008, presets in the database, everything reading them

This is the largest task, because the migration and every reader of `encodes.preset` must
change together for the suite to stay green.

**Files:**
- Create: `src/disc_ripper/migrations/0008_presets.sql`
- Modify:
  - `src/disc_ripper/presets.py`: the database functions.
  - `src/disc_ripper/encodes.py`
  - `src/disc_ripper/encodepool.py`
  - `src/disc_ripper/encoding.py`: delete `encode_argv`, `PRESETS`, `PRESET_LABEL`,
    `DEFAULT_SPEED`, `SIZE_FACTOR`, `estimate_seconds` and `estimate_size`. Change
    `output_name`.
  - `src/disc_ripper/filmid.py`: delete `quality_name`.
  - `src/disc_ripper/filmimport.py`
  - `src/disc_ripper/services.py`
  - `src/disc_ripper/settings.py`: remove `encode_after_rip` and `fast_quality`, and set the
    new `DEFAULT_VIDEO_SELECTION`.
  - `src/disc_ripper/web/dashboard_routes.py`
  - `src/disc_ripper/web/films_routes.py`
  - `src/disc_ripper/templates/_films_list.html`, `_video_card.html` and `_encodes_panel.html`
  - `tests/fixtures/encode/fake_encoder.py`
  - every existing test that used preset names. Update each one to use ids and keep what it
    asserts.
- Test: `tests/test_presets_db.py`, `tests/test_migration_0008.py`, and additions to
  `tests/test_run_encode.py`

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces, in `presets.py`:
  - **`@dataclass(frozen=True) class Preset`:** `id: int`, `name: str`,
    `settings: PresetSettings` and `fingerprint: str`.
  - **Reading presets:**
    - `list_presets(conn) -> list[Preset]`, ordered by id
    - `get_preset(conn, preset_id: int) -> Preset | None`
  - **Writing presets:**
    - `create_preset(conn, name: str, s: PresetSettings, now: str) -> int`
    - `update_preset(conn, preset_id: int, name: str, s: PresetSettings, now: str) -> None`
    - Both raise `PresetError("name", …)` on a duplicate name, ignoring case.
    - `delete_preset(conn, preset_id: int) -> str | None`: the refusal text, or None once
      deleted.
  - **Usage:** `uses(conn, preset_id: int) -> list[str]`: human phrases such as "a queued
    encode", "the Drive 2 dropdown", "a rip in progress" and "After ripping".
  - **Choices:**
    - `default_choice(conn) -> str`: `internal.encode_default`, default `"1"`
    - `set_default_choice(conn, choice: str) -> str | None`: an error message or None
    - `parse_choice(conn, value: str, *, allow_off: bool = True) -> str | None`: None when the
      value is `off` (if allowed) or an existing id, otherwise "Unknown preset."
    - `choices(conn) -> list[tuple[str, str]]`: (value, label) pairs for the existing presets,
      plus `("off", "Off")` last. Used by the dropdowns.
  - **Encode snapshots:**
    - `for_encode(conn, row) -> tuple[str, PresetSettings, str] | None`: (name, settings,
      fingerprint). It reads the snapshot when `row["preset_settings"]` is set, else the live
      preset, else None.
    - `fill_fingerprints(conn) -> None`: computes any NULL `presets.fingerprint` and
      `encodes.preset_fingerprint` from the settings. It runs once at startup, from
      `Services` start, after the migration.
- Produces, in `encodes.py`:
  - **`create(...)`** takes `preset_id: int, preset_name: str` in place of `preset`.
  - **`snapshot(conn, encode_id, name: str, settings_json: str, fingerprint: str) -> None`.**
  - **`history(conn, fingerprint: str, disc_type: str, n: int = 5) -> tuple[list[float], list[float]]`**
    replaces `recent_speeds`. It uses the same state rules for speeds. Sizes are
    `output_bytes / source_seconds` of done rows (`kind = 'film'` only).
  - **`reserved_bytes(conn)`** uses `encoding.estimate` from each running row's snapshot.
  - **`retry(conn, encode_id, now)`** returns None when the preset is gone. The route shows
    "That preset was deleted; choose another with Encode ▾."
- Changed: `encoding.output_name(raw: Path, preset_name: str) -> str` returns
  `f"{raw.stem} - {preset_name}.mkv"`.

**Migration 0008 contents:**
- **`presets`:**
  - columns `id INTEGER PRIMARY KEY`, `name TEXT NOT NULL`, `settings TEXT NOT NULL`,
    `fingerprint TEXT`, `created_at TEXT NOT NULL` and `updated_at TEXT NOT NULL`
  - `CREATE UNIQUE INDEX presets_name ON presets (lower(name))`
  - Rows 1–3 come from `SEEDS`, written literally as JSON.
  - Fast's two qualities are set with `json_set` from
    `COALESCE((SELECT CAST(value AS INTEGER) FROM settings WHERE key = 'fast_quality'), 24)`.
- **`encodes` is rebuilt** as `encodes_new`, copied, the old one dropped and the new one renamed.
  - It keeps every step 6 and 0007 column except `preset`.
  - It adds:
    - `preset_id INTEGER REFERENCES presets(id) ON DELETE SET NULL`
    - `preset_name TEXT NOT NULL DEFAULT ''`
    - `preset_settings TEXT`
    - `preset_fingerprint TEXT`
    - `kind TEXT NOT NULL DEFAULT 'film' CHECK (kind IN ('film','sample'))`
  - Old rows get the mapped id, the seed's name, and the seed's settings JSON as their
    snapshot. The fingerprint stays NULL until `fill_fingerprints` fills it.
  - The three indexes are recreated.
- **Drive and rip choices:** `drives.encode_choice` and `video_rips.encode_preset` are mapped
  by the old-name rule.
- **Settings keys:**
  - `encode_after_rip`, if present, becomes `internal.encode_default`, mapped the same way and
    stored as a JSON string. The old `encode_after_rip` and `fast_quality` keys are then
    deleted.
  - `video_track_selection` is updated to the new default (`json_quote`) only where its value
    equals `json_quote(old default)`.

**Encode pool changes (`_encode`):**
1. Load `presets.for_encode`. If it returns None, fail with "That preset was deleted; choose
   another with Encode ▾."
2. Call `encodes.snapshot` before doing any work.
3. Probe the source. `info.streams` gives the plan (`pick_tracks`), and `plan.notes` are
   written to the log.
4. Work out `interlaced`, from `needs_idet` (via the existing `_detect_interlaced`) or
   `forced_interlaced`.
5. Work out `crop`, from `needs_cropdetect`. Run `cropdetect_argv` at the same three points as
   idet, and pass the results through `pick_crop`.
6. Call `build_argv` with `source_height=info.height`.
7. Choose the parser with `progress_parser`. Run the subtitle fix-up pass when the engine is
   HandBrake.
8. **Checks:** after `check_output`, probe the output.
   - **Aspect check:** compare the output to `expected_dar(info.dar, info.width, info.height,
     crop)`. Skip it for HandBrake with crop `auto`, and write the skip to the log.
   - **Track check:** the output's audio languages (in order) must equal the plan's, and its
     subtitle count must equal the plan's. Otherwise fail with the copy text.
9. **Notes and log:**
   - The finished row's `message` gets `plan.notes` joined with a space, so the Films page can
     show them.
   - The log starts with `preset: <name> <settings JSON>`.

**Elsewhere:**
- **Estimates** everywhere use `encoding.estimate` with `encodes.history`, from the settings
  given by `presets.for_encode`. This covers the guard check, the space check, the queue display
  and `_other_reserved`.
- **Services:**
  - `_encode_preset_for` returns the rip's choice, else `default_choice`.
  - `_queue_encode` takes a choice string. It looks up the preset and stores its id and name.
  - `_set_encode_choice` uses `parse_choice`.
- **Film import:** `filmimport` uses `quality_for(enc["disc_type"], settings)` from
  `presets.for_encode`. Its log line shows `preset_name`.
- **Dropdowns:** the drive card, Films "Encode ▾" and "Encode all" are rendered from
  `presets.choices`. The Films and Encode panels show `preset_name`.

- [ ] **Step 1: Write the failing tests.**

```python
# tests/test_migration_0008.py — build a DB at user_version 7 by running migrations 1..7, insert:
#   settings fast_quality=21, encode_after_rip="fast", video_track_selection=<old default JSON>;
#   a drive with encode_choice='copy'; a video_rips row with encode_preset='standard';
#   encodes rows with preset 'standard' (done, speed 0.4, output_bytes 9e9) and 'fast' (queued).
# then migrate() to 8 and fill_fingerprints().
def test_presets_seeded_with_fast_quality(migrated):
    rows = {p.id: p for p in presets.list_presets(migrated)}
    assert [rows[i].name for i in (1, 2, 3)] == ["Standard", "Fast", "Copy only"]
    assert rows[2].settings.quality_dvd == 21 == rows[2].settings.quality_bluray
    assert all(p.fingerprint == presets.fingerprint(p.settings) for p in rows.values())

def test_old_rows_mapped(migrated):
    enc = migrated.execute("SELECT preset_id, preset_name, preset_settings, preset_fingerprint, kind FROM encodes ORDER BY id").fetchall()
    assert [(r[0], r[1], r[4]) for r in enc] == [(1, "Standard", "film"), (2, "Fast", "film")]
    assert enc[0][3] == presets.fingerprint(presets.PresetSettings.from_dict(json.loads(enc[0][2])))
    assert migrated.execute("SELECT encode_choice FROM drives").fetchone()[0] == "3"
    assert migrated.execute("SELECT encode_preset FROM video_rips").fetchone()[0] == "1"
    assert presets.default_choice(migrated) == "2"
    assert settings.get(migrated, "video_track_selection") == settings.DEFAULT_VIDEO_SELECTION
    assert migrated.execute("SELECT count(*) FROM settings WHERE key IN ('encode_after_rip','fast_quality')").fetchone()[0] == 0

def test_hand_edited_selection_kept(...):  # saved value differs from the old default → unchanged after migrate

# tests/test_presets_db.py
def test_create_update_delete_and_uses(conn):
    pid = presets.create_preset(conn, "Anime", settings_from(1, audio_languages=["jpn", "eng"]), NOW)
    with pytest.raises(PresetError): presets.create_preset(conn, "anime", seed(1), NOW)
    presets.update_preset(conn, pid, "Anime 2", settings_from(1, quality_bluray=22), NOW)
    assert presets.get_preset(conn, pid).name == "Anime 2"
    queue an encode with pid  →  presets.delete_preset(conn, pid) == "Anime 2 is in use by a queued encode; choose another preset there first."
    presets.set_default_choice(conn, "1"); assert "After ripping" in presets.uses(conn, 1)
    finish the encode → delete_preset returns None → the done encode row keeps preset_name "Anime 2", preset_id NULL

def test_last_preset_cannot_be_deleted(conn): ...  # delete 2 and 3 (default set to 1), then deleting 1 is refused

def test_parse_choice_and_choices(conn):
    assert presets.parse_choice(conn, "2") is None and presets.parse_choice(conn, "off") is None
    assert presets.parse_choice(conn, "99") == "Unknown preset." and presets.parse_choice(conn, "off", allow_off=False) == "Unknown preset."
    assert presets.choices(conn)[-1] == ("off", "Off") and presets.choices(conn)[0] == ("1", "Standard")

def test_history_by_fingerprint(conn): # two done encodes: one fingerprint A, one B → history(A) has only A's speed/size

def test_retry_with_deleted_preset_refused(conn): # failed encode, preset deleted → encodes.retry returns None
```

  Additions to `tests/test_run_encode.py`, using the fake encoder:
  - **Stubs and streams:** the source stubs now carry `streams`. Extend `fake_encoder.py` so the
    stub it writes holds the streams its argv mapped (`--audio`/`--subtitle` 1-based lists, or
    `-map 0:a:N` / `-map 0:s:N`). Extend `fake_probe` to read `streams` into `Stream`s.
  - `test_snapshot_written_and_used`: queue an encode with preset 1, edit preset 1's quality to
    22 before it starts, and run it. The recorded argv has `--quality 22`, the row has the
    snapshot, and the log starts with `preset: Standard`.
  - `test_track_check_fails_on_mismatch`: `FAKE_ENCODE_MODE=drop_audio` (a new fake mode that
    writes no audio streams but claims success). The encode fails with "The encode's tracks
    don't match the preset".
  - `test_fallback_note_in_message`: an English-only source with a `["jpn"]` preset. The done
    row's message contains "No Japanese audio in this rip; kept English."
  - `test_vaapi_crop_cap`: a new preset (`h264_vaapi`, `max_height` 720, crop `auto`) on a
    1080 Blu-ray stub. `FAKE_CROP=1920:800:0:140` makes the fake answer cropdetect runs. The
    argv's `-vf` is `crop=1920:800:0:140,format=nv12,hwupload,scale_vaapi=w=-2:h=720`, and the
    aspect check passes when the fake output's DAR is 2.4 (`FAKE_DAR=2.4`).
  - `test_running_encode_keeps_snapshot_after_edit`: edit the preset while the encode is
    running (`FAKE_ENCODE_MODE=hang`, then cancel). The snapshot is unchanged.

  Web tests: `test_web_films.py` dropdowns list "Standard", "Fast", "Copy only" and "Off" with
  values 1, 2, 3 and off. Posting `preset=99` gives "Unknown preset."

- [ ] **Step 2:** Run the new tests. They should fail.

- [ ] **Step 3: Implement** the migration, the database functions and the rewiring above. Then
  update the existing tests from names to ids, keeping what each one asserts.

- [ ] **Step 4:** Run `.venv/Scripts/python -m pytest -q`. Everything should pass.

- [ ] **Step 5: Commit:** `feat: presets in the database; encodes, pool and pages read them`.

---

### Task 6: The Presets page

**Files:**
- Create:
  - `src/disc_ripper/web/presets_routes.py`, registered where the other routers are
  - `src/disc_ripper/templates/presets.html`, `preset_edit.html` and `_preset_preview.html`
- Modify:
  - `src/disc_ripper/templates/base.html`: a nav link "Presets" between Films and Drives.
  - `src/disc_ripper/presets.py`: add `Example`, `EXAMPLES` and `preview(...)`.
- Test: `tests/test_web_presets.py`

**Interfaces:**
- Consumes: Tasks 1–5.
- Produces, in `presets.py`:
  - **`Example`:** `@dataclass(frozen=True)` with `label`, `disc_type`, `seconds`, `bytes`,
    `width`, `height`, `sar`, `dar` and `streams`.
  - **`EXAMPLES = (DVD, BLURAY)`,** with the spec's tracks:
    - **DVD:** 6600 s, 6 GB, 720×480, SAR 32:27. Audio eng ac3 6ch, eng ac3 2ch, fra ac3 6ch
      and eng commentary 2ch. Subtitles eng, fra and eng forced.
    - **Blu-ray:** 7800 s, 30 GB, 1920×1080. Audio eng truehd 8ch, eng ac3 6ch, jpn dts 6ch and
      eng commentary 2ch. Subtitles eng and jpn.
  - **`preview(conn, s: PresetSettings, name: str) -> list[dict]`,** one dict per example, with
    these keys:
    - `label`
    - `command`: `shlex.join` of the priority prefix plus `build_argv`
    - `keeps`: e.g. "audio 1 (English, 5.1 → E-AC-3 640 kb/s); subtitles 1, 3"
    - `notes`: the plan's notes, plus "deinterlaced if the check finds interlacing" when
      `needs_idet`, plus "crop found by cropdetect" when `needs_cropdetect`
    - `estimate`: "≈ 6.1 GB, 3 h 40 min", with "(rough)" or "(from N encodes)"

    For the preview, `crop` is passed as `(1920, 800, 0, 140)` for the Blu-ray and
    `(704, 480, 8, 0)` for the DVD when `needs_cropdetect`. `interlaced` is False. The source
    path is `/data/downloads/disc-ripper/<rip>/title_t00.mkv`, and the output is
    `/work/encode-<id>/title_t00 - <name>.mkv`.
- **Routes:**
  - `GET /presets`: the list. Each card has its summary lines and estimates for a 2-hour film
    (25 GB Blu-ray, 6 GB DVD) using the preset's history, plus Edit, Copy and Delete. At the
    top is an "After ripping" select from `choices`.
  - `POST /presets/default` (`choice`): saves it, or shows the error.
  - `GET /presets/new?from=<id>`: the edit form prefilled from that preset (Standard if no
    `from`), with the name "<name> copy".
  - `GET /presets/{id}`: the edit form.
  - `POST /presets/save`: `id` is optional, plus the form fields. It parses with
    `from_form(form, base)`, where base is the stored preset or the `from` preset. On a
    `PresetError` it re-renders the form with the message beside `field`. On success it
    redirects 303 to `/presets`.
  - `POST /presets/{id}/delete`: shows the refusal text or redirects.
  - `POST /presets/preview`: htmx, `hx-trigger="change"` on the form. It returns the
    re-rendered form fields with `disabled` and reasons from `disabled_fields` /
    `disabled_choices`, plus `_preset_preview.html`. A `PresetError` shows inline instead of
    the commands.
- **Form field names** are the `PresetSettings` field names plus `name` and `id`. Languages
  are typed as text ("eng, jpn"). Greyed fields render as `disabled`, with their reason as small
  text beside them.

- [ ] **Step 1: Write the failing tests** (`tests/test_web_presets.py`, using the existing
  authenticated client fixture):

```python
def test_list_shows_seeds_and_rough_estimates(client): r = client.get("/presets"); assert "Standard" in r.text and "Copy only" in r.text and "(rough)" in r.text
def test_nav_link(client): assert 'href="/presets"' in client.get("/").text
def test_copy_prefills(client): r = client.get("/presets/new?from=2"); assert 'value="Fast copy"' in r.text
def test_save_new_and_edit(client, conn):  # POST /presets/save with Standard fields, name "Anime", audio_languages "jpn, eng" → 303; list shows "Anime"; editing it to quality_bluray 22 keeps the id
def test_save_rejects_duplicate_and_bad_combo(client):  # name "standard" → "already" message; engine handbrake + mode hardware → message beside mode; status 200, form kept
def test_save_keeps_omitted_disabled_fields(client, conn):  # edit Fast posting no speed field → stored speed unchanged
def test_preview_greys_and_shows_command(client):
    r = client.post("/presets/preview", data={**fast_form(), "codec": "h264_vaapi"})
    assert "h264_vaapi" in r.text and "-qp 24" in r.text and "disabled" in r.text and "no speed setting" in r.text
    assert "French" not in r.text.split("Keeps:")[1].split("</")[0]  # Fast keeps English only
def test_delete_refused_in_use(client, conn):  # set After ripping to 2 → POST /presets/2/delete shows "Fast is in use by After ripping"
def test_after_ripping_default(client, conn): client.post("/presets/default", data={"choice": "off"}); assert presets.default_choice(conn) == "off"
```

- [ ] **Step 2:** Run it. It should fail.

- [ ] **Step 3: Implement** the routes and templates, following `films.html` and
  `settings.html` markup and `app.css` classes. Leave a commented placeholder where "Test on a
  rip…" will go.

- [ ] **Step 4:** Run the full suite. Then start the app locally (the README's dev command)
  and open `/presets` to check that greying and the preview update on change.

- [ ] **Step 5: Commit:** `feat: Presets page with greying, command preview and estimates`.

---

### Task 7: Image smoke checks and README

**Files:**
- Modify: `.github/workflows/build.yml` (smoke steps)
- Modify: `README.md`
- Test: CI (local `docker build` if available)

- [ ] **Step 1: Add smoke checks.** They follow the existing style (`setpriv --reuid=99
  --regid=100 --clear-groups sh -c '…'`):
  - `HandBrakeCLI --help` lists `x265_10bit`, `x264`, `eac3`, `ac3`, `av_aac` and `opus`. Each
    is checked with its own `grep -q`, so a failure names the missing one.
  - `ffmpeg -hide_banner -encoders` lists `libx265`, `libx264`, `hevc_vaapi`, `h264_vaapi`,
    `eac3`, `ac3`, `aac` and `libopus`.
  - `ffmpeg -hide_banner -filters` lists `bwdif`, `cropdetect`, `scale_vaapi` and
    `deinterlace_vaapi`.
- [ ] **Step 2: Update the README.**
  - Add a "Presets" section: what each option does, greying, the preview, estimates
    (rough → history), "After ripping", and "deleting is refused while in use".
  - In the step 5 testing section, the preset dropdown values change from names to the
    preset names.
  - Add a "Testing the preset builder on the server" checklist:
    1. Force Update.
    2. Open Presets and check the three seeds. Fast should show your old quality.
    3. Copy Standard as "Anime" with `jpn, eng` audio, and check the preview keeps Japanese
       first.
    4. Rip a disc with a second language. The rip keeps it (check the log's track list).
    5. Encode with the new preset, and check the tracks in Jellyfin.
    6. Try a 720p cap preset, and check that Radarr imports it as Bluray-720p.
    7. Try crop auto on a 2.39:1 film.
    8. Try deleting a preset that's in use.
- [ ] **Step 3: Verify.** If Docker is available, run `docker build -t disc-ripper:local .`,
  then the smoke commands. Otherwise push the branch only when the user asks, and let CI run
  them.
- [ ] **Step 4: Commit:** `ci: smoke-check preset encoders and filters; docs: presets`.
