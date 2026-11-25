# History, tidy main pages, and films deleted in Jellyfin (design)

Date: 2026-11-25. Builds on steps 2–6, step 5's film import (`film_imports`, Radarr client,
Jellyfin `movie_sources`) and the preset builder.

## Intent

The owner's main pages fill up:
- the drive cards' "Recent" job list
- the dashboard
- the Films page, which lists every rip ever made

Test rips make it worse. The owner wants finished things off the main pages, with a full record
kept on a History page. That record covers everything that ever happened, logs included.

The owner deletes films through Jellyfin. When a film disappears from Jellyfin, the ripper should
mark it **Unavailable** and remove it from Radarr, so the three programs stay in step without the
owner tidying each one by hand.

## How the three programs behave today (researched)

- **Jellyfin:** deleting a film in its UI deletes the film's whole folder from disk, extras
  included, and drops it from the library. The Jellyfin user needs permission to delete media.
  This comes from forum reports, not official docs.
- **Radarr:**
  - **After a delete:** Radarr keeps the film. After its next disk scan it shows the film as
    Missing (`hasFile: false`).
  - **No re-download:** the ripper imports every film unmonitored, so Radarr never downloads it
    again.
  - **Ripping it again:** if the disc is ripped again later, step 5 treats a Radarr film with no
    file as a fresh import, which already works.
- **The ripper:** notices nothing. It keeps showing the film as imported. The raw rip folder is
  already gone, because clean-up deletes it once Jellyfin has the film.

## Goals

1. **The main pages only show what matters now.** That means what's in progress, what needs the
   owner, and what finished in the last **7 days**.
   - **Test jobs** leave the drive cards after **24 hours**.
   - **Everything older** is reachable only from History.
2. **A History page** (`/history`, nav link between Drives and Settings). It lists everything
   ever ripped, newest first:
   - every DVD and Blu-ray rip, with its files, encodes (preset, outcome) and import outcome
     (including Unavailable)
   - every audio CD rip, with its album and state
   - every test job
   - every failed or cancelled job

   Every item links its logs: rip jobs, encodes and the import log. Filters: All / Films /
   Music / Tests, plus a text search on the disc label, film title or album. 50 items per page.
3. **Films deleted in Jellyfin are noticed.** A background check runs every **6 hours**. History
   also gets a **Check now** button.
   - When an imported film's file has gone from disk and Jellyfin no longer has it, the import is
     marked **Unavailable – deleted in Jellyfin** with the date.
   - The film is then removed from Radarr. Only Radarr's entry goes (`deleteFiles=false`); no
     files are touched.
   - This also happens for films the owner had in Radarr before the ripper. *(The owner decided
     removal from Radarr; applying it to every film is my call, explained to the owner. They
     described deleting in Jellyfin as "I don't want this film any more".)*
4. **Deleting inside the ripper.** Every delete has a confirmation step.
   - **Delete leftover files…:** on a rip that still has files in
     `downloads/disc-ripper/<rip_id>/`, such as raw rips or encodes not imported. This includes
     test rips and failed rips. It deletes that folder and nothing else.
   - **Delete film…:** on an imported film that's still available. It does what deleting in
     Jellyfin does:
     1. Radarr deletes the film with its files (`DELETE /movie/<id>?deleteFiles=true`), which
        uses Radarr's recycle bin if one is set.
     2. Jellyfin refreshes.
     3. The import is marked **Unavailable – deleted from the ripper**.

     The confirmation asks the owner to type the film's title, because this removes the film
     from their library.
   - **What a delete does to the record:** it never removes anything from History. The record,
     its logs and the rip's database rows stay. Deleting files or a film moves the rip off the
     main pages straight away. *(My call: the owner said "history keeps a record of everything
     that's ever happened".)*

## Non-goals

- **Music deleted in Jellyfin.** Not detected. CD rips are filed straight into the music library,
  and there is no Lidarr. History still lists them. *(My call; easy to add later.)*
- **Deleting History records or logs.** The record is permanent by design.
- **A Jellyfin webhook** for instant detection. The owner chose the 6-hour check, approach A.
- **TV.** Step 5 imports films only.

## Anti-goals

- **Removing films from Radarr because the share was briefly missing.** If the array is stopped or
  `/data` is unmounted, every film looks gone. Safeguards:
  - **Root folder check:** a film counts as missing only if its Radarr root folder exists and
    isn't empty.
  - **Jellyfin must be reachable,** and must report no item with that TMDb ID at that path. Without
    Jellyfin configured, the file being gone is enough, but only after the two-check rule below.
  - **Two checks:** a film must be seen missing on **two checks at least 1 hour apart** before
    anything happens. The first sighting is recorded and only logged.
  - **Too many at once:** if more than **3** films are seen missing in one check, nothing is
    removed. History shows "5 films look missing from disk. If your array was stopped, ignore
    this. Otherwise, confirm to mark them unavailable." with a **Confirm** button (a confirmation
    step).
