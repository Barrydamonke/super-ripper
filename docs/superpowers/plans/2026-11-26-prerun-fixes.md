# Pre-run Fixes (A1–A14) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make the app safe for a 50-film unattended run.
- Films always get imported, whatever happens to the encodes.
- Discs are never re-ripped by mistake.
- Space checks look at the right disk.
- Encodes can't block the queue.
- The raw rip is kept as long as the owner chooses.
- A crash leaves a trace and the app comes back up by itself.

**Spec:** the owner chose items A1–A14 from the audit list, quoted under each task below. The owner's extra requirement for A14: a setting for how long raws are kept, either **N hours after the encode** or **until the film is in Jellyfin**.

**Tech:** Python 3.12, FastAPI/Jinja/htmx, SQLite. Tests run with `.venv/Scripts/python -m pytest -q`, and the whole suite must stay green after every task.

## Global Constraints

- **No new runtime dependencies.** Commands are fixed argv lists, never a shell.
- **Migrations are pure SQL**, applied by `db.migrate`. Task 1 owns `0010_import_anyway.sql` and Task 3 owns `0011_encode_attempts.sql`. Any test asserting the schema version moves to 11.
- **Commits** end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Never commit `radarr-check/` or `status.json`.
- **Docs:** update the README wherever a behaviour it describes changes.
- **Copy text** is plain English, in the style of the existing messages.

---

### Task 1: Film import always happens, and never too early (A1, A2, A13)

**A1. A film is never imported if the disc's last encode fails or is cancelled, or after a restart.**
- `encodepool` must call `services.encode_done(encode_id)` for **failed and cancelled** encodes too, not only done ones. Today `_tell_done` is called only when `state == "done"`. `encode_done` only re-checks readiness and runs the raw sweep, so this is safe.
- Add `filmimport.sweep_ready(services) -> None`. It finds every `video_rips` row whose `main_title` is not NULL, has no `film_imports` row, and passes `ready_to_import`, then calls `maybe_start` for each. It is cheap: a single query, then `ready_to_import` per candidate.
- Call `sweep_ready`:
  - at start-up, after `resume_after_restart`;
  - in each pass of the 5-minute film confirm loop (`services._film_confirm_loop`);
  - after the Settings page saves the Radarr URL or key.

**A2. A failed extra lets the import go ahead, and its retry then lands in a deleted folder.**
- **Readiness** (`ready_to_import`) is also false when:
  - **a retry is pending** for a title of the rip: `services` keeps the auto-retry tasks, so expose `services.retry_pending(rip_id) -> bool`, which reads the jobs the pending timers will retry (store the job id → rip_id map when scheduling). Readiness takes an optional `retry_pending` callable, so it stays testable;
  - **a title failed:** the latest attempt of any title has state `failed` (`cancelled` is the owner's choice and doesn't count), **unless** the owner pressed **Import anyway**.
- **Import anyway:** migration `0010_import_anyway.sql` adds `video_rips.import_anyway INTEGER NOT NULL DEFAULT 0`. A route `POST /films/import-anyway` (form `rip_id`) sets it and calls `maybe_start`.
- **Films page:** a rip that isn't ready because a title failed shows "Title N failed. Retry it on the drive's card, or import without it." with an **Import anyway** button.
- **Clean-up guard:** before `_remove(folder)` in `_confirm_one`, refuse when the rip has any `rip_title` job queued, running or waiting for space, any encode queued or running, or `video_rips.state` in (`ripping`, `move_failed`). Log "clean-up postponed: the disc still has work" and try again next pass.

**A13. Radarr calls time out after 15 s, which fails the import on a slow array.**
- `radarr.py` gets per-call timeouts: `manual_import_view` 180 s, `lookup` and `lookup_tmdb` 60 s, everything else stays at the 15 s default.
- A timeout raises `RadarrError("Radarr took too long to answer <path>. It may be busy; press Retry.")`, distinct from "didn't answer" (connection refused).

