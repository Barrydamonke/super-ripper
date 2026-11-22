# Step 6: Encoding with three fixed presets (design)

Date: 2026-11-21. Builds on steps 1–4 (job queues, shutdown guard, DVD and Blu-ray
ripping into `/data/downloads/disc-ripper/<rip_id>/`, per-job logs) and the main
design plan (`Disc Ripper – Design Plan.md`: "Encoding and the preset builder",
"Scheduling and the shutdown guard", "Storage layout").

## Intent

Ripped films are lossless MKVs of 4–40 GB. The owner wants them encoded to small,
good-quality files before anything goes into the library, so identification and
import (step 5) were deliberately moved after this step. Today's rips are interim:
the owner may rip the discs again later, so the raw files are not precious.

The main plan's step 6 is split in two (decided with the owner):

- **This step:** the encode pipeline with three presets fixed in code.
- **A later step:** the preset builder page (every option editable, test encodes
  on sample clips, history-based estimates on the page).

## Goals

1. **Every finished title is encoded automatically.** When a `rip_title` job
   finishes and its file is in downloads, an encode of that file is queued with
   the default preset. Extras and TV titles are encoded too.
2. **Three presets** (decided with the owner: approach A below):

   | Preset | Engine | Video | Audio |
   | --- | --- | --- | --- |
   | **Standard** | HandBrakeCLI | x265, preset slow, RF 20 (Blu-ray) / RF 18 (DVD) | main track → E-AC-3 |
   | **Fast** | ffmpeg | `hevc_vaapi` on the UHD 630, constant quality | main track → E-AC-3 |
   | **Copy only** | ffmpeg | copied | copied |
3. **One global encode queue,** separate from the drive queues. One encode runs at
   a time by default, at `nice 19` and `ionice` idle.
4. **The shutdown guard knows about encodes.** A running encode holds `/api/busy`;
   an encode only starts if its estimate finishes before the grace period ends.
5. **The raw rip is deleted 24 hours after a good encode** (decided with the
   owner), and the owner can delete it sooner with **Delete raw now**.
