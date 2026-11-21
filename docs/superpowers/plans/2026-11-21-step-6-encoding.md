# Step 6: Encoding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every ripped title is encoded automatically with one of three fixed presets (Standard: HandBrake x265; Fast: ffmpeg VAAPI; Copy only), in a global low-priority queue that respects the shutdown guard. The raw rip is deleted 24 h after a good encode, and a Films page shows it all with logs.

**Architecture:**
- **Pure modules:** `encoding.py` (preset argv builders, audio plan, progress and idet parsers, estimates, guard decision) and `encodes.py` (the `encodes` table).
- **`encodepool.py`:** a global worker pool beside the per-drive `JobRunner`. It reuses `JobContext.run_process`, so encodes get logs, a stall watchdog and kill-on-cancel.
- **Wiring:** `services.py` queues encodes after rips, deletes raw files and handles the page actions. A new `films_routes.py` serves `/films`.

**Tech Stack:** Python 3.12 on `python:3.12-slim-trixie`; Debian `handbrake-cli`, `ffmpeg` (already in), Intel iHD VAAPI driver; FastAPI/Jinja/htmx (existing).

**Spec:** `docs/superpowers/specs/2026-11-21-step-6-encoding-design.md`

## Global Constraints

- **Presets:**
  - **Standard:** HandBrakeCLI `x265_10bit`, preset `slow`, RF **20** (Blu-ray) / **18** (DVD), no crop; DVDs add `--comb-detect --decomb --detelecine`.
  - **Fast:** ffmpeg `hevc_vaapi`, `-rc_mode CQP -qp <fast_quality>` (default **24**); `deinterlace_vaapi` only when idet says interlaced (DVDs only).
  - **Copy:** `ffmpeg -map 0 -c copy`.
- **Audio (Standard, Fast):** first audio track only, as E-AC-3. ≥6 channels → 5.1 at **640 kb/s**; otherwise the same channel count at **224 kb/s**.
- **All presets:** every subtitle track copied unchanged, chapters kept.
- **Low priority:** every encode command runs under `nice -n 19 ionice -c 3` (POSIX only).
- **Output:** `<raw stem> - <Preset>.mkv` (Preset = `Standard` / `Fast` / `Copy`). Written in `/work/encode-<id>/`, checked, then moved beside the raw file with `videofiles.move_to_downloads` (never overwrite).
- **Check:** ffprobe reads it, it has 1 video stream, it has audio if the source had audio, and its duration is within **max(2%, 5 s)** of the source's.
- **Guard:**
  - In `draining` no encode starts.
  - In `in_window`, start only if `now + estimate ≤ grace_ends_at`. A rough estimate gets **×1.25**.
  - An estimate longer than the whole session plus grace fails at once: "Too long to finish in one session (about N h); use Fast, or run it with the server on outside the schedule".
  - In `off_hours`, start freely.
- **Default speeds** (seconds of film per second; the average of the last **5** finished encodes of the same preset and disc type replaces them): Standard DVD **1.0**, Blu-ray **0.4**; Fast DVD **6**, Blu-ray **2**; Copy DVD **30**, Blu-ray **15**.
- **Size estimate:** `source_bytes ×` Standard **0.35**, Fast **0.5**, Copy **1.0**.
- **Raw deletion:** when any encode of the file is `done`, its output exists, it finished more than `raw_keep_hours` (**24**) ago, and no encode of that file is queued or running. **Delete raw now** applies the first condition only, after a browser `confirm()`.
- **Settings group "Encoding":**
  - `encode_after_rip`: choice standard / fast / copy / off, default standard;
  - `encode_slots`: int 1–2, default 1;
  - `fast_quality`: int 1–51, default 24;
  - `raw_keep_hours`: int 0–720, default 24;
  - `encode_stall_minutes`: int 1–240, default 10.
- **Interrupted encodes** are re-queued automatically at start-up.
- **Running encodes** hold `/api/busy`.
- **Logs:** `/config/logs/encode-<id>.log`, served at `/encodes/<id>/log`.
- **No new pip dependencies.** Fixed argv, never a shell. Tests pass on Windows (all tools faked) and Linux.

## Review Focus

