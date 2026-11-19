# Step 2: Detection, Queues and Shutdown Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect discs, run per-drive job queues (with a test job) with live progress, check cache space, and hold scheduled shutdowns while jobs run, including the unraid-backend side.

**Architecture:** Everything runs in the one uvicorn process: asyncio background tasks (drive poller, one worker per drive, schedule refresher, guard ticker) started from the FastAPI lifespan. Blocking work (ioctls, sector reads, SQLite) runs in threads via `asyncio.to_thread`. State lives in SQLite; changes are rendered to HTML fragments and pushed over SSE through an in-process broker.

**Tech Stack:** Python 3.12, FastAPI, Jinja2, htmx 2.0.4 + htmx-ext-sse (vendored), SQLite, `fcntl.ioctl`/ctypes for CD-ROM ioctls and SG_IO, tini. unraid-backend: Node (CommonJS, built-in `fetch`).

**Spec:** `docs/superpowers/specs/2026-11-18-step-2-detection-queues-guard-design.md`

## Global Constraints

- Python stdlib only for new runtime code (no new pip runtime dependencies); HTTP fetches use `urllib.request` in a thread.
- All timestamps stored in SQLite are UTC ISO strings from `clock.utc_now()`; schedule maths uses aware local datetimes (`datetime.now().astimezone()` / `ZoneInfo`).
- Background tasks start only when `AppConfig.run_background` is true (tests set it false).
- Never `--privileged`; the app keeps running as 99:100 with no capabilities.
- Child processes / ioctls use fixed arguments; disc labels are untrusted text (Jinja autoescape, never in shell commands).
- GB means 10^9 bytes.
- Poll interval 3 s; probe timeout 30 s; schedule refresh 5 min with 5 s HTTP timeout; worker re-check 30 s; SSE keep-alive 15 s; progress writes and events at most 1/s per job.
- Grace setting default 3, max 3.5 hours; unraid-backend cap 4 hours; unraid-backend busy poll every 60 s, 5 s timeout.
- uvicorn starts with `--timeout-graceful-shutdown 10`; the template keeps `--stop-timeout=60`.
- Tests must pass on Windows (Linux-only bits skipped or faked) and on Linux (`python:3.12-slim-bookworm` container).

## Review Focus

1. **Container stop with a dashboard tab open** must still mark running jobs interrupted (SSE streams must not block shutdown) → Task 10 test `test_lifespan_shutdown_marks_running_interrupted` plus the manual check in Task 12.
2. **Drive disappears or errors mid-poll** (unplugged, `EIO`, node missing) must give `unknown`/no crash and the poller must keep polling other drives → Task 6 test `test_poll_survives_drive_error`.
3. **Schedule source returns garbage** (HTML error page, 6 entries, `"25:00"`, only `start` set) must be rejected and the last good copy kept → Task 3 tests `test_fetch_rejects_invalid_and_falls_back` and Task 2 `test_parse_schedule_rejects`.
4. **Overnight window across the week wrap** (Sunday 22:00 → Monday 02:00 with `endNextDay`) must count as in-window on Monday 01:00 → Task 2 test `test_overnight_sunday_into_monday`.
5. **Hostile disc label** (`<script>`, non-ASCII, 0x00 bytes) must be shown escaped and trimmed → Task 4 test `test_iso9660_label_sanitised` and Task 11 test `test_dashboard_escapes_label`.

---

## File map

| File | Responsibility |
| --- | --- |
| `src/disc_ripper/migrations/0002_media_jobs.sql` | drive media columns, `jobs` table |
| `src/disc_ripper/config.py` | + `work_dir`, `busy_secret`, `run_background` |
| `src/disc_ripper/settings.py` | + `url` kind, two schedule URL settings, grace max 3.5 |
| `src/disc_ripper/schedule.py` | pure schedule parsing, windows, guard state |
| `src/disc_ripper/schedule_source.py` | fetch, cache in DB, `current_guard()` |
| `src/disc_ripper/discprobe.py` | pure sector/response parsers + `probe()` |
| `src/disc_ripper/cdrom.py` | ioctl / SG_IO wrapper for one drive |
| `src/disc_ripper/events.py` | SSE broker and event formatting |
| `src/disc_ripper/media.py` | drive poller (`MediaMonitor`), `drives.set_media` use |
| `src/disc_ripper/space.py` | cache space check |
| `src/disc_ripper/jobs.py` | job table access (sync, DB only) |
| `src/disc_ripper/runner.py` | per-drive workers, job context, test job |
| `src/disc_ripper/busy.py` | `/api/busy` report |
| `src/disc_ripper/services.py` | wires everything; start/stop; `Notifier` |
| `src/disc_ripper/web/dashboard_routes.py` | dashboard, actions, `/events`, `/api/busy` |
| `src/disc_ripper/templates/{dashboard,_drive_card,_guard}.html` | UI |
| `src/disc_ripper/static/htmx-ext-sse.js` | vendored htmx SSE extension 2.2.2 |
| `Dockerfile`, `docker/entrypoint.sh`, `unraid/disc-ripper.xml`, `README.md` | tini, graceful timeout, `BUSY_SECRET`, docs |
| `../barry-mc-network/unraid-backend/{ripperClient.js,powerControl.js,server.js}` | busy check (separate repo/PR) |

---

### Task 1: Schema, config and settings

**Files:**
- Create: `src/disc_ripper/migrations/0002_media_jobs.sql`
- Modify: `src/disc_ripper/config.py`, `src/disc_ripper/settings.py`, `src/disc_ripper/drives.py`, `src/disc_ripper/templates/settings.html`, `tests/conftest.py`
- Test: `tests/test_db.py`, `tests/test_settings.py`, `tests/test_drives.py`

