# Step 4: DVD and Blu-ray Ripping Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Insert a DVD or Blu-ray and MakeMKV rips its main title (plus any ticked extras) to a lossless MKV in `/data/downloads/disc-ripper/<rip_id>/`, then ejects it. Unclear or TV-looking discs wait on their dashboard card for the owner to choose titles.

**Architecture:**
- Two new job kinds on the existing per-drive queue: `scan_video` (`makemkvcon info`) and `rip_title` (`makemkvcon mkv`, one title per job). Both are `uses_drive`, so they hold shutdown, show on the card and die with "Cancel job and eject".
- Pure logic lives in small, fixture-tested modules: robot-output parsing, fingerprint, `settings.conf` text (`makemkv.py`), and title choice and hold rules (`titles.py`).
- DB access goes in `video_rips.py` and the handlers in `videorip.py`, mirroring step 3's `cd_rips.py` / `cdrip.py`.
- The card gains a held-disc title table and an extras row.

**Tech Stack:** Python 3.12 on `python:3.12-slim-trixie`; MakeMKV built from source (pinned); Debian `ffmpeg` for `ffprobe`; FastAPI/Jinja/htmx (existing).

**Spec:** `docs/superpowers/specs/2026-11-20-step-4-dvd-bluray-design.md`

## Global Constraints

- **No new pip dependencies.**
- **Subprocesses use fixed argument lists, never a shell.** Only the device path, title numbers, numbers from settings and fixed strings go into `makemkvcon` arguments; never disc text.
- **MakeMKV runs with `HOME=/config/makemkv`**; `settings.conf` is written there before every run from the `makemkv_key` and `video_track_selection` settings. The key is never logged or rendered back.
- **Every `rip_title` for a disc passes the `--minlength` its scan used** (stored on the scan), never the current setting.
- **Paths:** output goes to `/work/<jobId>/`, then `/data/downloads/disc-ripper/<rip_id>/` (extras in `…/<rip_id>/extras/`). The file name is whatever single `.mkv` MakeMKV wrote; never build a path from the disc label. Never overwrite: clashes get ` (2)`.
- **Hold rules:** "unclear" = another title within **3%** of the main title's duration, excluding identical segment maps. "TV-like" = **3+** titles of **18–75 min**, each within **±20%** of their median, with no title over **90 min** except a play-all within **10%** of their sum.
- **`needs_bytes`** = scanned title size × **1.02**, rounded up.
- **Post-rip check:** exit 0, exactly one `.mkv`, `ffprobe` reads it, ≥1 video and ≥1 audio stream, duration within **max(2%, 5 s)** of the scan.
- **Move check:** before copying, free space on the work pool (`statvfs(work_dir)`) must be ≥ file size + `cache_min_free_gb`.
- **Card text** is escaped (Jinja autoescape); labels and title names are untrusted.
- **Tests** pass on Windows (no MakeMKV or ffprobe there: both are faked) and on Linux.

## Review Focus

1. **A different disc in the drive between scan and rip, or before Retry,** must fail with "A different disc is in the drive." and never write another disc's title under this `rip_id`. Task 6: `test_rip_refuses_different_disc`.
2. **An expired MakeMKV (beta key or "too old" release)** must show MakeMKV's own message plus the "Update MakeMKV…" hint on the card, leave the disc in, and never auto-queue a rip. Task 5: `test_scan_too_old_shows_update_hint`.
3. **Eject pressed during a scan** must kill `makemkvcon` and must not queue the auto-start rip afterwards. Task 5: `test_cancel_scan_queues_nothing`.
4. **Disc labels and title names with quotes, commas, `<script>` or non-ASCII text** must parse correctly and render escaped. Task 3: `test_parse_quoted_values`; Task 8: `test_card_escapes_disc_text`.
5. **Live card updates while the owner ticks extras mid-rip** must not lose a tick. Each extra checkbox posts on its own change, so a re-render never drops pending form state. Task 8: `test_tick_extra_mid_rip_queues_job`.

---

## File map

