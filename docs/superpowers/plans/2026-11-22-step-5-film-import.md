# Step 5: Film Identification and Import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a film's main title has a done encode, identify it in Radarr and import the encode. Confident new films import on their own, and replaces wait for the owner by default. Folders are tidied, extras placed and the rip cleaned up once Jellyfin shows the film, with Undo throughout.

**Architecture:**
- **Pure modules:**
  - `radarr.py`: an HTTP client with an injected fetcher, like `jellyfin.py`.
  - `filmid.py`: label cleaning, scoring, confidence, ID parsing, quality and folder names.
  - `film_imports.py`: the `film_imports` table.
- **The flow:** `filmimport.py` runs in a service background task (no drive job) as resumable steps recorded on the row. Services triggers it when a main title's encode is done.
- **The Films page** gains the import UI.

**Tech Stack:** Python 3.12, urllib, SQLite, FastAPI/Jinja/htmx (existing); Radarr v3 API; Jellyfin API.

**Spec:** `docs/superpowers/specs/2026-11-22-step-5-film-import-design.md`

## Global Constraints

- **Films only.** A rip whose `main_title` is NULL (TV-like) is marked `tv_skipped` and never imported.
- **Settings group "Radarr":** `radarr_url` (url, ""), `radarr_api_key` (secret, ""), `ask_before_replace` (bool, **true**), `tidy_folders_on_replace` (bool, **true**). Without a URL and key, nothing is identified; the Films page says "Set up Radarr in Settings to import films."
- **Confident** = the top candidate's title similarity ≥ **0.9** and its runtime within **max(5 min, 6%)** of the main title, and no other candidate also meets both. A candidate with no runtime is never confident.
- **New films:** added `monitored: false`, `addOptions.searchForMovie: false`, root folder named **Movies** (matched by the last path component), quality profile = the first profile Radarr lists.
- **Replace safety checks:**
  - new length within **max(2%, 5 min)** of the old file's;
  - Radarr's recycle bin is set;
  - the new resolution is **not lower** than the old file's.
  
  With `ask_before_replace` on, or any check failing, the rip waits. Without a recycle bin, Replace needs the typed word `REPLACE`.
- **Quality names for ManualImport:** DVD → `DVD`; Blu-ray standard or fast → `Bluray-1080p`; Blu-ray copy → `Remux-1080p`. The `quality` object comes from `GET /api/v3/qualitydefinition`, matched by name.
- **`importMode: "move"`.** Poll the command until done (timeout **10 min**), then confirm that `movieFile.path` is the new file.
- **Folder tidy** (replace only, setting on): target folder `{title} ({year})`, using Radarr's lookup `folder` field when present.
- **The film is always left unmonitored.**
- **Jellyfin "seen"** = an item with the film's TMDb provider ID whose media source path **and** size equal the imported file's. Poll every **5 min**, up to **24 h**. Without Jellyfin configured, clean up 24 h after the import.
- **Clean-up** deletes the rip's downloads folder (raw, encodes, extras) and nothing else.
- **One import per TMDb ID at a time.** A second rip of the same film waits ("Another rip of this film is being imported"), then follows the replace rule.
- **The raw-deletion sweep skips** any rip with a `film_imports` row in `importing` or `imported`.
- **Logs:** `/config/logs/import-<rip_id>.log`, holding every Radarr call (method, path, status) and never the API key.
- **No new pip dependencies.** Untrusted text is escaped, and paths come only from Radarr or the app's own records.

## Review Focus

1. **The same title for different films** (a remake, e.g. "The Lion King" 1994 vs 2019) must not auto-import the wrong one. The runtime decides, and a tie waits. Task 2: `test_remake_with_different_runtime_is_resolved_by_runtime`, `test_remake_with_similar_runtime_waits`.
2. **Radarr unreachable or a wrong API key in the middle of an import** must leave the rip `failed` with a readable reason and Retry, and never half-imported without the owner knowing. Task 5: `test_radarr_down_mid_import_fails_and_retry_resumes`.
3. **Undo after the old file has been purged from Radarr's recycle bin** must say so and return only the new file. Task 6: `test_undo_replace_when_bin_emptied`.
4. **A replace that would lower the resolution** (a DVD over a 1080p file) must wait even with Ask before replacing off. Task 6: `test_downgrade_always_waits`.
5. **Undo pressed twice, or Retry pressed during an import,** must not run two flows at once. Task 5: `test_one_flow_per_rip_at_a_time`.