**Interfaces:**
- Produces:
  - `AppConfig(config_dir, sysfs_root, dev_root, work_dir: Path = Path("/work"), busy_secret: str = "", run_background: bool = True)`; `load_config` reads `WORK_DIR`, `BUSY_SECRET`.
  - `drives` table columns `media_state TEXT NOT NULL DEFAULT 'unknown'` (CHECK in `unknown, empty, tray_open, not_ready, disc`), `disc_type TEXT` (NULL or one of `audio, dvd, bluray, data, mixed, unknown`), `disc_label TEXT NOT NULL DEFAULT ''`, `media_changed_at TEXT`.
  - `jobs(id INTEGER PRIMARY KEY, drive_id TEXT NOT NULL REFERENCES drives(id) ON UPDATE CASCADE ON DELETE CASCADE, kind TEXT NOT NULL, state TEXT NOT NULL CHECK (state IN ('queued','waiting_space','running','done','failed','cancelled','interrupted')), params TEXT NOT NULL DEFAULT '{}', progress REAL NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT '', needs_bytes INTEGER NOT NULL DEFAULT 0, uses_drive INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, estimated_finish TEXT, error TEXT)` plus `CREATE INDEX jobs_drive_state ON jobs(drive_id, state, id)`.
  - Settings `schedule_status_url` ("Schedule source (unraid-backend /status)", kind `url`, default `""`) and `schedule_pi_url` ("Schedule fallback (Pi /schedule)", kind `url`, default `""`); `shutdown_grace_hours` maximum `3.5`, help mentions unraid-backend's 4-hour cap.
  - `drives.set_media(conn, drive_id: str, state: str, disc_type: str | None, label: str, now: str) -> bool` — returns True if any of the three values changed (and only then updates `media_changed_at`).
  - `conftest.config` fixture sets `work_dir=tmp_path/"work"` (created) and `run_background=False`.

- [ ] **Step 1: Write failing tests**
  - `test_db.py::test_migration_0002_from_step1_db`: build a DB at `user_version=1` with one drive row and a label; run `migrate`; `user_version == 2`; the row has `media_state == "unknown"`, label kept; inserting a job then `UPDATE drives SET id='serial:new'` moves the job's `drive_id` to `'serial:new'`.
  - `test_settings.py::test_url_setting`: `""` → `""`; `"http://10.0.0.5:3001/status"` kept; `"ftp://x"`, `"10.0.0.5:3001"`, `"http://"`, `"http://a b"` each raise with message `"Must be blank or an http:// or https:// URL."`.
  - `test_settings.py::test_grace_max`: `"3.5"` ok, `"4"` → error `"Must be at most 3.5."`.
  - `test_drives.py::test_set_media_reports_change`: first call True, identical call False, label change True; `media_changed_at` only moves on change.
  - Update the `VALID` dict in `tests/test_web_pages.py` with the two new URL keys (blank).
- [ ] **Step 2: Run** `pytest -q` — new tests FAIL.
- [ ] **Step 3: Implement** the migration, config fields, `url` kind in `_parse_one` (strip; blank allowed; `urllib.parse.urlsplit` scheme in http/https, non-empty netloc, no whitespace), the two settings, grace max, `set_media`, and the settings template renders `url` like `path`.
- [ ] **Step 4: Run** `pytest -q` — all PASS.
- [ ] **Step 5: Commit** `feat: schema, config and settings for step 2`

---

### Task 2: Schedule logic (pure)

**Files:**
- Create: `src/disc_ripper/schedule.py`
- Test: `tests/test_schedule.py`

**Interfaces:**
- Produces:
  - `DAYS: tuple[str, ...]` = monday…sunday.
  - `@dataclass(frozen=True) class DayEntry: day: str; start: str; end: str; end_next_day: bool`
  - `parse_schedule(data: object) -> list[DayEntry] | None` — None unless: list of 7 dicts in `DAYS` order, `day` equal to the expected name, `start`/`end` both `""` or both valid `HH:MM` (00–23, 00–59, two digits each), `endNextDay` bool or missing (→ False).
  - `@dataclass(frozen=True) class Window: start: datetime; end: datetime`
  - `windows_near(entries: list[DayEntry], now: datetime) -> list[Window]` — windows from entries for `now.date()` minus 2, minus 1 and today; each built with `datetime.combine(d, t, tzinfo=now.tzinfo)`; end on the next day when `end_next_day`; blank days skipped; sorted by start.
  - `Phase = Literal["no_schedule", "in_window", "draining", "off_hours"]`
  - `@dataclass(frozen=True) class GuardState: phase: Phase; scheduled_end: datetime | None; grace_ends_at: datetime | None` with property `holds_shutdown -> bool` (True for `no_schedule`, `in_window`, `draining`).
  - `guard_state(entries: list[DayEntry] | None, now: datetime, grace: timedelta) -> GuardState` — `in_window` if any window has `start <= now < end`; else the latest window with `end <= now`: `draining` if `now < end + grace`, otherwise `off_hours` (keeping that end); no windows at all → `off_hours` with Nones; `entries is None` → `no_schedule`.