| File | Responsibility |
| --- | --- |
| `Dockerfile`, `.github/workflows/build.yml` | MakeMKV build stage, `ffmpeg`, smoke checks |
| `src/disc_ripper/migrations/0004_video.sql` | `video_discs`, `video_rips`, `drives.video_hold` |
| `src/disc_ripper/settings.py` | `text` kind; DVD and Blu-ray settings |
| `src/disc_ripper/drives.py` | `set_media` clears the drive's video scan and hold when the disc leaves |
| `src/disc_ripper/makemkv.py` | argv, robot parser, fingerprint, `settings.conf`, error hints (pure) |
| `src/disc_ripper/titles.py` | main title, unclear and TV rules, start-or-hold decision, eject rule (pure) |
| `src/disc_ripper/video_rips.py` | `video_discs` and `video_rips` access, already-ripped guard |
| `src/disc_ripper/videorip.py` | `scan_video` and `rip_title` handlers, ffprobe check, move to downloads |
| `src/disc_ripper/runner.py` | `run_process(env=…)` |
| `src/disc_ripper/services.py` | video insert/present hooks, enqueue scan/rip/extra, hold, move again |
| `src/disc_ripper/web/dashboard_routes.py`, `templates/_drive_card.html`, `templates/_video_card.html` | held table, extras row, buttons |
| `tests/fixtures/makemkv/*` | robot-output fixtures, `fake_makemkvcon.py` |

---

### Task 1: MakeMKV and ffprobe in the image

**Files:** Modify `Dockerfile`, `.github/workflows/build.yml`, `README.md` (a "Updating MakeMKV" note).

**Interfaces:** Produces an image with `/usr/bin/makemkvcon` and `/usr/bin/ffprobe` runnable as uid 99. No Python changes.

- [ ] **Step 1: Add a `makemkv-build` stage** `FROM python:3.12-slim-trixie AS makemkv-build`:
  - `ARG MAKEMKV_VERSION`, `ARG MAKEMKV_OSS_SHA256`, `ARG MAKEMKV_BIN_SHA256`, set to the current release. Take the checksums from makemkv.com's published `makemkv-sha-<ver>.txt`, checked by hand when bumping.
  - `apt-get install build-essential pkg-config libc6-dev libssl-dev libexpat1-dev zlib1g-dev libavcodec-dev curl ca-certificates`.
  - Download both tarballs from `https://www.makemkv.com/download/`, then `echo "<sha>  <file>" | sha256sum -c`.
  - `makemkv-oss`: `./configure --prefix=/usr --disable-gui && make && make install DESTDIR=/staging`.
  - `makemkv-bin`: `mkdir -p tmp && echo accepted > tmp/eula_accepted && make && make install DESTDIR=/staging PREFIX=/usr`.
- [ ] **Step 2: In the final stage,**
  - `COPY --from=makemkv-build /staging/usr/ /usr/`;
  - add the runtime packages `libssl3t64 libexpat1 zlib1g libavcodec61 ffmpeg` (confirm the exact trixie package names with `apt-cache policy` in the build);
  - set `ENV MAKEMKV_HOME=/config/makemkv`.
- [ ] **Step 3: Add the CI smoke checks** after the cyanrip check:
  ```
  docker exec smoke setpriv --reuid=99 --regid=100 --clear-groups sh -c 'HOME=/tmp makemkvcon -r info disc:9999' | grep -q '^MSG:'
  docker exec smoke setpriv --reuid=99 --regid=100 --clear-groups ffprobe -version | grep -q '^ffprobe version'
  ```
- [ ] **Step 4: Build locally or in CI.** Run `docker build -t disc-ripper:dev .`. Expected: the build succeeds and both smoke commands exit 0. Record the image size before and after in the PR notes.
- [ ] **Step 5: Commit.** `git commit -m "build: MakeMKV (pinned, from source) and ffprobe in the image"`

### Task 2: Schema, settings and media hooks

**Files:**
- Create: `src/disc_ripper/migrations/0004_video.sql`
- Modify: `src/disc_ripper/settings.py`, `src/disc_ripper/drives.py:287-307`, `src/disc_ripper/templates/settings.html` (render the `text` kind)
- Test: `tests/test_db.py`, `tests/test_settings.py`, `tests/test_media.py`

**Interfaces:**
- Produces tables:
  - `video_discs(fingerprint TEXT PK, disc_type TEXT, label TEXT, scan TEXT JSON, minlength INT, held TEXT JSON '[]', as_film INT 0, drive_id TEXT, last_seen TEXT)`;
  - `video_rips(rip_id INT PK REFERENCES jobs(id), fingerprint TEXT, state TEXT CHECK IN ('ripping','ripped','move_failed','failed'), main_title INT NULL, files TEXT JSON '{}', message TEXT '', created_at, updated_at)`;
  - `drives.video_hold INTEGER NOT NULL DEFAULT 0`.