---

## File map

| File | Responsibility |
| --- | --- |
| `migrations/0007_film_imports.sql` | `film_imports` table; `encodes.imported_path`, `encodes.imported_at` |
| `settings.py` | the Radarr settings |
| `film_imports.py` | `film_imports` table access, the per-TMDb lock |
| `radarr.py` | Radarr v3 client (injected fetcher, `RadarrError`) |
| `filmid.py` | pure: clean label, normalise title, score, confident pick, parse ID/URL, quality name, folder name |
| `jellyfin.py` | `movie_sources(tmdb_id) -> list[(path, size)]` |
| `filmimport.py` | identify, import steps, replace, tidy, extras, Undo, Skip, Jellyfin confirm and clean-up |
| `services.py` | trigger on encode done; background loop; raw-sweep exclusion; actions |
| `web/films_routes.py`, `templates/_films_list.html` (+ new `_film_import.html`) | import state, Identify form, Replace comparison, Recently imported, Undo, logs |
| `tests/fixtures/radarr/*.json` | recorded or synthetic Radarr responses |

---

### Task 1: Schema, settings and the film_imports table

**Files:** Create `src/disc_ripper/migrations/0007_film_imports.sql`, `src/disc_ripper/film_imports.py`. Modify `src/disc_ripper/settings.py`, and the `VALID_FORM` dicts in `tests/test_settings.py` and `tests/test_web_pages.py`. Test: `tests/test_film_imports.py`, `tests/test_db.py`.

**Interfaces:**
- Table `film_imports(rip_id INTEGER PRIMARY KEY, state TEXT NOT NULL CHECK (state IN ('identifying','needs_identify','waiting_other','needs_replace_ok','importing','imported','in_jellyfin','failed','undone','skipped','tv_skipped')), step TEXT NOT NULL DEFAULT '', candidates TEXT NOT NULL DEFAULT '[]', tmdb_id INTEGER, movie_id INTEGER, title TEXT, year INTEGER, root_folder TEXT, encode_id INTEGER, replace TEXT NOT NULL DEFAULT '{}', old_path TEXT, old_folder TEXT, recycled_path TEXT, new_path TEXT, extras TEXT NOT NULL DEFAULT '[]', message TEXT NOT NULL DEFAULT '', error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, imported_at TEXT)`, with an index on `(tmdb_id, state)`.
- `ALTER TABLE encodes ADD COLUMN imported_path TEXT; ALTER TABLE encodes ADD COLUMN imported_at TEXT;`
- `film_imports.py` produces:
  - `get(conn, rip_id)`;
  - `ensure(conn, rip_id, now) -> Row`, which inserts `identifying` when the rip has no row;
  - `update(conn, rip_id, now, **fields)` (JSON fields passed as Python objects);
  - `claim(conn, rip_id, tmdb_id, now) -> bool`: in `BEGIN IMMEDIATE`, False when another rip with that `tmdb_id` is `importing`; otherwise sets this row `importing` with the TMDb ID;
  - `recent(conn, n=20)`, rows in `imported`/`in_jellyfin`, newest `imported_at` first;
  - `waiting_for(conn, tmdb_id)`, rows in `waiting_other` for that film;
  - `active_rip_ids(conn) -> set[int]`, rows in `importing`/`imported`.

- [ ] **Step 1: Write the failing tests.**
  - `test_migration_0007`;
  - `test_radarr_settings_defaults`: the defaults above, and the key is a secret;
  - `test_claim_one_per_film`;
  - `test_recent_and_active`.
  
  Also bump the migration-count asserts from 6 to 7.