- **Removing a Radarr film that now has a different file.** If Radarr's film has a file and its
  path isn't the one the ripper imported, the owner re-imported or replaced it. In that case
  Radarr is left alone, and only the ripper's import is marked Unavailable.
- **Deleting outside the ripper's own folders.** "Delete leftover files" only ever removes
  `downloads/disc-ripper/<rip_id>/`. It is refused while that rip has a queued or running job or
  encode, or an import in progress. "Delete film" only deletes through Radarr.
- **Hiding something that still needs the owner** because it's old. A rip waiting for Identify or
  Replace OK, or a CD needing a look, stays on the main pages however old it is.

## Constraints

- **Radarr 7 API:** `DELETE /api/v3/movie/{id}?deleteFiles=…&addImportExclusion=false`
  (`radarr.delete_movie` exists). Jellyfin uses the existing `movie_sources(tmdb_id, title)`.
- **One flow at a time:** the existing per-rip `import_lock` must be held for the check and for
  Delete film, so they can't race Undo, Retry or clean-up.
- **No new pip dependencies.** Files are 99:100, as before.
- **Logs:**
  - Every check decision and every delete goes in the import log
    (`/config/logs/import-<rip_id>.log`). For a rip with no import, it goes in a new
    `/config/logs/rip-<rip_id>.log`.
  - The Radarr API key never appears in logs.

## Approaches considered (owner chose A)

- **A. Automatic tidy-up plus a regular check (chosen).** Main pages show only current and recent
  things, and History holds the rest. A background check notices films gone from Jellyfin. No
  setup is needed.
- **B. The same, but clearing by hand.** Finished items stay until the owner presses Clear on each
  one. It needs more clicking, and clutter comes back if the owner forgets.
- **C. The same, but a Jellyfin webhook tells the ripper at once.** It needs the Webhook plugin
  installed and configured, and deletions go unnoticed if the plugin breaks.

## Design

### What counts as "finished" (leaves the main pages after 7 days)

- **A DVD or Blu-ray rip** is finished when none of these is true:
  - it is still ripping
  - it has a queued or running encode
  - its import is in `identifying`, `needs_identify`, `waiting_other`, `needs_replace_ok` or
    `importing`
  "Finished at" is the latest of: the rip's `updated_at`, its last encode's `finished_at`, and its
  import's `updated_at`.
- **A rip whose files were deleted,** or whose film is Unavailable, is finished at once and leaves
  the Films list straight away. It is filtered out, not just shown differently.
- **One import per rip:** `film_imports` has one row per rip (the key is `rip_id`). A re-rip of
  the same disc is a new rip with its own row, so the two never hide each other.
- **Test jobs** are `jobs` rows with `kind = 'test'`, the fake jobs from the dashboard's test
  form. They have no rip and no downloads folder, so they have no delete button. A real disc
  ripped "to test" is an ordinary rip.
- **A CD rip:** `needs_look` always shows. `filed` and `discarded` show for 7 days.
- **Drive-card "Recent" jobs:** jobs finished in the last 7 days, still at most 5, with test jobs
  only for 24 hours. A failed last job still shows its error banner while it's the newest job.

### Data (migration 0009)

- **`film_imports`** gains these columns:
  - `unavailable_at TEXT`
  - `unavailable_reason TEXT`: `'jellyfin'` or `'ripper'`
  - `missing_since TEXT`: the first sighting, for the two-check rule
  - `radarr_removed INTEGER NOT NULL DEFAULT 0`

  Unavailable is not a new `state`, so the table isn't rebuilt. Unavailable imports keep the state
  `in_jellyfin` (or `imported`).
- **`video_rips`** gains `files_deleted_at TEXT`, set by "Delete leftover files".
- **`internal.missing_warning`:** the last "too many missing" warning (the list of rip ids and
  when), cleared on Confirm or once the films reappear.

**Paths.** Radarr and the ripper share the `/data` namespace, as step 5 established.
`new_path` is Radarr's own `movieFile.path`, confirmed at import, and `root_folder` is Radarr's
root path. Both compare exactly on the ripper's side, with no mapping.

**Timing.** The first check runs 10 minutes after startup, so mounts are ready. After that it
runs every 6 hours. `missing_since` survives restarts.

### The check (`filmgone.py`, run from a Services loop every 6 h, plus Check now)

The candidates are imports in `in_jellyfin`, or `imported` with `new_path` set, that aren't
already Unavailable. For each candidate, with its `import_lock` held:

1. **File still there:** if the file at `new_path` exists, clear `missing_since` and move on.
2. **Root folder:** if the Radarr root folder holding `new_path` (`root_folder`) is missing or
   empty, skip all films under it, and log "share looks unavailable".
3. **Jellyfin:** if Jellyfin is configured and unreachable, skip the whole check. If
   `movie_sources(tmdb_id, title)` returns any source for that TMDb ID, the film is still in
   Jellyfin, so clear `missing_since` and move on.
   - Jellyfin's paths are not in the ripper's `/data` namespace. Step 5 matches on basename and
     size for that reason. So "present" here means any source at all for the TMDb ID, never a
     path compare.
   - A title-search miss only matters when the file is also gone from disk, and the two-check
     rule still applies.
   - If the file is gone but Jellyfin still lists the film, the film counts as present. It
     waits until Jellyfin drops it. Deleting through Jellyfin drops it at once.
