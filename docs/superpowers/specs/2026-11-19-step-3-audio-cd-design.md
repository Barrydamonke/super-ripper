# Step 3: Audio CD ripping (design)

Date: 2026-11-19. Builds on steps 1–2 (drive discovery, disc detection, per-drive
job queues, live dashboard, shutdown guard) and the main design plan
(`Disc Ripper – Design Plan.md`: "Ripping tools", "Identification", "Storage
layout", "Integrations").

## Intent

The owner is rebuilding their music library from scratch: the old folder was
disorganised and has been moved out of Jellyfin. Every album in the new library
comes from a CD ripped here, so the library is exactly as clean as this step
makes it. The library is also the source for the step-8 CD burner (custom
tracklists for the car), which relies on good tags and ReplayGain values.

The owner wants this mostly hands-off: insert a CD, it rips, and a clean match
files itself. Only discs that need a decision wait for them.

## Goals

1. Inserting an audio CD (or enhanced CD) rips it automatically to FLAC with
   cyanrip, with live per-track progress on the drive's dashboard card. When
   "Auto-start rips" is off, the card offers a **Rip** button instead.
2. Each track is checked against AccurateRip using the drive's read offset.
3. The app identifies the disc on MusicBrainz by its disc ID, tags the files,
   and fetches `cover.jpg` from the Cover Art Archive.
4. **Approval (decided: option B).**
   - A clean rip files itself into the library. Clean means an unambiguous
     MusicBrainz match, every track verified by AccurateRip, and not already
     in the library.
   - Anything else goes to a **Needs a look** list, with the reason shown.
   - Filed albums appear on a **Recently filed** list where their tags can
     still be fixed.
5. A Needs-a-look item can be resolved by:
   - choosing between candidate releases;
   - pasting a MusicBrainz release URL or ID;
   - typing album, album artist, year, disc number and track titles by hand;
   - discarding it.
6. Filing writes the album to
   `/data/MEDIA/Music/<Album Artist>/<Album> (<Year>)/` (naming below) and asks
   Jellyfin to refresh.
7. **Multi-disc sets (decided: file each disc as it is ripped).** Disc 1 is filed
   on its own; disc 2 joins the same folder later, matched by MusicBrainz
   release ID.
8. **CD rip settings:** a "CD ripping" group in Settings. **Read offset** is per
   drive, on the Drives page, pre-filled for the owner's two drives.

## Non-goals

- Lossy copies (MP3/AAC). The plan says FLAC only; Jellyfin transcodes for
  clients.
- Named CD presets. The settings are one set of defaults; they move onto the
  presets page when it arrives in step 6.
- A "find my drive's offset" button. The owner's offsets are known (below).
  `cyanrip -f` can be run by hand if a drive is ever replaced.
- Lidarr, and genre tagging (MusicBrainz genres are sparse and user-voted).
- DVD/Blu-ray/data-disc ripping (steps 4–5) and burning (step 8).

## Anti-goals

- **A messy library again.** An unverified, ambiguous or unmatched rip must never
  file itself silently.
- **An unattended rip that stops to ask a question.** cyanrip prompts when a disc
  matches several releases; it must never be run in a way that can prompt.
- **One album split across two folders** because disc 2 matched a slightly
  different name or year.
- **The same disc ripped twice by accident.** For example, a brief read glitch
  re-firing "inserted" (step 2's deferred minor M5), or someone pressing Rip
  twice.

## Constraints

- **cyanrip is not in Debian 12 (bookworm).** Debian 13 (trixie) has 0.9.3.
  - The image moves from `python:3.12-slim-bookworm` to `python:3.12-slim-trixie`.
  - All step 1–2 tests, the CI smoke test (tini PID 1, app uid 99) and the
    device-node entrypoint must still pass on it.
- **MusicBrainz API etiquette:**
  - at most 1 request per second;
  - a meaningful User-Agent: `super-ripper/<version> ( https://github.com/Barrydamonke/super-ripper )`;
  - no personal contact details in it.
- **Cover Art Archive:** `https://coverartarchive.org/release/<mbid>/front-1200` (follow redirects).
  - Missing art is normal; it is not an error.
- **Untrusted text.** Tags, titles and artist names come from the internet or
  were typed in.
  - They are never passed through a shell.
  - They are escaped in templates.
  - They are sanitised before being used in paths.
- **Ownership.** Files are written as 99:100, umask 0002, as in step 2.
- **Shutdown.** A CD rip is a `uses_drive` job, so it holds shutdown through
  `/api/busy` and is interrupted cleanly on container stop.
- **No new pip dependencies except `mutagen`** (tag writing).
  - HTTP keeps using urllib in a thread, as in step 2.

## Approaches considered

1. **The app owns metadata; cyanrip only rips (chosen).**
   - cyanrip runs with MusicBrainz and cover lookup off (`-N -U`). It reads the
     disc, verifies with AccurateRip, computes ReplayGain and writes FLAC.
   - The app looks up MusicBrainz, writes tags with mutagen and fetches covers.
   - A straight match, a choice between candidates and a manual fix all go
     through the same tagging and filing code.
2. **cyanrip owns metadata.** Less code for perfect matches, but every odd case
   still needs the app to look up and re-tag, giving two tagging paths that can
   disagree. cyanrip also prompts when a disc matches several releases.
3. **whipper instead of cyanrip.** Less actively maintained and heavier to
   install; cyanrip is the plan's choice.

## Design

### Library layout (decided with the owner)

```
/data/MEDIA/Music/
  <Album Artist>/
    <Album> (<Year>)/
      cover.jpg
      01 - <Title>.flac                     single disc
      1-01 - <Title>.flac                   release with more than one disc
  Various Artists/
    <Album> (<Year>)/
      01 - <Track Artist> - <Title>.flac    compilations include the track artist
```

- **Album Artist** is MusicBrainz's release artist credit as displayed (for
  example "Simon & Garfunkel").
  - Compilations have the MusicBrainz "Various Artists" artist
    (`89ad4ac3-39f7-470e-963a-56509c546377`) and go under "Various Artists".
- **Year** is the year of the chosen release's date. With no date, the
  `(<Year>)` part is left out.
- **The disc prefix** is used whenever the release has more than one medium,
  even for disc 1 ripped alone. That way disc 2 sorts correctly when it arrives.
- **Sanitising path components:**
  - replace `/ \ : * ? " < > |` and control characters with `_`;
  - trim trailing dots and spaces;
  - cap each component at 120 characters;
  - an empty result becomes `_`.
- **The release-to-folder link is stored in the database** (`albums` table) when
  an album is filed. The second disc of a set therefore finds the folder by
  release ID, never by recomputing the name.

### Disc identification at insert

- When the poller sees an audio or mixed disc, the app reads the TOC with the
  CD-ROM ioctls (`CDROMREADTOCHDR`, `CDROMREADTOCENTRY` for each track and the
  lead-out) and computes the **MusicBrainz disc ID** (SHA-1 over first/last
  track and offsets+150, MusicBrainz's base64 alphabet).
  - **Enhanced (mixed) CDs:** use the last *audio* track's end as the lead-out
    (data track start − 11400 sectors), per MusicBrainz's rule. Only the
    audio tracks are ripped.
  - The TOC parser and disc-ID function are pure, and tested against published
    MusicBrainz examples.
- The disc ID and track count are shown on the card.

### Starting a rip

- **On an insert event** (the step-2 `on_insert` hook), for an audio or mixed
  disc with **Auto-start rips** on, the app enqueues a `rip_cd` job:
  - `uses_drive=1`;
  - `needs_bytes` = audio length × 176,400 bytes/s × 0.75 (FLAC ratio, rounded up);
  - params: disc ID, TOC, the drive's read offset.
- **Duplicate-rip guard** *(builder's call, from the anti-goal)*: no new `rip_cd`
  job is started automatically for a disc ID that has a queued or running job,
  or any `cd_rips` row in state `needs_look` or `filed`, however long ago.
  - The card then shows "Already ripped (filed as …) — Rip again?" or "Already
    waiting on the Music page", with a **Rip again** button.
  - A disc whose earlier rip failed, was cancelled, interrupted or discarded
    does auto-start.
  - This also covers step 2's deferred minor M5 (a read glitch re-firing insert).
- **Disc already in the drive when the container starts** (first poll, so no
  insert event): the card shows a **Rip** button. The same button appears when
  auto-start is off.

### The rip job (`rip_cd` handler)

- **Command.** It runs cyanrip as a subprocess with a fixed argument list
  (no shell):
  - `-d /dev/srX`
  - `-s <offset>`
  - `-r <retries>`
  - `-P <3 secure | 0 fast>`
  - `-N -U` (no MusicBrainz, no cover lookup)
  - `-o flac`
  - `-D rip -F "{track}"`, fixed names inside `/work/<jobId>/`
  - `-L log`
  - `-Q` only if the eject happens through cyanrip (see Eject)
  - Everything after `-d` comes from settings and the drive row, never from
    disc text. The working directory is `/work/<jobId>/`.
- **ReplayGain stays on** (cyanrip's default). Its `REPLAYGAIN_*` tags are kept
  through re-tagging; step 8 needs them.
- **Progress:**
  - Parse cyanrip's stdout line by line for the current track and per-track
    progress, then report "Track 4 of 11 — 63%" with an estimate from the
    rate so far.
  - The exact line format must be confirmed against a real run of cyanrip
    0.9.3. **The parser must degrade gracefully:** unknown lines are ignored,
    and progress falls back to "Track N of M" (completed track files counted).
- **AccurateRip:**
  - Read cyanrip's log file after the rip for each track's AccurateRip result
    (verified with confidence N / not in database / mismatch).
  - The album is **verified** when every track is verified with confidence ≥ 1.
  - The log is kept with the files as `rip.log`.
- **Cancel / container stop:** terminate the cyanrip process (SIGTERM, then
  SIGKILL after 5 s). A retry starts from scratch.
  - This extends the step-2 runner: handlers can register a child process that
    `stop()`/cancel kill.
- **Exit status:**
  - non-zero exit, or missing track files → `failed` with the last error lines;
    the disc stays in for Retry;
  - success → the eject (below) and identification follow.
- **Retry re-checks the disc** *(builder's call)*: it re-reads the TOC first and
  refuses, saying so on the card, if the disc ID in the drive isn't the one the
  job was for.
- **Failures after the rip** *(builder's call)*: once the audio is safely ripped,
  the job counts as `done`. Any later failure (MusicBrainz down, the move to
  downloads, tagging, filing) puts the item on Needs a look with the reason,
  and the files stay wherever they last were. The card's "#14 done → …" line
  shows where it went.
- **Work-folder clean-up** *(builder's call)*:
  - `/work/<jobId>/` is removed once its files have moved to downloads.
  - The folder of a failed, cancelled or interrupted rip is kept for diagnosis.
    It is removed when that job is retried or discarded, and swept at start-up
    once its job finished more than 7 days ago, so the work budget isn't eaten.

### Eject

The disc is ejected when its rip finishes successfully, using the step-2 eject
path under the drive lock. On failure the disc stays in so Retry doesn't need a
re-insert.

### Identify, tag, decide

- **Lookup:** `GET https://musicbrainz.org/ws/2/discid/<discid>?inc=artist-credits+recordings+release-groups+labels&fmt=json`
  through a small client that enforces 1 request per second and the User-Agent.
  - Network errors are retried 3 times with backoff, then the reason becomes
    "MusicBrainz unreachable".
- **Candidates** are the releases returned, each with its medium matching this
  disc ID (which gives the disc number).
  - **Equivalent candidates:** many pressings share a disc ID. Candidates count
    as equivalent when album title, release artist credit and this medium's
    track titles all match. Equivalent candidates are **one** match; the release
    with the earliest date wins (ties: first returned).
  - Several *non*-equivalent groups → reason "several possible releases".
  - None → "no MusicBrainz match".
- **Tags** (Vorbis comments with Picard-compatible names, so Jellyfin and future
  tools read them):
  - `TITLE`, `ARTIST`, `ALBUM`, `ALBUMARTIST`
  - `TRACKNUMBER`, `TRACKTOTAL`, `DISCNUMBER`, `DISCTOTAL`
  - `DATE`, `ORIGINALDATE` (release group first release)
  - `LABEL`, `BARCODE`
  - `MUSICBRAINZ_ALBUMID`, `MUSICBRAINZ_RELEASEGROUPID`, `MUSICBRAINZ_TRACKID`
    (recording), `MUSICBRAINZ_ARTISTID`, `MUSICBRAINZ_ALBUMARTISTID`, `MUSICBRAINZ_DISCID`
  - existing `REPLAYGAIN_*` tags are preserved.
- **Cover:** fetch `front-1200` and save it as `cover.jpg` next to the files; it
  is not embedded. Optional by setting. A failure is logged, not a reason to hold.
- **Move out of /work:** right after tagging, the files and `cover.jpg` move to
  `/data/downloads/disc-ripper/<jobId>/` (plan: FLAC leaves the work area as
  soon as the rip finishes). This is a copy then delete across mounts.
- **Same album, different pressing** *(builder's call, to keep sets in one
  folder)*:
  - Disc IDs are attached to pressings unevenly, so disc 2 of a set can match a
    different release of the same album than disc 1 did.
  - When choosing among candidates, prefer one whose **release group** is
    already in `albums` with the **same number of discs**.
  - Such a disc **joins that album's folder**. Album-level tags (`ALBUM`,
    `ALBUMARTIST`, `DATE`, `MUSICBRAINZ_ALBUMID`, label, barcode) are copied from
    the filed album so Jellyfin shows one album. Track-level tags come from this
    disc's own match.
  - A match in the same release group but with a *different* disc count (for
    example a deluxe edition) is not joined automatically. It goes to Needs a
    look with "another edition of an album in the library".
- **Already owned?** The album is a duplicate if:
  - the `albums` table already has this release (or a joined release of the same
    group) **and** this disc number is already filed; or
  - Jellyfin is configured and reports a MusicAlbum with this MusicBrainz
    release ID. The query must be checked against the owner's Jellyfin version;
    if filtering by provider ID isn't supported, search by album name and
    compare `ProviderIds.MusicBrainzAlbum`.
  - Jellyfin errors don't block. The DB check alone decides, and a note is shown.
- **Decision:**
  - **File automatically** when approval is "only check the odd ones" and:
    - there is exactly one (equivalent) match;
    - every track is AccurateRip-verified;
    - it is not a duplicate.
  - **Otherwise it goes to Needs a look** with every reason that applies:
    "no MusicBrainz match", "several possible releases", "N tracks not verified
    (not in AccurateRip / mismatch)", "already in the library", "MusicBrainz
    unreachable".
  - With approval set to "always ask", everything goes to Needs a look.

### Filing into the library

- **Target folder:** from the `albums` table if the release is already there
  (later discs of a set); otherwise build it from the layout rules and record it.
- **Move** each file to its final name. This is a rename within `/data`, falling
  back to copy and delete.
  - If a target file already exists, stop and send the item back to Needs a
    look with "file already exists". Nothing is ever overwritten.
- Write `cover.jpg` if the folder doesn't have one.
- Record the filing in the `albums` table. Set the job's library state to
  **filed** and remove the empty downloads folder.
- **Jellyfin refresh:** `POST /Library/Refresh` with header
  `Authorization: MediaBrowser Token="<key>"`.
  - Debounced: one refresh 60 s after the last filing, so a stack of CDs
    triggers one scan.
  - Errors are logged and shown on the Music page; they don't fail the filing.

### Needs a look / Recently filed (new "Music" page)

```
Music
  Needs a look (2)
    Disc 3524728…  11 tracks · 2 tracks not verified · several possible releases   [Open]
    Unknown album  9 tracks  · no MusicBrainz match                               [Open]
  Recently filed
    Pink Floyd — The Dark Side of the Moon (1973) · 10 tracks · verified · 21:42   [Edit]
```

- **The item page shows:**
  - the reasons;
  - the tracks, with duration and AccurateRip status;
  - a play link per track (a plain `<audio>` element on the FLAC file), useful
    for checking an unverified track;
  - candidate releases (title, artist, year, country, label, barcode, track
    list), to choose with a radio button.
- **Resolve options:**
  - **Use this release** → tag, then file.
  - **MusicBrainz URL or ID** → look up that release and choose the medium
    whose track count matches (ask when several do) → tag, then file.
  - **Manual:** album, album artist, year, disc number / total, and a title (and
    artist, for compilations) per track → tag (no MusicBrainz IDs), then file.
    - Manual albums get an `albums` row keyed by a generated ID, so a later disc
      can be pointed at the same folder.
  - **Add to an existing album** *(builder's call)* → pick an album from the
    library (searchable list from `albums`) and a disc number. Album-level tags
    are copied from that album. Track titles come from this disc's MusicBrainz
    match if there is one, otherwise they are typed in. Then the disc is filed
    into that album's folder.
    - This is how disc 2 of a hand-entered album, or of an unmatched pressing,
      joins disc 1.
  - **Discard** → delete the downloads folder; the job is marked discarded.
  - **File anyway** (unverified tracks, or a duplicate the owner wants) → file
    with the current tags. A duplicate goes to a new folder with " (2)" appended,
    and never overwrites.
- **Edit on a filed album** reuses the same form. It re-tags the files in place
  and, if album, artist or year changed, moves the folder and updates the
  `albums` row.
- The page count of Needs a look shows in the nav ("Music (2)").

### Settings

New "CD ripping" group:

| Key | Kind | Default | Notes |
| --- | --- | --- | --- |
| `cd_rip_mode` | choice: secure / fast | secure | secure = paranoia 3, fast = 0 |
| `cd_retries` | int 0–50 | 10 | cyanrip `-r` |
| `cd_approval` | choice: odd_ones / always | odd_ones | |
| `cd_cover_art` | bool | true | |
| `jellyfin_url` | url | "" | e.g. `http://192.168.68.143:8096` |
| `jellyfin_api_key` | secret | "" | never rendered back; blank on save keeps the old value |

- The settings module gains `choice` and `secret` kinds.
- **FLAC compression level is not offered:** cyanrip 0.9.3 has no option for it.
  It only changes file size anyway.

Per drive: a `read_offset` column (int, −3000..3000), editable on the Drives
page. A migration pre-fills it by model string (`BH10LS38` → +667, `iHAS124`
with suffix A–F → +6), otherwise 0. Those are the AccurateRip drive database
values, with 1,236 and 1,556 agreeing submissions.

### Data (migration 0003)

- `drives.read_offset INTEGER NOT NULL DEFAULT 0`.
- `discs`: disc ID → last seen TOC and drive. Used by the duplicate-rip guard and
  to show the disc ID on the card.
- `cd_rips`: one row per rip job.
  - Fields: job ID, disc ID, state (`ripping` / `needs_look` / `filed` /
    `discarded`), reasons (JSON), candidates (JSON snapshot), the chosen
    release ID and medium, per-track AccurateRip results (JSON), downloads
    path, filed path, timestamps.
- `albums`: release key (MusicBrainz release ID or `manual:<uuid>`) → library
  folder path, release group ID (null for manual), album artist, album, year,
  disc total, the album-level tag values, and the discs filed (number → disc ID).
  - Releases that joined an album (other pressings) map to the same row through
    `album_releases(release_id → album key)`.

### Dashboard card while ripping

```
Blu-ray (HL-DT-ST BD-RE BH10LS38)                       [Cancel job and eject]
  Disc in · Audio CD · 11 tracks · disc ID xG3…
  Running: CD rip #14  [######----] Track 6 of 11 — 58%   finishes ~21:40   [Cancel]
```

After it finishes, the card's recent list shows "#14 done → filed: Pink Floyd —
The Dark Side of the Moon" or "→ needs a look", linked to the Music page.

## Testing

- **Unit tests:**
  - TOC → MusicBrainz disc ID, against MusicBrainz's documented example and an
    enhanced-CD example;
  - candidate grouping and choice;
  - tag mapping from recorded MusicBrainz JSON fixtures, including a compilation
    and a two-disc release;
  - path building and sanitising;
  - the decision rules;
  - filing: same-release folder reuse, never overwriting, " (2)" for
    file-anyway duplicates;
  - settings kinds `choice` and `secret`;
  - migration 0003 pre-filling offsets.
- **The rip handler** is tested with a fake `cyanrip` executable (a script on
  `PATH` in tests). It replays recorded stdout, writes fake FLAC files and a
  log, and can exit with an error or hang for the cancel test.
- **Web tests:** the Music page lists, each resolve path, the secret setting
  never echoed, the Rip button.
- **Image:** trixie base builds, tini/uid checks pass, and `cyanrip -V` works as
  uid 99.
- **Manual, on the server (written into the PR):**
  1. Rip a well-known CD: it files itself with `cover.jpg` and appears in Jellyfin.
  2. Rip an obscure CD: Needs a look, then fix it by hand.
  3. Rip disc 1 then disc 2 of a set: both land in one folder.
  4. Re-insert a filed CD: "Already ripped — Rip again?".
  5. Cancel mid-rip.
  6. Restart the container mid-rip: interrupted, Retry works.
  7. **Capture one real cyanrip stdout and log into the test fixtures**, and
     confirm the progress parser against them.

## Left to the builder

Module split and names; exact cyanrip output parsing (confirmed against a real
run); MusicBrainz client structure; how the Jellyfin duplicate query is shaped
on the owner's version; page layout details beyond the sketches; log wording.