- [ ] **Step 2: Run them.** `pytest tests/test_film_imports.py tests/test_db.py tests/test_settings.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full suite.** `pytest -q`. Expected: everything passes.
- [ ] **Step 5: Commit.** `git commit -m "feat: film_imports table and Radarr settings"`

### Task 2: Identification rules (pure)

**Files:** Create `src/disc_ripper/filmid.py`. Test: `tests/test_filmid.py`.

**Interfaces:**
- `clean_label(label: str) -> str`:
  - `"THE_LEGO_MOVIE"` → `"The Lego Movie"`;
  - `"MADAGASCAR_DISC_1"` → `"Madagascar"`;
  - `"FM1"` → `"Fm1"`;
  - `"SHREK_2_WS"` → `"Shrek 2"`.
  
  It drops `DISC_?\d+`, `D\d` at the end, `WS`, `FS` and region tokens (`PAL`, `NTSC`, `R2`, `UK`, `US`).
- `normalise(title: str) -> str`: lowercase, with accents stripped, punctuation removed, a leading "the "/"a "/"an " removed, and spaces collapsed.
- `@dataclass(frozen=True) class Candidate: tmdb_id: int; title: str; year: int | None; runtime_min: int | None; poster: str; in_library: bool; folder: str`.
- `@dataclass(frozen=True) class Scored: candidate: Candidate; title_score: float; runtime_ok: bool`.
- `score(candidates, names: list[str], main_seconds: int) -> list[Scored]`:
  - `title_score` is the best `difflib.SequenceMatcher` ratio of `normalise` over the names;
  - the list is sorted by title_score descending, then by runtime closeness.
- `confident(scored) -> Candidate | None`, per Global Constraints.
- `parse_ref(text) -> tuple[str, str] | None`:
  - `("imdb", "tt0319343")` from `tt0319343` or an IMDb URL;
  - `("tmdb", "10719")` from a bare number or a `themoviedb.org/movie/10719-elf` URL;
  - None otherwise.
- `quality_name(disc_type, preset) -> str`, per Global Constraints.
- `folder_name(candidate) -> str`: `candidate.folder`, else `f"{title} ({year})"` with characters `\/:*?"<>|` removed.

- [ ] **Step 1: Write the failing tests.**
  - `test_clean_label_cases` (parametrised with the cases above);
  - `test_normalise`;
  - `test_confident_exact_title_and_runtime` (The Lego Movie: 6035 s vs a 100 min candidate gives confident);
  - `test_remake_with_different_runtime_is_resolved_by_runtime` (two "The Lion King" candidates, 89 min and 118 min; the main title is 5340 s, so the 89 min one wins);
  - `test_remake_with_similar_runtime_waits` (88 and 90 min, so None);
  - `test_no_runtime_never_confident`;
  - `test_parse_ref`;
  - `test_quality_name`;
  - `test_folder_name`.
- [ ] **Step 2: Run them.** `pytest tests/test_filmid.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full suite.** `pytest -q`. Expected: everything passes.
- [ ] **Step 5: Commit.** `git commit -m "feat: film identification rules"`

### Task 3: The Radarr client

**Files:** Create `src/disc_ripper/radarr.py`, `tests/fixtures/radarr/` (synthetic JSON shaped like Radarr v3: `lookup_lego.json`, `movie_elf_with_file.json`, `rootfolder.json`, `qualityprofile.json`, `qualitydefinition.json`, `manualimport.json`, `command_started.json`, `command_completed.json`, `command_failed.json`, `mediamanagement.json`). Test: `tests/test_radarr.py`.

**Interfaces:**
- `class RadarrError(Exception)`, with a readable message: "Radarr refused the API key", "Radarr didn't answer at <url>", or "Radarr: <status> <message from body>".
- `class Radarr(url, api_key, fetch=http_request)`. The fetcher signature is `(method, url, headers, body: bytes | None, timeout) -> bytes`, and the `X-Api-Key` header is set. Methods return parsed JSON:
  - `lookup(term) -> list[dict]`, `lookup_tmdb(tmdb_id) -> dict`, `lookup_imdb(imdb_id) -> dict`;
  - `movie_by_tmdb(tmdb_id) -> dict | None`;
  - `movie(movie_id) -> dict`;
  - `add_movie(lookup: dict, root_folder: str, profile_id: int) -> dict`;
  - `update_movie(movie: dict, move_files: bool = False) -> dict`;
  - `delete_movie(movie_id, delete_files=False)`;
  - `root_folders() -> list[dict]`, `quality_profiles() -> list[dict]`, `quality_definitions() -> list[dict]`;
  - `manual_import_view(folder) -> list[dict]`;
  - `command(name, **body) -> int`, `command_status(id) -> str` ("queued"/"started"/"completed"/"failed");
  - `recycle_bin() -> str` (empty string when unset).
- `to_candidate(lookup: dict) -> filmid.Candidate`, using `tmdbId`, `title`, `year`, `runtime`, `remotePoster` and `folder`; `in_library` = has an `id`.
- Never log or embed the key in error text.

- [ ] **Step 1: Write the failing tests.** Use a fake fetcher that records calls and serves fixtures:
  - `test_lookup_and_candidates`;
  - `test_add_movie_payload` (monitored false, no search, the root folder and profile);
  - `test_update_movie_move_files_query`;
  - `test_command_and_status`;
  - `test_recycle_bin`;
  - `test_errors_are_readable` (401, a connection error, a 400 with a body message; the key not in the message);
  - `test_api_key_header`.
- [ ] **Step 2: Run them.** `pytest tests/test_radarr.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full suite.** `pytest -q`. Expected: everything passes.
- [ ] **Step 5: Commit.** `git commit -m "feat: Radarr client"`

