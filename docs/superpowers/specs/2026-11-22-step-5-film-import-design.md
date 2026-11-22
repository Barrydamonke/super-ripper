# Step 5: Film identification and import (design)

Date: 2026-11-22. Builds on steps 4 and 6 (DVD/Blu-ray rips in
`/data/downloads/disc-ripper/<rip_id>/`, encodes beside them, the Films page,
raw deletion 24 h after a good encode) and the main design plan
(`Disc Ripper – Design Plan.md`: "Identification", "Integrations", "Replacing a
film or adding a second version").

## Intent

The owner is re-ripping about 50 discs whose films are **all already in Radarr**
(from old ARM rips and downloads). Every file there has been renamed by Radarr,
but not every folder has (for example a year-less `Elf`). The owner wants each
encoded rip to end up in the library as that film, replacing the old file, with
as little clicking as is safe. TV discs are out of scope for now (decided with the
owner): they stay in downloads until the episode-mapping step.

## Goals

1. **Identify each film automatically** once its encode is done: search Radarr
   using the disc's own title (MakeMKV's disc name, e.g. "The Lego Movie") or,
   failing that, the cleaned-up volume label, and rank by title similarity and
   by runtime against the main title.
2. **A confident match imports on its own** (decided with the owner);
   **Recently imported** on the Films page lists each import with **Undo**.
   Anything else waits on the Films page with an **Identify** form.
3. **New film** (not in Radarr): add it unmonitored under **Movies** (default
   root folder, decided with the owner), ManualImport the encode, keep it
   unmonitored.
4. **Film already in Radarr with a file** (the usual case): **Replace**.
   - Setting **Ask before replacing**, on by default (decided with the owner):
     the rip waits on the Films page with old and new side by side and
     **Replace**, **Skip**, **Choose a different film**.
   - With the setting off, it replaces automatically, but only when the safety
     checks pass (below); otherwise it waits anyway.
5. **Tidy the folder after a replace:** ask Radarr to move the film to
   `{Movie Title} ({Release Year})` (setting **Tidy folder names on replace**, on
   by default).
6. **Extras** go into the film's `extras` folder (Radarr doesn't do this).
7. **Clean-up:** once Jellyfin shows the film, the rip's downloads folder is
   deleted.
8. **The film is left unmonitored** in Radarr, so it never grabs a download to
   "upgrade" the rip.

## Non-goals

- **TV** (Sonarr, episode mapping) — the episode-mapping step.
- **Second versions and "keep lossless"** — step 7.
- **Choosing Other movies automatically** — always Movies; the owner can switch a
  film on its entry.
- **Encoding or ripping changes.**
- **Identifying CDs** (done in step 3).

## Anti-goals

- **Overwriting the wrong film.** A replace never happens on a weak match, and
  never without a recoverable old file unless the owner confirms.
- **Losing the old file.** Replace only goes ahead (automatically or by click)
  when Radarr's recycle bin is set; if it isn't, the owner must type `REPLACE`
  to confirm a permanent delete.
- **Radarr grabbing downloads** for a ripped film (it stays unmonitored).
- **An import that half-happens** silently (e.g. added to Radarr but the file
  never moved): every step is checked, and a failure leaves the rip on the Films
  page with the reason and Retry.

## Constraints

- All containers mount the share as `/data`, so one path works for the app,
  Radarr and Jellyfin. ManualImport reads from `/data/downloads/disc-ripper/...`.
- Radarr v3 API (the owner's Radarr; endpoints below must be checked against its
  Swagger page before building): `GET /api/v3/movie/lookup?term=`,
  `GET /api/v3/movie/lookup/tmdb?tmdbId=`, `GET /api/v3/movie/lookup/imdb?imdbId=`,
  `GET /api/v3/movie?tmdbId=`, `POST /api/v3/movie`, `PUT /api/v3/movie/{id}`
  (with `moveFiles=true` for the folder tidy), `DELETE /api/v3/movie/{id}`,
  `GET /api/v3/rootfolder`, `GET /api/v3/qualityprofile`,
  `GET /api/v3/manualimport?folder=`, `POST /api/v3/command` (`ManualImport`,
  `RescanMovie`), `GET /api/v3/command/{id}`, `GET /api/v3/config/mediamanagement`
  (recycle bin path). Header `X-Api-Key`.
- HTTP through `urllib` in a thread, like the MusicBrainz and Jellyfin clients.
  **No new pip dependencies.**
- Untrusted text (disc names, Radarr titles) escaped in templates; never used to
  build paths except via Radarr's own paths.
- Settings: `radarr_url` (url), `radarr_api_key` (secret), `ask_before_replace`
  (bool, true), `tidy_folders_on_replace` (bool, true), in a "Radarr" group.

## Approaches considered

- **Radarr's API with ManualImport (chosen; one sensible way).** The app knows
  the film, so it tells Radarr exactly which movie and quality the file is;
  Radarr renames, files and replaces.