4. **First sighting:** if `missing_since` is empty, set it, log it, and move on.
5. **Not long enough:** if `missing_since` is less than 1 hour ago, move on.
6. **Otherwise** the film is confirmed missing.

**After checking every film:**
- **More than 3 confirmed:** nothing is acted on. Write `internal.missing_warning` and stop.
- **3 or fewer:** each one is acted on:
  1. **Look the film up in Radarr** (`movie(movie_id)`, or `movie_by_tmdb` as a fallback):
     - **no such film:** record `radarr_removed = 1`
     - **has a file at a different path:** leave it, and log "Radarr has another file; left
       alone"
     - **otherwise:** `delete_movie(id, delete_files=False)`, then record `radarr_removed = 1`
  2. **Mark the import:** set `unavailable_at = now` and `unavailable_reason = 'jellyfin'`, and log
     it.
  3. **Refresh the pages** with `films_changed()`.
- **"More than 3"** counts the films newly confirmed in that one check. Deletions spread over
  several checks are each acted on normally, which is intended.
- **Confirm on the warning** re-checks each listed film before acting. A film whose file has come
  back is skipped. The rest are acted on the same way, without the "more than 3" rule.
- **A Radarr error:** the film is still marked Unavailable, with `radarr_removed = 0`. History
  shows "Still in Radarr" with a **Remove from Radarr** button, and the next check retries the
  removal.

### Delete actions (routes, every one with a confirmation step)

- **The confirmation:**
  - Each button opens an inline confirmation panel. It says what will be deleted, the folder path
    or the film's title and Radarr folder, and that it can't be undone except through Radarr's
    recycle bin.
  - The panel has **Cancel** and **Delete** buttons.
  - Delete film also needs the film's title typed in, compared ignoring case and surrounding
    spaces.
- **Routes:**
  - `POST /rips/<rip_id>/delete-files` with `confirm=yes`
  - `POST /films/<rip_id>/delete-film` with `typed=<title>`
- **Checked again on the server:** the server repeats every refusal rule (busy rip, wrong typed
  title, already Unavailable). A post without the confirmation fields is refused with 400.
- **Where the buttons appear:**
  - **Delete leftover files…:** Films page rows and History items.
  - **Delete film…:** an imported film's row on Films and on History.

### Pages

**Main pages, filtered as above:**
- **Films:** the rip list and "Recently imported" (7 days).
- **Dashboard:** the drive cards' Recent list.
- **Music:** "Recently filed" (7 days).

Each of these gets a small "Older items are in History" link.

**History** (`/history`), newest first:

```
History                        [All | Films | Music | Tests]   [search____]  [Check now]

⚠ 5 films look missing from disk ... [Confirm]                      (only when warned)

25 Nov  Blu-ray  ELF_BD          Elf (2003) · imported 20 Nov · Unavailable – deleted in Jellyfin 24 Nov
        files: title_t00 (main) → Standard encode, done · logs: rip #41 · encode #12 · import
22 Nov  DVD      TEST_DISC       2 files in downloads · [Delete leftover files…] · logs: rip #38
21 Nov  CD       Abbey Road      filed · log: #36
20 Nov  Test     job #35         done (5 min, 1 GB) · log
```

Unavailable items show the reason, and "Still in Radarr [Remove from Radarr]" when
`radarr_removed = 0`.

### Testing

- **Unit (`filmgone`), with a fake Radarr and Jellyfin and a temporary library:**
  - **Not acted on:** a first sighting only marks; a second sighting under 1 h does nothing; the
    file coming back clears `missing_since`.
  - **Bailing out:** a missing or empty root folder skips the film; Jellyfin unreachable skips the
    whole check.
  - **The "too many" rule:** more than 3 missing films raises the warning and removes nothing;
    Confirm then acts on them.
  - **Radarr outcomes:** Radarr having another file leaves Radarr alone; a Radarr error still
    marks the film Unavailable and retries the removal later.
  - **Normal case:** a film confirmed missing is marked Unavailable and removed from Radarr.
- **The finished rule,** tested per item type:
  - a rip 8 days old waiting for Identify still shows
  - a finished rip 8 days old doesn't
  - a test job 25 h old doesn't
  - a CD needing a look always shows
- **Web:**
  - History lists every type, and its filters and search work
  - a delete without confirmation is refused
  - Delete leftover files is refused while an encode is queued
  - a wrong typed title is refused
  - Delete film calls Radarr with `deleteFiles=true` and refreshes Jellyfin
  - escaping is correct on labels and titles
- **Migration 0009:** it applies to a database at version 8.

## Left to the builder

- **Module and route layout,** following the existing patterns, for example `history_routes.py`
  and `filmgone.py`.
- **Markup, styling and wording,** within the copy above.
- **How History queries are built and paged,** within 50 per page.
