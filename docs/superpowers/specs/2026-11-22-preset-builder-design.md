# Preset builder (design)

Date: 2026-11-22. Builds on step 6 (encoding with three presets fixed in code), step 5 (film import
into Radarr) and the main design plan (`Disc Ripper – Design Plan.md`: "Encoding and the preset
builder"). This is the second half of the main plan's step 6, which was split in two.

## Intent

The owner wants to set the quality and audio of encodes themselves. Different films call for
different settings, and they want to change those settings in the app rather than having new
presets written into the code. Today the three presets (Standard, Fast, Copy only) are fixed in
`encoding.py`, and only Fast's quality number can be changed, in Settings.

## Goals

1. **Presets are data.** They live in the database. The owner can create, copy, rename, edit
   and delete them on a new **Presets** page, linked in the nav between Films and Drives.
2. **Every option from the main plan.** Each preset sets:
   - engine and mode, or copy-only
   - codec
   - quality, separately for DVD and Blu-ray
   - encoder speed
   - resolution cap
   - deinterlacing
   - crop detection
   - audio codec, bitrates and channels
   - which audio and subtitle languages to keep
   - whether commentary is kept
3. **Options an engine can't do are greyed out.** The page shows the exact command the preset
   runs on an example DVD and an example Blu-ray. The page also shows which tracks of those
   examples would be kept.
4. **The page shows estimates per preset:** an estimated file size and encode time. They come
   from this preset's past encodes, split by DVD and Blu-ray. With no history the estimate is a
   rough one.
5. **The three current presets become the starting presets,** with today's values. Fast takes
   the current `fast_quality` setting. Their commands match step 6's for the same input, apart
   from the explicit track mapping that languages need.
6. **The rip keeps every language** (decided with the owner: option 1). MakeMKV's default
   track selection keeps all audio and subtitle tracks, commentary included, and each preset
   decides what goes into the encode.
7. **Per-film choice keeps working.** These all list the owner's presets by name, plus Off:
   - the drive card's encode dropdown
   - **Encode ▾** on the Films page
   - the "after ripping" default

## Non-goals

- **Test encodes on samples:** three 30-second clips, extrapolated, and watchable to judge
  quality. This is the main plan's "Test on a sample". The owner wants it as the next step,
  so leave room for it (see "Room for sample tests").
- **HandBrake QSV and ffmpeg QSV.** Jellyfin crashed on QSV, and the main plan marks it
  experimental. The hardware path stays VAAPI.
- **File-size targets and two-pass encoding,** as the main plan says.
- **Free-text encoder options,** such as extra x265 parameters. Commands are fixed argument
  lists built from validated fields. *(my call: keeps the no-shell guarantee and greying
  meaningful)*
- **"Original language" as a language choice.** The film's original language isn't known
  when the encode starts, so a preset names its languages explicitly. A Japanese film gets a
  preset that keeps `jpn`. *(my call: could be added once step 5 identifies the film before
  the encode starts)*
- **Re-encoding past encodes** when a preset changes. Edits apply to encodes that start after
  the edit.
- **Keep lossless and second versions** (step 7), and TV.

## Anti-goals

- **A preset that silently produces a film with no sound.** If no audio track matches the
  preset's languages, the first audio track is kept and the encode's log says so.
- **An edit that breaks the queue.** Deleting a preset that a queued or running encode, a
  drive's choice or the after-rip default uses is refused, with a message saying what uses it.
- **Losing what an old encode was made with.** Each encode keeps a snapshot of the preset
  settings it used. The Films page and its log show the preset's name even after the preset is
  renamed or deleted.
- **Estimates built from someone else's settings.** History only counts encodes made with the
  preset's current settings.
- **Everything in step 6's anti-goals still holds:**
  - no lost films
  - lowest CPU and IO priority
  - the shutdown guard respected
  - output not written to the cache

## Constraints

- **Hardware and image as in step 6.** i5-8500 with UHD 630; VAAPI through
  `intel-media-va-driver-non-free`; Debian's `handbrake-cli` and `ffmpeg`.
- **Encoders in the image.** The image build must confirm every encoder the page offers:
  - HandBrake: `x265_10bit`, `x265`, `x264`, `eac3`, `ac3`, `av_aac`, `opus`
  - ffmpeg: `libx265`, `libx264`, `hevc_vaapi`, `h264_vaapi`, `eac3`, `ac3`, `aac`, `libopus`

  It also needs the filters `bwdif`, `cropdetect`, `scale_vaapi` and `deinterlace_vaapi`. This
  is a smoke check in CI, like step 6's.
- **No shell, no new pip dependencies,** and files written as 99:100 — all as before.
- **Settings already saved.** Changing a setting's default doesn't change a value the owner
  already saved (see Migration).

## Approaches considered (owner chose B)

- **A. Editable presets limited to today's three engine paths.** You'd set quality, x265
  speed, audio and languages. It's the smallest option, but has no free engine or codec choice,
  no resolution cap, no crop, and no estimates.
- **B. The main plan's builder without sample tests (chosen).** It has every option, greying,
  the command preview, and history-based estimates.
- **C. B plus test encodes on samples.** This is the most useful for tuning quality, and the
  biggest. It is noted as the next step.

The owner also considered just adding more presets in code, and preferred being able to
change presets themselves.

## Design

### Preset fields

A preset is one row. Its settings are one JSON object, validated by a pure
`PresetSettings.from_dict` that rejects unknown keys and bad combinations.

| Field | Values | Notes |
| --- | --- | --- |
| `name` | 1–40 chars: letters, digits, space, `-_()+.` | Unique, ignoring case. Used in output file names. |
| `engine` | `handbrake`, `ffmpeg` | |
| `mode` | `software`, `hardware`, `copy` | `hardware` needs `ffmpeg`. `copy` needs `ffmpeg` and greys out every video option and the audio codec. |
| `codec` | `x265_10bit`, `x265`, `x264` (software); `hevc_vaapi`, `h264_vaapi` (hardware) | Labels: "H.265 10-bit", "H.265", "H.264", "H.265 (VAAPI)", "H.264 (VAAPI)". In ffmpeg, `x265_10bit` is `libx265 -pix_fmt yuv420p10le`. |
| `quality_dvd`, `quality_bluray` | 0–51 | HandBrake RF, ffmpeg CRF or VAAPI QP. The label changes with the engine and mode. |
| `speed` | `ultrafast` … `veryslow` | Software only: HandBrake `--encoder-preset`, ffmpeg `-preset`. |
| `max_height` | `none`, `1080`, `720`, `576`, `480` | Scales down only, keeping the aspect ratio. HandBrake `--maxHeight`; ffmpeg `scale=-2:'min(ih,N)'` (software) or `scale_vaapi` (hardware). |
| `deinterlace` | `auto`, `off`, `always` | `auto`: DVDs only, as in step 6. HandBrake uses `--comb-detect --decomb --detelecine`. ffmpeg checks the DVD with `idet` first, then deinterlaces if it's interlaced. `always`: HandBrake `--decomb`, ffmpeg `bwdif` or `deinterlace_vaapi`. |
| `crop` | `off`, `auto` | HandBrake `--crop-mode auto` or `none`. ffmpeg runs `cropdetect` on the same three stretches as `idet`; the result goes in a software `crop=` filter before `hwupload`. The most common box wins, and the film is left uncropped if the stretches disagree by more than 8 px. |
| `audio_codec` | `eac3`, `ac3`, `aac`, `opus`, `copy` | `copy` passes the track through untouched. |
| `audio_channels` | `max51`, `stereo`, `keep` | `keep` is allowed for `aac`, `opus` and `copy` only (E-AC-3 and AC-3 stop at 5.1). Mono stays mono. |
| `bitrate_surround`, `bitrate_stereo` | 64–1536 kb/s | Used for 3+ channel and ≤2 channel outputs. Greyed for `copy`. |
| `audio_languages` | list of ISO 639-2 codes, e.g. `["eng", "jpn"]` | In this order. |
| `audio_tracks` | `first_per_language`, `all` | `first_per_language`: the first matching track of each language, in file order (MakeMKV puts the best first). |
| `keep_commentary` | bool | |
| `subtitles` | `all`, `forced_only`, `none` | Within `subtitle_languages`. |
| `subtitle_languages` | list of codes | |

**Track rules,** in a pure function `pick_tracks(streams, settings) -> TrackPlan`:

- **Untagged tracks** (`und` or no language) count as matching any language list.
- **Commentary** is a track with the `comment` disposition, or "commentary" in its title,
  ignoring case.
- **No audio track matches:** the first audio track is kept, and a note goes in the plan and the
  log.
- **Default flag:** the first kept audio track gets it. Subtitles never get it, but they keep
  "forced", as step 6 does.
- **Explicit track indices:** the commands use the indices the plan picks. HandBrake gets
  `--audio 1,3` with per-track `--aencoder`, `--ab` and `--mixdown` lists, and `--subtitle
  1,2`. ffmpeg gets `-map 0:a:N` and `-map 0:s:N`.
- **Probing:** `videofiles.parse_ffprobe` gains per-stream language, title, disposition and
  channels for audio and subtitles.

### Commands

`encoding.encode_argv(settings, source, output, *, disc_type, plan, interlaced, crop, sar) ->
list[str]` replaces the per-name builders. The step 6 details stay:

- priority prefix
- HandBrake `--json` with the subtitle default-flag fix-up pass afterwards
- ffmpeg `-progress pipe:1`
- `hevc_metadata` sample aspect ratio for anamorphic VAAPI encodes (and `h264_metadata` for
  `h264_vaapi`)
- chapters and metadata kept

Unit tests pin the exact argv for:

- the three seeded presets on DVD and on Blu-ray (matching step 6's lists, apart from the
  explicit track maps)
- each option on its own

- **Copy mode still picks tracks.** It maps the plan's tracks explicitly (`-map 0:v:0 -map
  0:a:N … -c copy`) instead of step 6's `-map 0`, because rips now carry every language.
- **Track check.** After the encode, the output's audio and subtitle languages and counts
  (read with ffprobe) must match the plan. Otherwise the encode fails with "The encode's
  tracks don't match the preset".
  - This catches HandBrake numbering tracks differently from ffprobe.
  - *(my call: HandBrake numbers MKV tracks in container order, which should match ffprobe's
    order within each track type. The check makes a mismatch loud instead of silent, and a
    fake-encoder test covers it.)*

### Data (migration 0008)

**`presets`:** `id`, `name`, `settings` (JSON), `fingerprint`, `created_at`, `updated_at`.

- The fingerprint is a short hash of the settings without the name, and changes on any real
  edit.
- **Seeded presets:** fixed ids 1 Standard, 2 Fast and 3 Copy only, so the old names map
  without guesswork. The migration contains the seeded settings JSON itself and never imports
  app code, so it can't drift when the code changes. The seeds reproduce step 6 exactly,
  because step 6 decombs DVDs only, never crops, and uses one QP for both disc types:
  - **Standard:**
    - HandBrake, software, `x265_10bit`, `slow`
    - RF 18 DVD / RF 20 Blu-ray
    - no resolution cap, deinterlace `auto`, crop `off`
    - E-AC-3 `max51`, 640/224 kb/s
  - **Fast:**
    - ffmpeg, hardware, `hevc_vaapi`
    - QP from `fast_quality` if it was saved, else 24, for both disc types
    - no resolution cap, deinterlace `auto`, crop `off`
    - E-AC-3 `max51`, 640/224 kb/s
  - **Copy only:**
    - ffmpeg, copy mode
    - audio `copy`, `audio_tracks: all`

**`encodes` is rebuilt** to drop the `CHECK (preset IN …)` constraint, keeping its data and
indexes. It gains these columns:

- `preset_id`: nullable once the preset is deleted.
- `preset_name`: the name at queue time, updated when the encode starts.
- `preset_settings`: the snapshot, written when the encode starts.
- `preset_fingerprint`: also written when the encode starts.

Existing rows map `standard`, `fast` and `copy` to the seeded presets, with their snapshots.
The old `preset` column goes.

- **Editing takes effect at start:** queued encodes use the preset as it is when they start
  *(my call: edits while tuning should apply to what's waiting)*.
- **`drives.encode_choice` and `video_rips.encode_preset`** hold a preset id as text, or
  `off`. Old values are mapped.
- **The after-rip default** moves from the `encode_after_rip` setting to a "After ripping"
  control at the top of the Presets page: a preset, or Off. It is stored as internal setting
  `internal.encode_default`, and the Settings entry is removed. `fast_quality` is removed from
  Settings too; its value moved into Fast.
- **The MakeMKV track selection:**
  - The default changes to keep all audio and subtitles:
    `-sel:all,+sel:video,+sel:audio,+sel:subtitle,-sel:mvcvideo,=100:all,-10:eng`. The builder
    checks the syntax against MakeMKV's documented selection grammar. *(my call: no capture of
    a scan with this string exists yet; the owner's first rip confirms it)*
  - The "old default" the migration compares against is step 6's exact
    `DEFAULT_VIDEO_SELECTION`:
    `-sel:all,+sel:video,+sel:(eng|nolang),+sel:(audio&single),-sel:(audio&special),+sel:(subtitle&forced),-sel:mvcvideo,=100:all,-10:eng`.
    If the saved value equals it, the migration updates it. A hand-edited value is left alone.
- **Rips made before this step** are English-only. A preset asking for another language on
  one of them hits the "no match, keep the first track" rule. The note goes in the log and in
  the finished encode's message on the Films page ("No Japanese audio in this rip; kept
  English"). *(my call)*
- **Seeded languages:** the seeded presets get `audio_languages ["eng"]`,
  `first_per_language`, no commentary, subtitles `all` in `["eng"]`. Encodes made with them
  therefore come out as today even though rips now carry more.

### The Presets page

```
Presets                                After ripping: [Standard ▾]   [New preset]

Standard          HandBrake · H.265 10-bit · RF 18 DVD / 20 Blu-ray · slow
                  Audio: E-AC-3 up to 5.1 · English · subtitles: English
                  2 h Blu-ray ≈ 6.1 GB, 3 h 40 min  (from 4 encodes)
                  DVD         ≈ 1.4 GB, 1 h 10 min  (rough)
                  [Edit] [Copy] [Delete]
Fast              ffmpeg VAAPI · H.265 · QP 24 ...
Copy only         ...
```

**Edit page** (`/presets/<id>`, and `/presets/new?from=<id>` for Copy):

- **Sections:** Video, Audio, Subtitles.
- **Greying:** options the engine and mode don't support are disabled, with a short reason
  ("Hardware encoding has no speed setting").
- **Live updates:** each change posts the form with htmx to `/presets/preview`. The server
  validates, re-renders the greying, and returns two previews.
- **Previews,** for:
  - **an example DVD:** 1 h 50 min, 720×480 anamorphic 16:9. Tracks: English AC-3 5.1,
    English AC-3 stereo, French AC-3 5.1, English commentary, English and French subtitles,
    and English forced subtitles.
  - **an example Blu-ray:** 2 h 10 min, 1080p. Tracks: English TrueHD 7.1, English AC-3 5.1,
    Japanese DTS-HD 5.1, English commentary, English and Japanese subtitles.
- **What each preview shows:**
  - the exact command
  - "Keeps: audio 1 (English, 5.1 → E-AC-3 640 kb/s), subtitles 1, 3"
  - the estimate
- **Same path as real encodes:** one renderer is shared with the real encode, so the preview
  can't drift from what runs.
- **Saving:** validation errors show beside the field.

**Rules for names and deletion:**

- **Renaming** doesn't rename existing encodes or their files.
- **Deleting** is refused while the preset is in use (see Anti-goals).
- **At least one preset** must remain.

### Estimates

`encoding.estimate(preset, disc_type, source_seconds, source_bytes, history)` gives
`(seconds, bytes, rough)`.

- **History:** the last 5 done encodes with this preset's fingerprint and disc type give the mean
  speed (seconds of film per second, already recorded) and MB per minute of source. This is the
  `output_bytes / source_seconds` ratio, so a new column isn't needed.
- **No history:** a rough default from a table in code, keyed by mode, codec and speed and by
  disc type (speed), and by codec, quality and disc type (MB per minute). It starts from step 6's
  numbers, and the table goes in one place so it can be tuned. Rough estimates keep the 25%
  margin in the shutdown guard.
- **Where it's used:**
  - the shutdown guard
  - the space check (reserved bytes)
  - the queued encode's display
  - the Presets page, using a 2-hour film at a typical size for each disc type: 25 GB
    Blu-ray, 6 GB DVD
- The estimate replaces step 6's `DEFAULT_SPEED` and `SIZE_FACTOR` tables.
  `encodes.recent_speeds` and `reserved_bytes` are rewritten to work from the fingerprint.
- **Which settings an estimate uses:**
  - A queued encode uses its preset's current row. Deleting a preset is refused while an
    encode with it is queued.
  - A running or finished encode uses its snapshot.

### Everything else that reads the preset

- **Output name:** `<stem> - <preset name>.mkv`. The preset's name is stored on the encode, so
  a later rename has no effect.
- **Radarr quality** (`filmid.quality_name`) reads the snapshot:
  - DVD → `DVD`
  - Blu-ray copy mode → `Remux-1080p`
  - Blu-ray with `max_height` 720 → `Bluray-720p`
  - Blu-ray with `max_height` 576 or 480 → `Bluray-576p` or `Bluray-480p` (both exist in
    Radarr 7)
  - otherwise `Bluray-1080p`
- **Progress parser:** chosen by engine.
- **Log:** the encode's log starts with the preset's name and its settings snapshot.

## Room for sample tests (the next step)

- **Source window:** `encode_argv` takes an optional `(start_seconds, duration)` window.
  HandBrake gets `--start-at seconds:N --stop-at seconds:D`; ffmpeg gets `-ss`/`-t`. It's built
  and unit-tested now but unused, so a sample test is three normal encodes of a window.
- **Test results:** the `encodes` table gains `kind` (`film` default, `sample` later) in
  migration 0008. History ignores `sample` rows until the sample step defines how they count.
- **Page space:** each preset card has space for a "Test on a rip…" button. It is not shown
  in this step.

## Testing

- **Unit tests:**
  - `PresetSettings` validation, every bad combination
  - `pick_tracks` (each rule above)
  - exact argv per option
  - the seeded presets against step 6's argv
  - estimates with and without history
  - fingerprint changes on edit and not on rename
  - quality names
- **Migration test:** a step 6 database with encodes on all three presets, a saved
  `fast_quality`, `encode_after_rip = fast`, a drive choice and the old track selection. It
  upgrades to the right presets, mappings and settings.
- **Web tests:**
  - create, copy, edit, rename and delete (refused while in use)
  - the preview returns greyed fields and the right command
  - the drive and Films dropdowns list presets
- **Encode pool:** an encode with a new preset (VAAPI H.264, 720p cap, crop auto) runs through
  the fake encoders, using the snapshot. Editing the preset after queueing changes what runs.
- **Image smoke check:** the encoders and filters listed under Constraints.

## Left to the builder

- **Code layout:** module split (e.g. `presets.py` for the table and validation, with the
  renderer staying in `encoding.py`).
- **Page markup and styling,** following the Films and Settings pages.
- **Exact wording** of greyed-out reasons and messages.
- **Default-estimate table numbers,** within reason, marked rough.