**Tests** (in `tests/test_filmimport.py` and `tests/test_radarr.py`):
- `test_failed_last_encode_still_imports`: main encode done, then the extra's encode fails. The film imports.
- `test_cancelled_last_encode_still_imports`: the same, with the extra's encode cancelled.
- `test_sweep_ready_imports_a_stuck_rip`: a ready rip with no row. `sweep_ready` imports it; a not-ready rip is left alone.
- `test_failed_title_blocks_until_import_anyway`: a failed extra attempt means no import. After the Import anyway route, the film imports without the extra. The Films page shows the message and the button.
- `test_pending_retry_blocks_import`
- `test_cleanup_postponed_while_disc_has_work`: an imported rip with a queued `rip_title` job keeps its folder, then cleans up once the job is gone.
- `test_manual_import_view_uses_long_timeout` and `test_timeout_message`

**Commit:** `fix: film import waits for failed titles, recovers stuck rips, longer Radarr timeouts`

---

### Task 2: Ripping and drives (A3–A7)

**A3. Leftover `.partial` folders are never deleted.** At start-up, and in the hourly raw-sweep loop, remove every `downloads/<rip>/.partial/<job>/` whose job is not `queued`, `running` or `waiting_space`, unless the job is `done` and the rip is `move_failed`. That last case is the stuck file **Move again** needs. Use `video_rips.drop_partial`. Log each removal.

**A4. A re-inserted disc is ripped again after its film was imported and cleaned up.**
- `video_rips.blocking_rip` also reports a rip whose `film_imports.state` is `imported` or `in_jellyfin`, with state `"in_library"`.
- `titles.hold_reasons` / `_decide` show the reason "already in the library (<film title>)", and the card offers **Rip again**, the same flow as the existing already-ripped case.
- A rip that was **skipped** (`film_imports.state = 'skipped'`) also counts as already ripped.

**A5. Space checks still look at `/work`, but rips now write to downloads.**
- For `rip_title` jobs, the runner's space check:
  - uses the free space of `config.downloads_dir`'s filesystem, plus the free-space floor;
  - leaves rips out of the work-area budget.
- The other jobs keep `/work` and the budget. In `SpaceChecker.check`, add a parameter `path: Path | None` (the default is `work_dir`) and `use_budget: bool = True`.
- `jobs.running_bytes`, which counts against the budget, sums only non-`rip_title` jobs for the budget. The free-space check counts running rips' not-yet-written bytes, via `videorip.outstanding_bytes(..., downloads_dir)`.
- `encodepool._other_reserved` passes `downloads_dir` to `outstanding_bytes`.
- The wait message for rips reads "Waiting for space on the downloads disk: needs X GB, Y GB free (keeping Z GB spare)."
- Update the help text of "Work area budget" to say it covers CD rips and encodes, not DVD and Blu-ray rips.

**A6. There is no Hold button during the DVD's 5-minute wait.**
- The held video card shows a **Hold** button whenever a DVD wait is pending for that drive. `services.dvd_wait_pending(drive_id) -> bool` reads the task map, which is keyed by drive.
- Pressing Hold cancels the waiting task, sets `video_hold`, and rewrites the held reason to drop "rips at HH:MM".

**A7. Double-clicking Retry rips the title twice.**
- `jobs.retry` refuses, returning None, when an active job (`queued`, `running` or `waiting_space`) of the same kind already exists on that drive with the same `rip_id` and `title` for rips, or the same `label` for scans.
- `jobs.retry` also refuses when a newer job for that rip and title already exists.
- Retry buttons get `hx-disabled-elt="this"`.