- Produces settings `makemkv_key` (secret, ""), `video_min_title_seconds` (int 0–3600, 120) and `video_track_selection` (text, default `settings.DEFAULT_VIDEO_SELECTION`), in the group "DVD and Blu-ray".
- `settings.DEFAULT_VIDEO_SELECTION = "-sel:all,+sel:video,+sel:(eng|nolang),+sel:(audio&single),-sel:(audio&special),+sel:(subtitle&forced),-sel:mvcvideo,=100:all,-10:eng"`. This is provisional; Task 9 confirms it on a real disc.
- `drives.set_media(...)`: when `state != "disc"`, it also runs `UPDATE video_discs SET drive_id = NULL WHERE drive_id = ?` and `UPDATE drives SET video_hold = 0 WHERE id = ?`.

- [ ] **Step 1: Write the failing tests.**
  - `test_migration_0004_tables`: after `db.migrate`, both tables exist and `drives` has `video_hold` = 0.
  - `test_text_setting_round_trip`: `settings.set(conn, "video_track_selection", "-sel:all,+sel:eng")` then `get` returns it, and a value with a newline is rejected.
  - `test_set_media_clears_video_disc`: insert a `video_discs` row with `drive_id="serial:A"` and set `video_hold=1`. After `set_media(conn, "serial:A", "empty", None, "", "t")` the row's `drive_id` is NULL and `video_hold` is 0.
- [ ] **Step 2: Run them.** `pytest tests/test_db.py tests/test_settings.py tests/test_media.py -q`. Expected: the three new tests FAIL.
- [ ] **Step 3: Implement the changes.**
  - Write the migration.
  - Add the `text` kind to `SettingDef.kind`: single line, at most 2000 characters, stripped. Add the three settings and the constant.
  - Extend `set_media`.
  - The settings template renders `text` as `<input type="text">`.
- [ ] **Step 4: Run the full suite.** `pytest -q`. Expected: everything passes.
- [ ] **Step 5: Commit.** `git commit -m "feat: video tables, DVD/Blu-ray settings, clear scan when the disc leaves"`

### Task 3: `makemkv.py`, the pure parts

**Files:**
- Create: `src/disc_ripper/makemkv.py`, `tests/fixtures/makemkv/info_dvd.txt`, `tests/fixtures/makemkv/info_bluray_decoy.txt`, `tests/fixtures/makemkv/info_too_old.txt`
- Test: `tests/test_makemkv.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Title: number: int; seconds: int; size: int; chapters: int; source: str; output_name: str; segments: str; name: str`
  - `@dataclass class Scan: titles: list[Title]; label: str; messages: list[str]` (each `MSG` text, in order)
  - `parse_line(line: str) -> tuple[str, list[str]] | None`: splits `KEY:a,b,"c, \"d\""` into its key and values. A value in quotes has `\"` and `\\` unescaped. Returns None for anything else.
  - `class RobotParser`: `feed(line: str) -> float | None` returns the overall progress from `PRGV:cur,total,max` as `total/max`; it collects `TINFO`/`CINFO`/`MSG` lines. `result() -> Scan`.
  - `info_argv(device: Path, minlength: int) -> list[str]` → `[*command_prefix(), "-r", "--progress=-same", f"--minlength={minlength}", "info", f"dev:{device}"]`
  - `mkv_argv(device: Path, title: int, minlength: int, out_dir: Path) -> list[str]` → `[..., "mkv", f"dev:{device}", str(title), str(out_dir)]`
  - `command_prefix() -> list[str]`: `makemkvcon`, or `MAKEMKVCON_BIN` (a path, or a JSON argv list), as `cyanrip.command_prefix`.
  - `fingerprint(disc_type: str, label: str, titles: list[Title]) -> str`: SHA-1 hex over `disc_type`, `label` and sorted `(seconds, size, chapters, source)`.
  - `settings_conf(key: str, selection: str) -> str`: `app_Key = "<key>"` (left out when the key is empty) and `app_DefaultSelectionString = "<selection>"`, with `"` and `\` escaped.
  - `update_hint(messages: list[str]) -> str | None`: returns "Update MakeMKV: set a new beta key in Settings or rebuild the image with a newer version." when any message matches `too old`, `expired` or `registration key` (case-insensitive).
- TINFO attribute IDs: `8` chapters, `9` duration `H:MM:SS`, `11` size in bytes, `16` source, `26` segments map, `27` output file name, `2` name. CINFO `2` is the disc name.

- [ ] **Step 1: Write the fixtures.**
  - `info_dvd.txt`: a synthetic robot transcript with `MSG`, `DRV`, `TCOUNT:3`, `CINFO:2,0,"FILM_LABEL"`, three titles (1:52:10 / 0:03:12 / 0:01:40) and `PRGV` lines.
  - `info_bluray_decoy.txt`: titles at 2:16:04 and 2:16:02 with different segment maps, plus a 0:12:30 extra.
  - `info_too_old.txt`: `MSG:5021,…,"This application version is too old. Please download the latest version at http://www.makemkv.com/ or enter a registration key to continue using the current version."`.
  - Line one of each file says `# synthetic: replace with a real capture` (as with the cyanrip fixtures).