1. **The raw file must never be deleted when the encode's output has gone** (owner deleted it, or the move failed). Expected: no deletion. Task 7: `test_raw_kept_when_output_missing`.
2. **An encode whose source raw was deleted or moved by hand** must fail with "The ripped file is no longer there." and never crash the pool. Task 6: `test_missing_source_fails_cleanly`.
3. **A container stop mid-encode** must leave no half file in downloads, and the encode must restart at boot. Task 5: `test_stop_marks_interrupted_and_start_requeues`; Task 6: `test_cancel_leaves_nothing_in_downloads`.
4. **Two encodes of the same file at once** (double-click, auto plus manual) must be refused. Task 7: `test_encode_refused_while_one_is_active`.
5. **A failed encode must be visible with its reason, Retry and log** on the Films page, not just a state word. Task 8: `test_failed_encode_shows_reason_retry_and_log`.

---

## File map

| File | Responsibility |
| --- | --- |
| `Dockerfile`, `.github/workflows/build.yml`, `unraid/disc-ripper.xml`, `README.md` | handbrake-cli, iHD driver, vainfo; GPU in the template |
| `src/disc_ripper/migrations/0005_encodes.sql` | `encodes` table |
| `src/disc_ripper/settings.py` | the Encoding settings |
| `src/disc_ripper/encodes.py` | `encodes` table access |
| `src/disc_ripper/encoding.py` | pure: presets, argv, audio plan, parsers, idet, estimates, guard decision |
| `src/disc_ripper/videofiles.py` | `MkvInfo` gains audio channel counts |
| `src/disc_ripper/runner.py` | `JobContext` takes an explicit log; `running_bytes` helper shared |
| `src/disc_ripper/encodepool.py` | the pool and the `run_encode` handler |
| `src/disc_ripper/busy.py`, `space` call sites | encodes in `/api/busy` and the space check |
| `src/disc_ripper/services.py` | queue after rip, manual actions, raw-deletion sweep, start/stop |
| `src/disc_ripper/web/films_routes.py`, `templates/films.html`, `_encodes_panel.html`, `base.html`, `dashboard.html` | Films page, dashboard panel, nav, logs |
| `tests/fixtures/encode/*` | fake `HandBrakeCLI` and `ffmpeg`, recorded progress output |

---

### Task 1: Image and Unraid template

**Files:** Modify `Dockerfile`, `docker/entrypoint.sh`, `.github/workflows/build.yml`, `unraid/disc-ripper.xml`, `README.md`.

**Interfaces:** Produces an image with `HandBrakeCLI`, `ffmpeg` with `hevc_vaapi`, the iHD driver and `vainfo`, all runnable as uid 99.

- [ ] **Step 1: Install the packages.**
  - In the final stage, enable Debian's `non-free` component: add `non-free` to `Components:` in `/etc/apt/sources.list.d/debian.sources`.
  - In the `apt-get install`, add `handbrake-cli intel-media-va-driver-non-free vainfo`.
  - Set `ENV LIBVA_DRIVER_NAME=iHD`.
  - Why non-free from the start: Intel's free driver build is documented as "features would be limited". On Gen9-family chips like the UHD 630, HEVC encoding runs through Intel's closed shader kernels, which only the non-free build ships, and HEVC has no low-power encode path on this generation.
- [ ] **Step 2: Let the app user open the GPU.**
  - The entrypoint drops to uid 99 with `setpriv --clear-groups`, which also strips any `--group-add` given to the container. So a `render` group added in the template would never reach the app.
  - Change `docker/entrypoint.sh`: when `/dev/dri/renderD128` exists, read its gid (`stat -c %g`) and pass `--groups <gid>` to `setpriv` instead of `--clear-groups`. Otherwise keep `--clear-groups`. No `--group-add` is needed in the template.
  - Test it in the image check: create a char device with a non-100 gid in a test container, if feasible; otherwise cover the gid-reading shell logic in `tests/test_entrypoint.py` by running the script's helper with `sh` against a temp file.
- [ ] **Step 3: Extend the CI smoke checks.** As uid 99:
  - `ffmpeg -hide_banner -encoders | grep -q hevc_vaapi`;
  - `HandBrakeCLI --help 2>&1 | grep -q x265_10bit`;
  - `HandBrakeCLI --help 2>&1 | grep -qw eac3`.

  Debian's HandBrake links the system x265 and ffmpeg, and missing 10-bit x265 or E-AC-3 encoders have happened in distro builds. If either check fails, stop and report: the Standard preset needs a change (8-bit `x265`, or a different audio encoder). `vainfo` is not run in CI (there's no GPU).