### Task 4: Jellyfin movie lookup

**Files:** Modify `src/disc_ripper/jellyfin.py`. Test: `tests/test_jellyfin.py` (or the existing Jellyfin tests' file).

**Interfaces:** `Jellyfin.movie_sources(tmdb_id: int) -> list[tuple[str, int]]` calls `GET /Items?Recursive=true&IncludeItemTypes=Movie&AnyProviderIdEquals=tmdb.<id>&Fields=MediaSources,Path` and returns `(MediaSources[].Path, MediaSources[].Size)` for each source. It raises `JellyfinError` on failure.

- [ ] **Step 1: Write the failing test.** `test_movie_sources` (fixture JSON with two sources).
- [ ] **Step 2: Run it.** `pytest tests/test_jellyfin.py -q` (or the actual file name). Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full suite.** `pytest -q`. Expected: everything passes.
- [ ] **Step 5: Commit.** `git commit -m "feat: Jellyfin movie sources by TMDb ID"`

### Task 5: Identify and import a new film

**Files:** Create `src/disc_ripper/filmimport.py`, `tests/fake_radarr.py` (an in-memory Radarr with the same methods as `Radarr`, holding movies, files, root folders, a recycle bin folder on disk and commands; `ManualImport` moves the file into `<root>/<folder>/` and updates `movieFile`). Modify `src/disc_ripper/services.py`. Test: `tests/test_filmimport.py`.

**Interfaces:**
- `services.radarr() -> Radarr | None` (from settings, like `services.jellyfin()`).
- `async run_import(services, rip_id) -> None` advances the row through these steps, recording `step` on the row and logging each:
  1. **Identify:**
     - skip when `main_title` is NULL (`tv_skipped`);
     - names from the scan's `label` (MakeMKV's disc name) plus `clean_label(video_discs.label)`;
     - `lookup` each name and score;
     - if confident, set tmdb/title/year/root `Movies` and continue; otherwise `needs_identify` with the top 5 candidates.
  2. **Claim:** `film_imports.claim`; if it's taken, `waiting_other`.
  3. **Find or add:** `movie_by_tmdb`, else `add_movie`.
  4. **Replace check:** Task 6. In this task a movie without a file goes straight on.
  5. **Pick the encode:** the most recent done, intact encode of the main title; none gives `failed` "No finished encode of the film."
  6. **ManualImport:**
     - `manual_import_view(folder of the encode)`;
     - `command("ManualImport", files=[{path, movieId, quality, languages}], importMode="move")`;
     - poll `command_status` every 5 s up to 10 min;
     - confirm `movie(id).movieFile.path`;
     - set `encodes.imported_path` and `imported_at`.
  7. **Unmonitor:** `update_movie` with monitored false.
  8. **Extras:** copy each extra's best done encode (else the raw) into `<movie folder>/extras/`, never overwriting; record the paths.
  9. **Done:** `imported`, `imported_at`, `new_path`; ask Jellyfin to refresh.
- Errors raise inside a step and become `failed` with the message and the step kept. `retry_import(services, rip_id)` resumes from the recorded step. Steps 3, 6 and 7 first check Radarr's state, so they're safe to repeat.
- **One flow per rip at a time:** an `asyncio.Lock` per rip in services. Actions arriving while a flow runs return "Busy, try again in a moment."
- **Trigger:** in `EncodePool`'s finish path (`done`), call `services.encode_done(encode_id)`, which starts `run_import` for that rip when the encode is the main title's and no import row exists, or the row is `identifying`.
- `choose_film(services, rip_id, ref_or_tmdb, root_folder="Movies")` (from the Identify form) sets the film and runs from step 2.