- [ ] **Step 2: Write the failing tests.**
  - `test_parse_quoted_values`: `parse_line('CINFO:2,0,"A \\"B\\", C <x> Ü"')` returns `("CINFO", ["2", "0", 'A "B", C <x> Ü'])`, and `parse_line("garbage")` returns None.
  - `test_parse_dvd_fixture`: three titles; `titles[0].seconds == 6730`; `size`, `chapters` and `source` match the fixture; `label == "FILM_LABEL"`.
  - `test_progress`: `feed("PRGV:100,32768,65536") == 0.5`, and a `TINFO` line returns None.
  - `test_argv_exact`: both argv builders return exactly the lists above for `Path("/dev/sr0")`, title 3, minlength 120 and `Path("/work/7")`.
  - `test_fingerprint_stable_and_sensitive`: the fingerprint is unchanged when titles are reordered, and changes when the label or any duration changes.
  - `test_settings_conf_escapes`: the key `ab"c\d` gives `app_Key = "ab\"c\\d"`; an empty key gives no `app_Key` line.
  - `test_update_hint`: the too-old fixture's messages give the hint; the DVD fixture's give None.
- [ ] **Step 3: Run them.** `pytest tests/test_makemkv.py -q`. Expected: they FAIL (no module).
- [ ] **Step 4: Implement `makemkv.py`.** Unknown keys and malformed lines are ignored. Durations that won't parse give `seconds=0`.
- [ ] **Step 5: Run them.** `pytest tests/test_makemkv.py -q`. Expected: they PASS.
- [ ] **Step 6: Commit.** `git commit -m "feat: MakeMKV robot-output parser, argv, fingerprint and settings.conf"`

### Task 4: `titles.py`, the title choice and the rules

**Files:** Create `src/disc_ripper/titles.py`. Test: `tests/test_titles.py`.

**Interfaces:**
- Consumes `makemkv.Title`.
- Produces:
  - `main_title(titles: list[Title]) -> Title | None`: the longest; a tie goes to the larger size.
  - `is_unclear(titles, main: Title) -> bool`: true when another title's duration is within 3% of `main.seconds`, unless its non-empty `segments` equal `main.segments`.
  - `looks_like_tv(titles) -> bool`: the rule in Global Constraints.
  - `hold_reasons(titles, *, auto_start: bool, hold_pressed: bool, already_ripped: str | None, from_insert: bool) -> list[str]`. The reasons, in this order:
    - `"two titles of about the same length (decoy playlists?)"`
    - `"looks like a TV disc"`
    - `"Hold pressed"`
    - `"auto-start is off"`
    - `f"already ripped ({already_ripped})"`
    - `"disc was in the drive at start-up"`
    - `"no titles found"`

    An empty list means start.
  - `should_eject(attempts: list[tuple[int, int, str]]) -> bool`: each entry is `(title, job_id, state)`. True when no state is `queued`, `waiting_space` or `running`, and the attempt with the highest `job_id` for each title is `done`.

- [ ] **Step 1: Write the failing tests** as tables of `Title` lists:
  - `test_plain_film_starts`: one 1:52 title plus three short extras → main = the 1:52 title, `hold_reasons(...) == []`.
  - `test_decoy_playlists_unclear`: 8164 s and 8162 s with different segment maps → unclear. With equal segment maps → not unclear.
  - `test_widescreen_fullscreen_dvd_unclear`: 6730 s and 6712 s → unclear.
  - `test_tv_disc_with_play_all`: four titles of 42–46 min plus a play-all of 176 min → `looks_like_tv` is true.
  - `test_film_with_many_extras_not_tv`: a 120-min main title plus six 5–20 min extras → false.
  - `test_reasons_order_and_flags`: every flag set gives the reasons in the order above.
  - `test_should_eject`:
    - `[(0, 5, "done")]` → True;
    - `[(0, 5, "failed"), (0, 7, "done")]` → True;
    - `[(0, 5, "done"), (3, 6, "queued")]` → False;
    - `[(0, 5, "failed")]` → False.
- [ ] **Step 2: Run them.** `pytest tests/test_titles.py -q`. Expected: they FAIL.
- [ ] **Step 3: Implement `titles.py`.**
- [ ] **Step 4: Run them.** `pytest tests/test_titles.py -q`. Expected: they PASS.
- [ ] **Step 5: Commit.** `git commit -m "feat: main-title choice, hold rules and eject rule"`

