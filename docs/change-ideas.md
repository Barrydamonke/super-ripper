# Change ideas (not yet chosen)

These came from the code review of Nov 26, 2026, plus items parked during earlier steps. Items A1–A14 from that review were built and merged on Nov 27. Pick any of the IDs below to have them built.

## Larger features still to build

| ID | Feature | Notes |
|---|---|---|
| **P1** | **Test on a clip** | Encode a short sample with a preset to judge size, speed and quality before the raw is deleted. Room is already left in the code: a source window in the command builder, and `kind = 'sample'` on encodes. |
| **P2** | **TV discs** | Sonarr identification and import, plus a screen for matching each title on the disc to an episode. TV discs are ripped and encoded today but not imported. |
| **P3** | **Second versions and Keep lossless** | Keep two versions of a film side by side in Jellyfin, or keep the lossless rip next to the encode. |
| **P4** | **Audio CD burning** | Tracklist builder, volume levelling, CD-TEXT, and erasing CD-RWs first. |
| **P5** | **Data discs to ISO** | Copy data discs to an ISO image with ddrescue. |

## B. Quality of life for a long run

| ID | Change |
|---|---|
| **B1** | Show the disc or film name on running jobs and on Recent rows, instead of "rip_title job #123". |
| **B2** | Add a summary line ("N ripped, M held, K failed") and a browser-tab badge when a drive needs you. |
| **B3** | Fold finished films behind "Show N finished" on the Films page. It redraws every film on every change and will slow down as the library grows. |
| **B4** | Encode queue: run main titles before extras, and add "Run next", "Cancel all queued" and the total time left. |
| **B5** | Add "Retry all failed imports", and retry once automatically when Radarr was briefly unreachable. |
| **B6** | Jellyfin clean-up: add "Clean up now" and "Check again" buttons, and nudge Jellyfin to refresh again if the film doesn't appear. |
| **B7** | Identify: pre-fill the search box with the cleaned disc label, and use a year in the label as a tie-break. |
| **B8** | Show "Waiting for the main title's encode", so a waiting film doesn't look stuck. |
| **B9** | Add a "Re-read disc" button. A disc whose first read failed stays "Unknown type" until it's re-inserted. |
| **B10** | Warn when a rip would run past the shutdown grace period, and show schedule-fetch errors and the date of an old schedule. |
| **B11** | Show each preset's measured speed (e.g. "0.4× real time"). |
| **B12** | Make the stall message agree with the "Retrying automatically" note, and clear stale retry notes after a restart. |
| **B14** | Move extras into the library instead of copying them (less disk work), through a temporary name so a crash can't leave half a file. |

## C. Audio CDs

| ID | Change |
|---|---|
| **C1** | Add a stall watchdog for cyanrip. A stuck CD blocks its drive forever. |
| **C2** | After a restart, a half-ripped CD shows as "needs a look" and can be filed incomplete. |
| **C3** | Fetch cover art for albums that are identified by hand. |
| **C4** | Don't force manual approval for a CD that AccurateRip doesn't know when it read cleanly. |
| **C5** | Add a "Look up again" button for when MusicBrainz was down. |
| **C6** | Add an "All albums" list. Only the last 20 filed albums can be edited today. |
| **C7** | Add quick "File as suggested" and "Discard" buttons on the list. |
| **C8** | Smaller fixes: adding disc 2 to an album filed as single-disc, a half-copied file blocking retries, files tagged twice, a cover-art fallback, and discs that start with a data track. |

## D. Security hardening (LAN-only, low urgency)

| ID | Change |
|---|---|
| **D1** | Rapid wrong-password attempts can freeze the UI. Add a non-blocking delay and a backoff. |
| **D2** | The login check runs a database query on the main thread for every request. Cache it. |
| **D3** | Create the secret file private from the start, add "log out everywhere", close the first-run setup race, and support a reverse proxy. |

## E. Remove or tidy

| ID | Change |
|---|---|
| **E1** | Delete the "Raw rip retention (24 h)" setting, which does nothing; the real one is "Keep raw rips". Also delete the unused "ISO folder" setting, and "burns" from the descriptions. |
| **E2** | After the mass rip, remove the old copy fallback for rips and the `/work` lookups for rips made before the `.partial` change. |
| **E3** | Move the test-job controls on the drive cards behind a debug setting. |
| **E4** | Delete unused code: `audio_plan`, `is_progress_noise`, `cd_rips.create`, and duplicated helpers. |
| **E5** | Prune old job logs and job rows. |
| **E6** | Show the app version in the page footer. It's already in the start-up log. |

## F. Small bugs (each under an hour)

| ID | Change |
|---|---|
| **F1** | Write MakeMKV's settings file atomically. The second drive could read it half-written and look unlicensed. |
| **F2** | Put a time limit on killing a MakeMKV stuck on a wedged drive. Today it can hang that drive's queue forever. |
| **F3** | Don't treat one `not_ready` reading as a disc removal. |
| **F4** | Fix a race that can drop "raw deleted" records. |
| **F5** | Delete a raw before marking it deleted, so a crash can't orphan the file. |
| **F6** | Count interrupted encodes and unfinished rips when refusing to delete a preset. |
| **F7** | Cap AC-3 stereo at 640 kb/s. |
| **F8** | Undo after clean-up loses the extras. |
| **F9** | Skipped rips still show Encode buttons. |
| **F10** | Undo doesn't restore a film's Radarr monitoring. |
| **F11** | Read the Radarr root folders and quality profile from Radarr instead of hard-coding them. |
| **F12** | Don't say "In Jellyfin" when Jellyfin isn't set up. |
| **F13** | Skip HandBrake's full-file subtitle fix-up pass when there are no subtitles. It doubles `/work` use. |
| **F14** | Use best-effort I/O priority rather than idle, so a starved encode isn't mistaken for a stall. |
| **F15** | Group all of Settings, and apply "Encodes at once" without a restart. |

## G. Parked earlier (rare or minor)

| ID | Change |
|---|---|
| **G1** | A preset whose stored settings stop being valid can't be deleted from the page, and its name stays taken. |
| **G2** | A finished title's `.partial` file is never swept if its rip record has gone. |
| **G3** | If Radarr adds a suffix to a recycled file's name, Undo's exact-name match misses it. |
| **G4** | The "Check the library" message after a misplaced import is never cleared. |
| **G5** | The encode-choice dropdown goes out of date when two browser tabs are open. |
| **G6** | Cancelling a disc's main title still lets its extras rip. |
| **G7** | The README's GPU check uses `setpriv`; a simpler command would do. |
| **G8** | The real cyanrip captures are still in `git stash`, waiting to become test fixtures. |
| **G9** | Rips and encodes don't count each other's not-yet-written bytes when the cache and `/work` share one pool. The free-space floor covers this in practice. |
