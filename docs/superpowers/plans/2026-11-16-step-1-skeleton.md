# Step 1 – Skeleton Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A container you can install on Unraid that serves a password-protected htmx web UI, stores state in SQLite, lists the optical drives by a stable ID, and has an editable settings page.

**Architecture:** A FastAPI app (`disc_ripper`, src layout) renders Jinja2 templates with htmx for partial updates. SQLite (Python's built-in `sqlite3`) in WAL mode lives in `/config`, with numbered SQL migrations. At start-up a root entrypoint creates device nodes for the optical drives only, using what it reads from `/sys`, then drops to 99:100 and runs uvicorn. GitHub Actions tests the code, builds the image and pushes it to GHCR.

**Tech Stack:** Python 3.12 (container) / 3.14 (local dev on Windows), FastAPI, uvicorn, Jinja2, itsdangerous, python-multipart, htmx 2.0.4 (vendored), pytest, httpx, Docker, GitHub Actions.

**Spec:** `Disc Ripper – Design Plan.md` (build order step 1; Environment and hardware; Architecture; Security and file permissions).

## Global Constraints

- SQLite runs inside the app container through Python's built-in `sqlite3`; no separate database container or server. The DB file is `/config/disc-ripper.db`, so it survives image updates. Connections use WAL mode.
- The app process runs as PUID 99 / PGID 100 (`nobody:users`). There is no `--privileged` flag and no Docker socket mount.
- Optical drives are identified by serial (from sysfs), never by `srX`/`sgX` number; the numbers are only the current location.
- Secrets (password hash, session secret) are never returned by any route or written to logs.
- No `shell=True` and no shell strings built from input; every subprocess uses a fixed argument list.
- htmx is served from `/static`, not from a CDN.
- Listening port: `8580`. Config dir env `CONFIG_DIR` (default `/config`), sysfs root `SYSFS_ROOT` (default `/sys`), device root `DEV_ROOT` (default `/dev`).
- Tests must pass on Windows (local) and Linux (CI). Use `pathlib`, and never call Linux-only functions such as `os.mknod` or `os.chown` at import time.
- Shell scripts are committed with LF line endings.

## Review Focus

1. **First boot on an empty `/config`:** the DB and secret key are created, and every page redirects to `/setup` until a password exists. (Task 6, `test_first_boot_redirects_to_setup`.)
2. **A drive with no readable serial** (missing or truncated `vpd_pg80`, no `wwid`): it is still listed, using the port-based ID, and nothing crashes. (Task 4, `test_falls_back_when_vpd_truncated`, `test_port_fallback_id`.)
3. **The sg number changes after a reboot** (`sg2` becomes `sg3`): it is the same drive record, and its label and enabled flag are kept. (Task 4, `test_sync_keeps_label_when_numbers_change`.)
4. **Device node not passed in, or not accessible:** the drives page shows "missing" or "no access" with a hint, and returns no 500. (Task 7, `test_drive_page_shows_node_problems`.)
5. **Session expired during an htmx request:** the browser goes to the login page instead of the login form being swapped into a table cell. (Task 6, `test_htmx_request_gets_hx_redirect`.)

---

## File Structure

```
pyproject.toml                         # package, deps, pytest config
.gitattributes                         # *.sh eol=lf
.dockerignore
Dockerfile
docker/entrypoint.sh                   # root: create device nodes, drop to PUID/PGID, run uvicorn
unraid/disc-ripper.xml                 # Unraid container template
.github/workflows/build.yml            # test → build → push to GHCR
src/disc_ripper/
  __init__.py
  config.py                            # AppConfig from env
  db.py                                # connect() + migrate()
  migrations/0001_initial.sql
  settings.py                          # settings registry, form parsing, storage
  auth.py                              # password hashing, secret key, sessions
  drives.py                            # sysfs discovery, node status, DB sync
  devnodes.py                          # plan/apply mknod for optical drives (entrypoint)
  app.py                               # create_app(): routers, middleware, static
  web/__init__.py
  web/deps.py                          # get_conn, get_config, templates
  web/auth_routes.py                   # /setup /login /logout
  web/drive_routes.py                  # /drives, rescan, update
  web/settings_routes.py               # /settings, password change
  templates/{base,setup,login,drives,_drive_table,settings}.html
  static/htmx.min.js, static/app.css
tests/
  conftest.py                          # config, fake sysfs builder, clients
  test_db.py test_settings.py test_auth.py test_drives.py test_devnodes.py
  test_web_auth.py test_web_pages.py
```

---

### Task 1: Project scaffold, config and database

**Files:**
- Create: `pyproject.toml`, `.gitattributes`, `src/disc_ripper/__init__.py`, `src/disc_ripper/config.py`, `src/disc_ripper/db.py`, `src/disc_ripper/migrations/0001_initial.sql`, `tests/conftest.py`, `tests/test_db.py`

**Interfaces:**
- Produces:
  - `AppConfig(config_dir: Path, sysfs_root: Path, dev_root: Path)` (frozen dataclass) with properties `db_path -> config_dir / "disc-ripper.db"` and `secret_key_path -> config_dir / "secret_key"`
  - `load_config(env: Mapping[str, str] = os.environ) -> AppConfig`
  - `db.connect(db_path: Path) -> sqlite3.Connection`: creates the parent dir, `row_factory = sqlite3.Row`, `PRAGMA journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=5000`
  - `db.migrate(conn: sqlite3.Connection) -> int`: applies the `migrations/NNNN_*.sql` files whose number is greater than `PRAGMA user_version`, in order, one transaction each, and returns the new version
  - Fixture `config(tmp_path) -> AppConfig` with dirs `tmp_path/"config"`, `tmp_path/"sys"`, `tmp_path/"dev"`

`pyproject.toml`: hatchling build backend, `requires-python = ">=3.12"`, deps `fastapi>=0.115`, `uvicorn[standard]>=0.32`, `jinja2>=3.1`, `itsdangerous>=2.2`, `python-multipart>=0.0.18`; extra `dev = ["pytest>=8.3", "httpx>=0.27"]`; `[tool.pytest.ini_options] testpaths = ["tests"]`. `.gitattributes`: `*.sh text eol=lf`.

`0001_initial.sql`:

```sql
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE drives (
  id TEXT PRIMARY KEY,
  id_source TEXT NOT NULL CHECK (id_source IN ('serial','wwid','port')),
  vendor TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '', revision TEXT NOT NULL DEFAULT '',
  sg_name TEXT, sr_name TEXT,
  label TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1, present INTEGER NOT NULL DEFAULT 0,
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
);
```

- [ ] **Step 1: Write the failing tests** in `tests/test_db.py`
  - `test_load_config_defaults`: `load_config({})` gives `/config`, `/sys`, `/dev`.
  - `test_load_config_from_env`: `CONFIG_DIR`, `SYSFS_ROOT`, `DEV_ROOT` are honoured.
  - `test_connect_uses_wal(config)`: `conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"`, and the parent dir is created.
  - `test_migrate_fresh_db(config)`: `migrate()` returns `1`; the `settings` and `drives` tables exist.
  - `test_migrate_is_idempotent(config)`: a second `migrate()` returns `1` and changes nothing.
- [ ] **Step 2: Run them to verify they fail.** Run: `python -m venv .venv; .venv\Scripts\pip install -e .[dev]; .venv\Scripts\pytest tests/test_db.py -v`. Expected: FAIL (module not found).
- [ ] **Step 3: Implement `config.py`, `db.py` and the migration.** Read the migrations with `importlib.resources.files("disc_ripper") / "migrations"`, sorted by their 4-digit prefix.
- [ ] **Step 4: Run the tests.** Expected: 5 passed.
- [ ] **Step 5: Commit.** `git add -A; git commit -m "feat: project scaffold, config and SQLite migrations"`

### Task 2: Settings registry

**Files:**
- Create: `src/disc_ripper/settings.py`, `tests/test_settings.py`

**Interfaces:**
- Consumes: `db.connect`, `db.migrate`
- Produces:
  - `SettingDef(key: str, label: str, kind: Literal["int","float","bool","path"], default: Any, unit: str = "", minimum: float | None = None, maximum: float | None = None, help: str = "")`
  - `SETTINGS: tuple[SettingDef, ...]`
  - `class SettingsError(ValueError)` with `errors: dict[str, str]`
  - `get_all(conn) -> dict[str, Any]`: defaults merged with stored values
  - `get(conn, key: str) -> Any`: raises `KeyError` for unknown keys
  - `parse_form(form: Mapping[str, str]) -> dict[str, Any]`: a bool is True only when present with value `on`, `true` or `1`
  - `save(conn, values: dict[str, Any]) -> None`: stores JSON values
  - `get_internal(conn, key: str, default: Any = None) -> Any` and `set_internal(conn, key: str, value: Any) -> None`: the key must start with `internal.`; these keys are never part of `get_all`

Registry values (from the spec):

| key | kind | default | unit | min | max |
| --- | --- | --- | --- | --- | --- |
| `work_budget_gb` | int | 60 | GB | 1 | 10000 |
| `cache_min_free_gb` | int | 20 | GB | 0 | 10000 |
| `auto_start_rips` | bool | True | | | |
| `shutdown_grace_hours` | float | 3 | hours | 0 | 11 |
| `raw_retention_hours` | int | 24 | hours | 1 | 720 |
| `iso_dir` | path | `/data/ISOs` | | | |

A `path` must be absolute POSIX (it starts with `/`) and have no `..` segment. The help text for `shutdown_grace_hours` says unraid-backend's safety cap must stay longer than this value.

- [ ] **Step 1: Write the failing tests**
  - `test_defaults_when_empty`: `get_all(conn)["work_budget_gb"] == 60` and `["iso_dir"] == "/data/ISOs"`.
  - `test_parse_form_valid`: the form `{"work_budget_gb": "100", "cache_min_free_gb": "20", "shutdown_grace_hours": "2.5", "raw_retention_hours": "24", "iso_dir": "/data/ISOs"}` gives `auto_start_rips is False` (the box is missing) and `shutdown_grace_hours == 2.5`.
  - `test_parse_form_collects_all_errors`: `work_budget_gb="abc"`, `cache_min_free_gb="-1"`, `iso_dir="data/../x"` raise `SettingsError` whose `errors` has exactly those 3 keys.
  - `test_save_round_trip`: after `save()`, `get(conn, "work_budget_gb") == 100`.
  - `test_internal_keys_hidden`: after `set_internal(conn, "internal.x", 1)`, `"internal.x" not in get_all(conn)`; `set_internal(conn, "x", 1)` raises `ValueError`.
- [ ] **Step 2: Run them to verify they fail.** Run: `.venv\Scripts\pytest tests/test_settings.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement `settings.py`.**
- [ ] **Step 4: Run the tests.** Expected: 5 passed.
- [ ] **Step 5: Commit.** `git add -A; git commit -m "feat: settings registry"`

### Task 3: Passwords, secret key and sessions

**Files:**
- Create: `src/disc_ripper/auth.py`, `tests/test_auth.py`

**Interfaces:**
- Consumes: `settings.get_internal`, `settings.set_internal`
- Produces:
  - `MIN_PASSWORD_LENGTH = 8`
  - `hash_password(password: str) -> str`: `hashlib.scrypt` with n=2\*\*14, r=8, p=1, dklen=32 and a 16-byte random salt, stored as `scrypt$16384$8$1$<salt b64>$<hash b64>`
  - `verify_password(password: str, stored: str) -> bool`: `hmac.compare_digest`; False for any malformed `stored`
  - `load_or_create_secret(path: Path) -> str`: `secrets.token_urlsafe(32)` written once, then `chmod 0o600` (errors ignored on Windows)
  - `class SessionManager(secret: str)` with `COOKIE_NAME = "dr_session"`, `MAX_AGE = 30 * 24 * 3600`, `issue(session_version: int) -> str`, and `read(token: str | None) -> int | None` (None if the token is missing, tampered with or expired; uses `itsdangerous.URLSafeTimedSerializer`)
  - `is_password_set(conn) -> bool`
  - `set_password(conn, password: str) -> int`: raises `ValueError` if shorter than `MIN_PASSWORD_LENGTH`; stores the hash in `internal.auth.password_hash`, increments `internal.auth.session_version` (starting at 0) and returns the new version
  - `check_login(conn, password: str) -> int | None`: the current session version on success
  - `current_session_version(conn) -> int`

- [ ] **Step 1: Write the failing tests**
  - `test_hash_and_verify`: `verify_password("correct horse", hash_password("correct horse"))` is True; a wrong password gives False; `verify_password("x", "garbage")` gives False.
  - `test_hashes_are_salted`: two hashes of the same password differ.
  - `test_secret_created_once(tmp_path)`: two calls return the same value; the file exists.
  - `test_session_round_trip`: `read(issue(3)) == 3`; `read("tampered")` is None; `read(None)` is None.
  - `test_session_expiry`: with a `MAX_AGE` of `-1` patched on the instance, `read(issue(1))` is None.
  - `test_set_password_bumps_version`: the first `set_password` returns 1, the second 2; `check_login` with the new password gives 2 and with the old password gives None.
  - `test_short_password_rejected`: `set_password(conn, "short")` raises `ValueError`.
- [ ] **Step 2: Run them to verify they fail.** Run: `.venv\Scripts\pytest tests/test_auth.py -v`
- [ ] **Step 3: Implement `auth.py`.**
- [ ] **Step 4: Run the tests.** Expected: 7 passed.
- [ ] **Step 5: Commit.** `git commit -m "feat: password hashing and signed sessions"`

### Task 4: Drive discovery

**Files:**
- Create: `src/disc_ripper/drives.py`, `tests/test_drives.py`
- Modify: `tests/conftest.py` (add the fake sysfs builder)

**Interfaces:**
- Produces:
  - `DiscoveredDrive(id: str, id_source: Literal["serial","wwid","port"], vendor: str, model: str, revision: str, sg_name: str, sr_name: str | None, sg_dev: tuple[int, int], sr_dev: tuple[int, int] | None)` (frozen dataclass)
  - `discover(sysfs_root: Path) -> list[DiscoveredDrive]`: sorted by sg number; only devices whose `device/type` is `5` (optical)
  - `read_vpd_serial(data: bytes) -> str | None`
  - `node_status(dev_root: Path, name: str | None) -> Literal["ok", "missing", "no-access"]`: missing when `name` is None or the node doesn't exist; no-access when `os.access(path, R_OK | W_OK)` is False
  - `sync(conn, found: list[DiscoveredDrive], now: str) -> None`: upserts by `id` (keeping `label`, `enabled` and `first_seen`), sets `present=1` for those found and `present=0` for the rest
  - `list_drives(conn) -> list[sqlite3.Row]`: present drives first, then by `sg_name`
  - `update_drive(conn, drive_id: str, *, label: str, enabled: bool) -> bool`: False if the drive is unknown; the label is stripped and limited to 40 chars
  - Fixture `add_sg(sysfs_root, n, *, type=5, vendor="HL-DT-ST", model="BD-RE WH16NS40", rev="1.05", sr: int | None = 0, vpd_serial: bytes | None = None, wwid: str | None = None) -> Path`, which creates `class/scsi_generic/sg{n}/dev` (`21:{n}`), `.../device/{type,vendor,model,rev}`, `.../device/block/sr{sr}/dev` (`11:{sr}`), and the optional `device/vpd_pg80` and `device/wwid`

Sysfs paths read for `sgN` under `<sysfs_root>/class/scsi_generic/sgN/`: `dev`, `device/type`, `device/vendor`, `device/model`, `device/rev`, `device/vpd_pg80`, `device/wwid`, `device/block/sr*/dev`. Values are stripped of whitespace.

ID rule, first match wins:
1. **`serial:<serial>`:** from `read_vpd_serial`. The page bytes are `[0]` peripheral, `[1]` = `0x80`, `[2:4]` big-endian length, then ASCII. The result is None if the data is shorter than 4 bytes, the page code isn't `0x80`, or the decoded text is empty after stripping spaces and NULs.
2. **`wwid:<wwid>`:** from a non-empty `device/wwid`.
3. **`port:<port>:<model>`:** `<port>` is the first `ataN` part of `os.path.realpath(sgN/device)`, or failing that the last part matching `\d+:\d+:\d+:\d+`, or failing that `sgN`.

- [ ] **Step 1: Write the failing tests**
  - `test_discovers_optical_only`: an sg0 disk (`type=0`) and an sg1 optical drive with `vpd_serial=b"\x05\x80\x00\x08ABC123  "` give one drive, `id == "serial:ABC123"`, `sr_name == "sr0"`, `sg_dev == (21, 1)`.
  - `test_falls_back_when_vpd_truncated`: `vpd_serial=b"\x05\x80"` plus `wwid="naa.5000"` gives `id == "wwid:naa.5000"`.
  - `test_port_fallback_id`: no vpd or wwid, with `device` as a symlink into `devices/pci0000:00/ata3/host2/target2:0:0/2:0:0:0` gives `id == "port:ata3:BD-RE WH16NS40"`. Skip the test if `os.symlink` raises `OSError` (Windows without developer mode).
  - `test_drive_without_sr`: `sr=None` gives `sr_name is None` and `sr_dev is None`.
  - `test_sync_keeps_label_when_numbers_change`: sync the drive as sg1/sr0, `update_drive(label="Blu-ray", enabled=False)`, then sync the same serial as sg3/sr1. The row has `sg_name == "sg3"`, `label == "Blu-ray"`, `enabled == 0`, `present == 1`, and `first_seen` is unchanged.
  - `test_sync_marks_absent`: syncing with `[]` sets `present == 0`.
  - `test_node_status`: missing file gives `"missing"`; `None` gives `"missing"`; an existing file gives `"ok"`.
- [ ] **Step 2: Run them to verify they fail.** Run: `.venv\Scripts\pytest tests/test_drives.py -v`
- [ ] **Step 3: Implement `drives.py`.**
- [ ] **Step 4: Run the tests.** Expected: 7 passed (or 6 passed and 1 skipped on Windows).
- [ ] **Step 5: Commit.** `git commit -m "feat: optical drive discovery by serial"`

### Task 5: Device nodes for the entrypoint

The design plan maps drives with `--device` by ID. `sg` nodes have no `/dev/disk/by-id` links, and their numbers change when disks are added. So the container instead gets cgroup permission for the sr (block 11) and sg (char 21) majors. Its root entrypoint then creates only the nodes for the optical drives it found in sysfs, owned by group PGID. With this approach the Unraid template never needs editing when numbers change.

**Files:**
- Create: `src/disc_ripper/devnodes.py`, `tests/test_devnodes.py`

**Interfaces:**
- Consumes: `drives.discover`, `DiscoveredDrive`
- Produces:
  - `DeviceNode(path: Path, kind: Literal["c","b"], major: int, minor: int)` (frozen dataclass)
  - `plan_nodes(found: list[DiscoveredDrive], dev_root: Path) -> list[DeviceNode]`: one `c` node per `sg_dev` and one `b` node per `sr_dev`
  - `apply_nodes(nodes: list[DeviceNode], gid: int, mode: int = 0o660) -> list[str]`: for each node, `os.mknod` if missing, then `os.chown(path, 0, gid)` and `os.chmod(path, mode)`. It returns one human-readable line per node, and a failure on one node is reported in its line and doesn't stop the rest.
  - `main() -> int`: reads `SYSFS_ROOT`, `DEV_ROOT`, `PGID` (default 100), prints the lines, and always returns 0

- [ ] **Step 1: Write the failing tests**
  - `test_plan_nodes`: a drive with `sg_dev=(21, 2)` and `sr_dev=(11, 0)` gives `[DeviceNode(dev/"sg2","c",21,2), DeviceNode(dev/"sr0","b",11,0)]`.
  - `test_plan_skips_missing_sr`: `sr_dev=None` gives only the sg node.
  - `test_apply_reports_failure_and_continues`: `monkeypatch` `os.mknod` to raise `PermissionError` for the first node and `os.chown`/`os.chmod` to no-ops (use `raising=False`, so the test also runs on Windows where `os.mknod` doesn't exist). Two lines come back, and the first one contains `"failed"`.
- [ ] **Step 2: Run them to verify they fail.** Run: `.venv\Scripts\pytest tests/test_devnodes.py -v`
- [ ] **Step 3: Implement `devnodes.py`.** Use `stat.S_IFCHR`/`stat.S_IFBLK` with `os.makedev`, both looked up at call time and not at import time.
- [ ] **Step 4: Run the tests.** Expected: 3 passed.
- [ ] **Step 5: Commit.** `git commit -m "feat: create optical device nodes at container start"`

### Task 6: Web app, login and first-run setup

**Files:**
- Create: `src/disc_ripper/app.py`, `src/disc_ripper/web/__init__.py`, `web/deps.py`, `web/auth_routes.py`, `templates/base.html`, `templates/setup.html`, `templates/login.html`, `static/app.css`, `static/htmx.min.js`, `tests/test_web_auth.py`
- Modify: `tests/conftest.py` (add the `client` and `logged_in_client` fixtures)

**Interfaces:**
- Consumes: `load_config`, `db.connect`, `db.migrate`, `auth.*`, `drives.discover`, `drives.sync`
- Produces:
  - `create_app(config: AppConfig | None = None) -> FastAPI`: runs `migrate`, loads or creates the secret, runs one `discover` + `sync`, mounts `/static`, and adds the auth middleware and the routers. `app.state.config` and `app.state.sessions` are set. The uvicorn entry is `disc_ripper.app:create_app --factory`.
  - `web.deps.get_conn(request) -> Iterator[sqlite3.Connection]`: one connection per request, closed afterwards
  - `web.deps.templates: Jinja2Templates`, pointing at the package `templates` dir
  - `GET /healthz` returns `{"status": "ok"}` with no auth
  - Fixtures: `client(config) -> TestClient` (`follow_redirects=False`), and `logged_in_client(config)`, where the password is set to `"test-password"` and the client is logged in

Middleware rules, in order (open paths: `/healthz`, `/static/*`):

1. No password set: any path other than `/setup` redirects to `/setup`.
2. Password set and path is `/setup`: redirect to `/login`.
3. Path is `/login`: allowed.
4. The session cookie doesn't read, or its version ≠ `current_session_version`: redirect to `/login?next=<path>` (GET only).

Every "redirect" is a 303, except for requests with the `HX-Request` header, which get a 200 with an `HX-Redirect: <target>` header and an empty body. The cookie is set with `httponly=True`, `samesite="lax"` and `max_age=SessionManager.MAX_AGE`. A failed login waits 1 second (`time.sleep`, patchable) and re-renders with "Wrong password." and a 401. `next` is only followed if it starts with `/` and not `//`; otherwise it falls back to `/`. `POST /logout` deletes the cookie and redirects to `/login`. `GET /` redirects to `/drives`.

Download htmx 2.0.4 to `static/htmx.min.js` from `https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js`. `base.html` loads it and `app.css`, and has a nav (Drives, Settings, a Log out form button) that is shown only when logged in.

- [ ] **Step 1: Write the failing tests**
  - `test_healthz_open(client)`: 200, `{"status": "ok"}`.
  - `test_first_boot_redirects_to_setup(client, config)`: `GET /drives` returns 303 to `/setup`; `config.db_path` and `config.secret_key_path` exist.
  - `test_setup_validation(client)`: a password of `"short"` gives 400 with "at least 8"; a mismatched confirmation gives 400 with "do not match".
  - `test_setup_then_login_flow(client)`: `POST /setup` with a matching 8+ character password returns 303 to `/login`; `GET /setup` now returns 303 to `/login`; `POST /login` with the right password returns 303 and sets the `dr_session` cookie; `GET /drives` is no longer a redirect.
  - `test_wrong_password(client, monkeypatch)`: with `time.sleep` patched out, the response is 401 containing "Wrong password."
  - `test_open_redirect_blocked(client)`: logging in with `next=//evil.example` redirects to `/`.
  - `test_htmx_request_gets_hx_redirect(client)`: after setup, without a session, `POST /drives/rescan` with `HX-Request: true` returns 200 with header `HX-Redirect: /login`.
  - `test_logout(logged_in_client)`: `POST /logout`, then `GET /drives` returns 303 to `/login?next=/drives`.
- [ ] **Step 2: Run them to verify they fail.** Run: `.venv\Scripts\pytest tests/test_web_auth.py -v`
- [ ] **Step 3: Implement the app, middleware, auth routes and templates.** Add stub `GET /drives` and `POST /drives/rescan` routes that return a placeholder; Task 7 replaces them.
- [ ] **Step 4: Run the full suite.** Run: `.venv\Scripts\pytest -v`. Expected: all tests pass.
- [ ] **Step 5: Commit.** `git commit -m "feat: web app with first-run setup and login"`

### Task 7: Drives and settings pages

**Files:**
- Create: `src/disc_ripper/web/drive_routes.py`, `web/settings_routes.py`, `templates/drives.html`, `templates/_drive_table.html`, `templates/settings.html`, `tests/test_web_pages.py`
- Modify: `src/disc_ripper/app.py` (replace the stub routes with the routers)

**Interfaces:**
- Consumes: `drives.discover`, `sync`, `list_drives`, `update_drive`, `node_status`; `settings.SETTINGS`, `get_all`, `parse_form`, `save`; `auth.check_login`, `set_password`
- Produces:
  - `GET /drives`: the full page, with a table of model, vendor, ID (and its source), label, enabled, current `srX`/`sgX`, and the status of each node
  - `POST /drives/rescan` (htmx): runs `discover` + `sync` and returns the `_drive_table.html` partial
  - `POST /drives/update` (htmx, form fields `drive_id`, `label`, `enabled`): returns the partial, or a 404 for an unknown ID
  - `GET /settings`: one input per `SETTINGS` entry, plus the change-password form
  - `POST /settings`: on success, re-renders with "Settings saved."; on `SettingsError`, a 400 with each message next to its field and the submitted values kept
  - `POST /settings/password` (fields `current`, `new`, `confirm`): a wrong current password gives 400 "Current password is wrong."; on success a new cookie is issued for the new version, so this browser stays logged in while other sessions are logged out

Node-status hints shown in the table:
- **missing:** "Not passed into the container – check the template's device rules, then restart the container."
- **no-access:** "No permission – the node's group must be PGID."
- **Drive not present:** "Not detected at last scan."

- [ ] **Step 1: Write the failing tests**
  - `test_drives_page_lists_drive(logged_in_client, config, add_sg)`: sg1 with serial `ABC123`, then `POST /drives/rescan`. The response contains `BD-RE WH16NS40` and `serial:ABC123`.
  - `test_drive_page_shows_node_problems(...)`: no `dev/sg1` file gives "Not passed into the container"; creating `config.dev_root/"sg1"` and rescanning stops that message appearing for sg1.
  - `test_update_drive_label(...)`: `POST /drives/update` with `label="Blu-ray"` puts `Blu-ray` in the response, and the label survives a rescan.
  - `test_update_unknown_drive(logged_in_client)`: 404.
  - `test_settings_save_and_errors(logged_in_client)`: a valid post shows "Settings saved." and `work_budget_gb` 100 persists; `work_budget_gb=abc` gives 400 with the field error, and the other submitted values are still in the form.
  - `test_settings_never_shows_secrets(logged_in_client)`: the `/settings` HTML doesn't contain `scrypt$` or the secret key's contents.
  - `test_change_password_keeps_this_session(logged_in_client)`: after a successful change, `GET /settings` is 200 with the cookie returned by that response; the old cookie value now redirects to login.
- [ ] **Step 2: Run them to verify they fail.** Run: `.venv\Scripts\pytest tests/test_web_pages.py -v`
- [ ] **Step 3: Implement the routes and templates.** Rescan uses `hx-post="/drives/rescan" hx-target="#drive-table" hx-swap="outerHTML"`.
- [ ] **Step 4: Run the full suite.** Run: `.venv\Scripts\pytest -v`. Expected: all tests pass.
- [ ] **Step 5: Run the app locally and look at it.** Run: `$env:CONFIG_DIR="$PWD\.devconfig"; .venv\Scripts\uvicorn disc_ripper.app:create_app --factory --port 8580`, open http://localhost:8580, set a password, log in, view Drives (empty on Windows) and save Settings. Add `.devconfig/` and `.venv/` to `.gitignore`.
- [ ] **Step 6: Commit.** `git commit -m "feat: drives and settings pages"`

### Task 8: Container image and Unraid template

**Files:**
- Create: `Dockerfile`, `docker/entrypoint.sh`, `.dockerignore`, `unraid/disc-ripper.xml`
- Modify: `README.md` (install and update instructions)

**Interfaces:**
- Consumes: `disc_ripper.devnodes:main`, `disc_ripper.app:create_app`
- Produces: an image that listens on 8580 with `/config` as a volume and runs the app as `PUID:PGID`

`Dockerfile`: `FROM python:3.12-slim-bookworm`; env `PYTHONUNBUFFERED=1 CONFIG_DIR=/config PUID=99 PGID=100 UMASK=0002 PORT=8580`; `pip install --no-cache-dir .`; `EXPOSE 8580`; `VOLUME /config`; a `HEALTHCHECK` that fetches `http://127.0.0.1:$PORT/healthz` with Python's `urllib`; `ENTRYPOINT ["/entrypoint.sh"]`.

`docker/entrypoint.sh` (POSIX sh, `set -eu`):
- **Running as root:** run `python -m disc_ripper.devnodes`, then `mkdir -p "$CONFIG_DIR"`, then `chown "$PUID:$PGID" "$CONFIG_DIR"`, then `umask "$UMASK"`, then `exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups uvicorn disc_ripper.app:create_app --factory --host 0.0.0.0 --port "$PORT"`.
- **Not running as root:** skip straight to `exec uvicorn ...`.

`unraid/disc-ripper.xml`:
- Repository `ghcr.io/barrydamonke/super-ripper:latest`, WebUI `http://[IP]:[PORT:8580]/`
- Port 8580
- Paths `/config` → `/mnt/cache/appdata/disc-ripper`, `/data` → `/mnt/user/Barry-server`, `/work` → `/mnt/user/disc-ripper-work`
- Variables `TZ`, `PUID=99`, `PGID=100`
- ExtraParams `--device-cgroup-rule='b 11:* rmw' --device-cgroup-rule='c 21:* rmw' --stop-timeout=60`

The README explains: create the `disc-ripper-work` share (primary storage cache, no secondary, exclusive), add the template, and either make the GHCR package public or log Unraid in to ghcr.io.

- [ ] **Step 1: Build the image locally.** Run: `docker build -t disc-ripper:dev .` (Docker Desktop must be running). Expected: the build succeeds.
- [ ] **Step 2: Smoke-run it.** Run: `docker run -d --name dr-smoke -p 8580:8580 disc-ripper:dev`, then `curl.exe -s http://localhost:8580/healthz`. Expected: `{"status":"ok"}`.
- [ ] **Step 3: Check that it dropped privileges.** Run: `docker exec dr-smoke sh -c "grep -E '^(Uid|Gid)' /proc/1/status"`. Expected: both lines show `99` / `100`. Check that `docker logs dr-smoke` shows the devnodes summary (no drives on Docker Desktop) and no traceback. Then `docker rm -f dr-smoke`.
- [ ] **Step 4: Commit.** `git commit -m "feat: container image and Unraid template"`

### Task 9: CI – test, build and publish to GHCR

**Files:**
- Create: `.github/workflows/build.yml`

**Workflow:**
- Triggers: `push` to `main`, and `pull_request`. Permissions: `contents: read`, `packages: write`.
- **`test` job:** ubuntu-latest, `actions/setup-python@v5` with 3.12, `pip install -e .[dev]`, `pytest -v`.
- **`image` job** (needs `test`):
  - `docker/setup-buildx-action@v3`, then `docker/metadata-action@v5` with `images: ghcr.io/${{ github.repository }}` (it lowercases the name), tags `type=raw,value=latest,enable={{is_default_branch}}` and `type=sha`.
  - Build with `docker/build-push-action@v6` (`load: true`, tag `disc-ripper:ci`), smoke-run it, and poll `curl -fsS localhost:8580/healthz` for up to 30 s.
  - Then, only on push to `main`: `docker/login-action@v3` with `GITHUB_TOKEN` and build-push with `push: true` and the metadata tags.

- [ ] **Step 1: Write the workflow.**
- [ ] **Step 2: Push the branch and open the PR.** Run: `git push -u origin step-1-skeleton`, then `gh pr create --title "Step 1: skeleton" --body "<summary of the step, ending with the attribution line>"`.
- [ ] **Step 3: Watch CI.** Run: `gh pr checks --watch`. Expected: `test` and `image` pass (the PR builds without pushing).
- [ ] **Step 4: Commit any fixes** and repeat Step 3 until green.

### Task 10: Install on the Unraid server (you, after merging)

- [ ] Merge the PR, then wait for the `main` workflow to push `ghcr.io/barrydamonke/super-ripper:latest`. Make the package public, or log Unraid in to ghcr.io.
- [ ] Create the `disc-ripper-work` share (cache only, exclusive) and add the container from `unraid/disc-ripper.xml`.
- [ ] Open `http://<server>:8580`, set the password and log in.
- [ ] On Drives, both drives (DVD and Blu-ray) are listed with their model, an ID that starts with `serial:` (or a fallback), and node status "ok". Label them.
- [ ] Reboot the server and check that the labels are still on the right drives.