- [ ] **Step 1: Write failing tests** (use `ZoneInfo("Europe/London")`, a helper `week(**days)` building 7 entries, blank by default; 2026-11-16 is a Monday)
  - `test_parse_schedule_accepts_example`: the `schedule info.md` sample shape (with `endNextDay` missing on some) parses; missing flag → False.
  - `test_parse_schedule_rejects`: 6 entries; wrong order; `"25:00"`; `"9:00"`; only `start` set; `endNextDay: "yes"`; a string; `None` → all `None`.
  - `test_in_window`: Monday 18:00–23:00, now Mon 20:00 → `in_window`, `scheduled_end` Mon 23:00, `grace_ends_at` Tue 02:00 (grace 3 h).
  - `test_draining_then_off_hours`: same, now Mon 23:30 → `draining`; now Tue 02:00 → `off_hours`, `holds_shutdown` False.
  - `test_overnight_sunday_into_monday`: Sunday 22:00–02:00 `endNextDay`, now Mon 01:00 → `in_window`, end Mon 02:00.
  - `test_overnight_under_yesterday_then_draining`: Friday 18:00–02:00 next day; now Sat 03:00 → `draining`.
  - `test_blank_day_off_hours`: all blank → `off_hours`, `scheduled_end is None`.
  - `test_no_schedule`: `guard_state(None, …)` → `no_schedule`, `holds_shutdown` True.
  - `test_start_equals_now_and_end_exclusive`: now == start → `in_window`; now == end → `draining`.
- [ ] **Step 2: Run** `pytest tests/test_schedule.py -q` — FAIL (module missing).
- [ ] **Step 3: Implement** `schedule.py` as specified.
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat: schedule windows and guard phases`

---

### Task 3: Schedule source and cache

**Files:**
- Create: `src/disc_ripper/schedule_source.py`
- Test: `tests/test_schedule_source.py`

**Interfaces:**
- Consumes: `schedule.parse_schedule`, `schedule.guard_state`, `settings.get/get_internal/set_internal`.
- Produces:
  - `Fetcher = Callable[[str, float], bytes]` (url, timeout → body); default `http_get(url, timeout)` using `urllib.request.urlopen` (raises on non-200).
  - `fetch_schedule(status_url: str, pi_url: str, fetch: Fetcher = http_get, timeout: float = 5.0) -> tuple[list | None, str]` — tries `status_url` (JSON object; take `["schedule"]`), then `pi_url` (JSON array). Returns `(raw_list, source)` where `source` is `"unraid-backend"` or `"pi"`, or `(None, error_text)` naming each failure. Blank URLs are skipped; both blank → `(None, "No schedule URL set")`.
  - `save_cached(conn, raw: list, source: str, fetched_at: str) -> None` (internal key `internal.schedule`), `load_cached(conn) -> CachedSchedule | None` with `@dataclass(frozen=True) class CachedSchedule: entries: list[DayEntry]; source: str; fetched_at: str` (re-validated on load; invalid → None).
  - `current_guard(conn, now: datetime) -> GuardState` — cached entries (or None) + `timedelta(hours=settings.get(conn, "shutdown_grace_hours"))`.
  - `async def refresh(db_path: Path, fetch: Fetcher = http_get) -> str | None` — runs fetch in a thread, saves when valid, returns the error text or None.

- [ ] **Step 1: Write failing tests** with a fake `fetch` dict url→bytes/exception:
  - `test_fetch_prefers_status_url`: both valid → source `"unraid-backend"`.
  - `test_fetch_rejects_invalid_and_falls_back`: status returns `b"<html>"`, then `{"schedule": null}`, then a 6-entry list: each case falls back to the valid Pi list (source `"pi"`); Pi invalid too → `(None, msg)` with both URLs in `msg`.
  - `test_refresh_keeps_last_good`: refresh with valid data, then with all failing → `load_cached` still returns the first schedule and `refresh` returned an error string.
  - `test_current_guard_uses_grace_setting`: grace setting 1.5 → `grace_ends_at` = end + 1.5 h.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat: fetch and cache the power schedule`

---

### Task 4: Disc probing parsers

**Files:**
- Create: `src/disc_ripper/discprobe.py`
- Test: `tests/test_discprobe.py`

**Interfaces:**
- Produces:
  - `DiscType = Literal["audio", "dvd", "bluray", "data", "mixed", "unknown"]`; `MediaKind = Literal["cd", "dvd", "bd", "unknown"]`
  - Constants `CDS_AUDIO=100, CDS_DATA_1=101, CDS_DATA_2=102, CDS_XA_2_1=103, CDS_XA_2_2=104, CDS_MIXED=105`.
  - `clean_label(raw: bytes | str) -> str` — decode ASCII ignoring errors, drop non-printables, strip, cap 64 chars.
  - `iso9660_pvd(sector16: bytes) -> tuple[str, int, int] | None` — requires byte 0 == 1 and bytes 1–6 == `b"CD001"`; returns (label from bytes 40–72, root extent LBA (LE uint32 at 156+2), root data length (LE uint32 at 156+10)).
  - `directory_names(data: bytes) -> list[str]` — walk directory records (length byte 0; 0 → skip to next 2048 boundary), name length at 32, name at 33; skip names `\x00`/`\x01`; strip `;1`; upper-case.
  - `udf_label(read_sector: Callable[[int], bytes]) -> str | None` — AVDP at 256 (tag id LE uint16 at 0 == 2); main VDS extent length at 16, location at 20 (LE uint32); scan its sectors for tag id 6 (LVD) and terminator 8; LVD logical volume identifier is a 128-byte dstring at offset 84 (byte 0 compression id 8 → 1 byte/char, 16 → UTF-16BE; last byte = used length).
  - `media_kind(config_header: bytes) -> MediaKind` — current profile = BE uint16 at bytes 6–7: 0x08–0x0A → cd; 0x10–0x2B → dvd; 0x40–0x43 → bd; else unknown.
  - `class ProbeTarget(Protocol): def disc_status(self) -> int; def get_configuration(self) -> bytes; def read_sector(self, lba: int) -> bytes`
  - `probe(target: ProbeTarget) -> tuple[DiscType, str]` — rules in spec "Probing a disc"; any exception in a step degrades (media unknown, label `""`) rather than raising; if the disc status itself fails → `("unknown", "")`. With media unknown and ISO 9660 present: `VIDEO_TS` → dvd else data; with no ISO 9660 and media unknown → `unknown`.