### Task 5: Scanning: `scan_video` job, the fake makemkvcon and the insert hooks

**Files:**
- Create: `src/disc_ripper/video_rips.py`, `src/disc_ripper/videorip.py`, `tests/fixtures/makemkv/fake_makemkvcon.py`
- Modify: `src/disc_ripper/runner.py` (`run_process` gains `env: dict[str, str] | None = None`, merged over `os.environ`), `src/disc_ripper/services.py`
- Test: `tests/test_videorip_scan.py`, `tests/test_runner.py`

**Interfaces:**
- `video_rips.py` produces:
  - `save_scan(conn, fingerprint, disc_type, label, scan_json, minlength, held: list[str], drive_id, now)`, an upsert;
  - `disc_in_drive(conn, drive_id) -> Row | None`;
  - `blocking_rip(conn, fingerprint, downloads_dir: Path, work_dir: Path) -> Row | None`, applying the spec's guard. Active jobs are matched by `json_extract(jobs.params, '$.fingerprint')`.
- `videorip.py` produces:
  - `write_settings_conf(home: Path, key: str, selection: str) -> None`, mode 0600;
  - `async run_scan_video(ctx) -> None`, registered as `runner.HANDLERS["scan_video"]`;
  - `scan_env(config) -> dict` → `{"HOME": str(config.config_dir / "makemkv")}`.
- `scan_video` params: `{sr_name, disc_type, label, from_insert}`.
- Handler flow:
  1. Write `settings.conf`.
  2. Run `info_argv` through `ctx.run_process(..., env=scan_env)` and feed lines to `RobotParser`. Progress goes to the card as "Scanning disc… N%".
  3. A non-zero exit, or no titles, raises `RuntimeError(last MSG + (" " + hint if hint))`.
  4. Compute the fingerprint and `hold_reasons`, using `drives.video_hold` and `blocking_rip`, then call `save_scan`.
  5. If there are no reasons, `await ctx.services.start_video_rip(drive_id, fingerprint, main=main.number, extras=[], again=False)`.
- `Services` gains:
  - `async enqueue_video_scan(drive_id, *, from_insert: bool) -> int | None`: none while a `scan_video` job for that drive is queued or running;
  - `on_insert` and `on_present` dispatch `dvd` and `bluray` to it (`from_insert` True or False);
  - `async hold_video(drive_id)` sets `drives.video_hold = 1` and cancels a queued (not running) `rip_title` main job for the disc in that drive.
- `fake_makemkvcon.py`:
  - reads `FAKE_MAKEMKV_MODE` (`ok` | `fail_old` | `hang` | `short` | `no_audio`) and `FAKE_MAKEMKV_INFO` (fixture name, default `info_dvd.txt`);
  - for `info`, replays the fixture;
  - for `mkv`, replays `PRGV` lines and writes `<out_dir>/title_t<NN>.mkv` holding a JSON stub `{"seconds": …, "video": 1, "audio": n}`, which the tests' fake ffprobe reads;
  - writes `argv.txt` and `env_home.txt` into its working directory.

- [ ] **Step 1: Write the failing tests.** They follow `tests/test_cdrip_job.py`'s `setup` and `run` helpers, with `MAKEMKVCON_BIN` set to the fake.
  - `test_run_process_env`: a child started with `env={"X_TEST": "1"}` prints it.
  - `test_scan_saves_titles_and_autostarts`: inserting the DVD fixture saves a `video_discs` row with `drive_id` set and `held == []`, and queues one `rip_title` with `role="main"`, `title=0`, `minlength=120`, `needs_bytes == ceil(size * 1.02)`.
  - `test_scan_decoy_is_held`: with the decoy fixture, `held` contains the decoy reason and no `rip_title` is queued.
  - `test_scan_from_present_never_autostarts`: `from_insert=False` adds "disc was in the drive at start-up".
  - `test_scan_too_old_shows_update_hint`: in `fail_old` mode the job is `failed` and its error contains "too old" and "Update MakeMKV"; nothing is queued.
  - `test_cancel_scan_queues_nothing`: in `hang` mode, `runner.cancel(scan_id)` leaves `runner.LIVE_PROCESSES` empty and the job `cancelled`, with no `rip_title` rows.
  - `test_hold_during_scan`: `hold_video` while scanning → `held` includes "Hold pressed" and nothing is queued.
  - `test_settings_conf_written_with_key`: with `makemkv_key` set, `<config>/makemkv/.MakeMKV/settings.conf` contains it, and `env_home.txt` is `<config>/makemkv`.
