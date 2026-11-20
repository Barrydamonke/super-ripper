# Step 3: Audio CD Ripping Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Insert an audio CD and it rips to verified, tagged FLAC. A clean match files itself into a tidy music library; anything odd waits on a Music page for a decision.

**Architecture:**
- cyanrip runs as a `rip_cd` job subprocess with its own lookups off. It rips, checks AccurateRip and computes ReplayGain.
- The app computes the MusicBrainz disc ID from the TOC, looks up candidates, writes Vorbis tags with mutagen, fetches cover art, decides "file automatically" or "needs a look", and files into `/data/MEDIA/Music`.
- Pure logic (disc ID, candidate grouping, tag mapping, naming, decisions) lives in small modules tested on fixtures. I/O is injected.

**Tech Stack:**
- Python 3.12 on `python:3.12-slim-trixie`
- cyanrip 0.9.3 (Debian trixie)
- mutagen
- FastAPI/Jinja/htmx (existing)
- MusicBrainz WS/2 JSON API, Cover Art Archive, Jellyfin API

**Spec:** `docs/superpowers/specs/2026-11-19-step-3-audio-cd-design.md`

## Global Constraints

- **New runtime dependency:** `mutagen` only. HTTP keeps using `urllib.request` in a thread.
- **MusicBrainz:**
  - at most 1 request per second;
  - User-Agent `super-ripper/<version> ( https://github.com/Barrydamonke/super-ripper )`;
  - never any personal contact details.
- **Subprocesses use fixed argument lists, never a shell.** Disc and tag text never goes into an argument; for cyanrip, only the device path, numbers from settings or the drive row, and fixed strings.
- **Untrusted text:**
  - tags and titles are escaped in templates;
  - path components are sanitised by `library.safe_component` (Task 4);
  - nothing ever overwrites an existing library file.
- **Files** are written as 99:100 with umask 0002 (the process already runs that way).
- **CD rip jobs** are `kind="rip_cd"`, `uses_drive=True`, so they hold `/api/busy` and the poller skips the drive.
- **Disc IDs** are MusicBrainz disc IDs (28 characters, MusicBrainz base64 alphabet).
- **Library root:** new config `MUSIC_DIR`, default `/data/MEDIA/Music`. Downloads root: new config `DOWNLOADS_DIR`, default `/data/downloads/disc-ripper`.
- **Tests** pass on Windows (Linux-only bits skipped or faked) and on Linux (`python:3.12-slim-trixie`).

## Review Focus

1. **A disc whose MusicBrainz data has odd characters** (`AC/DC`, `Sigur Rós`, a title ending in `.`, a 300-character classical title) must give valid, distinct paths, with the tags keeping the original text. Task 4: `test_safe_component_cases`; Task 6: `test_file_album_unicode_and_slash`.
2. **The second disc arriving while the first is still on Needs a look** (not filed yet) must not create a second folder later. It should go to Needs a look with "disc N of an album waiting for a look". Task 8: `test_second_disc_while_first_waiting`.
3. **MusicBrainz returning HTTP 503 or 429 (rate limited)** must retry with backoff and then end as "MusicBrainz unreachable", never as a crash or a filed album without tags. Task 5: `test_client_retries_503_then_gives_up`.
4. **cyanrip killed mid-rip, or the disc swapped before Retry**, must never mix two discs' files or tag disc B with disc A's data. Task 7: `test_retry_refuses_different_disc`; Task 7: `test_retry_uses_fresh_work_folder`.
5. **A Jellyfin URL or key that's set but wrong** must not block filing or the duplicate check. The Music page shows the error. Task 8: `test_jellyfin_errors_do_not_block_filing`.

---

## File map

| File | Responsibility |
| --- | --- |
| `Dockerfile`, `.github/workflows/build.yml`, `pyproject.toml` | trixie base, cyanrip, mutagen |
| `src/disc_ripper/migrations/0003_audio_cd.sql` | `drives.read_offset`, `discs`, `cd_rips`, `albums`, `album_releases` |
| `src/disc_ripper/settings.py` | `choice` and `secret` kinds; CD and Jellyfin settings |
| `src/disc_ripper/config.py` | `music_dir`, `downloads_dir` |
| `src/disc_ripper/cdtoc.py` | TOC read (ioctls) and MusicBrainz disc ID (pure) |
| `src/disc_ripper/library.py` | path naming and sanitising (pure) |
| `src/disc_ripper/musicbrainz.py` | rate-limited client, candidates, tag mapping |
| `src/disc_ripper/coverart.py` | Cover Art Archive fetch |
| `src/disc_ripper/tagging.py` | mutagen read/write, keeping ReplayGain |
| `src/disc_ripper/filing.py` | move into the library, `albums` rows, joining, no overwrite |
| `src/disc_ripper/cyanrip.py` | command line, stdout progress parser, log AccurateRip parser |
| `src/disc_ripper/cdrip.py` | `rip_cd` handler and the post-rip pipeline (identify, decide, file) |
| `src/disc_ripper/cd_rips.py` | `cd_rips` table access |
| `src/disc_ripper/jellyfin.py` | duplicate query and debounced refresh |
| `src/disc_ripper/runner.py` | child-process registration and kill on cancel/stop |
| `src/disc_ripper/web/music_routes.py`, `templates/music*.html` | Music page, item page, resolve actions |
| `src/disc_ripper/web/dashboard_routes.py`, `templates/_drive_card.html` | Rip / Rip again buttons, CD progress line |
| `tests/fixtures/mb/*.json`, `tests/fixtures/cyanrip/*` | recorded MusicBrainz responses, fake cyanrip output |

