# Step 2: detection, queues and the shutdown guard (design)

Date: 2026-11-18. Builds on step 1 (skeleton) and the main design plan
(`Disc Ripper – Design Plan.md`, sections "Disc insert detection", "Architecture",
"Cache budget", "Scheduling and the shutdown guard").

## Intent

Every later step (CD rips, video rips, encodes, burns) creates long jobs that a
scheduled shutdown could cut off. Step 2 builds the machinery those jobs run on,
before any of them exist:

- see a disc go in, and say what kind of disc it is
- one job queue per drive, kept in SQLite, with live progress in the browser
- the cache-space check a job must pass before it starts
- the shutdown guard: read the Pi's schedule, report busy/not busy on
  `/api/busy`, and the unraid-backend change that asks before powering off

Ripping doesn't exist yet, so step 2 adds a **test job** (decided: option B) to
exercise the queue, progress, the cache check and a real shutdown end to end.

## Goals

1. The dashboard shows each enabled drive's state (empty, tray open, not ready,
   disc in) and, for a disc, its type and volume label, updated live within ~5 s
   of inserting or ejecting.
2. Eject (and close tray) buttons per drive.
3. A test job per drive with a chosen duration and a pretend size; it queues,
   runs one at a time per drive, shows a progress bar and estimated finish, and
   can be cancelled.
4. Jobs survive a container restart: anything running becomes "interrupted"
   with a Retry button.
5. `GET /api/busy` with a shared secret reports busy while a job runs, and
   stops reporting busy once the grace period after the scheduled end has passed.
6. unraid-backend holds the poweroff flag while the ripper is busy, for at most
   4 hours after the shutdown request, and treats an unreachable ripper as idle.
7. The dashboard shows the schedule state: today's window, scheduled end, grace
   end, and a warning if no schedule could be read.

## Non-goals (later steps)

Real rips, encodes, burns; the hold/auto-start rules; disc IDs and auto-retry of
interrupted rips on the same disc; encode gating against the deadline (step 6
uses the deadline function built here). No new tools in the image beyond tini.

## Anti-goals

- The guard must never keep the server on indefinitely: past the grace period
  `/api/busy` says not busy, whatever is running, and unraid-backend stops
  asking after 4 hours regardless.
- A ripper bug or outage must never block shutdown (unreachable = idle).
- Polling must never disturb a drive in use by a job that holds the drive.

## Approaches considered

1. **Everything in the web app's process (chosen).** One uvicorn process runs
   the web UI plus asyncio background tasks: a drive poller, one worker per
   drive, and a schedule refresher. Blocking calls (ioctls, sector reads) run in
   threads. Progress is written to SQLite and pushed to open browsers through an
   in-process event broker. Simple, instant progress. If the process dies, jobs
   die with it; restart marks them interrupted, which the plan needs for
   shutdowns anyway.
2. **Separate worker process.** Isolates jobs from web bugs, but needs two
   supervised processes and IPC (or DB polling) for live progress. Too many
   moving parts for a single-user box.
3. **Celery/RQ.** Needs Redis, which the plan rules out. The SQLite job table is
   the queue.

## Design

### Process and lifecycle

- Run uvicorn with exactly one worker (already the case). Background tasks start
  in the FastAPI lifespan and are cancelled on shutdown.
- Add `tini` to the image and make it PID 1 (`ENTRYPOINT ["tini", "--",
  "/entrypoint.sh"]`) so later child processes are reaped and signalled. The
  entrypoint still execs setpriv → uvicorn.
- On SIGTERM uvicorn runs the lifespan shutdown: cancel workers, mark running
  jobs `interrupted`. Must finish well inside the template's
  `--stop-timeout=60`. Trap: uvicorn first waits for open connections to close,
  and SSE streams never close by themselves, so start uvicorn with
  `--timeout-graceful-shutdown 10`; otherwise one open browser tab stalls the
  stop until Docker kills the process and jobs are never marked.
- Database access from background tasks goes through `asyncio.to_thread` with
  its own short-lived connection, so a busy SQLite lock never stalls the event
  loop (and with it every page and stream).
- On start-up, before workers start: any job still `running` (crash, power cut)
  becomes `interrupted`.

### Data (migration `0002`)

`drives` gains disc-state columns (so a page load shows the last known state
without waiting for a poll):