- [ ] **Step 1: Write failing tests** using builders in the test file: `pvd(label, root_lba, root_len)`, `dir_sector(names)`, `udf_disc(label, utf16=False)` (dict lba → bytes), `config_header(profile)`, and a `FakeTarget`.
  - `test_iso9660_label_sanitised`: label `b"<script>\x00\xff MOVIE  "` → `"<script> MOVIE"` (escaping is the template's job), 70-char input capped at 64.
  - `test_directory_names`: `["VIDEO_TS", "AUDIO_TS;1"]` round-trip to `["VIDEO_TS", "AUDIO_TS"]`.
  - `test_udf_label` 8-bit and UTF-16 → `"THE_MATRIX"`; missing anchor → None.
  - `test_media_kind`: 0x08 cd, 0x10 dvd, 0x41 bd, 0x50 unknown, short buffer unknown.
  - `test_probe_audio_cd` (status 100 → `("audio", "")`), `test_probe_mixed`, `test_probe_dvd_video` (dvd + VIDEO_TS → `("dvd", "MOVIE")`), `test_probe_data_dvd` (no VIDEO_TS → data), `test_probe_bluray_udf_label` (bd, no PVD → `("bluray", "THE_MATRIX")`), `test_probe_config_fails_uses_filesystem` (get_configuration raises, VIDEO_TS present → dvd), `test_probe_status_fails` → `("unknown", "")`.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat: identify disc type and volume label`

---

### Task 5: CD-ROM device wrapper

**Files:**
- Create: `src/disc_ripper/cdrom.py`
- Test: `tests/test_cdrom.py`

**Interfaces:**
- Consumes: `discprobe.ProbeTarget` protocol.
- Produces:
  - Constants `CDROMEJECT=0x5309, CDROMCLOSETRAY=0x5319, CDROM_DRIVE_STATUS=0x5326, CDROM_DISC_STATUS=0x5327, CDROM_LOCKDOOR=0x5329, CDSL_CURRENT=0x7FFFFFFF, SG_IO=0x2285`; drive status codes `CDS_NO_INFO=0, CDS_NO_DISC=1, CDS_TRAY_OPEN=2, CDS_DRIVE_NOT_READY=3, CDS_DISC_OK=4`.
  - `MediaState = Literal["unknown", "empty", "tray_open", "not_ready", "disc"]`
  - `class CdDrive(path: Path, *, ioctl: Callable | None = None, open_fn=os.open, close_fn=os.close, pread=os.pread)` implementing `ProbeTarget` plus `drive_status() -> MediaState`, `eject() -> None`, `close_tray() -> None`. Each call opens `O_RDONLY | O_NONBLOCK`, does its work, closes (also on error). `ioctl` defaults to `fcntl.ioctl`, imported lazily so the module imports on Windows. `eject` = `LOCKDOOR 0` (errors ignored) then `CDROMEJECT`. `read_sector(lba)` = `pread(fd, 2048, lba*2048)`, short read → `OSError`.
  - `get_configuration()` sends CDB `46 01 00 00 00 00 00 00 08 00` through SG_IO (ctypes `sg_io_hdr`, `interface_id=ord('S')`, `dxfer_direction=-3`, 8-byte buffer, 32-byte sense, timeout 5000 ms) and raises `OSError` when `status`, `host_status` or `driver_status` is non-zero. Field order of `sg_io_hdr` (Linux `<scsi/sg.h>`): `c_int interface_id, c_int dxfer_direction, c_ubyte cmd_len, c_ubyte mx_sb_len, c_ushort iovec_count, c_uint dxfer_len, c_void_p dxferp, c_void_p cmdp, c_void_p sbp, c_uint timeout, c_uint flags, c_int pack_id, c_void_p usr_ptr, c_ubyte status, c_ubyte masked_status, c_ubyte msg_status, c_ubyte sb_len_wr, c_ushort host_status, c_ushort driver_status, c_int resid, c_uint duration, c_uint info`.

- [ ] **Step 1: Write failing tests** with a recording fake `ioctl(fd, request, arg)`, fake `open_fn`/`close_fn`:
  - `test_drive_status_mapping`: return values 1,2,3,4,0 → empty, tray_open, not_ready, disc, unknown; request is `CDROM_DRIVE_STATUS` with arg `CDSL_CURRENT`.
  - `test_drive_status_error_is_unknown`: ioctl raises `OSError(EIO)` → `"unknown"`; fd still closed.
  - `test_eject_unlocks_then_ejects`: requests in order `[CDROM_LOCKDOOR, CDROMEJECT]`; LOCKDOOR raising doesn't stop the eject; CDROMEJECT raising `OSError(EBUSY)` propagates.
  - `test_read_sector_offset`: fake `pread` sees `(fd, 2048, 16*2048)`.
  - `test_get_configuration_builds_cdb` (skip on Windows): fake ioctl inspects the `sg_io_hdr` (cmd_len 10, CDB bytes, dxfer_len 8), fills the buffer with a header whose profile is 0x0041, returns 0 → `discprobe.media_kind(result) == "bd"`.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat: CD-ROM ioctl wrapper`

---

### Task 6: Event broker and drive poller

**Files:**
- Create: `src/disc_ripper/events.py`, `src/disc_ripper/media.py`
- Test: `tests/test_events.py`, `tests/test_media.py`

**Interfaces:**
- Consumes: `cdrom.CdDrive`, `discprobe.probe`, `drives.set_media`, `jobs.running_uses_drive` (Task 8 — define it in this task in `jobs.py` as a stub-free query: `running_uses_drive(conn, drive_id: str) -> bool`; Task 8 adds the rest of `jobs.py`).
- Produces:
  - `events.format_sse(name: str, data: str) -> str` — `event: <name>\n` + one `data: <line>\n` per line + `\n`.
  - `events.Broker` with `subscribe() -> Subscription` (context manager; `Subscription.queue: asyncio.Queue[tuple[str, str]]`, `Subscription.dropped: bool`), `publish(name: str, data: str) -> None` (non-blocking; a full queue (max 200) marks that subscription dropped and removes it).
  - `class Notifier(Protocol): async def drive_changed(self, drive_id: str) -> None; async def guard_changed(self) -> None`
  - `media.MediaMonitor(config: AppConfig, notifier: Notifier, *, drive_factory: Callable[[Path], CdDrive] = CdDrive, interval: float = 3.0, probe_timeout: float = 30.0, on_insert: Callable[[str], Awaitable[None]] | None = None)`
    - `drive_lock(drive_id: str) -> threading.Lock` (one per drive, shared with eject)
    - `async def poll_once(*, first: bool = False) -> None` — for each present, enabled drive with an `sr_name` and no running `uses_drive` job: status in a thread under the lock; on change to `disc` → probe in a thread (under the lock, with `asyncio.wait_for(…, probe_timeout)`; timeout → `("unknown", "")`); `set_media`; if changed → `notifier.drive_changed`; `on_insert` fired on a change to `disc` unless `first`. Any exception for one drive is logged and that drive gets `unknown`; others continue.
    - `async def run() -> None` — `poll_once(first=True)`, then loop forever with `interval`.

- [ ] **Step 1: Write failing tests** (`asyncio.run` inside sync tests; fake drives with scripted `drive_status()` sequences and a fixed probe result; a recording notifier):
  - `test_format_sse_multiline`: `"a\nb"` → `"event: x\ndata: a\ndata: b\n\n"`.
  - `test_broker_drops_slow_subscriber`: queue max 2 via a constructor arg; 3 publishes → `dropped` True, later publishes don't raise.
  - `test_insert_probes_and_notifies`: empty → disc: DB row `media_state="disc"`, `disc_type="dvd"`, label set; notifier called once per change; `on_insert` called once.
  - `test_first_poll_does_not_fire_insert`.
  - `test_eject_clears_type_and_label`: disc → empty gives `disc_type None`, label `""`.
  - `test_poll_survives_drive_error`: drive A raises `OSError`, drive B empty→disc still processed; A's row `unknown`.
  - `test_skips_drive_with_running_uses_drive_job`: insert a running job with `uses_drive=1` → the fake's `drive_status` isn't called.
  - `test_disabled_or_absent_drive_not_polled`.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement.** DB access inside the poller uses a fresh `db.connect` per poll in a thread.
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat: poll drives for inserted discs`

---

### Task 7: Cache space check

**Files:**
- Create: `src/disc_ripper/space.py`
- Test: `tests/test_space.py`

**Interfaces:**
- Produces:
  - `GB = 1_000_000_000`
  - `tree_size(path: Path) -> int` (files' `st_size` under path via `os.walk`, not following symlinks, ignoring files that vanish).
  - `class SpaceChecker(work_dir: Path, *, statvfs=os.statvfs, size_fn=tree_size, clock=time.monotonic, cache_seconds: float = 60.0)` with `check(needs_bytes: int, running_bytes: int, budget_gb: float, min_free_gb: float) -> str | None` — None = fits. `needs_bytes == 0` → None always. Missing `work_dir` → `"Work area /work not found."` (actual path). Budget fail → `"Waiting for cache space: needs 12.0 GB, work area budget has 3.2 GB left."`. Free fail → `"Waiting for cache space: needs 12.0 GB, cache would drop below the 20 GB minimum free."`. Free space = `f_bavail * f_frsize`. `tree_size` result cached for `cache_seconds`; `invalidate()` clears it.

- [ ] **Step 1: Write failing tests** with fake `statvfs` and `size_fn`:
  - `test_zero_needed_always_fits` (even with missing dir).
  - `test_budget_counts_used_and_running`: used 40 GB + running 10 GB + needs 15 GB, budget 60 → message; needs 10 GB → None.
  - `test_min_free`: free 25 GB, needs 10 GB, min 20 → message with `"20 GB minimum free"`.
  - `test_missing_work_dir`.
  - `test_tree_size_cached`: two `check` calls within 60 s call `size_fn` once; after advancing the fake clock, twice.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat: cache space check`

---

### Task 8: Job table and per-drive workers

**Files:**
- Create/extend: `src/disc_ripper/jobs.py`, create `src/disc_ripper/runner.py`
- Test: `tests/test_jobs.py`, `tests/test_runner.py`

**Interfaces:**
- Consumes: `space.SpaceChecker`, `events.Notifier`, `settings.get`.
- Produces (`jobs.py`, all sync, take a connection, commit their own writes):
  - `enqueue(conn, drive_id: str, kind: str, params: dict, *, needs_bytes: int = 0, uses_drive: bool = False, now: str) -> int`
  - `get(conn, job_id: int) -> sqlite3.Row | None`
  - `next_job(conn, drive_id: str) -> sqlite3.Row | None` — lowest id in `queued`/`waiting_space`.
  - `start(conn, job_id, now)`, `wait_space(conn, job_id, message)`, `finish(conn, job_id, state: Literal["done","failed","cancelled","interrupted"], now, *, error: str | None = None)`, `set_progress(conn, job_id, progress: float, message: str, estimated_finish: str | None)`.
  - `cancel_queued(conn, job_id, now) -> Literal["cancelled", "running", "not_found", "finished"]`
  - `retry(conn, job_id, now) -> int | None` — copies kind/params/needs/uses_drive for `interrupted`/`failed`/`cancelled`; else None.
  - `mark_interrupted(conn, now) -> int` — all `running` → `interrupted` with message `"Interrupted by a restart or shutdown."`.
  - `running_jobs(conn) -> list[Row]`, `running_uses_drive(conn, drive_id) -> bool`, `running_bytes(conn) -> int`, `drive_jobs(conn, drive_id, recent: int = 5) -> DriveJobs` with `@dataclass class DriveJobs: running: Row | None; queued: list[Row]; recent: list[Row]`.
- Produces (`runner.py`):
  - `class JobContext` — `job_id: int`, `params: dict`, `async def report(progress: float, message: str, estimated_finish: datetime | None = None)` (DB write and `notifier.drive_changed` at most once per second, always for progress 1.0), `async def sleep(seconds: float)` (scaled by the runner's `time_scale`).
  - `Handler = Callable[[JobContext], Awaitable[None]]`; `HANDLERS: dict[str, Handler] = {"test": run_test_job}`.
  - `async def run_test_job(ctx)` — `duration_s` from params; reports `estimated_finish = now + duration`; ticks once per (scaled) second with message `"Test job: 3m 20s left"`; ends at progress 1.0.
  - `class JobRunner(config: AppConfig, notifier: Notifier, space: SpaceChecker, *, handlers: dict[str, Handler] = HANDLERS, time_scale: float = 1.0, recheck_seconds: float = 30.0)`:
    - `sync_workers(drive_ids: set[str]) -> None` — start a worker task per id not yet running, cancel workers for ids no longer present.
    - `wake(drive_id: str) -> None`
    - `async def cancel(job_id: int) -> bool` — queued → cancelled via `cancel_queued`; running → cancel its task, final state `cancelled`; returns whether anything changed.
    - `async def stop() -> None` — cancel all workers; running jobs end `interrupted`.
    - Worker loop: `next_job`; space check (`settings` budget/min free, `running_bytes`) → `wait_space` + notify + wait for wake or `recheck_seconds`; else `start`, run handler, `finish("done")`; handler exception → `finish("failed", error=str(exc))`; notify after each state change.

- [ ] **Step 1: Write failing tests**
  - `test_jobs.py`: `test_enqueue_and_next_job_order`, `test_cancel_queued_states` (queued → cancelled; running → "running"; done → "finished"; 999 → "not_found"), `test_retry_copies_job` (only for interrupted/failed/cancelled), `test_mark_interrupted`, `test_drive_jobs_grouping`, `test_running_bytes_sums_running_only`.
  - `test_runner.py` (`time_scale=0.001`, recording notifier, fake `SpaceChecker`):
    - `test_runs_jobs_one_at_a_time_in_order`: two test jobs on drive A (duration 2 s each) — never both `running`; both `done`; A's second starts after the first's `finished_at`.
    - `test_two_drives_run_in_parallel`.
    - `test_waiting_space_blocks_queue_then_runs`: fake check returns a message first, None later → state `waiting_space` with that message, then `done` after `wake`.
    - `test_cancel_running_job`: final state `cancelled`, next queued job starts.
    - `test_handler_error_marks_failed`: handler raising `RuntimeError("boom")` → `failed`, `error == "boom"`.
    - `test_stop_marks_running_interrupted`.
    - `test_progress_throttled`: a handler reporting 100 times instantly → at most 2 DB writes per real second and a final 1.0.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat: per-drive job queues with a test job`

---

### Task 9: Busy report

**Files:**
- Create: `src/disc_ripper/busy.py`
- Test: `tests/test_busy.py`

**Interfaces:**
- Consumes: `schedule.GuardState`, `jobs.running_jobs`.
- Produces: `busy_report(conn, guard: GuardState) -> dict` with keys `busy` (bool: any running job and `guard.holds_shutdown`), `phase`, `scheduledEnd`, `graceEndsAt` (ISO or None), `jobs` (list of `{"id", "drive", "kind", "estimatedFinish"}`; `drive` = label or model).

- [ ] **Step 1: Write failing tests**: `test_idle_not_busy`; `test_running_job_busy_in_window_and_draining`; `test_off_hours_never_busy` (running job, phase `off_hours` → False, job still listed); `test_no_schedule_busy_if_running`; `test_queued_job_does_not_hold`.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat: busy report for the shutdown guard`

---

### Task 10: Services wiring, lifespan and container changes

**Files:**
- Create: `src/disc_ripper/services.py`
- Modify: `src/disc_ripper/app.py`, `src/disc_ripper/web/drive_routes.py`, `Dockerfile`, `docker/entrypoint.sh`, `unraid/disc-ripper.xml`
- Test: `tests/test_services.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `class Services(config: AppConfig, *, drive_factory=CdDrive, fetch=http_get)`: attributes `broker`, `monitor`, `runner`, `space`; implements `Notifier` (`drive_changed` renders `_drive_card.html` for that drive and publishes `drive-<key>`; `guard_changed` renders `_guard.html` and publishes `guard`); `drive_key(drive_id: str) -> str` = first 10 hex chars of SHA-1 of the id; `refresh_drives() -> None` (re-reads present+enabled drives → `runner.sync_workers`); `async def start()` — `jobs.mark_interrupted`, then tasks: `monitor.run()`, schedule refresher (every 300 s, `schedule_source.refresh`, `guard_changed` after each run, logging the error once per failure streak), guard ticker (`guard_changed` every 60 s), `refresh_drives()`; `async def stop()` — cancel tasks, `runner.stop()`.
  - `create_app` uses a FastAPI `lifespan`: `app.state.services = Services(config)`; start/stop only if `config.run_background`. Remove the `@app.on_event` style if any.
  - Drive routes `rescan` and `update` call `request.app.state.services.refresh_drives()`.
  - Rendering helpers used by both `Services` and routes live in `web/dashboard_routes.py` (Task 11) as `render_drive_card(conn, drive_row, now) -> str` and `render_guard(conn, now) -> str`; in this task create them with minimal templates that Task 11 fills out.

- [ ] **Step 1: Write failing tests**
  - `test_lifespan_shutdown_marks_running_interrupted`: config with `run_background=True`, a fake drive factory (always empty), fake fetch failing; `with TestClient(create_app(cfg))`: enqueue a 60 s test job (`time_scale` irrelevant), wait until `running`; exit the context → job state `interrupted`.
  - `test_startup_marks_leftover_running_interrupted`: DB with a `running` job before app start → `interrupted` after startup.
  - `test_drive_key_stable`: same id → same key, valid CSS identifier chars only.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.**
- [ ] **Step 4: Container changes**: Dockerfile installs `tini`, `ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]`; entrypoint adds `--timeout-graceful-shutdown 10` to the uvicorn command; template adds `<Config Name="Busy secret" Target="BUSY_SECRET" Default="" Mode="" Description="Shared secret unraid-backend sends to /api/busy (X-Shutdown-Secret). Use a long random string." Type="Variable" Display="always" Required="false" Mask="true"></Config>` and `WORK_DIR` needs no template entry (default `/work`).
- [ ] **Step 5: Run** `pytest -q` — PASS. `docker build -t disc-ripper:step2 .` succeeds; `docker run --rm --entrypoint sh disc-ripper:step2 -c "tini --version"` prints a version.
- [ ] **Step 6: Commit** `feat: background services, lifespan and tini`

---

### Task 11: Dashboard, actions, SSE and `/api/busy` routes

**Files:**
- Create: `src/disc_ripper/web/dashboard_routes.py`, `src/disc_ripper/templates/dashboard.html`, `_drive_card.html`, `_guard.html`, `src/disc_ripper/static/htmx-ext-sse.js`
- Modify: `src/disc_ripper/app.py` (router, `/` → dashboard, `/api/busy` in open paths), `templates/base.html` (nav "Dashboard" first; load `htmx-ext-sse.js`), `static/app.css` (progress bar), `README.md`
- Test: `tests/test_web_dashboard.py`

**Interfaces:**
- Consumes: `Services`, `jobs.*`, `runner.JobRunner.cancel/wake`, `media.MediaMonitor.drive_lock`, `cdrom.CdDrive`, `busy.busy_report`, `schedule_source.current_guard`, `events.format_sse`.
- Produces routes (all login-protected except `/api/busy`; POSTs return the re-rendered `_drive_card.html` for that drive with `hx-target` = the card and `hx-swap="outerHTML"`):
  - `GET /` → dashboard (drive cards for present+enabled drives, guard panel, `hx-ext="sse" sse-connect="/events"`; each card `sse-swap="drive-<key>"`, guard `sse-swap="guard"`).
  - `POST /dashboard/eject` (`drive_id`), `POST /dashboard/close` (`drive_id`) — run `CdDrive(dev_root/sr_name).eject()/close_tray()` via `asyncio.to_thread` under `monitor.drive_lock`; `OSError` → card shows `"Eject failed: <strerror>"`. If a running job `uses_drive`, cancel it first.
  - `POST /jobs/test` (`drive_id`, `minutes` int 1–360, `size_gb` float 0–500) → `jobs.enqueue(kind="test", params={"duration_s": minutes*60, "size_gb": size_gb}, needs_bytes=int(size_gb*GB))`, `runner.wake`; invalid → card with the error, status 400.
  - `POST /jobs/{job_id}/cancel`, `POST /jobs/{job_id}/retry`.
  - `GET /events` → `StreamingResponse(media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})` from `async def event_stream(services, request) -> AsyncIterator[str]`: initial burst (guard + every card), then broker events; `": ping\n\n"` after 15 s idle; ends when the request disconnects or the subscription is dropped.
  - `GET /api/busy` → 503 `{"error": "BUSY_SECRET not set"}` if unset; 401 on wrong/missing `X-Shutdown-Secret` (`hmac.compare_digest`); else `busy_report`.
- Card content (spec sketch): label (or model), model line, media state text (`Empty`, `Tray open`, `Not ready`, `Disc in`, `Unknown`) plus type (`Audio CD`, `DVD`, `Blu-ray`, `Data disc`, `Enhanced CD`, `Unknown type`) and quoted label; running job with `<progress>` + percent + "finishes ~HH:MM" (local); queued list with Cancel; last 5 finished with state and Retry for interrupted/failed/cancelled; test job form; warning `"Expected to finish after the grace period ends (HH:MM)."` when the estimate (or now + chosen minutes, computed on render using the default 20 min) is past `grace_ends_at`.
- Guard panel: `"No schedule could be read. The shutdown guard only knows whether a job is running."` for `no_schedule`; otherwise today's window, phase in words (`On schedule until 23:00`, `Draining until 02:00`, `Outside scheduled hours`), grace length, schedule source and fetch time.

- [ ] **Step 1: Vendor** `htmx-ext-sse@2.2.2/sse.js` from unpkg into `static/htmx-ext-sse.js`; check the file header names version 2.2.2.
- [ ] **Step 2: Write failing tests** (`logged_in_client`, drives added with `add_sg` + rescan; `services` from `client.app.state.services`; for eject a fake drive factory injected via `app.state.services.drive_factory`):
  - `test_root_is_dashboard`: `/` 200, contains `sse-connect="/events"` and the drive's card id.
  - `test_dashboard_escapes_label`: `set_media(..., label="<script>x</script>")` → page contains `&lt;script&gt;`, not `<script>x`.
  - `test_queue_test_job`: POST `/jobs/test` minutes 20 size 0 → 200, card shows "Queued", DB has the job with `params.duration_s == 1200`.
  - `test_queue_test_job_validation`: minutes 0 / 361 / "x" → 400 with an error message, no job.
  - `test_cancel_and_retry_routes`.
  - `test_eject_error_shown`: fake drive's `eject` raises `OSError(errno.EBUSY, "Device or resource busy")` → card contains `"Eject failed: Device or resource busy"`.
  - `test_busy_endpoint_auth`: unset secret → 503; set (`config.busy_secret="s3cret"`) wrong → 401; right → 200 JSON with `busy` False; works without the login cookie.
  - `test_event_stream_initial_burst`: drive `event_stream` directly with a fake request (`is_disconnected` → True after the burst) — first chunks are `event: guard` and `event: drive-<key>`.
  - `test_events_requires_login`: `/events` without session → 303 to login.
  - Existing `test_root_redirects_to_drives` changes to assert the dashboard renders.
- [ ] **Step 3: Run** — FAIL. **Step 4: Implement.** **Step 5: Run** `pytest -q` — PASS.
- [ ] **Step 6: Manual check locally** (`run_background` on, no drives): start uvicorn, log in, open `/`, queue a 1-minute test job on a fake drive inserted into the DB, watch the bar move without reloading; stop uvicorn with the tab open → it exits within ~10 s and the job is `interrupted`.
- [ ] **Step 7: README**: step 2 state, `BUSY_SECRET`, the two schedule URL settings, and the manual test list from the spec.
- [ ] **Step 8: Commit** `feat: dashboard with live drive state and test jobs`

---

### Task 12: unraid-backend busy check (barry-mc-network)

**Files (repo `../barry-mc-network`, branch `ripper-busy-guard`):**
- Create: `unraid-backend/ripperClient.js`
- Modify: `unraid-backend/powerControl.js`, `unraid-backend/server.js`, `CLAUDE.md` (Paired timeouts), `README.md` (env vars)

**Interfaces:**
- Produces:
  - `ripperClient.js`: `async function fetchRipperBusy({ url, secret, timeoutMs = 5000 }) -> boolean` — true only for HTTP 200 with JSON `busy === true`; any error/timeout/other status → false.
  - `powerControl.createPowerControl({ signalPath, logger, checkRipperBusy = null, ripperPollMs = 60_000, ripperMaxHoldMs = 4 * 60 * 60 * 1000 })`:
    - `requestShutdown()` also triggers an immediate ripper check and starts a 60 s interval while pending (cleared when the flag is written or the request expires).
    - `maybeExecuteShutdown(totalPlayers)`: unchanged players logic; then if `checkRipperBusy` is set, the last answer was busy, and `Date.now() - pendingSince < ripperMaxHoldMs` → return (log "waiting for ripper" once per change); otherwise write the flag.
    - Exports `RIPPER_MAX_HOLD_MS`.
  - `server.js`: `RIPPER_BUSY_URL`, `RIPPER_BUSY_SECRET` env; both set → pass `checkRipperBusy`, else null (behaviour as today).

- [ ] **Step 1:** Branch from barry-mc-network `main`; implement the three files.
- [ ] **Step 2: Verify** with a scratch Node script (not committed) that stubs `fetchRipperBusy` and drives `createPowerControl` with fake time: busy blocks the flag, idle writes it, 4 h cap writes it while busy, unset check behaves as before. Run: `node scratch-test.js` → all assertions pass.
- [ ] **Step 3:** `CLAUDE.md` Traps → Paired timeouts gains: "`RIPPER_MAX_HOLD_MS` (4 h) in powerControl.js must stay longer than super-ripper's `shutdown_grace_hours` maximum (3.5 h)." README documents the two env vars.
- [ ] **Step 4: Commit** `feat: hold shutdown while the disc ripper is busy`, push, open a PR describing the manual checks.

---

### Task 13: Final verification and PR

- [ ] **Step 1:** `pytest -q` on Windows → all pass (Linux-only ones skipped).
- [ ] **Step 2:** Linux run: `docker run --rm -v <repo>:/src:ro python:3.12-slim-bookworm sh -c "cp -r /src /app && cd /app && rm -rf .venv && pip install -q -e '.[dev]' && pytest -q"` → all pass, none skipped except Windows-only.
- [ ] **Step 3:** Build the image and smoke-run it: `/healthz` 200; `/api/busy` 503 without `BUSY_SECRET`, 401 with a wrong header when set; `docker stop` returns in < 15 s with a browser-like `curl -N /events` stream open (after logging in with a cookie jar).
- [ ] **Step 4:** Whole-branch review; fix findings.
- [ ] **Step 5:** Push `step-2-detection-queues-guard`, open the PR with the on-server manual test list (insert/eject each drive with a CD, DVD and Blu-ray; two test jobs on one drive; restart mid-job; oversize job waits; `curl /api/busy`; a real evening shutdown with a test job running past the scheduled end).