- **`DownloadedMoviesScan`.** Radarr guesses the film from the file name and can
  reject a rip it can't match — a failure mode for no gain. Ruled out (main plan).
- **Copying into the library ourselves.** Bypasses Radarr: no rename, no quality
  tracking, no replace handling. Ruled out.

## Design

### When identification runs

- Triggered when a rip's **main title** gets its first `done` encode (any
  preset). Extras ride along with the main title's import. A rip whose main
  title has no done encode stays unidentified; a rip with no main title (TV-like)
  is skipped ("TV: not imported yet").
- The import uses the **best done encode** of the main title: the one whose
  output is intact (same rule as raw deletion); if several, the most recent.
- Identification and import run as a background task in the services (not a
  drive job; they don't need a drive) and never hold `/api/busy`.
- The **raw-deletion sweep must not delete the raw while an import is in
  progress**; afterwards it doesn't matter (the encode is the imported file).

### Search and confidence

- **Search terms:** MakeMKV's disc name (`CINFO` 2, stored in the scan as
  `label`), else the volume label cleaned up: underscores to spaces, drop
  `DISC_?\d`, `D\d`, region codes and `WS`/`FS`, title-case. Both are tried if
  they differ.
- **Candidates:** Radarr lookup results. **Score** = title similarity (normalised:
  lowercase, punctuation and leading "the" removed; ratio from `difflib`) and
  runtime closeness (candidate runtime in minutes vs the main title's scanned
  length).
- **Confident** *(my call)*: the top candidate has title similarity ≥ 0.9 and
  runtime within max(5 min, 6%), and no other candidate also meets both. Anything
  else waits for the owner with the top 5 candidates shown.
- A missing runtime on the candidate is never confident.

### Identify form (Films page)

- Shown for any rip waiting for identification (and on "Choose a different
  film"): search box (name, optional year), or paste an IMDb (`tt…`) or TMDb ID
  or a TMDb/IMDb URL. Results list poster (Radarr's `remotePoster`), title, year,
  runtime, and "in your library" when Radarr has it. **Use this film** imports
  (subject to the replace rule) and a root-folder choice (Movies / Other movies)
  appears next to it, Movies preselected.

### Importing

1. **Find or add the film in Radarr:**
   - `GET /api/v3/movie?tmdbId=`; if present, use it.
   - Otherwise `POST /api/v3/movie` with the lookup's fields, `rootFolderPath` =
     the Movies root (from `/api/v3/rootfolder`, matched by folder name `Movies`;
     error if not found), `qualityProfileId` = the first profile *(my call; the
     film is unmonitored so the profile only labels it)*, `monitored: false`,
     `addOptions: {searchForMovie: false}`.
2. **Replace check:** if the movie `hasFile`, this is a replace:
   - Gather old and new details: old from Radarr's `movieFile` (path, size,
     `mediaInfo` resolution, codecs, runtime, audio), new from ffprobe.
   - **Safety checks:** new length within 2% of the old file's (or 5 min,
     whichever is larger — DVD vs Blu-ray cuts differ slightly *(my call)*), and
     Radarr's recycle bin is set.
   - If `ask_before_replace` is on, or a check fails: wait for the owner
     ("Replace?"). A failing check is named in the comparison.
   - Otherwise continue.
3. **ManualImport:** `GET /api/v3/manualimport?folder=<the encode's folder>` to get
   Radarr's view of the file, then `POST /api/v3/command` `ManualImport` with the
   encode's path, `movieId`, a quality (from the disc type and preset:
   DVD → `DVD`; Blu-ray Standard/Fast → `Bluray-1080p`; Copy of a Blu-ray →
   `Remux-1080p`), languages from Radarr's view, `importMode: "move"`.
   Poll the command until `completed` or `failed` (timeout 10 min); then confirm
   via `GET /api/v3/movie/{id}` that `movieFile.path` is the new file.
4. **Unmonitor:** `PUT /api/v3/movie/{id}` with `monitored: false` (it may already
   be).
5. **Tidy the folder** (replace only, setting on): if the folder name differs
   from `{title} ({year})` (Radarr's `folderName` or computed), `PUT` the movie with
   the new path and `moveFiles=true`; confirm the move.
6. **Extras:** copy each extra's best done encode (or its raw file if it has no
   encode) into `<movie folder>/extras/`, never overwriting (" (2)").
7. **Jellyfin:** ask Jellyfin to refresh (existing debouncer), then poll
   (every 5 min, up to 24 h) for the item by TMDb provider ID with the new path.
   When seen, delete the rip's downloads folder (raw, encodes, extras) and mark
   the import `in_jellyfin`. If Jellyfin isn't configured, delete the folder
   24 h after the import *(my call)*.