---

### Task 1: Debian trixie base with cyanrip and mutagen

**Files:** Modify `Dockerfile`, `pyproject.toml`, `.github/workflows/build.yml`.

**Interfaces:** Produces an image with `cyanrip` (0.9.3) and `mutagen` importable. No code changes.

- [ ] **Step 1:** `Dockerfile`: change `FROM python:3.12-slim-bookworm` to `FROM python:3.12-slim-trixie`, and add `cyanrip` to the `apt-get install` line. In `pyproject.toml`, add `"mutagen>=1.47"` to `dependencies`. In the CI test job, nothing changes (it runs on the runner's Python).
- [ ] **Step 2:** Extend the CI smoke test with `docker exec smoke setpriv --reuid=99 --regid=100 --clear-groups cyanrip -V`. Expected: the output starts with `cyanrip 0.9.3`.
- [ ] **Step 3: Verify locally.**
  - Run `docker build -t disc-ripper:s3 .` and the existing smoke checks (healthz 200; PID 1 is `tini`; the app uid is 99).
  - Run `docker run --rm --entrypoint sh disc-ripper:s3 -c "python -c 'import mutagen; print(mutagen.version_string)'; cyanrip -V"`. Expected: a version line from each.
  - Run the full pytest suite in `python:3.12-slim-trixie` (as in step 2's Task 13). Expected: all pass.
- [ ] **Step 4: Commit** `build: move to Debian trixie for cyanrip; add mutagen`

---

### Task 2: Schema, config and settings

**Files:**
- Create: `migrations/0003_audio_cd.sql`
- Modify: `settings.py`, `config.py`, `drives.py`, `web/drive_routes.py`, `templates/_drive_table.html`, `templates/settings.html`, `web/settings_routes.py`
- Test: `tests/test_db.py`, `tests/test_settings.py`, `tests/test_web_pages.py`

**Interfaces:**
- Produces, schema:
  - `drives.read_offset INTEGER NOT NULL DEFAULT 0`, pre-filled by the migration: `UPDATE drives SET read_offset = 667 WHERE model LIKE '%BH10LS38%'`, and `= 6 WHERE model LIKE '%iHAS124%' AND model NOT LIKE '%iHAS124 W%' AND model NOT LIKE '%iHAS124 Y%'`.
  - `discs(disc_id TEXT PRIMARY KEY, toc TEXT NOT NULL, track_count INTEGER NOT NULL, drive_id TEXT, last_seen TEXT NOT NULL)`.
  - `cd_rips(job_id INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE, disc_id TEXT NOT NULL, state TEXT NOT NULL CHECK (state IN ('ripping','needs_look','filed','discarded')), reasons TEXT NOT NULL DEFAULT '[]', candidates TEXT NOT NULL DEFAULT '[]', release_id TEXT, medium INTEGER, accuraterip TEXT NOT NULL DEFAULT '[]', downloads_path TEXT, filed_path TEXT, album_key TEXT, summary TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL)`, plus `CREATE INDEX cd_rips_disc ON cd_rips(disc_id, state)`.
  - `albums(album_key TEXT PRIMARY KEY, folder TEXT NOT NULL UNIQUE, release_group_id TEXT, album_artist TEXT NOT NULL, album TEXT NOT NULL, year TEXT NOT NULL DEFAULT '', disc_total INTEGER NOT NULL DEFAULT 1, album_tags TEXT NOT NULL DEFAULT '{}', discs TEXT NOT NULL DEFAULT '{}', filed_at TEXT NOT NULL)`.
  - `album_releases(release_id TEXT PRIMARY KEY, album_key TEXT NOT NULL REFERENCES albums(album_key) ON UPDATE CASCADE)`.
- Produces, config: `AppConfig.music_dir: Path = Path("/data/MEDIA/Music")` and `downloads_dir: Path = Path("/data/downloads/disc-ripper")`, from `MUSIC_DIR` and `DOWNLOADS_DIR`. The conftest sets both under `tmp_path` and creates them.
- Produces, settings:
  - `SettingDef` gains `choices: tuple[tuple[str, str], ...] = ()` (value, label) for kind `"choice"`, and kind `"secret"`.
  - New settings, in this order, after `iso_dir`:
    - `cd_rip_mode` (choice `secure`/"Secure (re-read until sure)", `fast`/"Fast"; default `secure`)
    - `cd_retries` (int, 0–50, default 10)
    - `cd_approval` (choice `odd_ones`/"Only check the odd ones", `always`/"Always ask"; default `odd_ones`)
    - `cd_cover_art` (bool, default True)
    - `jellyfin_url` (url, default "")
    - `jellyfin_api_key` (secret, default "")
  - `settings.parse_form(form, current: Mapping[str, Any] | None = None)`: a blank secret keeps `current[key]`.
  - `settings.get_all` returns secrets, but the settings template renders a secret as an empty password input with the hint "Set — leave blank to keep" when a value exists.
- Produces, drives: `drives.update_drive(conn, drive_id, *, label, enabled, read_offset: int | None = None)`; the Drives form gains a read-offset input (int −3000..3000; invalid → 400 with an error).

- [ ] **Step 1: Write failing tests:**
  - `test_db.py::test_migration_0003_prefills_offsets`: drives with models `BD-RE BH10LS38`, `iHAS124 F`, `iHAS124 Y`, `DVD-RW XYZ` → offsets 667, 6, 0, 0. `user_version == 3`.
  - `test_db.py::test_load_config_music_dirs`: defaults and env overrides for both new paths.
  - `test_settings.py::test_choice_setting`: `"fast"` ok; `"turbo"` → `"Must be one of: secure, fast."`.
  - `test_settings.py::test_secret_blank_keeps_current`: `parse_form({... "jellyfin_api_key": ""}, current={"jellyfin_api_key": "abc"})["jellyfin_api_key"] == "abc"`; a non-blank value replaces it.
  - `test_web_pages.py::test_secret_never_rendered`: save the key `"s3cr3t-key"` via POST /settings, then `"s3cr3t-key" not in client.get("/settings").text`, and the page contains `Set — leave blank to keep`.
  - `test_web_pages.py::test_read_offset_update`: POST `/drives/update` with `read_offset=667` stores 667; `read_offset=99999` → 400, unchanged.
- [ ] **Step 2: Run** `python -m pytest -q`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement** as specified. The `VALID`/`VALID_FORM` dicts in existing tests gain the new keys where needed.
- [ ] **Step 4: Run.** Expected: all PASS.
- [ ] **Step 5: Commit** `feat: schema, settings and drive read offsets for audio CDs`

---

### Task 3: TOC and MusicBrainz disc ID

**Files:**
- Create: `src/disc_ripper/cdtoc.py`
- Modify: `src/disc_ripper/cdrom.py`
- Test: `tests/test_cdtoc.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Toc: first: int; last: int; offsets: tuple[int, ...]` (LBA start of each track first..last); `leadout: int` (LBA); `data_tracks: frozenset[int]`.
    - `Toc.audio_tracks -> list[int]`
    - `Toc.audio_seconds -> float` (audio tracks' total length / 75)
    - `to_json() -> str` and `Toc.from_json(s) -> Toc`.
  - `musicbrainz_disc_id(toc: Toc) -> str`, with this algorithm:
    1. Treat a trailing data track (enhanced CD) as excluded: `last` = last audio track, and lead-out = data track start − 11400.
    2. SHA-1 over `"%02X" % first`, `"%02X" % last`, `"%08X" % (leadout + 150)`, then 99 values `"%08X" % (offset + 150)` for tracks 1..99 (0 for missing).
    3. Encode with base64 using the alphabet with `+`→`.`, `/`→`_`, `=`→`-`.
  - `cdrom.CdDrive.read_toc() -> Toc`: `CDROMREADTOCHDR` (0x5305; `struct {u8 first; u8 last}`), then `CDROMREADTOCENTRY` (0x5306) for each track and for the lead-out (track `0xAA`), with `cdte_format = CDROM_LBA (0x01)`. The ctypes layout is `cdte_track u8, cdte_adr_ctrl u8 (adr low 4 bits, ctrl high 4), cdte_format u8, (padding to 4), cdte_addr union {msf u8[3]; lba int32}, cdte_datamode u8`. A data track has `ctrl & 0x04`.

- [ ] **Step 1: Write failing tests:**
  - `test_disc_id_musicbrainz_example`: use the worked example from https://musicbrainz.org/doc/Disc_ID_Calculation (fetch the page and copy its track offsets, lead-out and expected ID verbatim into the test). The expected ID must equal the doc's.
  - `test_disc_id_enhanced_cd`: a TOC with tracks 1–10 audio and track 11 data at LBA 200000 gives the same ID as an audio-only TOC with last = 10 and lead-out 188600.
  - `test_toc_json_round_trip`.
  - `test_audio_seconds`: lead-out 95462 with first offset 0 and one track → `95462 / 75`.
  - `test_read_toc_ioctls` (fake ioctl filling the ctypes structs): two audio tracks plus a lead-out → `Toc(first=1, last=2, offsets=(0, 15000), leadout=30000, data_tracks=frozenset())`. A track with ctrl 0x4 → it is in `data_tracks`.
- [ ] **Step 2: Run.** Expected: FAIL (module missing).
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: read the CD table of contents and compute the MusicBrainz disc ID`

---

### Task 4: Library naming

**Files:**
- Create: `src/disc_ripper/library.py`
- Test: `tests/test_library.py`

**Interfaces:**
- Produces:
  - `VARIOUS_ARTISTS_ID = "89ad4ac3-39f7-470e-963a-56509c546377"`
  - `safe_component(text: str) -> str`:
    - replaces `/ \ : * ? " < > |` and characters below 0x20 (and 0x7F) with `_`;
    - collapses whitespace runs;
    - strips trailing `.` and spaces, and leading spaces;
    - caps at 120 characters (cut, then re-strip);
    - an empty result becomes `_`.
  - `album_folder(album_artist: str, album: str, year: str, *, compilation: bool) -> PurePosixPath`:
    - `<artist>/<album> (<year>)`, with `(year)` omitted when blank;
    - `Various Artists` when `compilation`.
  - `track_filename(track: int, title: str, *, disc: int | None, track_artist: str | None) -> str`:
    - `01 - Title.flac`, or `1-01 - Title.flac` when `disc` is not None;
    - `01 - Artist - Title.flac` when `track_artist` is given (compilations).

- [ ] **Step 1: Write failing tests:**
  - `test_safe_component_cases`:
    - `"AC/DC"` → `"AC_DC"`
    - `"Sigur Rós"` unchanged
    - `"Live..."` → `"Live"`
    - `"a" * 300` gives length 120
    - `"  "` → `"_"`
    - `'What?: "Yes"'` → `"What__ _Yes_"`
  - `test_album_folder`:
    - `("Pink Floyd", "The Dark Side of the Moon", "1973", compilation=False)` → `PurePosixPath("Pink Floyd/The Dark Side of the Moon (1973)")`
    - year `""` → `"Pink Floyd/The Dark Side of the Moon"`
    - compilation → `"Various Artists/…"`
  - `test_track_filename`:
    - `(1, "Speak to Me", disc=None, track_artist=None)` → `"01 - Speak to Me.flac"`
    - `(1, "X", disc=2, track_artist=None)` → `"2-01 - X.flac"`
    - `(3, "I Have a Dream", disc=1, track_artist="Westlife")` → `"1-03 - Westlife - I Have a Dream.flac"`
    - track 100 → `"100 - …"`
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: library folder and file naming`

---

### Task 5: MusicBrainz client, candidates and tags

**Files:**
- Create: `src/disc_ripper/musicbrainz.py`, `src/disc_ripper/coverart.py`, `tests/fixtures/mb/` (recorded JSON)
- Test: `tests/test_musicbrainz.py`

**Interfaces:**
- Produces:
  - `USER_AGENT = f"super-ripper/{__version__} ( https://github.com/Barrydamonke/super-ripper )"`.
  - `class MusicBrainz(fetch: Callable[[str, dict[str, str], float], bytes] = http_get_json, clock=time.monotonic, sleep=time.sleep)`:
    - all calls are synchronous (callers use `to_thread`);
    - one shared limiter, so requests are at least 1.0 s apart;
    - retries 3 times on 429/503/URLError with backoff 2, 4, 8 s, then raises `MusicBrainzUnavailable`.
    - `lookup_discid(disc_id: str) -> list[dict]`: releases from `/ws/2/discid/<id>?inc=artist-credits+recordings+release-groups+labels&fmt=json`. A 404 gives `[]`.
    - `get_release(release_id: str) -> dict`: `/ws/2/release/<id>?inc=artist-credits+recordings+release-groups+labels+discids&fmt=json`.
  - `parse_release_ref(text: str) -> str | None`: accepts a bare MBID or any `musicbrainz.org/release/<mbid>` URL.
  - `@dataclass(frozen=True) class TrackInfo: number: int; title: str; artist: str; recording_id: str; artist_ids: tuple[str, ...]`
  - `@dataclass(frozen=True) class Candidate:`
    - `release_id: str; release_group_id: str; title: str; album_artist: str; album_artist_ids: tuple[str, ...]`
    - `date: str; original_date: str; country: str; label: str; barcode: str`
    - `medium: int; medium_count: int; tracks: tuple[TrackInfo, ...]`
    - property `compilation -> bool` (the album artist IDs include `VARIOUS_ARTISTS_ID`)
    - property `year -> str` (the first 4 characters of `date`, or "")
  - `candidates_for_disc(releases: list[dict], disc_id: str) -> list[Candidate]`: one per release, using the medium whose `discs[].id == disc_id`. Artist credit text joins `name` + `joinphrase`.
  - `group_equivalent(cands: list[Candidate]) -> list[list[Candidate]]`: equal when title, album_artist and the tuple of track titles match; each group is sorted by date (empty dates last), then input order.
  - `choose(groups, *, filed_groups: Mapping[str, int]) -> Candidate | None`:
    1. Prefer a candidate whose `release_group_id` is in `filed_groups` with `filed_groups[gid] == medium_count`.
    2. Otherwise, with exactly one group, take that group's first entry.
    3. Otherwise `None` (ambiguous).
  - `tags_for(c: Candidate, track: TrackInfo, disc_id: str) -> dict[str, str]`: keys exactly as in the spec's Tags list (`TRACKNUMBER` and `DISCNUMBER` as plain integers; `TRACKTOTAL` = this medium's track count; `DISCTOTAL` = `medium_count`).
  - `coverart.fetch_front(release_id: str, fetch=...) -> bytes | None`: `https://coverartarchive.org/release/<id>/front-1200`, follows redirects; 404 or network error → None.

- [ ] **Step 1: Record fixtures.**
  - Save real responses (via WebFetch or curl, with the User-Agent) into `tests/fixtures/mb/`:
    - one single-disc album with several equivalent pressings for one disc ID;
    - one compilation (Various Artists);
    - one 2-disc release (both mediums carry disc IDs);
    - one disc ID with a 404.
  - Note each fixture's source URL in `tests/fixtures/mb/README.md`.
- [ ] **Step 2: Write failing tests:**
  - `test_candidates_for_disc_picks_matching_medium`: the 2-disc fixture with disc 2's ID → `medium == 2`, `medium_count == 2`, and track titles from disc 2.
  - `test_group_equivalent_pressings`: the pressings fixture gives 1 group, ordered by date.
  - `test_choose`: one group → the earliest pressing; two different groups → None; a group already filed with the same disc count wins over other groups; a different disc count doesn't.
  - `test_tags_for_compilation`: `ALBUMARTIST == "Various Artists"`, per-track `ARTIST` differs, and `MUSICBRAINZ_ALBUMARTISTID == VARIOUS_ARTISTS_ID`.
  - `test_parse_release_ref`: a URL with a query, a bare ID, and junk → None.
  - `test_client_rate_limit`: three calls with a fake clock and sleep → the sleeps keep the calls ≥ 1.0 s apart; the User-Agent header equals `USER_AGENT`.
  - `test_client_retries_503_then_gives_up`: four 503s → `MusicBrainzUnavailable` after sleeps 2, 4, 8; a 503 then 200 → result.
- [ ] **Step 3: Run.** Expected: FAIL. **Step 4: Implement.** **Step 5: Run.** Expected: PASS.
- [ ] **Step 6: Commit** `feat: MusicBrainz lookup, candidate choice and tag mapping`

---

### Task 6: Tagging and filing

**Files:**
- Create: `src/disc_ripper/tagging.py`, `src/disc_ripper/filing.py`
- Test: `tests/test_tagging.py`, `tests/test_filing.py`

**Interfaces:**
- Consumes: `library.*`, `musicbrainz.Candidate/TrackInfo/tags_for`.
- Produces:
  - `tagging.write_tags(path: Path, tags: Mapping[str, str]) -> None`: replaces every Vorbis comment except `REPLAYGAIN_*` (kept as-is).
  - `tagging.read_tags(path) -> dict[str, str]`.
  - `tagging.duration_seconds(path) -> float`.
  - `@dataclass class AlbumPlan`:
    - `album_key: str; release_group_id: str | None; album_artist: str; album: str; year: str`
    - `compilation: bool; disc: int | None; disc_total: int`
    - `album_tags: dict[str, str]`
    - `tracks: list[tuple[Path, dict[str, str]]]` (source file → full tags)
  - `filing.FileExists(Exception)`.
  - `filing.file_album(conn, plan: AlbumPlan, music_dir: Path, *, cover: Path | None, now: str, allow_duplicate_folder: bool = False) -> Path`:
    1. Folder: an existing `albums` row for `plan.album_key` (or a release in `album_releases`) uses its folder. Otherwise `music_dir / library.album_folder(...)`, and when that folder exists and belongs to a different `album_key` (or to none), append ` (2)`, ` (3)`… only if `allow_duplicate_folder`, else raise `FileExists`.
    2. Check every target name first and raise `FileExists` before moving anything.
    3. `os.replace`, falling back to `shutil.copy2` and unlink on `EXDEV`.
    4. `cover.jpg` is copied only if the folder has none.
    5. Upsert `albums`, merging `discs` with `{disc or 1: disc_id}`. Insert into `album_releases` for the release.
    6. Return the folder.
  - `filing.retag_album(conn, album_key, new_album_tags: dict[str, str], music_dir, now) -> Path`: rewrites album-level tags in every file of the folder; if artist, album or year changed, renames the folder (refusing with `FileExists` if the target exists) and updates the row.

- [ ] **Step 1: Write failing tests.** Use a tiny valid FLAC generated once by the test: write the minimal FLAC with mutagen-friendly STREAMINFO from a committed 1-second silent FLAC fixture `tests/fixtures/silence.flac` created with `ffmpeg -f lavfi -i anullsrc=r=44100:cl=stereo -t 1 -c:a flac`; commit the binary.
  - `test_write_tags_keeps_replaygain`
  - `test_file_album_single_disc`: files land at `Pink Floyd/The Dark Side of the Moon (1973)/01 - Speak to Me.flac`, `cover.jpg` is copied, and the `albums` row is written.
  - `test_second_disc_joins_folder_by_release`: disc 2 of the same `album_key` lands in the same folder as `2-01 - …`.
  - `test_other_pressing_joins_via_album_releases`: a plan with a different release but the existing `album_key` lands in the same folder.
  - `test_never_overwrites`: a target file exists → `FileExists`, and no source file was moved.
  - `test_duplicate_folder_suffix`: `allow_duplicate_folder=True` → `… (1973) (2)`.
  - `test_file_album_unicode_and_slash`: album `AC/DC Live` by `Sigur Rós` → folder `Sigur Rós/AC_DC Live`; `read_tags` shows `ALBUM == "AC/DC Live"`.
  - `test_retag_album_moves_folder`.
- [ ] **Step 2: Run.** Expected: FAIL. **Step 3: Implement.** **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: write tags and file albums into the library`

---

### Task 7: cyanrip runner and the rip job

**Files:**
- Create: `src/disc_ripper/cyanrip.py`, `src/disc_ripper/cd_rips.py`, `tests/fixtures/cyanrip/fake_cyanrip.py`, `tests/fixtures/cyanrip/stdout.txt`, `tests/fixtures/cyanrip/log.txt`
- Modify: `src/disc_ripper/runner.py`, `src/disc_ripper/cdrip.py` (new)
- Test: `tests/test_cyanrip.py`, `tests/test_cdrip_job.py`

**Interfaces:**
- Produces, `runner`:
  - `JobContext.run_process(argv: list[str], *, cwd: Path, on_line: Callable[[str], Awaitable[None]]) -> int`: starts it with `asyncio.create_subprocess_exec` (stdout merged with stderr, new session), calls `on_line` for each decoded line and returns the exit code.
  - On cancel or worker stop, the process gets SIGTERM, then SIGKILL after 5 s.
  - `JobContext.config` (the `AppConfig`) and `JobContext.services` (set by `Services`; None in tests that don't need it).
- Produces, `cyanrip`:
  - `build_argv(*, device: Path, offset: int, retries: int, mode: str) -> list[str]`, exactly `["cyanrip", "-d", str(device), "-s", str(offset), "-r", str(retries), "-P", "3" if mode == "secure" else "0", "-N", "-U", "-G", "-o", "flac", "-D", "rip", "-F", "{track}", "-L", "log"]`. `CYANRIP_BIN` env (tests) overrides `"cyanrip"`.
  - `class ProgressParser(track_total: int)`:
    - `feed(line: str) -> tuple[int, float] | None` returns (current track, track fraction 0–1) when a line carries progress, otherwise None;
    - lines it doesn't recognise are ignored;
    - the patterns come from the recorded fixture.
  - `@dataclass(frozen=True) class TrackCheck: track: int; status: Literal["verified", "not_found", "mismatch", "unknown"]; confidence: int`.
  - `parse_accuraterip(log_text: str, track_count: int) -> list[TrackCheck]`: a track missing from the log is `unknown`.
- Produces, `cd_rips`:
  - `create(conn, job_id, disc_id, now)`
  - `get(conn, job_id) -> Row | None`
  - `update(conn, job_id, **fields)` (JSON-encodes list/dict values; sets `updated_at`)
  - `blocking_rip_for_disc(conn, disc_id) -> Row | None` (a row in `needs_look`/`filed`, or one whose job is queued or running)
  - `needs_look(conn) -> list[Row]`
  - `recently_filed(conn, limit=20) -> list[Row]`
- Produces, `cdrip`:
  - `async def run_rip_cd(ctx: JobContext) -> None`, registered as `HANDLERS["rip_cd"]`. Params: `{"disc_id", "toc" (Toc JSON), "offset", "drive_dev"}`.
  - Steps:
    1. Re-read the TOC and refuse with `RuntimeError("A different disc is in the drive.")` if the disc ID differs.
    2. Create a fresh `/work/<jobId>/`.
    3. Run cyanrip, reporting "Track N of M — P%" with the estimate.
    4. Non-zero exit or a missing `rip/NN.flac` → raise with the last 5 output lines.
    5. Parse `rip/log*`.
    6. Eject under the drive lock.
    7. Call `cdrip.after_rip(...)` (Task 8), wrapped so its errors become Needs a look (never a job failure).
  - Expected track files: `rip/01.flac` … for each audio track. Confirm against the real 0.9.3 `-D rip -F "{track}"` output and adjust (record in the ledger).

- [ ] **Step 1: Fixtures and fake.**
  - Write `fake_cyanrip.py`, a Python script invoked through `CYANRIP_BIN="python tests/fixtures/cyanrip/fake_cyanrip.py"`. Ruling allowed: make `CYANRIP_BIN` a JSON list for the argv prefix.
  - It reads `FAKE_CYANRIP_MODE` (`ok` | `fail` | `hang`), prints `stdout.txt` line by line, and writes `rip/01.flac` … (copies of `silence.flac`) plus `rip/log.log` from `log.txt`.
  - Until a real capture exists, `stdout.txt` and `log.txt` are written from cyanrip 0.9.3's source formats. Mark them with a header comment "synthetic — replace with a real capture".
- [ ] **Step 2: Write failing tests:**
  - `test_build_argv_exact`
  - `test_progress_parser_fixture` (the fixture walks tracks 1..3, with fractions rising)
  - `test_progress_parser_ignores_junk`
  - `test_parse_accuraterip_fixture` (track 2 mismatch, others verified with confidence > 0)
  - `test_rip_job_ok`: runner and fake ok; job `done`; `cd_rips.state` leaves `ripping`; the eject was called on the fake drive.
  - `test_rip_job_fail_keeps_disc_in`: fake `fail`; job `failed` with the fake's last line in `error`; eject not called.
  - `test_rip_job_cancel_kills_process`: fake `hang`, `runner.cancel` → state `cancelled` within 6 s, and the fake process is gone.
  - `test_retry_refuses_different_disc`: the fake drive TOC gives a different ID → `failed` with "A different disc is in the drive."
  - `test_retry_uses_fresh_work_folder`: the old job's folder is deleted when its retry starts. A startup sweep removes the folders of jobs finished more than 7 days ago (test the sweep function `cdrip.sweep_work(conn, work_dir, now)` directly).
- [ ] **Step 3: Run.** Expected: FAIL. **Step 4: Implement.** **Step 5: Run.** Expected: PASS.
- [ ] **Step 6: Commit** `feat: rip audio CDs with cyanrip as a queued job`

---

### Task 8: After the rip: identify, decide, file; Jellyfin

**Files:**
- Create: `src/disc_ripper/jellyfin.py`
- Modify: `src/disc_ripper/cdrip.py`, `src/disc_ripper/services.py`
- Test: `tests/test_cdrip_pipeline.py`, `tests/test_jellyfin.py`

**Interfaces:**
- Produces, `jellyfin`:
  - `class Jellyfin(url, api_key, fetch=...)` with:
    - `has_album(release_id: str) -> bool` (raises `JellyfinError`);
    - `refresh() -> None`.
  - The `Authorization` header is `MediaBrowser Token="<key>"`.
  - The query is `/Items?Recursive=true&IncludeItemTypes=MusicAlbum&Fields=ProviderIds&searchTerm=<album>`, then compare `ProviderIds.MusicBrainzAlbum`. Adjust after checking on the owner's version; ledger it.
  - `class RefreshDebouncer(delay=60.0)` with `trigger()`. It calls `refresh` once, `delay` seconds after the last trigger; errors are kept in `last_error` for the Music page.
- Produces, `cdrip`:
  - `async def after_rip(ctx, *, disc_id: str, toc: Toc, checks: list[TrackCheck], rip_dir: Path) -> None`:
    1. `MusicBrainz.lookup_discid`
    2. candidates, groups and choose
    3. write tags
    4. cover
    5. move to `downloads_dir/<jobId>/`
    6. duplicate check
    7. decide; file or set `needs_look`
    8. update `cd_rips` and summary
    9. notify the card and the Music page
  - `decide(*, chosen: Candidate | None, groups: int, checks, duplicate: bool, other_edition: bool, waiting_sibling: bool, approval: str, mb_error: bool) -> list[str]`: the reasons; an empty list means file automatically. The reason strings, exactly:
    - `"no MusicBrainz match"`
    - `"several possible releases"`
    - `"{n} track(s) not verified"`
    - `"already in the library"`
    - `"another edition of an album in the library"`
    - `"disc {n} of an album waiting for a look"`
    - `"MusicBrainz unreachable"`
    - `"you asked to check every album"` (approval = always)
  - `def build_plan(conn, cand: Candidate, files: list[Path], disc_id: str) -> AlbumPlan`: when the release group is already filed with the same disc count, the album-level tags come from that album's `album_tags` and `album_key` (joining).

- Produces, `services`:
  - `Services` owns one `MusicBrainz` client, a `Jellyfin` (or None when the URL or key is blank, re-read on each use) and a `RefreshDebouncer`.
  - It sets `ctx.services` for `rip_cd` jobs.
  - `Services.start()` calls `cdrip.sweep_work(conn, config.work_dir, now)` before the workers start.

- [ ] **Step 1: Write failing tests** (MusicBrainz, cover and Jellyfin faked):
  - `test_clean_match_files_itself`: the folder exists in `music_dir`; `cd_rips.state == "filed"`; `RefreshDebouncer.trigger` was called.
  - `test_unverified_goes_to_look` with reason `"1 track(s) not verified"`.
  - `test_several_releases_goes_to_look`.
  - `test_no_match_goes_to_look`.
  - `test_mb_unreachable_goes_to_look`: the files are still moved to downloads.
  - `test_duplicate_goes_to_look`.
  - `test_other_pressing_of_filed_album_joins`.
  - `test_deluxe_edition_goes_to_look` with `"another edition of an album in the library"`.
  - `test_second_disc_while_first_waiting`: disc 1's rip is `needs_look`, and disc 2 of the same release group gets `"disc 2 of an album waiting for a look"`.
  - `test_approval_always`.
  - `test_jellyfin_errors_do_not_block_filing`: `has_album` raises → it files via the DB check alone and the summary notes "Jellyfin check failed".
  - `test_refresh_debounced`: three triggers within the delay → one refresh.
- [ ] **Step 2: Run.** Expected: FAIL. **Step 3: Implement.** **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: identify, decide and file ripped CDs; Jellyfin check and refresh`

---

### Task 9: Starting rips from the dashboard

**Files:** Modify `services.py`, `media.py`, `web/dashboard_routes.py`, `templates/_drive_card.html`. Test: `tests/test_cd_autostart.py`, `tests/test_web_dashboard.py`.

**Interfaces:**
- Produces:
  - `Services.on_insert(drive_id)`, wired into `MediaMonitor(on_insert=...)`:
    - for an audio or mixed disc, read the TOC (under the drive lock, in a thread) and upsert `discs`;
    - if `auto_start_rips` is on and `cd_rips.blocking_rip_for_disc` is None → `enqueue_cd_rip(drive_id, toc)`.
  - `Services.enqueue_cd_rip(drive_id: str, toc: Toc) -> int`:
    - `jobs.enqueue(kind="rip_cd", uses_drive=True, needs_bytes=ceil(toc.audio_seconds * 176400 * 0.75), params={...})`;
    - `cd_rips.create`;
    - `wake`.
  - Route `POST /dashboard/rip` (`drive_id`, optional `again=1`): reads the TOC; refuses non-audio discs with an error on the card; when blocked and not `again`, returns the card with the "Already ripped…" message.
  - The card shows the disc ID, the track count, a **Rip** button (no running job; audio disc; not blocked), and the blocked message with **Rip again**. Recent `rip_cd` jobs show "→ filed: <summary>" or "→ needs a look", linking to `/music/<jobId>`.
  - `media.py`: a disc going `unknown → disc` without passing through `empty`/`tray_open` doesn't fire `on_insert` (step 2's M5).
- [ ] **Step 1: Write failing tests:**
  - `test_insert_autostarts_rip`
  - `test_insert_does_not_restart_filed_disc`
  - `test_insert_after_failed_rip_autostarts`
  - `test_autostart_off_shows_rip_button`
  - `test_rip_again_route`
  - `test_glitch_unknown_to_disc_no_insert`
  - `test_needs_bytes_estimate` (a 60-minute TOC gives ≥ 476 MB)
- [ ] **Step 2: Run.** Expected: FAIL. **Step 3: Implement.** **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: start CD rips on insert or from the dashboard`

---

### Task 10: The Music page

**Files:** Create `web/music_routes.py` and `templates/music.html`, `music_item.html`, `_music_resolve.html`. Modify `app.py` (router) and `base.html` (nav "Music (N)"). Test: `tests/test_web_music.py`.

**Interfaces:**
- Produces routes (all login-protected; POSTs go through the existing cross-origin check):
  - `GET /music`: Needs a look (reasons, track count, Open) and Recently filed (summary, Edit), plus `jellyfin.RefreshDebouncer.last_error` if set.
  - `GET /music/{job_id}`: the item page.
    - Reasons; a track table (number, title, duration, AccurateRip status); `<audio controls src="/music/{job_id}/track/{n}">`.
    - Candidate radio list; MusicBrainz URL box; manual form; add-to-existing picker (`<select>` of `albums` ordered by artist, then album, plus a disc-number input); **File anyway**; **Discard**.
  - `GET /music/{job_id}/track/{n}`: `FileResponse` of that downloads file. `n` must be an int within the track count; the path is resolved under `downloads_dir` and must stay inside it.
  - `POST /music/{job_id}/use-release` (`release_id` from the candidates only)
  - `POST /music/{job_id}/use-url` (`ref`): uses `parse_release_ref` → `get_release`; the medium is chosen by matching disc ID, else by track count. Several mediums match → the page asks for `medium`.
  - `POST /music/{job_id}/manual` (`album, album_artist, year, disc, disc_total, title_<n>, artist_<n>`): requires album, album artist and every title; year is 4 digits or blank; `album_key = "manual:" + uuid4`.
  - `POST /music/{job_id}/add-to` (`album_key, disc`)
  - `POST /music/{job_id}/file-anyway`
  - `POST /music/{job_id}/discard`
  - `GET /music/album/{album_key}/edit` and `POST /music/album/{album_key}/edit` → `filing.retag_album`.
  - Each resolve action ends in `filing.file_album`. `FileExists` → back to the item page with "A file with that name is already in the library." and the item is unchanged.
- [ ] **Step 1: Write failing tests:**
  - `test_music_page_lists`
  - `test_use_release_files`
  - `test_use_url_rejects_junk`
  - `test_manual_requires_titles`
  - `test_manual_files_with_manual_key`
  - `test_add_to_existing_album`
  - `test_file_anyway_duplicate_suffix`
  - `test_discard_removes_files`
  - `test_track_route_rejects_traversal` (`/music/1/track/../../etc` → 404/422; `n` beyond the track count → 404)
  - `test_edit_album_renames_folder`
  - `test_nav_count`
  - `test_titles_escaped` (a title `<b>x</b>` renders escaped)
- [ ] **Step 2: Run.** Expected: FAIL. **Step 3: Implement.** **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: Music page — review, resolve and edit ripped albums`

---

### Task 11: Docs, real-run fixtures and final verification

- [ ] **Step 1: README.** Update the current state (step 3); CD settings; Jellyfin URL/key; read offsets; the library layout; the on-server test list from the spec's Testing section.
- [ ] **Step 2: Verify.**
  - `python -m pytest -q` on Windows; the full suite in `python:3.12-slim-trixie`.
  - Image smoke: healthz; tini/uid 99; `cyanrip -V` as uid 99.
- [ ] **Step 3: Live check in a container.** Real uvicorn with fake cyanrip on `PATH` and a fake drive. Insert → auto rip → filed into a temp music dir; check the card and the Music page.
- [ ] **Step 4:** Whole-branch review; fix Critical and Important findings with tests first.
- [ ] **Step 5:** Push and open the PR. It says the cyanrip stdout/log fixtures are synthetic until the first real rip, and asks the owner for that capture (`docker exec` command to copy the job's `rip.log` and the container log lines).