- [ ] **Step 2: Run them.** `pytest tests/test_videorip_scan.py tests/test_runner.py -q`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement** `run_process(env=…)`, `video_rips.py`, `run_scan_video`, the fake and the `Services` methods. Also add `Services.start_video_rip` with the signature from Task 7, implementing only the main-title path for now: one `rip_title` job plus its `video_rips` row, in one `BEGIN IMMEDIATE` transaction. Task 7 adds extras, TV titles and the guard.
- [ ] **Step 4: Run the full suite.** `pytest -q`. Expected: everything passes.
- [ ] **Step 5: Commit.** `git commit -m "feat: scan_video job, hold rules applied on insert, fake makemkvcon"`

### Task 6: The `rip_title` job: rip, check, move, eject

**Files:** Modify `src/disc_ripper/videorip.py`, `src/disc_ripper/video_rips.py`. Test: `tests/test_videorip_rip.py`.

**Interfaces:**
- `rip_title` params: `{rip_id, fingerprint, title, role ("main" | "extra" | "title"), minlength, expected_seconds, sr_name}`.
- Produces:
  - `@dataclass class MkvInfo: seconds: float; video: int; audio: int`;
  - `parse_ffprobe(text: str) -> MkvInfo`, taking `ffprobe -v error -print_format json -show_format -show_streams` output;
  - `PROBE: Callable[[Path], MkvInfo]`, a module attribute (it runs `ffprobe`) that tests monkeypatch;
  - `check_output(work: Path, expected_seconds: int) -> Path`, which raises `RuntimeError` with the reason;
  - `move_to_downloads(config, rip_id: int, role: str, src: Path, statvfs=os.statvfs, min_free_gb: float) -> Path`;
  - `video_rips.record_file(conn, rip_id, title, path, role, now)`;
  - `video_rips.set_state(conn, rip_id, state, message, now)`;
  - `video_rips.attempts(conn, rip_id) -> list[tuple[int, int, str]]`, for `titles.should_eject`;
  - `async run_rip_title(ctx)`, registered as `runner.HANDLERS["rip_title"]`;
  - `async Services.move_again(rip_id) -> str | None`, which returns an error message or None.
- Handler flow:
  1. Compare the drive row's `disc_type` and `disc_label` with the `video_discs` row for `fingerprint`; a mismatch raises "A different disc is in the drive."
  2. Clear `/work/<jobId>/`. Also delete the work folders of earlier attempts at the same `rip_id` and title that didn't finish `done`: a cancelled or interrupted Blu-ray leaves tens of GB that would otherwise sit in the budget until the 7-day sweep. Then write `settings.conf`.
  3. Run `mkv_argv` and report "Main title — N%", "Extra k of m — N%" or "Title k of m — N%", with an estimate.
  4. Run `check_output`. On failure, delete the work folder and re-raise.
  5. Run `move_to_downloads`. Any `OSError` or space shortfall sets the row to `move_failed` with the message "Couldn't move to downloads: …" and keeps the file. The job still ends `done`.
  6. When all titles are moved and none are pending, set the state to `ripped`.
  7. If `should_eject(attempts)`, eject under the drive lock, with failures logged.
- On an exception other than cancel: if no title of this `rip_id` is done and none is pending, set `failed`.
- `move_to_downloads` copies to `<name>.mkv.part`, fsyncs, renames to the first free name (`name.mkv`, `name (2).mkv`, …), then removes `src` and its now-empty folder. An existing `.part` from an earlier attempt is overwritten.

- [ ] **Step 1: Write the failing tests**, with `PROBE` patched to read the fake's JSON stub:
  - `test_rip_main_moves_and_ejects`: the file ends up at `downloads/<rip_id>/title_t00.mkv`, `/work/<job>` is gone, the state is `ripped`, `drive.ejected == 1`, and `argv.txt` has `--minlength=120` even after the setting has been changed to 300.
  - `test_extra_goes_to_extras_and_eject_waits`: main plus a queued extra → one eject, after the extra, with the extra in `downloads/<rip_id>/extras/`.
  - `test_short_file_fails_and_keeps_disc_in`: in `short` mode the job is `failed` with "length", the work folder is removed and there's no eject.
  - `test_no_audio_fails`: `no_audio` mode → `failed` with "audio".
  - `test_rip_refuses_different_disc`: with the drive row's label changed → `failed` with "A different disc is in the drive.", and makemkvcon never ran (no `argv.txt`).
  - `test_retry_same_folder_then_eject`: a failed main job then a `jobs.retry` → the retry writes to the same `downloads/<rip_id>/` and ejects.
  - `test_retry_removes_partial_work`: an interrupted attempt's `/work/<old job>/title_t00.mkv` is gone once the retry starts.
  - `test_move_space_shortfall_is_move_failed`: a fake `statvfs` with too little free → the job is `done`, the state `move_failed`, the file still in `/work`; then `move_again` with enough space → `ripped`.
  - `test_move_never_overwrites_and_ignores_part`: a pre-existing `title_t00.mkv` and a stale `title_t00.mkv.part` → the result is `title_t00 (2).mkv`, with no `.part` left.
  - `test_parse_ffprobe`: a recorded ffprobe JSON (fixture `ffprobe_ok.json`) gives `MkvInfo(seconds=6730.04, video=1, audio=2)`.