- **Any failure** stops the import with the reason on the Films page and
  **Retry** (which resumes from the failed step; steps 1, 3 and 4 are safe to
  repeat because each checks Radarr's state first).

### Undo (Recently imported)

- Available until the rip's downloads folder is cleaned up.
- **New film:** move the imported file back to `downloads/<rip_id>/`, then
  `DELETE /api/v3/movie/{id}?deleteFiles=false&addImportExclusion=false`, then
  remove the now-empty movie folder (only if empty).
- **Replace:** move the new file back to downloads; move the old file back from
  Radarr's recycle bin (`<recycleBin>/<old folder name>/<old file name>`) into the
  movie folder; if the folder was tidied, move the film back to its old path
  (`PUT moveFiles=true`); `RescanMovie`. If the old file isn't in the recycle bin
  (bin emptied), Undo says so and only returns the new file.
- Undo leaves the rip waiting for identification again (so "Choose a different
  film" is just Undo + Identify).

### Decisions on edge cases *(my calls, from the builder check)*

- **Which encode is imported:** chosen at the moment the import actually runs
  (including after **Replace** is clicked): the most recent done encode of the
  main title whose output is intact. Its preset decides the quality label. A
  better encode added while a rip waits is therefore the one imported.
- **A film in Radarr without a file** (monitored-missing): a plain import, no
  replace check; it's set unmonitored afterwards like any ripped film.
- **After `importMode: move`** the encode's file is in the library: the encode row
  records the new path and an `imported` mark, and the Films page shows
  "imported". The raw and any other encodes of the rip stay in downloads until
  clean-up (so Undo has everything); the raw-deletion sweep skips imported rips.
- **The old file's place in the recycle bin** is recorded, not guessed: list the
  bin's matching folder before and after the import and store the new arrival's
  exact path. Undo of a replace uses that path; if it's gone, Undo says so.
- **Jellyfin "seen":** an item with the film's TMDb provider ID whose media
  source path equals the imported file's path **and** whose size equals that
  file's size (a same-name replace keeps the path, so path alone isn't enough).
  New Jellyfin client code (`/Items` with `AnyProviderIdEquals`, fields
  `MediaSources`) — check against the owner's Jellyfin version.
- **Two rips of the same film at once:** one import per TMDb ID at a time; the
  second waits ("Another rip of this film is being imported") and then becomes a
  replace of the first (subject to Ask before replacing).
- **Downgrade guard:** a replace where the new file's resolution is lower than the
  old file's (e.g. a DVD encode over a 1080p file) counts as a failed safety
  check, so it always waits for the owner, with "lower resolution than your
  current file" shown. If Radarr refuses an import, the import fails with
  Radarr's reason.

### Data (migration)

`film_imports`, one row per rip being imported:
`rip_id` (PK), `state` (`identifying`/`needs_identify`/`needs_replace_ok`/
`importing`/`imported`/`in_jellyfin`/`failed`/`undone`/`skipped`/`tv_skipped`),
`candidates` (JSON, top 5 with scores), `tmdb_id`, `movie_id`, `title`, `year`,
`root_folder`, `encode_id`, `replace` (JSON: old file details, safety check
results), `old_path`, `old_folder`, `new_path`, `extras` (JSON paths),
`message`, `error`, `created_at`, `updated_at`, `imported_at`.

### Films page

- Per rip, a line under the rip header for its import state: "Identifying…",
  "Waiting for you: pick the film" (Identify form), "Replace? <comparison>
  [Replace] [Skip] [Choose a different film]", "Importing…", "Imported as Elf
  (2003) → Movies/Elf (2003)/… [Undo]", "In Jellyfin", "Failed: <reason>
  [Retry] [log]".
- **Recently imported:** a section at the top with the last 20 imports and Undo.
- **Skip** deletes the rip's downloads folder after a `confirm()` and marks it
  skipped.

### Logs

`/config/logs/import-<rip_id>.log`: every Radarr call (method, path, status, no
API key), decisions, and errors; linked from the Films page.

## Testing

- **Unit:** label cleaning; scoring and the confident rule (tables of
  candidates, incl. a remake with the same title and a different runtime);
  quality mapping; folder-name tidy check.
- **Radarr client** against recorded JSON fixtures and a fake fetcher (lookup,
  movie, rootfolder, manualimport, command polling, errors, timeouts).
- **Import flow** with a fake Radarr: new film; replace with ask on (waits);
  replace auto (checks pass / length fails / recycle bin missing); folder tidy;
  extras; Jellyfin confirm then clean-up; failure + Retry resumes; Undo for new
  and replace (old file restored from a fake recycle bin).
- **Web:** Identify search, paste ID, Use this film, Replace/Skip/Choose
  different, Recently imported + Undo, settings (key never echoed).
- **Manual, on the server:** check the endpoints against the Radarr Swagger page;
  import one new film; replace one film with ask on; Undo it; turn ask off and
  replace another; check the folder tidy, the recycle bin and Jellyfin's watch
  history on a replaced film.

## Left to the builder

Module split (e.g. `radarr.py` client, `filmid.py` scoring, `filmimport.py`
flow); exact Radarr JSON shapes (verify against fixtures/Swagger); page layout
beyond the sketch; log wording.