6. **A Films page** lists every ripped DVD and Blu-ray, its files, their encodes
   and buttons to encode, retry, cancel and delete raw. Every rip job and encode
   has a **log** link there (owner's requirement). The dashboard shows the
   encode queue too.

## Non-goals

- **The preset builder page,** sample test encodes, re-ordering the queue, and
  per-preset history shown on a page. All later.
- **Keep lossless / second versions** (step 7). Step 6 always deletes the raw file
  in the end.
- **Identification, renaming after the film, import** (step 5). Encodes keep the
  rip's neutral name plus the preset.
- **QSV** through HandBrake or ffmpeg (Jellyfin crashed on it; experimental in the
  plan).
- **Black-bar cropping.** Off in both engines *(my call; the plan has it off by
  default for ffmpeg, and the builder will add it as an option)*.
- **Encoding audio CDs.** FLAC only, as before.

## Anti-goals

- **Losing a film.** The raw file is only deleted when an encode of it has passed
  its check and the encoded file is still on disk.
- **Slowing Jellyfin.** Encodes always run at the lowest CPU and disk priority, and
  never more than the configured number at once.
- **An encode that blocks shutdown for hours,** or a server that powers off half
  way through one without the encode starting again.
- **Filling the cache** with encode output.

## Constraints

- **Hardware:** i5-8500 (6 cores) with UHD 630. Software x265 slow is roughly
  3–4 hours per 1080p film. VAAPI is the hardware path (Jellyfin moved off QSV
  after kernel crashes).
- **Image:** add Debian's `handbrake-cli`, Intel's VAAPI media driver and
  `vainfo` for the smoke test; `ffmpeg` is already there.
  - The driver is `intel-media-va-driver-non-free`, which needs Debian's
    `non-free` component. The free build is "limited", and HEVC encoding on this
    chip generation needs Intel's closed shader kernels.
  - The build must confirm that Debian's HandBrakeCLI offers `x265_10bit` and
    `eac3`, since distro builds have lacked them.
- **GPU access:** the Unraid template passes `/dev/dri` into the container. The
  entrypoint drops to uid 99 with `setpriv --clear-groups`, which would also drop
  any `--group-add`. So it reads the render node's group itself and keeps it
  (`setpriv --groups <gid>`), and the template needs no group setting.
- **Encodes don't belong to a drive,** but the `jobs` table requires a `drive_id`
  and its workers are per drive. Encodes therefore get their own table and pool
  (see Design).
- **Files:** written as 99:100, umask 0002. Fixed argument lists, never a shell;
  file paths are the only non-fixed arguments and come from the app's own records.
- **No new pip dependencies.**

## Approaches considered (owner chose A)

- **A. HandBrake for Standard, ffmpeg for Fast and Copy only (chosen).** HandBrake
  deinterlaces, detelecines and handles DVDs well out of the box; ffmpeg is the
  reliable route to VAAPI and is already in the image; Debian packages both. Costs
  two command builders and two progress parsers, which the preset builder needs
  anyway.
- **B. ffmpeg for everything (libx265 for Standard).** One engine and a smaller
  image, but the app would have to detect and fix interlacing and telecine itself,
  and getting that wrong ruins DVD encodes.
- **C. HandBrake for everything, QSV for Fast.** HandBrake has no VAAPI on Linux,
  and QSV is the route that crashed Jellyfin. Ruled out.

## Design

### Presets (code, not settings)

- **Standard:** `HandBrakeCLI` with `--encoder x265_10bit` *(my call: 10-bit avoids
  banding at the same size; the UHD 630 and Jellyfin's clients decode it)*,
  `--encoder-preset slow`, `--quality 20` for Blu-ray and `18` for DVD,
  `--crop-mode none`. DVDs also get `--comb-detect --decomb --detelecine`.
  `--json` progress.
- **Fast:** `ffmpeg -vaapi_device /dev/dri/renderD128`, video
  `-vf format=nv12,hwupload` (plus `deinterlace_vaapi` first when the source is
  interlaced) and `-c:v hevc_vaapi -rc_mode CQP -qp <n>` (anamorphic DVDs also get their
  sample aspect ratio written back with `-bsf:v hevc_metadata=sample_aspect_ratio=…`,
  because `hevc_vaapi` is known to lose it, which would squash 16:9 DVDs). `<n>` is the setting
  `fast_quality` (default 24 *(my call, a starting point)*), tuned after the
  owner's first test encode. Interlacing: for DVDs, run ffmpeg's `idet` filter on
  three 20-second stretches (at 20%, 50% and 80% of the film) before encoding;
  interlaced if most analysed frames are TFF/BFF. Blu-rays are treated as
  progressive. `-progress pipe:1`.
- **Copy only:** `ffmpeg -map 0 -c copy`. The rip already holds only the tracks
  step 4's selection kept, so this is a straight remux under the new name.
- **Audio (Standard and Fast):** one audio track: the source's first audio track
  (MakeMKV puts the default first), converted to E-AC-3. 640 kb/s when it has 6 or
  more channels (downmixed to 5.1 if more); otherwise it keeps its channel count at
  224 kb/s *(my calls: the other English tracks on a Blu-ray are the same mix in
  other codecs, and a stereo source shouldn't be faked up to 5.1)*.
- **Subtitles (all presets):** every subtitle track is copied unchanged.
- **Chapters and metadata** are kept.
- Every command is prefixed with `nice -n 19 ionice -c 3`.
- Commands are built by pure functions (`encode_argv(preset, source, output,
  disc_type, interlaced, settings) -> list[str]`), unit-tested exactly, and logged
  in the encode's log.

### Data (migration 0005)

`encodes`, one row per encode attempt:

- `id`, `rip_id` (the rip whose file it is), `title`, `role`, `source` (the raw
  file's path), `disc_type`, `source_seconds`, `source_bytes`;
- `preset` (`standard` / `fast` / `copy`);
- `state` (`queued` / `running` / `done` / `failed` / `cancelled` /
  `interrupted`), `progress`, `message`, `error`, `estimated_finish`;
- `output` (path once moved to downloads), `output_bytes`, `speed` (seconds of
  film encoded per second, recorded when done, for estimates);
- `created_at`, `started_at`, `finished_at`.

The raw file's deletion is recorded on the rip: `video_rips.files[title]` gains
`raw_deleted_at`.

### The encode pool

- An `EncodePool` beside the per-drive `JobRunner`, with the same pieces: one
  worker loop, cancel, stop, a `JobContext`-like context that reuses
  `run_process` (so the encode gets a log, `idle_timeout` and SIGTERM/SIGKILL on
  cancel), progress at most once a second, pushed to the dashboard and Films page.
  The builder may factor shared code out of `runner.py`.
- **Slots:** setting `encode_slots` (1–2, default 1).
- **Order:** oldest queued first.
- **Before starting** an encode, the pool checks, in order:
  1. **Guard:** in the `draining` phase no encode starts. In `in_window`, it starts
     only if `now + estimate <= grace_ends_at`; otherwise it waits with the message
     "Waiting for the next session: needs about 3 h 20 min, the grace period ends
     at 01:00". In `off_hours` (server on outside the schedule) encodes start freely.
     - A rough (history-free) estimate gets a 25% safety margin for this check
       *(my call)*.
     - **Fitting in a session:** the owner's sessions are about 10 hours plus a
       3-hour grace period, and the longest expected encode (Standard, a long
       Blu-ray) is about 4–5 hours, so an encode always fits when started early
       in a session. An encode whose estimate is longer than a whole session plus
       grace is failed at once with "Too long to finish in one session (about N h);
       use Fast, or run it with the server on outside the schedule" *(my call)*.
     - **An estimate that turns out wrong** never blocks shutdown past the grace
       period: once the grace period ends the guard's phase is `off_hours` and
       `/api/busy` reports not busy (existing behaviour), so the server powers
       off, the encode is interrupted and it is re-queued at the next boot.
  2. **Space:** the estimated output size must fit the work budget and the cache's
     free-space floor, using the same `SpaceChecker` (its "running bytes" must
     include running encodes' reserved bytes).
  3. **The source still exists.** If not, the encode fails with "The ripped file is
     no longer there."
- **Logs:** `/config/logs/encode-<id>.log`, linked from the Films page and the
  dashboard queue, at `/encodes/<id>/log`.
- **Stall watchdog** *(my call, the same idea as MakeMKV's)*: HandBrake and ffmpeg
  both print progress constantly, so an encode silent for `encode_stall_minutes`
  (default 10) is stopped and fails with "The encoder stopped responding".

### Running an encode

1. Write into `/work/encode-<id>/<stem> - <Preset>.mkv` (stem = the raw file's
   name without `.mkv`).
2. Progress: HandBrake's `--json` "Progress" fraction / ffmpeg's `out_time_us`
   over `source_seconds`. The message reads "Standard — 42% · finishes ~16:40".
3. **Check:** ffprobe reads it; it has one video stream; it has audio if the source
   had audio; duration within max(2%, 5 s) of the source's (reuse
   `videofiles.check_output`'s logic); display aspect within 2% of the source's
   (catches a squashed DVD).
4. **Move** beside the raw file in `downloads/<rip_id>/` (or `extras/`), with the
   step 4 move: `.part`, fsync, never overwrite, space check, cancel-safe.
5. Record `output`, `output_bytes`, `speed`; state `done`.
6. On failure, delete the work folder; the raw file is untouched.

### Starting encodes

- **Automatically:** setting `encode_after_rip` (choice: `standard` / `fast` /
  `copy` / `off`, default `standard`). After a `rip_title` job's file is recorded
  in downloads, queue one encode with that preset. A `move_failed` file is queued
  once **Move again** succeeds.
- **By hand** on the Films page: **Encode ▾** (pick a preset) per file, and
  **Encode all ▾** per rip. Refused while that file already has a queued or
  running encode, and for a file whose raw has been deleted.
- **Retry** on a failed, cancelled or interrupted encode queues a new attempt.
- **Interrupted encodes** (container stopped mid-encode) are queued again
  automatically at start-up, unlike rips (main plan: "Encodes: re-queued
  automatically at the next boot").

### Estimates

- **Duration** = `source_seconds / speed`, where `speed` is the average of the
  last 5 finished encodes with the same preset and disc type; with no history, a
  conservative default *(my calls)*:

  | Preset | DVD | Blu-ray |
  | --- | --- | --- |
  | Standard | 1.0× | 0.4× |
  | Fast | 6× | 2× |
  | Copy | 30× | 15× |

  A history-free estimate is marked "rough" on the page.
- **Size** for the space check = `source_bytes ×` 0.35 (Standard), 0.5 (Fast),
  1.0 (Copy) *(my calls)*.
- While running, the estimate is replaced by the rate so far, as for rips.

### Raw-file deletion

- A sweep every 10 minutes (and at start-up) deletes a raw file when:
  - an encode of that file is `done`, its `output` still exists, and it finished
    more than `raw_keep_hours` ago (setting, default 24); and
  - no encode of that file is queued or running.

  Any preset's good encode counts, including Copy only (a lossless remux, so
  nothing is lost) and a Fast test *(my call)*. To keep the raw file for another
  encode, queue it within the 24 hours: deletion waits while one is queued or
  running.
- **Delete raw now** on the Films page does the same at once, after a browser
  `confirm()` ("Delete the raw rip? It can't be undone; the disc is the backup.").
  Refused unless the first condition holds.
- Deletion records `raw_deleted_at`, writes a line to the rip's and encode's log,
  and the Films page shows "raw deleted".
- The step 4 "Already ripped" guard keeps working: the downloads folder (with the
  encode in it) still exists.

### Films page (`/films`)

Sketched with the owner:

```
Films
  Encoding now: #22 The Lego Movie_t01 · Standard · 34% · finishes ~16:40  [Cancel] log
  Queued: #23 FM1_t04 · Standard · ~1 h 20 min (rough)                     [Cancel]

  Rip 14 · THE_LEGO_MOVIE · Blu-ray · 21 Nov                    [Encode all ▾] log
    The Lego Movie_t01.mkv   1:40:35   26.7 GB raw   encoding (Standard)
  Rip 15 · FM1 · DVD · 21 Nov                                    [Encode all ▾] log
    FM1_t04.mkv              1:22:13    3.6 GB raw   → FM1_t04 - Standard.mkv 1.1 GB · raw deleted in 23 h [Delete raw now]
    extras/GM2_t00.mkv       0:07:46    0.2 GB raw   [Encode ▾]
```

- Newest rip first; each rip's job logs and each encode's log are linked.
- A failed encode shows its reason with **Retry** and **log**, as on drive cards.
- Live updates over the existing `/events` stream (one `films` event).
- The nav gets "Films". The dashboard gets an "Encoding" panel with the running and
  queued encodes (same lines as the top of the Films page).

### Settings ("Encoding" group)

| Key | Kind | Default |
| --- | --- | --- |
| `encode_after_rip` | choice standard / fast / copy / off | standard |
| `encode_slots` | int 1–2 | 1 |
| `fast_quality` | int 1–51 | 24 |
| `raw_keep_hours` | int 0–720 | 24 |
| `encode_stall_minutes` | int 1–240 | 10 |

### Shutdown guard and `/api/busy`

- `busy_report` includes running encodes (kind `encode`, preset, estimated finish),
  and they hold shutdown like rips do.
- On container stop, running encodes are killed and marked `interrupted`, then
  re-queued at start-up.

## Testing

- **Unit:** argv builders for each preset × disc type × interlaced (exact lists);
  HandBrake JSON and ffmpeg `-progress` parsers against recorded output (synthetic
  first, real captures from the server later); idet result parsing; estimate
  maths; the guard decision; raw-deletion rules; migration 0005.
- **Pool:** with fake `HandBrakeCLI` and `ffmpeg` executables (like the fake
  makemkvcon): success, failure, stall, cancel mid-encode, interrupted then
  re-queued, guard waiting, space waiting, two slots.
- **Web:** Films page lists, Encode / Encode all / Retry / Cancel / Delete raw now
  (refusals included), log routes, dashboard panel, nav.
- **Image:** `HandBrakeCLI --version`, `ffmpeg -encoders | grep hevc_vaapi`,
  `vainfo` (skipped in CI where there's no GPU, run on the server).
- **Manual, on the server:**
  1. Encode The Lego Movie with Standard and Madagascar with Fast; play both.
  2. Check Fast's quality and size, and tune `fast_quality`.
  3. Check Madagascar's Standard encode for combing (deinterlacing).
  4. Delete raw now; let another raw expire after 24 h.
  5. Stop the container mid-encode; the encode restarts at the next boot.
  6. An encode that would run past the grace period waits for the next session.
  7. Capture real HandBrake `--json` and ffmpeg `-progress` output for fixtures.

## Left to the builder

- Module split (e.g. `encoding.py` for presets and parsers, `encodes.py` for the
  table, `encodepool.py`), and how much of `runner.py` to share.
- The exact HandBrake JSON fields and ffmpeg progress keys (confirm on real runs).
- Which Intel driver package works on the UHD 630.
- Page layout beyond the sketch; log wording.