- [ ] **Step 2: Run them.** `pytest tests/test_videorip_rip.py -q`. Expected: they FAIL.
- [ ] **Step 3: Implement** the handler and helpers.
- [ ] **Step 4: Run the full suite.** `pytest -q`. Expected: everything passes.
- [ ] **Step 5: Commit.** `git commit -m "feat: rip_title job with ffprobe check, move to downloads, eject when done"`

### Task 7: Starting rips: extras, TV titles, already ripped

**Files:** Modify `src/disc_ripper/services.py`, `src/disc_ripper/video_rips.py`. Test: `tests/test_video_start.py`.

**Interfaces:**
- `async Services.start_video_rip(drive_id, fingerprint, *, main: int | None, extras: list[int], again: bool) -> int | None` returns the `rip_id`, or None when refused. In one `BEGIN IMMEDIATE` transaction:
  1. Refuse if `blocking_rip` finds an active job. Refuse if it finds a ripped rip and `again` is False.
  2. Refuse when there are no titles at all.
  3. Queue the main title first (`role="main"`), then extras (`role="extra"`). With `main=None`, queue every ticked title as `role="title"`.
  4. `rip_id` = the first job's ID, written into every job's params (insert, then `UPDATE jobs SET params = json_set(params, '$.rip_id', ?)`).
  5. Insert the `video_rips` row (`main_title` = `main`).
  6. Clear `drives.video_hold`, then wake the worker and push the card.
- `async Services.add_video_extra(rip_id, title) -> int | None` queues a `rip_title` (`role="extra"`, or `"title"` when the rip has no main), unless that title already has a queued, running or done attempt for this `rip_id`.
- `video_rips.set_as_film(conn, fingerprint, value: bool)`.

- [ ] **Step 1: Write the failing tests.**
  - `test_start_with_extras_order`: `main=0, extras=[4, 2]` gives three jobs in the order 0, 4, 2, all with the same `rip_id` = the first job's ID.
  - `test_tv_titles_are_peers`: `main=None, extras=[1, 2, 3]` gives roles of all `"title"` and `main_title` NULL. The files land directly in `downloads/<rip_id>/` (end-to-end with the fake).
  - `test_double_start_refused`: two concurrent `start_video_rip` calls queue only one set.
  - `test_already_ripped_needs_again`: once ripped, `again=False` gives None and `again=True` a new `rip_id`. Deleting the downloads folder makes `again=False` work.
  - `test_failed_rip_does_not_block`: a failed or cancelled main with nothing pending → `again=False` succeeds.
  - `test_add_extra_once`: the same title twice → the second call returns None.
- [ ] **Step 2: Run them.** `pytest tests/test_video_start.py -q`. Expected: they FAIL.
- [ ] **Step 3: Implement** the methods.
- [ ] **Step 4: Run the full suite.** `pytest -q`. Expected: everything passes.
- [ ] **Step 5: Commit.** `git commit -m "feat: start video rips with extras or TV titles; already-ripped guard"`

### Task 8: The dashboard card and routes

**Files:**
- Create: `src/disc_ripper/templates/_video_card.html`, included from `_drive_card.html` when `disc_type in ("dvd", "bluray")`
- Modify: `src/disc_ripper/web/dashboard_routes.py`, `src/disc_ripper/static/app.css`
- Test: `tests/test_web_dashboard.py`