**Tests:**
- `test_startup_sweeps_leftover_partials`: keeps the folders of active and move-failed jobs.
- `test_reinsert_after_import_is_held_in_library`: also covers a skipped rip.
- `test_rip_space_checked_on_downloads_not_work_budget`: a 70 GB rip with the default 60 GB budget starts when downloads has space.
- `test_rip_waits_for_downloads_space`
- `test_hold_during_dvd_wait_cancels_it`
- `test_double_retry_queues_once`
- `test_retry_refused_when_newer_job_exists`

**Commit:** `fix: sweep leftover partials, hold discs already in the library, rip space on downloads, Hold during DVD wait, no double retry`

---

### Task 3: Encoding: the queue never blocks, and raws are kept as chosen (A11, A12, A14)

**A11. An encode is failed permanently on a guessed "too long".**
- In `encoding.guard_decision`, a **rough** estimate never returns `too_long`. It returns `wait` with the message "Waiting for the next session: probably needs about N h (a rough guess until this preset has history)" when it doesn't fit the time left, and `start` in off-hours.
- With a measured estimate, `too_long` stays, but the message becomes "Too long to finish in one session (about N h). Choose a faster preset, or run it with the server on outside the schedule". No preset name.

**A12. An encode that crashes the container is re-queued forever, and a broken preset row blocks the whole queue silently.**
- **Attempt limit:** migration `0011_encode_attempts.sql` adds `encodes.attempts INTEGER NOT NULL DEFAULT 0`. `encodes.start` increments it. `requeue_interrupted` leaves an encode `interrupted`, with the message "Interrupted twice; press Retry to try again", once `attempts >= 2`. Retry creates a fresh row with `attempts` 0, as today.
- **Bad preset:** in the pool, if `presets.for_encode` / `estimate_for` raises `PresetError` (or any `ValueError`) for the head row, finish that encode as `failed` with "Its preset settings are no longer valid: <message>. Edit the preset, then Retry." and carry on with the next row.
- `presets.list_presets` skips (and logs) rows that don't validate, so the Presets, Films and dashboard pages still load.
- `films_routes._active_rows` treats an estimate error as no estimate.

**A14. Keep raws for N hours, or until the film is in Jellyfin.**
- **New setting** `raw_keep_until` (choice, group "Encoding", label "Keep raw rips"), with the choices:
  - `hours`: "for the hours below after a good encode" (the default);
  - `jellyfin`: "until the film is in Jellyfin".
- The existing `raw_keep_hours` keeps its meaning and its label "Keep raw rips for", with help text "Used when Keep raw rips is set to hours. 0 deletes the raw as soon as the encode passes its check."
- **The sweep** (`services._sweep_raw` / `_raw_checks`) with `jellyfin` mode:
  - **Film rips** (`main_title` not NULL, Radarr set up): the raw sweep never deletes a raw. The Jellyfin clean-up already deletes the whole folder once the film shows, and that is the moment.
  - **Rips that will never import** (TV-like rips, `film_imports.state` = `tv_skipped`, or Radarr not set up): fall back to `raw_keep_hours`.
  - **Delete raw now** still works in either mode.
- **Subtitle shortfall:** whatever the mode, the sweep never deletes the raw of a title whose newest done encode has a message containing "subtitle tracks" (the HandBrake shortfall note). The Films row shows "raw kept: the encode lost subtitles", and **Delete raw now** stays available.
- **Films page countdown:** in `jellyfin` mode it says "raw kept until the film is in Jellyfin".

**Tests:**
- `test_rough_too_long_waits_not_fails`
- `test_measured_too_long_message_has_no_preset_name`
- `test_interrupted_twice_stays_interrupted`
- `test_invalid_preset_fails_that_encode_and_queue_moves_on`
- `test_list_presets_skips_invalid_row`
- `test_jellyfin_mode_keeps_film_raws`
- `test_jellyfin_mode_falls_back_to_hours_for_tv`
- `test_subtitle_shortfall_keeps_raw`
- `test_settings_raw_keep_until_choice`