- [ ] **Step 1: Write the failing tests** with the fake Radarr and the step 6 test helpers for rips and encodes:
  - `test_confident_new_film_imports`: the file is in `<Movies>/The Lego Movie (2014)/`, the movie is unmonitored, the row is `imported`, `encodes.imported_path` is set, and the log has no API key;
  - `test_unsure_waits_with_candidates`;
  - `test_tv_rip_skipped`;
  - `test_choose_film_by_tmdb_id`;
  - `test_second_rip_of_same_film_waits`;
  - `test_radarr_down_mid_import_fails_and_retry_resumes` (fail on ManualImport, then Retry succeeds without re-adding the movie);
  - `test_one_flow_per_rip_at_a_time`;
  - `test_extras_copied_into_extras_folder`;
  - `test_encode_done_triggers_import`;
  - `test_no_radarr_configured_does_nothing`.
- [ ] **Step 2: Run them.** `pytest tests/test_filmimport.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full suite.** `pytest -q`. Expected: everything passes.
- [ ] **Step 5: Commit.** `git commit -m "feat: identify films and import new ones into Radarr"`

### Task 6: Replace, folder tidy and Undo

**Files:** Modify `src/disc_ripper/filmimport.py`, `tests/fake_radarr.py`. Test: `tests/test_filmimport_replace.py`.

**Interfaces:**
- **Replace check** (step 4):
  - old details from `movie.movieFile` (path, size, `mediaInfo.resolution`, video codec, audio codec/channels, runtime);
  - new details from ffprobe (`videofiles.PROBE`) on the picked encode;
  - the checks per Global Constraints, stored in `replace` as `{"old": …, "new": …, "checks": {"length": bool, "recycle_bin": bool, "resolution": bool}}`;
  - wait (`needs_replace_ok`) when `ask_before_replace` is on or any check fails; otherwise continue.
- `approve_replace(services, rip_id, typed: str = "")`: refused while the recycle bin is unset unless `typed == "REPLACE"`; continues from step 5.
- **Recording the recycle bin:** before ManualImport, list the files under `<recycle_bin>`. After it, find the new arrival whose name equals the old file's name, and store `recycled_path`, `old_path` and `old_folder`.
- **Folder tidy** (after unmonitor, when replacing and the setting is on): when the movie folder's name != `filmid.folder_name(candidate)`, `update_movie(movie | {"path": <root>/<folder_name>}, move_files=True)`, then confirm that `movie(id).path` changed and the file exists there.
- `undo(services, rip_id)`:
  - **new film:** move `new_path` back to `downloads/<rip_id>/`, `delete_movie(id, delete_files=False)`, remove the empty movie folder;
  - **replace:** move the new file back; if the folder was tidied, move it back to `old_folder` via `update_movie(move_files=True)`; move `recycled_path` back to `old_path`; `command("RescanMovie", movieId=id)`;
  - a missing `recycled_path` gives the message "The old file is no longer in Radarr's recycle bin, so only the new file was taken back.";
  - remove extras copied into the library;
  - clear `encodes.imported_path`;
  - the row becomes `undone`, then `needs_identify` with its candidates kept.
- `skip(services, rip_id)` deletes `downloads/<rip_id>/` and sets the row `skipped`.

- [ ] **Step 1: Write the failing tests.**
  - `test_replace_waits_when_ask_on`;
  - `test_replace_auto_when_ask_off_and_checks_pass`;
  - `test_length_mismatch_waits`;
  - `test_downgrade_always_waits`;
  - `test_no_recycle_bin_needs_typed_replace`;
  - `test_folder_tidied_after_replace` (from `Elf` to `Elf (2003)`);
  - `test_tidy_off_keeps_folder`;
  - `test_undo_new_film`;
  - `test_undo_replace_restores_old_file_and_folder`;
  - `test_undo_replace_when_bin_emptied`;
  - `test_skip_deletes_rip_folder`.
- [ ] **Step 2: Run them.** `pytest tests/test_filmimport_replace.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full suite.** `pytest -q`. Expected: everything passes.
- [ ] **Step 5: Commit.** `git commit -m "feat: replace existing films with safety checks, folder tidy and Undo"`

### Task 7: Jellyfin confirmation, clean-up and the services loop

**Files:** Modify `src/disc_ripper/filmimport.py`, `src/disc_ripper/services.py` (the raw-sweep exclusion). Test: `tests/test_filmimport_cleanup.py`.

**Interfaces:**
- `confirm_and_clean(services, now) -> None`: for every `imported` row:
  - if Jellyfin is configured and `movie_sources(tmdb_id)` contains `(new_path, size)`, delete `downloads/<rip_id>/` and set `in_jellyfin`;
  - without Jellyfin, do the same 24 h after `imported_at`;
  - after 24 h with Jellyfin configured but the film not seen, leave the row with message "Jellyfin hasn't shown this film after 24 hours; the rip is kept. Check the library." No deletion.