**Interfaces:**
- `render_drive_card` adds `video` (the `video_discs` row for the drive, its `scan` parsed into `Title`s), `video_blocking` (`blocking_rip`) and `video_rip` (the active or latest `video_rips` row for that fingerprint, with its jobs' titles and states) to the template context.
- Routes, each returning the card:
  - `POST /dashboard/video/rip`, form `drive_id, main ("" = none), extras (repeated), again`;
  - `POST /dashboard/video/hold`, form `drive_id`;
  - `POST /dashboard/video/extra`, form `drive_id, rip_id, title`;
  - `POST /dashboard/video/move`, form `drive_id, rip_id`;
  - `POST /dashboard/video/as-film`, form `drive_id, value`.
- Card content, following the spec's sketches:
  - While scanning: the progress line plus **Hold**.
  - Held: "Held: <reasons joined by '; '>" and the title table (Main radio column unless TV-like and not `as_film`; Extra checkboxes; #, length `H:MM:SS`, size in GB to one decimal place, chapters, source). Then **Rip** (or **Rip again** when already ripped), and "Treat as a film" / "Treat as TV" links when TV-like.
  - While ripping: an extras row. Each unticked title is a checkbox with `hx-post="/dashboard/video/extra"` on `change`. Queued and done titles show as "(queued)" or "(ripped)".
  - `move_failed`: the message plus **Move again**.
  - The recent line for a `rip_title` job: `#N done → downloads/<rip_id>/<file> (H:MM:SS, X.X GB)`.

- [ ] **Step 1: Write the failing tests**, using the existing web test client and fixtures, with DB rows set up directly:
  - `test_held_card_lists_titles`: a held decoy disc → both 2:16 titles, the reason text and a Rip button are rendered.
  - `test_rip_button_queues_main_and_extras`: posting `main=0&extras=4` → two jobs.
  - `test_tv_card_has_no_main_column_until_as_film`.
  - `test_tick_extra_mid_rip_queues_job`: with a running main, posting `/dashboard/video/extra` → a queued extra, and the card shows "(queued)".
  - `test_hold_button_while_scanning`.
  - `test_move_again_button`.
  - `test_card_escapes_disc_text`: a label `<script>x</script>` renders escaped.
  - `test_makemkv_key_never_rendered`: the settings page doesn't contain the saved key.
- [ ] **Step 2: Run them.** `pytest tests/test_web_dashboard.py -q`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement** the template, routes and CSS. Keep the table usable at phone width: the source column hides under 480 px.
- [ ] **Step 4: Run the full suite.** `pytest -q`. Expected: everything passes.
- [ ] **Step 5: Commit.** `git commit -m "feat: video disc card: held titles, Hold, extras, Move again"`

### Task 9: Real captures, track selection, docs, final check

**Files:** Modify `README.md`, `tests/fixtures/makemkv/*`, `src/disc_ripper/settings.py` (`DEFAULT_VIDEO_SELECTION` if needed), `Disc Ripper – Design Plan.md` (tick step 4 in the build order).

- [ ] **Step 1: Ask the owner to capture real output** on the server, for one DVD and one Blu-ray (decoy-style if available). Use `/dev/sr0` or `/dev/sr1` to match the drive the disc is in:
  ```
  docker exec disc-ripper sh -c 'HOME=/config/makemkv makemkvcon -r --progress=-same --minlength=120 info dev:/dev/sr0 > /tmp/info.txt 2>&1'
  docker exec disc-ripper sh -c 'mkdir -p /tmp/mkvtest && HOME=/config/makemkv makemkvcon -r --progress=-same --minlength=120 mkv dev:/dev/sr0 0 /tmp/mkvtest > /tmp/mkv.txt 2>&1; ffprobe -v error -print_format json -show_format -show_streams /tmp/mkvtest/*.mkv > /tmp/ffprobe.json'
  docker cp disc-ripper:/tmp/info.txt /mnt/user/Barry-server/makemkv-info.txt
  docker cp disc-ripper:/tmp/mkv.txt /mnt/user/Barry-server/makemkv-mkv.txt
  docker cp disc-ripper:/tmp/ffprobe.json /mnt/user/Barry-server/ffprobe.json
  ```
  Turn auto-start off first, so the app doesn't rip the same disc at the same time.
- [ ] **Step 2: Replace or add fixtures** from the captures (trim the `PRGV` lines). Fix any parser mismatch, with a test per mismatch. Check whether `mkv` output includes `TINFO` lines; if it does, add the fingerprint comparison to the Task 6 disc check with a test.
- [ ] **Step 3: Confirm `settings.DEFAULT_VIDEO_SELECTION`** against the captured `SINFO` streams and the ffprobe output: English audio without commentary, English and forced subtitles, and the only audio track kept when there's no English. Adjust the string and its test.
- [ ] **Step 4: Update the README:** the MakeMKV key setting, where rips land, how to bump `MAKEMKV_VERSION` and its checksums, and the manual test list from the spec's Testing section.
- [ ] **Step 5: Run the full verification.** `pytest -q`, `docker build`, then the CI smoke test. Expected: all green. Then run the spec's manual server checks 1–7 with the owner and note the results in the PR.
- [ ] **Step 6: Commit.** `git commit -m "test: real MakeMKV captures; docs for step 4"`
