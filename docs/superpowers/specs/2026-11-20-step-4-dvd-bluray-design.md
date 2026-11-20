# Step 4: DVD and Blu-ray ripping (design)

Date: 2026-11-20. Builds on steps 1–3 (drive discovery, disc detection,
per-drive job queues, live dashboard, shutdown guard, the CD rip job) and the
main design plan (`Disc Ripper – Design Plan.md`: "Disc lifecycle", "Ripping
tools", "Storage layout", "Scheduling and the shutdown guard").

## Intent

Step 4 makes the ripper handle films: insert a DVD or Blu-ray, MakeMKV rips its
main title to MKV, and the disc comes back out. Identification (Radarr, step 5)
and encoding (step 6) don't exist yet, so this step ends with a lossless MKV in
the downloads share, where the owner can watch or check it and where steps 5–6
will pick it up later.

As with CDs, the owner wants this mostly hands-off. Only discs where the main
title is genuinely unclear, or that look like TV discs, wait for them.

## Goals

1. **Insert a DVD or Blu-ray:** the app scans its titles with MakeMKV and picks
   the main title, which is the longest one, since there is no Radarr runtime
   to match yet.
2. **It rips straight away unless it is held.** A disc is held, with the reason
   on its card, when:
   - the main title is unclear (see Title choice);
   - it looks like a TV disc;
   - the owner pressed **Hold** on the card before the rip started;
   - "Auto-start rips" is off;
   - this disc was already ripped ("Already ripped" with **Rip again**, as for CDs);
   - the disc was already in the drive when the container started.
3. **A held disc** shows its title list on the card. The owner picks the main
   title, ticks any extras, and presses **Rip**.
4. **While it rips,** the owner can tick extra titles. They queue on the same
   drive after the main title.
5. **Where it ends up (decided with the owner: option B).** A finished title
   that passes its check moves from `/work/<job>/` to
   `/data/downloads/disc-ripper/<job>/`, with MakeMKV's own neutral file name
   (for example `INCEPTION_t00.mkv`). Extras go in `<job>/extras/`. The work
   copy is then deleted, which frees the work area.
6. **Eject:** the drive ejects once that disc's queue has finished and every
   rip in it succeeded. On a failure the disc stays in, so Retry needs no
   re-insert. "Eject" ("Cancel job and eject" while a job runs) stays available
   throughout, as in step 2.
7. **MakeMKV is in the image**, built from source at a pinned version
   (decided with the owner: approach A below). The licence or beta key is a
   Settings value, never baked into the image.

## Non-goals

- **Identification, duplicate checks against Radarr, import** (step 5) and
  **encoding** (step 6). Step 4 does not rename files after the film.
- **Tidying the downloads folder.** Rips accumulate until step 5 exists; the
  owner clears them by hand, or step 5 adopts them.
- **TV episode mapping** (step 9). A TV-looking disc is held. The owner can rip
  any titles they want; the files are left as MakeMKV names them.
- **"Keep lossless"** (plan, Disc lifecycle step 6). In step 4 every rip is
  lossless and kept, so the option means nothing until encoding exists.
- **UHD Blu-ray.** It needs LibreDrive-capable drive firmware, which the
  owner's drives aren't known to have. A UHD disc fails with MakeMKV's own
  message.
- **A films page.** The card's recent-jobs line shows where each rip went.
  Step 5 brings the page where films are identified.
- **Automatic beta-key fetching.** The owner pastes the key into Settings.

## Anti-goals

- **Ripping the wrong title silently.** A Blu-ray with decoy playlists, or a DVD
  with near-identical cuts, must be held, never guessed at.
- **Filling the cache.** A 40 GB rip must not start unless it fits the work
  budget and the free-space floor, and a finished rip must leave the work area.
- **A disc that can't be got out.** Eject must work during a scan as well as a
  rip.
- **The same disc ripped twice by accident**, as with CDs.
- **An image that changes under the owner.** The MakeMKV version is pinned, and
  only a deliberate rebuild changes it.

## Constraints

- **MakeMKV is not in Debian.** The image builds `makemkv-oss` and
  `makemkv-bin` from makemkv.com's source tarballs (see Image). The base stays
  `python:3.12-slim-trixie`.
- **MakeMKV needs the drive's `sg` node as well as `sr`.** Step 1's entrypoint
  already creates both for optical drives. MakeMKV runs as uid 99 like
  everything else, with no `--privileged`.
- **MakeMKV keeps its settings in `$HOME/.MakeMKV/`.** The app runs it with
  `HOME=/config/makemkv`, so the key and MakeMKV's data files persist across
  container recreation.
- **Beta-key releases expire.** On the free beta key, an old MakeMKV release
  stops working some time after a newer one ships. The fix is a version bump
  and a rebuild, so the error has to be obvious on the card.
- **Title numbers depend on scan settings.** `makemkvcon mkv … <title>` numbers
  titles the same way `info` does only when the minimum-length filter is the
  same. The scan records the `--minlength` it used, and every rip of that disc
  passes the same value.
- **MakeMKV cannot resume.** A cancelled, failed or interrupted rip restarts
  from the beginning (main plan, shutdown section).
- **Shutdown.** Scans and rips are `uses_drive` jobs, so they hold shutdown
  through `/api/busy` and are interrupted cleanly on container stop, as in
  steps 2–3.
- **Untrusted text.** Disc labels and MakeMKV title names come off the disc.
  They are never passed through a shell, are escaped in templates, and are
  never used to build a path except through MakeMKV's own output name inside
  the job folder.
- **Ownership:** files are written as 99:100 with umask 0002.
- **No new pip dependencies.**

## Approaches considered

### What happens to a ripped film before steps 5–6 exist (owner chose B)

- **A. Leave it in the work area.** Step 4 is then mostly a way to test rips. A
  couple of Blu-rays fill the 60 GB budget and everything else waits for cache
  space until the owner clears files by hand.
- **B. Move it to `/data/downloads/disc-ripper/<job>/` with a neutral name
  (chosen).** The work area frees up, the owner can watch or check the rip, and
  steps 5–6 pick it up from there. The downloads share sits on the cache only
  until the mover runs, so this costs little.
- **C. Build step 5 (Radarr identification and import) into this step.** Films
  would go from disc into Radarr as unencoded remuxes. This makes the step much
  bigger.

### How MakeMKV gets into the image (owner chose A)

- **A. Build from source in a build stage, at a pinned version (chosen).**
  Predictable and reversible; the cost is a slower image build, a somewhat
  bigger image, and a version bump every few months on the beta key.
- **B. Download and build it at container start, kept in `/config`.** Always
  current, but every start depends on makemkv.com, the first start takes
  minutes, and the code that runs isn't a version the owner chose.
- **C. Copy the programs from an existing MakeMKV image.** Most are Alpine
  (musl) based and won't run on this Debian (glibc) image without carrying a
  second set of libraries. Ruled out.

## Design

### Image

- **A new build stage** on the same trixie base:
  - Install the build dependencies (`build-essential`, `pkg-config`, the
    `-dev` packages for OpenSSL, Expat, zlib and FFmpeg's libavcodec).
  - Download `makemkv-oss-<ver>.tar.gz` and `makemkv-bin-<ver>.tar.gz`, and
    check them against SHA-256 values written in the Dockerfile.
  - Build `makemkv-oss` with `--disable-gui`, then `makemkv-bin`, accepting the
    EULA non-interactively. Install into a staging prefix.
- **`ARG MAKEMKV_VERSION`** holds the version, next to the checksums. A version
  bump changes three lines.
- **The final stage** copies in `makemkvcon` and its libraries (`libmakemkv`,
  `libdriveio`, `libmmbd`), and installs only their runtime dependencies.
- **`ffmpeg`** (the Debian package) is added to the final stage for `ffprobe`,
  which the post-rip check uses. Step 6 needs it anyway.
- **CI smoke test additions:** as uid 99, `makemkvcon` starts and prints its
  version line, and `ffprobe -version` works.

### Settings

New "DVD and Blu-ray" group:

| Key | Kind | Default | Notes |
| --- | --- | --- | --- |
| `makemkv_key` | secret | "" | Written to MakeMKV's `settings.conf` as `app_Key` before each run. Blank: MakeMKV's own trial or free-DVD behaviour. |
| `video_min_title_seconds` | int 0–3600 | 120 | MakeMKV `--minlength`; shorter titles aren't listed. |
| `video_track_selection` | text | see below | MakeMKV's selection string, written to `settings.conf` as `app_DefaultSelectionString`. |

**Which tracks go into the MKV (decided with the owner: a fixed list).** These
aren't full archival rips yet, and proper presets come later, in step 6.

- **Audio:** English tracks, except commentary.
- **Subtitles:** English, plus every forced track.
- **Always kept:** a track with no language set, and the only audio track on
  a title. That way a film with no English audio still has sound.
- **The cost:** a non-English original-language track is dropped when the
  disc also has English audio. The owner accepts this for now.
- **The setting holds MakeMKV's own selection syntax**, so step 6 can replace
  it with a preset. The builder writes the default string from MakeMKV's
  documented selection tokens and confirms it on a real disc, using the scan's
  `SINFO` stream list to check which tracks were kept.
- **Scan and rip use the same `settings.conf`**, so the title sizes from the
  scan (`TINFO` 11) already reflect the selection. The space check therefore
  sees the size of the file that will actually be written.

`auto_start_rips` and the cache settings already exist and apply to video discs
too.

### Scanning (`scan_video` job)

- **On insert** of a disc whose probe type is `dvd` or `bluray` (the step-2
  `on_insert` hook), the app queues a `scan_video` job:
  - `uses_drive=1`, `needs_bytes=0`;
  - it runs `makemkvcon -r --progress=-same --minlength=<n> info dev:/dev/srX`,
    with a fixed argument list and no shell;
  - the card shows "Scanning disc…" with MakeMKV's progress.
- **Why a job and not an inline call:** a Blu-ray scan can take a minute or
  more. As a job it shows on the card, is cancelled by "Cancel job and eject",
  holds shutdown, and never holds the drive lock long enough to block the eject
  button.
- **Parsing** MakeMKV's robot output (`CINFO`, `TINFO`, `SINFO`, `MSG`, `PRGV`,
  `PRGT`, `PRGC`, `DRV` lines). For each title it keeps:
  - its number;
  - duration (`TINFO` id 9);
  - size in bytes (id 11);
  - chapter count (id 8);
  - source (id 16, for example `00800.mpls`);
  - MakeMKV's output file name (id 27);
  - the segment map (id 26), if present.

  The parser ignores unknown lines. As in step 3, the exact formats are
  confirmed against a real capture from the owner's server before the PR is
  finished.
- **The disc fingerprint:** SHA-1 over the disc type, volume label and the
  sorted (duration, size, chapters, source) of every title. It is used by the
  "Already ripped" guard and the Retry check.
- **Disc already in the drive at start-up** (`on_present`): it is scanned too,
  so the card can show the title list, but it never auto-rips.
- **Scan failure** (no key, version too old, unreadable disc): the job fails
  with MakeMKV's last `MSG` text on the card. A "too old" or "key" message also
  adds "Update MakeMKV: set a new beta key in Settings or rebuild the image with
  a newer version." The disc stays in.

### Title choice and hold rules

Pure functions, unit-tested:

- **The main title** is the longest by duration. A tie on duration goes to the
  larger size.
- **Unclear** if any other title's duration is within 3% of the main title's
  *(builder's call, the plan says "a few percent")*. Identical copies (the same
  segment map) don't count, since MakeMKV usually drops those itself.
- **Looks like a TV disc** *(builder's call)* if three or more titles are each
  between 18 and 75 minutes long, within ±20% of their median length, and no
  title is longer than 90 minutes other than a "play all" title of roughly
  their sum (within 10%).
- **Decision:**
  - Auto-start (queue the main title's rip) only when auto-start is on, Hold
    wasn't pressed, the choice is clear, it isn't TV-like, the disc isn't
    already ripped, and the scan came from an insert, not a start-up.
  - Otherwise the disc is **held**. The card lists every reason that applies,
    for example "Held: two titles of about the same length (decoy playlists?)".

### Hold button

- While a video disc is being scanned, or its main-title rip is queued but not
  started, the card shows **Hold**. Pressing it stops the auto-start for that
  disc. If the rip is already queued, it is cancelled back to the held state.
- **Hold applies to the disc in the drive only** *(builder's call)*. The next
  disc follows the normal rules. To hold everything, turn auto-start off.

### Starting a rip

- **From auto-start or the held card's Rip button**, inside one write
  transaction (as with `_enqueue_cd_rip`, so a double-click or an insert can't
  queue twice):
  - one `rip_title` job for the main title, with `role=main`;
  - one `rip_title` job per ticked extra, with `role=extra` and the main job's
    ID as its `rip_id`;
  - a `video_rips` row keyed by the main job's ID.
- **A TV-looking disc has no main title** *(my call: episodes aren't extras,
  and step 9's mapping wants them side by side)*. Its held card shows only
  tick boxes, with no Main column. Every ticked title is a `rip_title` job with
  `role=title`, saved directly in `<rip_id>/` with no `extras/`. `rip_id` is
  the first of those jobs' IDs, and `main_title` is null. The owner can still
  rip a TV-looking disc as a film: a "Treat as a film" link on the card brings
  back the Main column.
- **Extras ticked during the rip** add `rip_title` jobs with the same `rip_id`,
  queued behind the main title on that drive. A title already queued or ripped
  for this `rip_id` can't be ticked twice.
- **`rip_id` is carried in every job's params**, so a Retry (which creates a
  new job ID) still writes to the same downloads folder and counts toward the
  same disc.
- **`needs_bytes`** is the title's size from the scan × 1.02, rounded up, so
  the existing space check makes a rip that doesn't fit wait as "Waiting for
  cache space".
- **Already-ripped guard** *(my call, following step 3's rule)*. Auto-start is
  blocked for a fingerprint when either:
  - a `rip_title` job for it is queued, waiting for space or running; or
  - its `video_rips` row is `ripped` with the downloads folder still there, or
    `move_failed` with the file still in `/work`.

  The card then shows "Already ripped (downloads/<job>/)" with **Rip again**.
  A rip that failed, was cancelled or was interrupted doesn't block; the disc
  just rips again. Nor does one whose folder the owner has deleted, which is
  how a film rip is discarded in step 4: there is no Discard button until
  step 5 brings a films page.

### The rip job (`rip_title` handler)

- **Command:**
  `makemkvcon -r --progress=-same --minlength=<scan's value> mkv dev:/dev/srX <title> /work/<jobId>/`
  - a fixed argument list, run through `JobContext.run_process` so cancel and
    stop kill it (SIGTERM, then SIGKILL);
  - `HOME=/config/makemkv`, with `settings.conf` written first from the
    `makemkv_key` setting.
- **Before ripping,** the handler checks the disc in the drive is still this
  disc. It compares the probe's type and volume label with the scan's. If
  MakeMKV's `mkv` output lists the titles, it also compares the fingerprint
  *(builder confirms from the real capture)*. On a mismatch the job fails with
  "A different disc is in the drive."
- **Progress:** `PRGV` gives the overall fraction, and the card shows "Main
  title — 42% · finishes ~21:40", or "Extra 2 of 3 — …" for extras. The
  estimate comes from the rate so far, as for CDs.
- **Post-rip check:** MakeMKV exited 0, and exactly one `.mkv` file appeared.
  `ffprobe` must read it, and it must have a video stream and at least one
  audio stream. Its duration must be within 2% (or 5 s, whichever is larger)
  of the scanned title's. Anything else is `failed`, with MakeMKV's
  last messages, and the partial file is deleted.
- **Move to downloads:** copy `/work/<jobId>/<name>.mkv` to
  `/data/downloads/disc-ripper/<rip_id>/` (extras go in `…/<rip_id>/extras/`).
  Write it as `<name>.mkv.part`, fsync it, rename it, then delete the work copy
  and folder.
  - **Space for the copy** *(my call)*. While it runs, the file exists twice on
    the cache. Just before copying, the handler checks that the cache pool has
    the file's size plus the free-space floor available. If it doesn't, the
    move isn't attempted and the job goes down the "move fails" path below. The
    rip's own space check stays at the title size, so big Blu-rays can still
    start under the default 60 GB budget.
  - **If the move fails** (for example no space on the cache), the job still
    counts as `done`, because the rip itself is safe. The `video_rips` row
    records "Couldn't move to downloads: …", the file stays in `/work`, and the
    card offers **Move again** *(builder's call, mirroring step 3's "failures
    after the rip")*.
  - **Never overwrite:** a name clash in the target folder gets a ` (2)`
    suffix.
- **Work-folder sweep:** the step-3 rules apply unchanged. A failed, cancelled
  or interrupted job's folder is removed on retry or discard, and swept 7 days
  after the job finished.

### Eject

- After each `rip_title` job ends, if no job with the same `rip_id` is queued or
  running, and the latest attempt at each title for that `rip_id` is `done`
  (an earlier failed attempt that a Retry superseded doesn't count), the drive ejects (the
  step-2 eject path, under the drive lock). An eject failure is logged and
  doesn't fail the job.
- If any rip for the disc failed, the disc stays in. The card shows Retry on
  the failed job, and the drive ejects when the retry succeeds.

### Data (migration 0004)

- `video_discs`:
  - `fingerprint` (primary key), `disc_type`, `label`;
  - `scan` (JSON: the titles as parsed), `minlength`;
  - `drive_id`, `last_seen`.
- `video_rips`: one row per ripped disc, keyed by the main job's ID (`rip_id`):
  - `fingerprint`;
  - `state` (`ripping` / `ripped` / `move_failed` / `failed`), where `failed`
    means no title of it was ripped and nothing is queued;
  - `main_title` (null for a TV-looking disc);
  - `files` (JSON: title → downloads path, role);
  - `message`, `created_at`, `updated_at`.
- **The current disc's scan per drive:** the card finds the scan for the disc
  that's in the drive now. Storage is the builder's choice, but a re-insert or
  a different disc must never show an old title list.
- **Job kinds:**
  - `scan_video`, params `{sr_name, disc_type, label, from_insert}`;
  - `rip_title`, params `{rip_id, fingerprint, title, role, minlength,
    expected_seconds, sr_name}`.

### Dashboard card

Scanning:

```
Blu-ray (HL-DT-ST BD-RE BH10LS38)            ● Tray closed     [Cancel job and eject]
  Disc in · Blu-ray · "INCEPTION"
  Running: scan #31  [####------] Scanning disc… 40%                    [Hold] [Cancel]
```

Held:

```
  Disc in · Blu-ray · "SOME_FILM"
  Held: two titles of about the same length (decoy playlists?)
    Main  Extra  #   Length    Size     Chapters  Source
    (•)   [ ]    0   2:16:04   38.2 GB  28        00800.mpls
    ( )   [ ]    1   2:16:02   38.2 GB  28        00801.mpls
    ( )   [x]    4   0:12:30    2.1 GB   1        00020.mpls
  [Rip]
```

Ripping, with extras still tickable:

```
  Running: rip #32 main title  [######----] 58% · finishes ~21:40       [Cancel]
  Extras: [ ] #3 0:04:10  [x] #4 0:12:30 (queued)  [ ] #5 0:01:30
```

Finished (the recent line): `#32 done → downloads/32/INCEPTION_t00.mkv (2:28:07, 41.6 GB)`.

## Testing

- **Unit tests:**
  - the robot-output parser, against fixtures (synthetic first, then replaced
    with real captures of a DVD and a Blu-ray from the owner's drives);
  - the fingerprint;
  - title choice, the unclear rule and the TV rule, with tables of title lists
    (a plain film; decoy playlists; a DVD with widescreen and full-screen cuts;
    a TV disc with a play-all title; a disc with one long title and many
    extras);
  - the eject rule;
  - the `settings.conf` writer: it escapes the key and the selection string,
    and never logs the key;
  - migration 0004.
- **Handlers** are tested with a fake `makemkvcon` executable, like step 3's
  fake cyanrip:
  - it replays recorded robot output and writes a small, real MKV (made with
    ffmpeg in a test fixture, or skipped when ffmpeg is missing);
  - it can fail with a "too old" message, write a file of the wrong length, or
    hang for the cancel test.
- **Web tests:** held card, Rip with extras, Hold during a scan, extras ticked
  mid-rip, the Already ripped / Rip again flow, the key never echoed back.
- **Image:** builds, `makemkvcon` runs as uid 99, `ffprobe` works, and the
  step 1–3 smoke tests still pass.
- **Manual, on the server (written into the PR):**
  1. Insert a plain film DVD in each drive. It rips and ejects, and the MKV
     plays from downloads.
  2. Insert a Blu-ray with decoy playlists, if the owner has one. It's held,
     then ripped after the owner picks a title.
  3. Tick an extra mid-rip. It lands in `extras/`, and the eject waits for it.
  4. Press Hold during a scan. Check one rip's tracks with `ffprobe`: English
     audio, English and forced subtitles, no commentary.
  5. Cancel mid-rip. Restart the container mid-rip; Retry works.
  6. Re-insert a ripped disc: "Already ripped — Rip again?".
  7. Insert a disc too big for the remaining budget: "Waiting for cache space".
  8. **Capture real `makemkvcon -r` output** from a DVD and a Blu-ray (info and
     mkv) into the test fixtures, and confirm the parser against them.

## Left to the builder

- Module split and names.
- Exact robot-output parsing (confirmed against real captures).
- Where the current scan per drive is stored.
- Dockerfile layout of the build stage.
- The precise "too old" and "key" message codes.
- Card layout details beyond the sketches; log wording.