| Column | Values |
| --- | --- |
| `media_state` | `unknown`, `empty`, `tray_open`, `not_ready`, `disc` |
| `disc_type` | `audio`, `dvd`, `bluray`, `data`, `mixed`, `unknown`, or NULL when no disc |
| `disc_label` | volume label text, `''` if none |
| `media_changed_at` | UTC timestamp of the last state change |

New `jobs` table:

| Column | Notes |
| --- | --- |
| `id` | integer primary key; queue order within a drive |
| `drive_id` | FK → `drives.id` (`ON UPDATE CASCADE`, so the step-1-PR-#2 rename keeps working) |
| `kind` | `test` (later steps add `rip_cd`, `rip_video`, `encode`, `burn`, ...) |
| `state` | `queued`, `waiting_space`, `running`, `done`, `failed`, `cancelled`, `interrupted` |
| `params` | JSON (test job: `duration_s`, `size_gb`) |
| `progress` | 0.0–1.0 |
| `message` | short status line shown under the bar |
| `needs_bytes` | space the job needs in `/work` before it starts (test job: `size_gb`) |
| `uses_drive` | 1 if the job holds the physical drive (test job: 0; rips: 1) |
| `created_at`, `started_at`, `finished_at`, `estimated_finish` | UTC ISO strings |
| `error` | text for failed jobs |

Finished jobs stay in the table (history); the dashboard shows the last few per
drive.

### Drive polling (`media.py`)

- Every 3 s, for each present and enabled drive with an `sr` node and no running
  `uses_drive` job: open `/dev/srX` with `O_RDONLY | O_NONBLOCK`, call
  `ioctl(CDROM_DRIVE_STATUS, CDSL_CURRENT)`, close. Map `CDS_NO_DISC` → empty,
  `CDS_TRAY_OPEN` → tray_open, `CDS_DRIVE_NOT_READY` → not_ready, `CDS_DISC_OK` →
  disc, anything else / error → unknown.
- All ioctls for a drive go through one per-drive lock, so a poll never races an
  eject.
- On a change to `disc`, probe (below) in a thread with a 30 s timeout, then
  write the state and publish an event. On a change away from `disc`, clear
  type and label.
- On start-up the first poll fills in state without treating present discs as
  "newly inserted" events (step 2 has no insert actions, but later steps will
  care; the poller exposes an `on_insert` hook that is not fired on the first
  poll).

### Probing a disc

1. `ioctl(CDROM_DISC_STATUS)`: `CDS_AUDIO` → `audio`; `CDS_MIXED` → `mixed`
   (enhanced CD); data statuses continue.
2. Physical media: SCSI GET CONFIGURATION (opcode 0x46, RT=1, starting feature
   0) through `SG_IO` on the `sr` node; the "current profile" field gives CD,
   DVD or BD. CD data → `data`.
3. DVD: read the ISO 9660 root directory (DVD-Video discs are UDF bridge and
   always carry ISO 9660). A `VIDEO_TS` directory → `dvd`; otherwise `data`.
4. BD: `bluray`. (A BD-R data disc is rare; step 4's MakeMKV scan refines it.)
5. Label: ISO 9660 primary volume descriptor (sector 16, volume identifier) when
   present; otherwise UDF logical volume identifier (anchor at sector 256 →
   main volume descriptor sequence → logical volume descriptor). Strip, keep
   printable ASCII, cap at 64 chars. Unreadable → `''`.
6. If any step fails, `disc_type = unknown` and the error is logged; the drive
   still shows "disc in".

The sector parsers take bytes so they're unit-tested on fabricated sectors.

### Eject and close tray

Direct actions, not queue jobs (they take a second and must work while a test
job runs): unlock the door (`CDROM_LOCKDOOR 0`), then `CDROMEJECT`; close is
`CDROMCLOSETRAY`. Run in a thread under the drive's lock. If a running job
`uses_drive`, the button reads "Cancel job and eject" and cancels it first (no
such job exists in step 2, but the rule is set now). Errors (e.g. `EBUSY`) show
on the drive card.

### Queues and workers (`jobs.py`)

- One asyncio worker task per present, enabled drive (workers are rebuilt after
  a Rescan or an enable/disable). Jobs for an absent or disabled drive stay
  queued and are shown as such; the dashboard only offers new jobs on enabled
  drives.
- One asyncio worker task per drive's queue. It sleeps on an `asyncio.Event` that is set
  when a job for that drive is enqueued, cancelled or finishes, and every 30 s
  (to retry `waiting_space`).
- Picking a job: the lowest `id` with state `queued` or `waiting_space` for that
  drive. If its `needs_bytes` doesn't pass the space check, set it to
  `waiting_space` with the reason in `message` and wait; otherwise set `running`.
  Only one job per drive runs; a waiting job blocks the jobs behind it (keeps
  order predictable).
- Job handlers are async functions registered by kind, given a context with
  `report(progress, message, estimated_finish)` and a cancellation point.
  `report` writes to SQLite at most once a second and publishes an event.
- **Test job:** params `duration_s` (1 min–6 h) and `size_gb` (0–500). Sets
  `estimated_finish = start + duration`, ticks progress each second, finishes
  `done`. It doesn't touch the drive or write to `/work`.
- Cancel: a queued job → `cancelled`; a running one is task-cancelled →
  `cancelled`.
- Retry (interrupted, failed or cancelled): enqueue a copy as a new job at the
  back of the queue.

### Cache space check (`space.py`)

`fits(needs_bytes)` is true when both hold:

- `work_used + needs_bytes <= work_budget_gb`, where `work_used` is the size of
  `/work` (a walk of the tree, cached for 60 s) plus the `needs_bytes` of jobs
  already running;
- `statvfs(/work).free - needs_bytes >= cache_min_free_gb`. `/work` is an
  exclusive share, so statvfs reports the cache pool itself.

`/work` is a new `WORK_DIR` config entry (default `/work`). If it doesn't exist,
jobs that need space wait with a message saying so; jobs needing 0 bytes run.

### Schedule (`schedule.py`)

- **Source:** two new settings (a new `url` kind: blank or `http(s)://...`):
  `schedule_status_url` (unraid-backend `/status`, e.g.
  `http://192.168.1.x:3001/status`, read the `schedule` field) and
  `schedule_pi_url` (fallback, the Pi's `GET /schedule` on :8090). Refresh every
  5 min with a 5 s timeout; try the status URL, then the Pi. A schedule is
  accepted only if valid: 7 entries in Monday–Sunday order, `day` matches,
  `start`/`end` both `HH:MM` or both blank, `endNextDay` bool or missing. The
  last good copy is kept in an internal setting so it survives restarts and Pi
  outages, with its fetch time.
- **Windows:** for the entries of today, yesterday and the day before, build
  concrete windows in the container's local time (`TZ`): start on that date,
  end on that date or the next if `endNextDay`. Same rules as
  `schedule_util.is_scheduled_online`.
- **Phase** at time `now`, with `grace = shutdown_grace_hours`:
  - `no_schedule`: nothing valid has ever been fetched. Guard off, dashboard
    warns.
  - `in_window`: `now` is inside a window. `scheduled_end` = that window's end;
    `deadline` = end + grace.
  - `draining`: no window contains `now`, but the most recent window end `E`
    has `E <= now < E + grace`.
  - `off_hours`: anything else (e.g. the server was switched on by hand).
- `deadline_for(now)` and `phase_for(now)` are pure functions of the schedule
  and the time, unit-tested across overnight windows, `endNextDay`, blank days
  and the week wrap.
- DST: windows are built with zone-aware datetimes; a time that doesn't exist on
  the change day is shifted forward. Not otherwise special-cased.

### Busy endpoint

`GET /api/busy`, header `X-Shutdown-Secret` compared in constant time with the
`BUSY_SECRET` environment variable. No secret configured → 503; wrong secret →
401. It bypasses the login check (it has its own secret) and is a GET, so the
cross-origin check doesn't apply.

```json
{
  "busy": true,
  "phase": "draining",
  "scheduledEnd": "2026-11-18T23:00:00+00:00",
  "graceEndsAt": "2026-11-19T02:00:00+00:00",
  "jobs": [{"id": 12, "drive": "Blu-ray", "kind": "test", "estimatedFinish": "..."}]
}
```

`busy` is true when at least one job is `running` (queued and `waiting_space`
jobs don't hold shutdown), **except** in `draining` past `graceEndsAt` or in
`off_hours`, where it is always false. With `no_schedule`, `busy` is simply
"a job is running"; unraid-backend's 4-hour cap is then the only limit.

Template: new masked variable `BUSY_SECRET`.

### Grace setting

`shutdown_grace_hours` keeps default 3 but its maximum drops from 11 to 3.5, so
it always stays under unraid-backend's 4-hour cap (help text says so).

### Live updates (SSE)

- `GET /events` (login required) streams `text/event-stream`. On connect it
  sends the current state of every drive card and the schedule panel, then
  changes as they happen, plus a comment line every 15 s as a keep-alive.
- Events carry rendered HTML fragments. Event names: `drive-<n>` (the dashboard
  position of the drive, so names stay valid identifiers) and `guard`.
- Front end: vendored htmx SSE extension (`htmx-ext-sse`, pinned version), with
  `sse-swap` on each card. On reconnect the server's initial burst brings the
  page back up to date.
- Broker: a set of bounded `asyncio.Queue`s, one per open stream. A stream that
  falls behind is dropped (the browser reconnects and gets a fresh burst).
  Progress events are throttled to 1 per second per job.

### Dashboard page

`/` becomes the dashboard (Drives stays for labels and enabling). Sketch:

```
Schedule: today 18:00–23:00 · draining until 02:00 (grace 3 h)   [warning if none]

Blu-ray (HL-DT-ST BD-RE BH10LS38)                       [Eject] [Close tray]
  Disc in · Blu-ray · "THE_MATRIX"
  Running: test job #12   [#########-------] 58%  finishes ~21:42   [Cancel]
  Queued:  test job #13 (20 min, 5 GB)                                [Cancel]
  Recent:  #11 done · #10 interrupted [Retry]
  Add test job: [20] min  [0] GB  [Queue]
  ! Expected to finish after the grace period ends (02:00)

DVD (ATAPI iHAS124 F)                                    [Eject] [Close tray]
  Empty
```

The "past the grace period" warning shows on the form (from the chosen
duration) and on a job whose estimate passes the deadline.

### unraid-backend change (barry-mc-network, separate PR)

- New env vars: `RIPPER_BUSY_URL` (e.g. `http://192.168.1.x:8580/api/busy`) and
  `RIPPER_BUSY_SECRET`. Either unset → feature off, behaviour as today.
- While a shutdown request is pending, poll the URL every 60 s (and once
  immediately when the request arrives) with a 5 s timeout. Keep the last
  answer: `busy` true only when the response is 200 with `"busy": true`;
  anything else (timeout, error, bad JSON, 4xx/5xx) counts as idle.
- `maybeExecuteShutdown(totalPlayers)`: if players > 0, wait as today. Else if
  the ripper is busy and less than 4 h have passed since the request arrived,
  wait. Otherwise write the flag. Log each change of the ripper's answer once.
- Record the paired values in barry-mc-network's CLAUDE.md "Paired timeouts":
  the 4 h cap must stay above the ripper's grace maximum.
- That repo has no automated tests; the PR describes manual checks.

### Errors and logging

Log to stdout (seen in the Unraid container log): state changes per drive,
probe results, job start/finish/fail, schedule fetch failures (once per
failure streak, not every 5 min), busy answers that change.

## Testing

- Unit: schedule parsing, validation, windows and phases (overnight, blank days,
  week wrap, grace edges); ISO 9660 / UDF / GET CONFIGURATION parsers on
  fabricated bytes; ioctl wrappers with a fake `ioctl`; space check with a fake
  statvfs and tree; worker ordering, `waiting_space`, cancel, retry, interrupted
  on start-up; busy endpoint (secret, phases, grace cut-off); SSE initial burst
  and an update; migration from a step-1 database.
- Manual, on the server (written into the PR): insert/eject both drives, check
  type and label for a CD, DVD and Blu-ray; queue two test jobs on one drive and
  one on the other; restart the container mid-job (interrupted + Retry); a
  test job with a size bigger than the budget waits; `curl` `/api/busy` with and
  without the secret; then a real evening: a test job running past the
  scheduled end, confirming unraid-backend waits, then shuts down after the job
  or at the grace end.

## Left to the builder

Exact module split and names, the fragment templates, the htmx-ext-sse version
(pinned, vendored), test-job form limits within the ranges above, log wording.