- [ ] **Step 4: Update the template.** In `unraid/disc-ripper.xml` `ExtraParams`, append `--device=/dev/dri`. In the README "Install on Unraid", add that on the server `docker exec disc-ripper vainfo` must list `VAProfileHEVCMain : VAEntrypointEncSlice`. If it doesn't, Fast won't work.
- [ ] **Step 5: Build.** Run `docker build -t disc-ripper:dev .`, then the smoke checks. Expected: all exit 0. Record the image size before and after.
- [ ] **Step 6: Commit.** `git commit -m "build: HandBrakeCLI, Intel VAAPI driver; GPU group for the app user"`

### Task 2: Schema, settings and the encodes table

**Files:**
- Create: `src/disc_ripper/migrations/0005_encodes.sql`, `src/disc_ripper/encodes.py`
- Modify: `src/disc_ripper/settings.py`, plus `tests/test_settings.py` and `tests/test_web_pages.py` (`VALID_FORM` gains the int settings)
- Test: `tests/test_encodes.py`, `tests/test_db.py`

**Interfaces:**
- Table `encodes(id INTEGER PK, rip_id INTEGER NOT NULL, title INTEGER NOT NULL, role TEXT NOT NULL, source TEXT NOT NULL, disc_type TEXT NOT NULL, source_seconds REAL NOT NULL, source_bytes INTEGER NOT NULL, source_audio INTEGER NOT NULL DEFAULT -1, preset TEXT NOT NULL CHECK (preset IN ('standard','fast','copy')), state TEXT NOT NULL CHECK (state IN ('queued','running','done','failed','cancelled','interrupted')), progress REAL NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT '', error TEXT, estimated_finish TEXT, output TEXT, output_bytes INTEGER, speed REAL, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT)`, with an index on `(state, id)` and one on `(rip_id, title)`.
- `encodes.py` produces:
  - `create(conn, *, rip_id, title, role, source: Path, disc_type, source_seconds, source_bytes, source_audio, preset, now) -> int | None`: returns None when that `(rip_id, title)` has a queued or running encode. The check and insert run in one `BEGIN IMMEDIATE`.
  - `get(conn, id)`;
  - `next_queued(conn) -> Row | None`, oldest first;
  - `start(conn, id, now) -> bool`, guarded on state queued;
  - `set_progress(conn, id, progress, message, finish_iso | None)`;
  - `wait(conn, id, message)`: stays queued, message set;
  - `finish(conn, id, state, now, *, error=None, output=None, output_bytes=None, speed=None)`;
  - `cancel_queued(conn, id, now) -> str` ("cancelled" | "running" | "gone");
  - `retry(conn, id, now) -> int | None`: new row with the same source fields, refused like `create`;
  - `mark_interrupted(conn, now) -> int`;
  - `requeue_interrupted(conn, now) -> int`: interrupted → queued, the same rows;
  - `running(conn) -> list[Row]`, `queued(conn) -> list[Row]`;
  - `for_rip(conn, rip_id) -> list[Row]`;
  - `recent_speeds(conn, preset, disc_type, n=5) -> list[float]`;
  - `reserved_bytes(conn) -> int`: the sum of running encodes' size estimates, using `encoding.estimate_size`.
- Settings exactly as in Global Constraints, group "Encoding". The `encode_after_rip` choice labels are "Standard (HandBrake x265)", "Fast (hardware)", "Copy only" and "Off".

- [ ] **Step 1: Write the failing tests.**
  - `test_create_refuses_second_active_for_same_file`;
  - `test_retry_copies_source_fields`;
  - `test_requeue_interrupted`;
  - `test_recent_speeds_last_five_by_preset_and_type`;
  - `test_migration_0005`;
  - `test_encoding_settings_defaults`, which asserts every default above.
- [ ] **Step 2: Run them.** `pytest tests/test_encodes.py tests/test_db.py tests/test_settings.py -q`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement** the migration, `encodes.py` and the settings. `reserved_bytes` may import `encoding` lazily, or take the factor table as a parameter until Task 3 exists. If so, record the choice in the report.
- [ ] **Step 4: Run the full suite.** `pytest -q`. Expected: everything passes.
- [ ] **Step 5: Commit.** `git commit -m "feat: encodes table and Encoding settings"`

### Task 3: Presets and command lines (pure)