**Commit:** `fix: rough estimates hold instead of failing, encodes give up after two crashes, bad presets can't block the queue; raws kept N hours or until in Jellyfin`

---

### Task 4: Crash-proofing (A8, A9, A10)

**A8. Make a crash diagnosable and survivable.**
- **Restart policy:** `unraid/disc-ripper.xml` ExtraParams gains `--restart=unless-stopped`. The README's install section says to add it to an existing install by hand: Advanced View → Extra Parameters.
- **Crash trace:** the Dockerfile gets `ENV PYTHONFAULTHANDLER=1`.
- **App log file:**
  - `services._configure_logging` also adds a `RotatingFileHandler` at `/config/logs/app.log` (5 files × 2 MB, same format), created if missing. A failure to open it logs a warning and carries on.
  - The start-up line logs the app version: the `disc_ripper` package version, and `GIT_SHA` from the env if the image sets it.
  - The Dockerfile takes `ARG GIT_SHA` → `ENV GIT_SHA`, and CI passes `--build-arg GIT_SHA=${{ github.sha }}` to both builds.
- **Guarded start-up:** in `Services.start`, each reconciliation step runs in its own try/except that logs the exception and carries on. That covers `mark_interrupted`, `requeue_interrupted`, `fill_fingerprints`, `cd_rips.reconcile`, each `video_rips.settle` iteration, `sweep_work`, the partial sweep and `resume_after_restart`. The web UI and the loops must always start.
- **Entrypoint:** in `docker/entrypoint.sh`, a failure of the device-node step (`python -m disc_ripper.devnodes`) logs "device setup failed; continuing without optical drives" and continues, instead of exiting under `set -e`.

**A9. Pin the Python dependencies.**
- Add `constraints.txt` at the repo root with exact versions:
  - `fastapi==0.163.1`, `starlette==1.10.0`, `uvicorn==0.54.0`, `h11==0.16.0`, `anyio==4.16.0`, `idna==3.20`, `click==8.5.1`
  - `jinja2==3.1.6`, `markupsafe==3.0.4`, `itsdangerous==2.2.0`, `python-multipart==0.0.34`, `mutagen==1.48.1`
  - `pydantic==2.14.3` and `pydantic-core` / `annotated-types` / `typing-extensions` / `typing-inspection` / `sniffio` at the versions `.venv` has. Check with `.venv/Scripts/python -m pip freeze`.
- `pyproject.toml` changes `uvicorn[standard]>=0.32` to plain `uvicorn>=0.32`, which drops uvloop, httptools, watchfiles and websockets, the C extensions. The app uses SSE, not websockets; confirm nothing imports `websockets`.
- The Dockerfile installs with `pip install -c constraints.txt .`
- **Verify:** the full suite passes, and CI's image build passes.

**A10. Back up the database before an upgrade changes it.**
- In `db.migrate`, when there are pending migrations and the DB file exists and is non-empty:
  - first copy it with SQLite's backup API (`conn.backup`) to `<config>/backups/disc-ripper-v<old>.db`, creating the folder;
  - keep the newest 5 backups and delete older ones.
- `migrate` gets the DB path from `conn` via `PRAGMA database_list`, so the signature doesn't change. Skip `:memory:`.
- **README:** a "Going back to an older version" section. Set the template's Repository to `ghcr.io/<owner>/disc-ripper:sha-<commit>`, stop the container, and copy the matching backup over `disc-ripper.db` (deleting the `-wal` and `-shm` files).

**Tests:**
- `test_startup_survives_a_failing_step`: monkeypatch `cd_rips.reconcile` to raise; `Services.start` still starts its loops.
- `test_app_log_file_written`
- `test_migrate_backs_up_before_upgrading`: keeps 5.
- `test_migrate_no_backup_when_up_to_date`
- `test_no_websockets_import`: grep `src/`.

**Commit:** `fix: restart policy, crash trace and app log, guarded start-up, pinned deps, DB backup before migrations`