- A services loop runs `confirm_and_clean` every 300 s and once at start. At start-up, rows left in `importing` (crash) become `failed` with "Interrupted by a restart; press Retry."
- The raw-deletion sweep and Delete raw now refuse for rips in `film_imports.active_rip_ids`: "This rip is being imported or was imported; it's cleaned up once Jellyfin has the film."

- [ ] **Step 1: Write the failing tests.**
  - `test_cleanup_after_jellyfin_shows_it`;
  - `test_same_path_but_old_size_is_not_seen`;
  - `test_no_jellyfin_cleans_after_24h`;
  - `test_not_seen_after_24h_keeps_rip_with_message`;
  - `test_restart_marks_importing_failed`;
  - `test_raw_sweep_skips_imported_rips`.
- [ ] **Step 2: Run them.** `pytest tests/test_filmimport_cleanup.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full suite.** `pytest -q`. Expected: everything passes.
- [ ] **Step 5: Commit.** `git commit -m "feat: clean up imported rips once Jellyfin has the film"`

### Task 8: Films page: import state, Identify, Replace, Recently imported

**Files:** Create `src/disc_ripper/templates/_film_import.html`. Modify `src/disc_ripper/web/films_routes.py`, `src/disc_ripper/templates/_films_list.html`, `src/disc_ripper/static/app.css`. Test: `tests/test_web_films_import.py`.

**Interfaces:**
- Routes (all return the re-rendered list like the existing Films actions; refusals show their reason):
  - `POST /films/identify/search` (`rip_id`, `q`) re-renders that rip's Identify results using `radarr.lookup`, or `parse_ref` for IDs/URLs;
  - `POST /films/identify/choose` (`rip_id`, `tmdb_id`, `root`);
  - `POST /films/replace` (`rip_id`, `typed`);
  - `POST /films/skip` (`rip_id`, with `hx-confirm="Delete this rip? Its files in downloads will be removed."`);
  - `POST /films/undo` (`rip_id`, with `hx-confirm="Undo this import?"`);
  - `POST /films/import/retry` (`rip_id`);
  - `GET /films/import/<rip_id>/log` (plain text).
- **Page:**
  - a **Recently imported** section at the top (title, year, old vs new when replaced, Undo);
  - under each rip, its import state per the spec's wording, including the Replace comparison table (Resolution, Video, Audio, Size, Length; old vs new, with failed checks in red and their reason) and the Identify form (search box, results with poster `<img loading="lazy">`, title, year, runtime, "in your library", a root select of Movies/Other movies, **Use this film**);
  - "Set up Radarr in Settings to import films." when Radarr isn't configured.

- [ ] **Step 1: Write the failing tests.**
  - `test_waiting_rip_shows_identify_form_and_candidates`;
  - `test_search_and_choose`;
  - `test_paste_imdb_id`;
  - `test_replace_comparison_and_button`;
  - `test_replace_typed_confirm_without_recycle_bin`;
  - `test_recently_imported_with_undo`;
  - `test_failed_import_shows_reason_retry_and_log`;
  - `test_radarr_not_configured_message`;
  - `test_import_text_escaped`.
- [ ] **Step 2: Run them.** `pytest tests/test_web_films_import.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full suite.** `pytest -q`. Expected: everything passes.
- [ ] **Step 5: Commit.** `git commit -m "feat: Films page import, Identify, Replace and Undo"`

### Task 9: README and verification

**Files:** Modify `README.md`.

- [ ] **Step 1: Write the README "Importing films" section.** Cover:
  - the Radarr settings (where to find the API key);
  - auto-import vs waiting;
  - Replace and its safety checks;
  - Undo, Skip and the folder tidy;
  - clean-up after Jellyfin;
  - a "Testing step 5 on the server" list, from the spec's manual tests.

  The list starts with "open Radarr's Swagger page (`<radarr>/api/v3/…` docs) and check the endpoints in the design's Constraints". It also includes "turn on Radarr's recycle bin before replacing".
- [ ] **Step 2: Verify.** Run `pytest -q`, `docker build -t disc-ripper:dev .` and the CI smoke checks. Expected: all green.
- [ ] **Step 3: Commit.** `git commit -m "docs: step 5 README; verification"`