**Files:**
- Create: `src/disc_ripper/encoding.py`
- Modify: `src/disc_ripper/videofiles.py`. `MkvInfo` gains `audio_channels: tuple[int, ...] = ()`, `sar: tuple[int, int] = (1, 1)` and `dar: float | None = None` (the first video stream's `sample_aspect_ratio` and `display_aspect_ratio`), all filled by `parse_ffprobe`.
- Test: `tests/test_encoding.py`

**Interfaces:**
- `PRESETS = ("standard", "fast", "copy")`; `PRESET_LABEL = {"standard": "Standard", "fast": "Fast", "copy": "Copy"}`.
- `output_name(raw: Path, preset: str) -> str`: `"The Lego Movie_t01 - Standard.mkv"`.
- `audio_plan(channels: int) -> tuple[int, int]`: returns (output channels, kb/s). `8 → (6, 640)`, `6 → (6, 640)`, `2 → (2, 224)`, `1 → (1, 224)`.
- `handbrake_prefix()` and `ffmpeg_prefix()`: the `HANDBRAKE_BIN` and `FFMPEG_BIN` env overrides (path or JSON list), like `cyanrip.command_prefix`.
- `priority_prefix() -> list[str]`: `["nice", "-n", "19", "ionice", "-c", "3"]` on POSIX, `[]` otherwise.
- `encode_argv(preset, source: Path, output: Path, *, disc_type, audio_channels: int | None, interlaced: bool, fast_quality: int, sar: tuple[int, int] = (1, 1)) -> list[str]`, without the priority prefix. `audio_channels=None` means the source has no audio. Exact lists:
  - Standard, Blu-ray, 6-channel audio:
    `[*handbrake_prefix(), "-i", src, "-o", out, "--format", "av_mkv", "--markers", "--encoder", "x265_10bit", "--encoder-preset", "slow", "--quality", "20", "--crop-mode", "none", "--auto-anamorphic", "--audio", "1", "--aencoder", "eac3", "--ab", "640", "--mixdown", "5point1", "--all-subtitles", "--subtitle-burned=none", "--json"]`
  - Standard, DVD: the same with `"--quality", "18"`, and `"--comb-detect", "--decomb", "--detelecine"` inserted before `"--audio"`.
  - Standard with stereo audio: `"--ab", "224", "--mixdown", "stereo"`. With mono: `"--mixdown", "mono"`.
  - Standard with no audio: the `--audio … --mixdown …` group is replaced by `"--audio", "none"`.
  - Fast:
    `[*ffmpeg_prefix(), "-hide_banner", "-nostdin", "-y", "-vaapi_device", "/dev/dri/renderD128", "-i", src, "-map", "0:v:0", "-map", "0:a:0?", "-map", "0:s?", "-map_chapters", "0", "-vf", "format=nv12,hwupload", "-c:v", "hevc_vaapi", "-rc_mode", "CQP", "-qp", "24", "-c:a", "eac3", "-b:a", "640k", "-ac", "6", "-c:s", "copy", "-progress", "pipe:1", "-nostats", out]`
    With `interlaced=True`, the filter is `"format=nv12,hwupload,deinterlace_vaapi"`.
    When `sar != (1, 1)` (anamorphic DVDs), insert `"-bsf:v", f"hevc_metadata=sample_aspect_ratio={n}/{d}"` before `"-progress"`. `hevc_vaapi` is known to drop the aspect ratio when the driver won't write the stream's header fields (FFmpeg tickets #8376 and #6276), which turns 16:9 DVDs into squashed 720×576 pictures. With stereo: `"-b:a", "224k", "-ac", "2"`. With no audio: drop `-map 0:a:0?` and the audio options and add `"-an"`.
  - Copy:
    `[*ffmpeg_prefix(), "-hide_banner", "-nostdin", "-y", "-i", src, "-map", "0", "-c", "copy", "-progress", "pipe:1", "-nostats", out]`
- `idet_argv(source: Path, start_seconds: float, seconds: float = 20) -> list[str]`: `[*ffmpeg_prefix(), "-hide_banner", "-nostdin", "-ss", f"{start:.0f}", "-t", "20", "-i", src, "-map", "0:v:0", "-vf", "idet", "-f", "null", "-"]`.

- [ ] **Step 1: Write the failing tests.**
  - `test_output_name`;
  - `test_audio_plan` (the four cases above);
  - `test_standard_bluray_argv_exact`, `test_standard_dvd_argv_exact`, `test_standard_no_audio`;
  - `test_fast_argv_exact`, `test_fast_interlaced_and_stereo`, `test_fast_no_audio`;
  - `test_copy_argv_exact`;
  - `test_idet_argv`;
  - `test_priority_prefix_posix_only` (monkeypatch `os.name`);
  - `test_parse_ffprobe_audio_channels_and_aspect`: an audio stream with `"channels": 6` gives `(6,)`; a video stream with `"sample_aspect_ratio": "64:45", "display_aspect_ratio": "16:9"` gives `sar == (64, 45)` and `dar == pytest.approx(1.778, abs=0.001)`; `"0:1"` or a missing SAR gives `(1, 1)`;
  - `test_fast_anamorphic_sets_sar`: `sar=(64, 45)` puts `-bsf:v hevc_metadata=sample_aspect_ratio=64/45` in the argv; `(1, 1)` doesn't.
  
  Paths in the expected lists are `str(Path(...))`.
- [ ] **Step 2: Run them.** `pytest tests/test_encoding.py tests/test_videorip_rip.py -q`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full suite.** `pytest -q`. Expected: everything passes.
- [ ] **Step 5: Commit.** `git commit -m "feat: encode presets and command lines"`

### Task 4: Progress, idet, estimates and the guard decision (pure)

**Files:** Modify `src/disc_ripper/encoding.py`. Create `tests/fixtures/encode/handbrake_progress.txt` and `tests/fixtures/encode/ffmpeg_progress.txt` (synthetic, first line `# synthetic: replace with a real capture`). Test: `tests/test_encoding.py`.

**Interfaces:**
- `class HandBrakeProgress`: `feed(line) -> float | None`. HandBrake `--json` prints blocks like `Progress: {` … `"Progress": 0.4123,` … `"State": "WORKING"`, spread over lines. Track the latest `"Progress": <float>` inside a `WORKING` block, and return it when the value changes. `"State": "WORKDONE"` gives `1.0`.
- `class FfmpegProgress(source_seconds: float)`: `feed(line) -> float | None`. `out_time_us=<n>` gives `n / 1e6 / source_seconds` (clamped 0–1); `progress=end` gives `1.0`. It must:
  - ignore `out_time_us=N/A` and negative values;
  - never go backwards (keep the maximum so far);
  - cap a jump of more than 10% in one update at the previous value plus 1%. With copied subtitle streams, ffmpeg's reported time is known to be erratic and can snap to subtitle timestamps.
- `is_progress_noise(line: str) -> bool`: True for the lines that would flood a log over a multi-hour encode, namely ffmpeg's `-progress` keys (`frame=`, `fps=`, `stream_`, `bitrate=`, `total_size=`, `out_time`, `dup_frames=`, `drop_frames=`, `speed=`, `progress=continue`) and HandBrake's JSON progress blocks (lines inside a `Progress: {` … `}` block, tracked with state, so it's a small class `ProgressNoise` with `__call__`). `progress=end`, errors and everything else are kept.
- `parse_idet(text: str) -> bool`: reads the last `Multi frame detection: TFF: a BFF: b Progressive: c Undetermined: d` line. Interlaced when `a + b > c`.
- `estimate_seconds(source_seconds, preset, disc_type, recent: list[float]) -> tuple[float, bool]`: returns (seconds, rough). It uses the mean of `recent` when it's non-empty, else the default speed table. `rough = not recent`.
- `estimate_size(source_bytes, preset) -> int`, using the factors in Global Constraints.
- `guard_decision(guard, now, estimate_s, rough) -> tuple[str, str]`: returns (`"start"` | `"wait"` | `"too_long"`, message). It applies ×1.25 when rough. `too_long` when the estimate exceeds the session length plus grace (`guard.grace_ends_at - window start`). Because `GuardState` has no window start, add `window_start: datetime | None` to `GuardState` in `schedule.py`, set in `guard_state`. The messages are exactly the spec's ("Waiting for the next session: needs about 3 h 20 min, the grace period ends at 01:00").

- [ ] **Step 1: Write the failing tests.**
  - `test_handbrake_progress_fixture` (monotonic, ends at 1.0);
  - `test_ffmpeg_progress_fixture`;
  - `test_ffmpeg_progress_ignores_na_jumps_and_going_back`;
  - `test_progress_noise_filter`: ffmpeg keys and a HandBrake progress block are noise; `progress=end`, `x265 [info]: ...` and an error line are not;
  - `test_parse_idet` (interlaced and progressive samples);
  - `test_estimate_defaults_and_history`: Standard Blu-ray 6035 s with no history gives `(15087.5, True)`; with history `[0.5, 0.5]` it gives `(12070.0, False)`;
  - `test_estimate_size`;
  - `test_guard_decision`:
    - in_window fits, so start;
    - an estimate that ends after grace means wait, with the exact message;
    - draining means wait;
    - off_hours means start;
    - longer than the session plus grace means too_long;
    - rough estimates get the ×1.25.
- [ ] **Step 2: Run them.** `pytest tests/test_encoding.py tests/test_schedule.py -q`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement.** Existing `GuardState` constructions in tests and code get `window_start` (default `None`) without other changes.
- [ ] **Step 4: Run the full suite.** `pytest -q`. Expected: everything passes.
- [ ] **Step 5: Commit.** `git commit -m "feat: encode progress parsers, estimates and guard decision"`

### Task 5: The encode pool

**Files:**
- Create: `src/disc_ripper/encodepool.py`, `tests/fixtures/encode/fake_encoder.py` (one script used for both `HANDBRAKE_BIN` and `FFMPEG_BIN`; mode from `FAKE_ENCODE_MODE` = ok | fail | hang | short | no_audio; it replays the progress fixtures and writes the output file named by `-o` or the last argv item, as a JSON stub `{"seconds":…, "video":1, "audio":n, "audio_channels":[6]}`; for idet runs, which have `-f null`, it prints an idet summary chosen by `FAKE_IDET` = progressive | interlaced)
- Modify: `src/disc_ripper/runner.py` (`JobContext.__init__` gains `log: joblog.JobLog | None = None`, defaulting to the job log; `run_process` gains `log_filter: Callable[[str], bool] | None = None`, and lines it returns True for are not written to the log; `on_line` still sees every line), `src/disc_ripper/busy.py`, and the `SpaceChecker` call site in `runner.py` (running bytes = `jobs.running_bytes + encodes.reserved_bytes`)
- Test: `tests/test_encodepool.py`, `tests/test_busy.py`

**Interfaces:**
- `class EncodePool(config, notifier, space, *, services=None, recheck_seconds=30.0, now=lambda: datetime.now().astimezone())`, with:
  - `start()` starts the worker tasks for `settings.encode_slots`;
  - `wake()`;
  - `async cancel(encode_id) -> bool`;
  - `async stop()`, which marks running encodes interrupted.
  
  It duck-types what `JobContext` needs (`config`, `services`, `db`, `notify`, `time_scale`), so `JobContext(pool, encode_id, "", params, log=JobLog(config_dir/"logs"/f"encode-{id}.log"))` works. Its `notify(_)` calls `services.encodes_changed()` when present.
- **Worker loop per slot:**
  1. Take `encodes.next_queued`.
  2. Compute the estimate (Task 4), then the guard decision (`schedule_source.current_guard`), then the space check (`estimate_size`).
  3. Wait, or fail `too_long`, or `encodes.start` and run `run_encode` (Task 6).
  4. On exception, `finish(failed, error=str(exc))`. On cancel, `cancelled`. On worker stop, `interrupted`.
  5. Log "finished: …" like the runner.
- `busy_report` lists running encodes as `{"id": …, "drive": "encoder", "kind": "encode", "estimatedFinish": …}`, and they count towards `busy`.

- [ ] **Step 1: Write the failing tests.** Use a stub `run_encode` patched into the pool:
  - `test_pool_runs_oldest_first_one_slot`;
  - `test_two_slots_run_together`;
  - `test_guard_wait_sets_message_and_stays_queued`;
  - `test_too_long_fails_immediately`;
  - `test_space_wait`;
  - `test_cancel_running_and_queued`;
  - `test_stop_marks_interrupted_and_start_requeues` (the `requeue_interrupted` call happens in `Services.start`, Task 7; here assert `interrupted`);
  - `test_encode_log_written_to_encode_log_path`;
  - `test_busy_report_includes_running_encode`.
- [ ] **Step 2: Run them.** `pytest tests/test_encodepool.py tests/test_busy.py -q`. Expected: they FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full suite.** `pytest -q`. Expected: everything passes.
- [ ] **Step 5: Commit.** `git commit -m "feat: global encode pool with guard, space check and busy report"`

### Task 6: Running one encode

**Files:** Modify `src/disc_ripper/encodepool.py`. Test: `tests/test_run_encode.py`.

**Interfaces:**
- `async run_encode(ctx, row) -> None`:
  1. If `row.source` is missing, raise "The ripped file is no longer there."
  2. Read `fast_quality` and `encode_stall_minutes`. Make `/work/encode-<id>/`.
  3. For `fast` and DVD, run idet at 20%, 50% and 80% of `source_seconds` (`idet_argv`). Interlaced if most of the three say so. Log the result.
  4. Read the source's first audio channel count with `videofiles.PROBE(source).audio_channels` (None when there's no audio).
  5. Build `priority_prefix() + encode_argv(...)`, passing `sar=PROBE(source).sar` for Fast, and run it with `ctx.run_process(..., idle_timeout=minutes*STALL_UNIT_SECONDS, log_filter=encoding.ProgressNoise())`. Feed lines to the preset's progress parser and report "Standard — 42%" with an estimated finish from the rate so far. `runner.ProcessStalled` becomes `RuntimeError("The encoder stopped responding: no output for N minutes, so it was stopped. Press Retry; the log has the encoder's last messages.")`. A non-zero exit becomes an error with the last 3 output lines.
  6. Run `videofiles.check_output(work, round(source_seconds), row.source_audio)`. Then compare the output's `dar` with the source's: more than 2% apart fails with "The encode's picture shape (DAR 1.33) doesn't match the rip's (1.78)". This catches a squashed anamorphic DVD.
  7. Run `videofiles.move_to_downloads(config, rip_id, role, out, min_free_gb=…, cancel=event, reserved_bytes=…)`, cancel-safe exactly like `videorip._rip_title`'s shielded move.
  8. Remove the work folder. `finish(done, output, output_bytes, speed=source_seconds / elapsed)`.
  9. On any failure, remove the work folder and re-raise. The raw file is never touched.
- `STALL_UNIT_SECONDS = 60` (tests shrink it).

- [ ] **Step 1: Write the failing tests.** Use the fake encoder via `HANDBRAKE_BIN` / `FFMPEG_BIN` and `videofiles.PROBE` patched to read stubs:
  - `test_standard_encode_lands_beside_raw`: the output is `downloads/<rip>/<stem> - Standard.mkv`, the work folder is gone and the raw file is intact;
  - `test_extra_encode_lands_in_extras`;
  - `test_fast_dvd_runs_idet_and_deinterlaces`: `FAKE_IDET=interlaced` puts `deinterlace_vaapi` in the recorded argv;
  - `test_encode_failure_keeps_raw_and_cleans_work`;
  - `test_encode_stall_message`;
  - `test_short_output_fails_check`;
  - `test_squashed_aspect_fails_check`: the fake writes `dar` 1.333 for a 1.778 source, so the encode fails with "picture shape";
  - `test_encode_log_stays_small`: a fake encode emitting 10,000 progress blocks leaves a log under 50 KB that still holds the command and `exit code 0`;
  - `test_missing_source_fails_cleanly`;
  - `test_cancel_leaves_nothing_in_downloads`;
  - `test_speed_recorded`.
- [ ] **Step 2: Run them.** `pytest tests/test_run_encode.py -q`. Expected: they FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full suite.** `pytest -q`. Expected: everything passes.
- [ ] **Step 5: Commit.** `git commit -m "feat: run an encode: idet, encoder, check, move beside the raw"`

### Task 7: Queueing, raw deletion and services wiring

**Files:** Modify `src/disc_ripper/services.py`, `src/disc_ripper/videorip.py` (after a file is recorded, call the services hook), `src/disc_ripper/video_rips.py`. Test: `tests/test_encode_services.py`.

**Interfaces:**
- **`Services`:**
  - `self.encoder = EncodePool(...)`. It's started in `start()`, after `encodes.requeue_interrupted`, and stopped in `stop()`.
  - `async queue_encode(rip_id, title, preset) -> int | None`: refused when the raw is deleted, the file is unknown, or an encode is active. It reads source fields from `video_rips.files[title]`, `video_discs` (disc_type, scan seconds and audio) and the file's size.
  - `async encode_all(rip_id, preset) -> list[int]`.
  - `async retry_encode(id)`, `async cancel_encode(id)`.
  - `async file_recorded(rip_id, title)`: queues with `encode_after_rip` unless it's `off`. It's called from `videorip._after_move` and from `move_again_sync`'s success path, through the event loop.
  - `delete_raw_sync(conn, rip_id, title, *, now, keep_hours: float | None) -> str | None`: returns an error, or None. `keep_hours=None` is "Delete raw now" (it needs a `done` encode with an existing output). Deletion records `raw_deleted_at` in `video_rips.files[title]` and logs to the encode's log.
  - `_raw_sweep_loop()`: every 600 s, plus once at start, applies `delete_raw_sync` with `raw_keep_hours` to every candidate.
  - `encodes_changed()`: publishes the `films` and `encodes` SSE events (renderers from Task 8; a no-op until then).
- `video_rips.mark_raw_deleted(conn, rip_id, title, now)`.

- [ ] **Step 1: Write the failing tests.**
  - `test_rip_completion_queues_default_preset`;
  - `test_encode_after_rip_off`;
  - `test_encode_refused_while_one_is_active`;
  - `test_encode_all_queues_each_file`;
  - `test_raw_deleted_after_keep_hours` (with a fixed `now`);
  - `test_raw_kept_while_another_encode_queued`;
  - `test_raw_kept_when_output_missing`;
  - `test_delete_raw_now_needs_done_encode`;
  - `test_encode_refused_after_raw_deleted`;
  - `test_start_requeues_interrupted`;
  - `test_move_again_queues_encode`.
- [ ] **Step 2: Run them.** `pytest tests/test_encode_services.py -q`. Expected: they FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full suite.** `pytest -q`. Expected: everything passes.
- [ ] **Step 5: Commit.** `git commit -m "feat: queue encodes after rips, delete raw files after a good encode"`

### Task 8: Films page, dashboard panel and logs

**Files:**
- Create: `src/disc_ripper/web/films_routes.py`, `src/disc_ripper/templates/films.html`, `src/disc_ripper/templates/_films_list.html`, `src/disc_ripper/templates/_encodes_panel.html`
- Modify: `src/disc_ripper/app.py` (router), `src/disc_ripper/templates/base.html` (nav "Films"), `src/disc_ripper/templates/dashboard.html` (panel with `sse-swap="encodes"`), `src/disc_ripper/services.py` (renderers for `encodes_changed`), `src/disc_ripper/static/app.css`
- Test: `tests/test_web_films.py`

**Interfaces:**
- Routes:
  - `GET /films`;
  - `POST /films/encode` (form `rip_id, title, preset`);
  - `POST /films/encode-all` (form `rip_id, preset`);
  - `POST /encodes/{id}/cancel`, `POST /encodes/{id}/retry`;
  - `POST /films/delete-raw` (form `rip_id, title`);
  - `GET /encodes/{id}/log`: plain text, 404 "No log for encode #N".

  Actions return the re-rendered `_films_list.html`. Refusals add a short error line.
- **Page content:** follow the spec's sketch.
  - The top section is the running and queued encodes (state, preset, %, finish, "rough" marker, Cancel, log).
  - Then rips, newest first, each with:
    - its job log links (from `jobs` with that `rip_id`);
    - each file: name, length, raw size or "raw deleted", and encodes (done → output name and size, "raw deleted in N h" plus **Delete raw now** with `hx-confirm="Delete the raw rip? It can't be undone; the disc is the backup."`; failed → reason, **Retry**, log);
    - **Encode ▾** (a select plus a button).
  - The `<div sse-swap="films">` updates live.
- `_encodes_panel.html` holds the same top section, used on the dashboard.

- [ ] **Step 1: Write the failing tests.**
  - `test_films_page_lists_rips_files_and_encodes`;
  - `test_encode_button_queues`;
  - `test_encode_all`;
  - `test_failed_encode_shows_reason_retry_and_log`;
  - `test_delete_raw_now_confirm_and_refusal`;
  - `test_encode_log_route`;
  - `test_dashboard_shows_encode_panel`;
  - `test_nav_has_films`;
  - `test_films_escape_disc_text`.
- [ ] **Step 2: Run them.** `pytest tests/test_web_films.py -q`. Expected: they FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full suite.** `pytest -q`. Expected: everything passes.
- [ ] **Step 5: Commit.** `git commit -m "feat: Films page, encode panel and encode logs"`

### Task 9: Docs and final verification

**Files:** Modify `README.md`.

- [ ] **Step 1: Write the README section "Encoding".** Cover the three presets, the settings, where encodes land, raw deletion and Delete raw now, the Films page, the GPU setup (from Task 1), and a "Testing step 6 on the server" list taken from the spec's manual tests. The list includes capturing real HandBrake `--json` and ffmpeg `-progress` output into `tests/fixtures/encode/`.
- [ ] **Step 2: Verify.** Run `pytest -q`, `docker build -t disc-ripper:dev .` and the CI smoke checks locally. Expected: all green.
- [ ] **Step 3: Commit.** `git commit -m "docs: step 6 README; verification"`
