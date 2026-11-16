# Disc Ripper – Design Plan

Nov 13, 2026 · @Joseph

## Goals and scope

One container on the Unraid server that rips, encodes and burns discs from its own web UI, hands finished films and shows to Radarr and Sonarr, and checks Jellyfin before a rip is wasted on something you already own.

**In scope**

- Rip DVD and Blu-ray (including BD-R) to MKV (MakeMKV), audio CDs to FLAC (cyanrip), data discs to ISO (ddrescue)
- Encode with ffmpeg through a preset builder page, hardware (VAAPI) or software (x265)
- Burn audio CDs, playable video DVDs (authored from a file, or a pre-made ISO with menus) and data CDs/DVDs; erase RW discs first
- Identify discs automatically through Radarr, Sonarr and MusicBrainz, with a manual identify screen as fallback
- Duplicate check against Jellyfin, with replace-existing and add-as-second-version options; hand-off into Radarr and Sonarr for naming
- Movies and music first; TV discs supported through a manual episode-mapping screen

**Out of scope**

- Blu-ray burning (Blu-ray is read-only here)
- Guaranteed UHD Blu-ray ripping (needs a compatible drive and firmware), game discs, LaserDisc, HD DVD
- Public exposure and multiple user accounts: admin only, on the LAN and over Tailscale

## Environment and hardware

The service runs as one Docker container on the new Unraid build, next to Jellyfin, Radarr and Sonarr.

| Item | Detail |
| --- | --- |
| Host | New Unraid build (Gigabyte Z370 HD3, Corsair Carbide 200R case) |
| CPU / iGPU | Intel i5-8500, UHD 630 used for hardware encoding |
| Optical drives | 2 DVD drives and 1 Blu-ray drive, all on real SATA ports |
| Blu-ray drive | Believed to be a writer; unverified. Check `cat /proc/sys/dev/cdrom/info` |
| Cache | A cache pool exists; share `Barry-server` is set to cache, then mover |
| Access | LAN and Tailscale, with one password |
| Shutdown | A JSON-driven scheduler (source available) powers the server off, not always at midnight |

**Device handling.** Pass each drive in with `--device`: the `/dev/srX` node and its matching `/dev/sgX` node (MakeMKV needs the `sg` one). Map by `/dev/disk/by-id/...` or by-path, because `srX` numbering can change between boots. Do not use `--privileged`. If `/dev/sg*` is missing on the host, load the `sg` module at boot with the User Scripts plugin.

**GPU history.** Jellyfin was moved from QSV back to VAAPI after kernel crashes under load. Hardware encodes use ffmpeg with VAAPI as the default, and ffmpeg's QSV encoders stay selectable in the preset builder. The hardware path is ffmpeg, not HandBrake's QSV. Jellyfin rarely transcodes now, so contention for the iGPU is low but still handled (see the scheduling section).

**Image build.** MakeMKV is compiled from source, so the Dockerfile uses a multi-stage build (or borrows from an existing MakeMKV image such as jlesage's). The image also needs ffmpeg with VAAPI/QSV support and the Intel media driver, cyanrip, ddrescue, xorriso/growisofs, cdrdao and dvdauthor, with `/dev/dri` passed in for the iGPU.

## Architecture

One FastAPI (Python) backend runs one job queue per drive and shells out to command-line tools, so a drive can never do two things at once.

&#91;embedded content: architecture · 3 layers\]

State lives in SQLite (WAL mode) under `/config`, and a job table doubles as the queue, so no Redis is needed. Tool progress is parsed per tool and pushed to the browser over SSE or WebSocket. A single admin password protects the UI. The front-end framework is not chosen yet: htmx is least work, SvelteKit or React gives a slicker look.

## Storage layout and path mapping

Config and the database live permanently on the cache in appdata; in-progress rips and encodes live under `downloads`. The `Barry-server` share is mounted as `/data` in the service, Radarr, Sonarr and Jellyfin, so every path means the same thing to all of them.

| Purpose | Container path | Notes |
| --- | --- | --- |
| Config and SQLite DB | `/config` → `/mnt/cache/appdata/disc-ripper` | Cache only. Mount the direct cache path, not `/mnt/user`, to avoid FUSE locking problems. SQLite in WAL mode. |
| Work area (in-progress rips, encodes) | `/data/downloads/disc-ripper/<jobId>/` | Neutral job-ID names until the disc is identified |
| Finished, awaiting identification | `/data/downloads/disc-ripper/awaiting-id/` (proposed) | Kept outside every Jellyfin library path. Final location still to confirm. |
| Hand-off to Radarr/Sonarr | `/data/downloads/Completed/disc-ripper/` (proposed) | Separate from the `radarr` folder used by qBittorrent |
| Movies | `/data/MEDIA/All Movies/Movies` and `/data/MEDIA/All Movies/Other movies` | Radarr root folders |
| TV | `/data/MEDIA/All TV Shows/Tv_shows` and `/data/MEDIA/All TV Shows/Other Tv` | Sonarr root folders |
| Music | `/data/MEDIA/Music/<Artist>/<Album>` | Reached via a staging step you approve |

**Risk: the mover.** The share is cache, then mover. If the mover runs while a job is mid-pipeline, finished intermediate files can be moved to the array, which slows the next step. Options: keep the work area on a cache-only share, or make the job scheduler avoid the mover window. This is listed under open items.

**Ownership.** Files are written as `nobody:users` (99:100) so Radarr, Sonarr and Jellyfin can read, move and rename them.

**Existing ARM output.** `MEDIA/unidentified` holds leftovers from Automatic Ripping Machine. ARM must not run against the same drives at the same time as this service.

## Disc lifecycle

A rip starts as soon as the cooldown ends and never waits for identification.

&#91;embedded content: disc lifecycle · 8 steps, 1 decision\]

Unidentified rips stay under a job-ID name in the Awaiting list until you identify them, then continue into Radarr or Sonarr. The drive ejects 5 minutes after the rip finishes, and a "force eject now" button is always available.

## Identification

Ripping never waits for identification. The service guesses automatically, and when it cannot, the disc lands in an "Awaiting identification" list with a screen modelled on Jellyfin's Identify form.

1. **At insert**, read the volume label (and the disc's own title metadata on Blu-ray) and clean it up: underscores, `DISC_1`, region codes.
2. **Movies and TV**: search Radarr (`/api/v3/movie/lookup`) and Sonarr (`/api/v3/series/lookup`), so no separate TMDb key is needed. Rank candidates by title similarity and by how close the main title's runtime is to the candidate's runtime.
3. **Audio CDs**: cyanrip looks the disc up on MusicBrainz and tags the files.
4. **Confident match**: preselect it, but still show it for confirmation. **No match**: queue it for manual identification.
5. **Duplicate check**: as soon as a guess exists, ask Jellyfin whether the title is already in the library (TMDb ID for films, MusicBrainz release ID for albums), and show the existing file's details with three choices: skip, replace it, or add the new rip as a second version (see the section on replacing and second versions). The rip carries on regardless; only the import waits for your choice.

**Manual identify screen.** Fields: Name, Year, IMDb ID, plus TMDb ID for movies or TVDB ID for TV (Sonarr uses TVDB, not Zap2It). Results show poster, year and runtime so the right candidate is easy to pick. A destination picker chooses the Radarr or Sonarr root folder (Movies, Other movies, Tv\_shows, Other Tv).

**TV discs.** After identifying the series, a manual mapping screen assigns each ripped title to an episode, showing runtimes to help. Automatic runtime matching is deliberately not attempted.

Unidentified items keep a neutral job-ID name until confirmed, and the Awaiting list shows a count so they are not forgotten.

## Ripping tools

Every tool is a command-line program; the backend runs it as a subprocess and parses its progress output.

| Disc | Tool | Output | Progress source |
| --- | --- | --- | --- |
| DVD, Blu-ray | MakeMKV (`makemkvcon`) | MKV per selected title | `-r --progress=-same` (machine-readable) |
| Audio CD | cyanrip | FLAC, tagged from MusicBrainz, AccurateRip checked | Log parsing |
| Data disc, damaged disc | ddrescue | ISO | Its status output |
| Disc probing | `dvd+rw-mediainfo`, `blkid`, `cd-discid` | Disc type, blank or RW state | One-shot calls |

**Title selection.** A quick `makemkvcon info` scan at insert lists every title. By default only the largest title is ripped for movies. A menu lets you tick other titles (extras, other cuts) before the cooldown ends.

**Extras.** Not ripped by default. When ticked, they are kept in the film's `extras` folder for Jellyfin, which Radarr's import does not do for you.

**Auto detection order.** Audio tracks present: audio CD. `VIDEO_TS` present: DVD-Video. `BDMV` present: Blu-ray. Anything else with a filesystem: data disc. You can override the guess in the UI.

**MakeMKV licence.** It needs the beta key (changes periodically) or a purchased licence, stored in config, not baked into the image.

**Eject.** The drive ejects 5 minutes after a job finishes, with a "force eject now" button. A queued job for the same disc cancels the auto-eject.

## Encoding and the preset builder

All encodes use ffmpeg, driven by presets you build on a preset-builder page. The default goal is small files, and keeping the lossless MKV is a per-job or per-preset option for special occasions.

| Engine | Encoders | Strength | Limits |
| --- | --- | --- | --- |
| Hardware | `hevc_vaapi`, `h264_vaapi` on the iGPU | Fast, frees the CPU | Quality set by a constant-quality mode or a bitrate; no two-pass, so an exact file size is only approximated from duration. Test on the real box first. |
| Software | `libx265` (and `libx264`) | Smallest files for a given quality, CRF and two-pass available | Slow on the i5-8500 |

**Preset builder options.** Engine (VAAPI, QSV or software) and codec; quality target or approximate file-size target; resolution cap; audio codec, bitrate and channel layout; audio and subtitle languages to keep; deinterlacing; crop detection; keep or discard the lossless MKV; and a copy-only (remux) mode that re-encodes nothing and only drops unwanted audio and subtitle tracks.

**Known gaps compared with HandBrake**, which the service must cover itself:

- **Interlacing.** PAL DVDs are often interlaced; detect with ffmpeg's `idet` filter and deinterlace when needed.
- **Black bars.** Run `cropdetect` on a sample and apply the result.
- **Subtitles.** DVD and Blu-ray subtitles are bitmaps. They can be copied into the MKV but not turned into text without OCR.

**Music.** CDs rip to FLAC. An MP3 preset (LAME) is built from the FLAC and offered alongside it.

**Concurrency.** Encodes run in a global pool, separate from the per-drive rip queues, so three drives ripping does not mean three simultaneous encodes. Default is 1 encode at a time; the UI can force 2 and re-order the queue. Progress is read from ffmpeg's -progress output.

## Scheduling, throttling and the shutdown guard

The service never starts a rip or encode it expects to run well past the next scheduled shutdown, and it lets the shutdown scheduler ask whether anything is running.

**Cooldown.** After a disc is inserted there is a 5-minute window to identify it and choose what is ripped. A "start now" button skips the wait, and a setting changes the default (0 minutes means immediate).

**Per-drive queues.** Each drive runs one job at a time, so a drive can never rip and burn at once. The queue lives in SQLite and survives restarts; a job found as "running" at startup is marked interrupted.

**Encode pool.** One encode at a time by default. A menu setting can force two, and queued jobs can be re-ordered.

**Jellyfin throttle.** While Jellyfin is transcoding, encodes pause (SIGSTOP) and resume afterwards. Direct-play streams do not trigger it. A toggle in the menu turns the throttle off. The check uses Jellyfin's active sessions.

**Shutdown guard.**

- The service estimates how long a job will take (disc size and drive speed for rips, source duration for encodes) and refuses to start it if it would finish too far past the next scheduled shutdown.
- How far past shutdown is acceptable is a setting.
- `GET /api/busy` reports whether jobs are running. Your existing JSON scheduler is extended to call it and postpone shutdown while busy.
- On SIGTERM the container saves job state and stops child processes cleanly. MakeMKV rips and encodes cannot resume, so an interrupted job restarts from the beginning.
- Reminder: the scheduler's JSON format and path still need to be shared before this part can be designed in detail.

## Integrations: Jellyfin, Radarr, Sonarr

Radarr and Sonarr own naming and the final library folders; the service identifies the disc, delivers the file, and tells them what it is. Check every endpoint below against the Swagger docs of your installed versions before building.

| Service | Used for | Calls |
| --- | --- | --- |
| Jellyfin | Duplicate check; detecting transcodes; library refresh | `GET /Items` filtered by TMDb ID (`AnyProviderIdEquals`) or search term; `GET /Sessions`; `POST /Library/Refresh` |
| Radarr | Identification, adding the film, import and renaming | `GET /api/v3/movie/lookup`, `GET /api/v3/movie`, `POST /api/v3/movie`, ManualImport command |
| Sonarr | Same for TV, using TVDB IDs | `GET /api/v3/series/lookup`, `POST /api/v3/series`, ManualImport command |

**Reminder: use ManualImport, not DownloadedMoviesScan.** `DownloadedMoviesScan` parses the filename and can reject files it cannot match. Because you identify the film yourself, import with Radarr's ManualImport command and the explicit movie ID. For Sonarr, pass the series ID and the episode IDs from your manual mapping screen.

**Hand-off steps for a film.**

1. Check the film is in Radarr (`GET /api/v3/movie`, matched on TMDb ID). If not, add it with search-on-add disabled so Radarr does not go looking for a download.
2. Move the finished file to `/data/downloads/Completed/disc-ripper/`.
3. Send the ManualImport command with the file path as Radarr sees it and the movie ID.
4. Radarr renames and moves the file into the chosen root folder.

**Path rule.** The path in the command must be the path inside Radarr's container. Since all containers mount the share as `/data`, one path works for everyone; confirm each container's mapping before building.

**Jellyfin refresh.** Currently manual. Two easy upgrades: enable Radarr's and Sonarr's Connect notification for Jellyfin, or add a setting that makes the service call `POST /Library/Refresh` after an import (off by default, to match today's behaviour). Music always needs the service to trigger the refresh, because Radarr and Sonarr never see it.

**Naming and extras.** The ripped file is renamed by Radarr or Sonarr, honouring your naming format. Renaming is now enabled in both. The movie folder format is {Movie Title} ({Release Year}); series and season folders are {Series Title} and Season {season}. The Standard Movie Format is {Movie Title} ({Release Year}) {Quality Full}, and the Standard Episode Format is {Series Title} - S{season:00}E{episode:00} - {Episode Title} {Quality Full}. Extras go into the film's `extras` folder by the service.

## Replacing a film or adding a second version

When the duplicate check finds a film already in Jellyfin, you choose per disc what happens. The rip still starts; only the import waits for your choice.

| Choice | What happens | How | Risk |
| --- | --- | --- | --- |
| Skip | Nothing is imported. The rip stays in Awaiting or is deleted, your pick. | None | None |
| Replace | The new rip takes the old file's place. The film keeps its Radarr entry and folder. | Radarr ManualImport with the existing movie ID; Radarr renames and deals with the old file | Destructive: the old file is recycled or deleted, depending on Radarr's recycle-bin setting |
| Add as second version | Both files stay and Jellyfin shows a version picker. | Service copies the file into the film's folder using Jellyfin's version naming; Radarr is not told | Naming must match what Jellyfin groups; Radarr tracks only one file per movie |

**Safeguards for Replace**

- Before anything is touched, show old and new side by side: resolution, codec, size, duration, audio tracks and subtitles. The old file's details come from Radarr's file record.
- Act only after the new file has finished and passed an ffprobe check: its duration must be within a set tolerance of the old file's (tolerance is a setting).
- Read Radarr's recycle-bin setting. Radarr's recycle bin is currently set, so a replaced file can be recovered. If it is ever empty, warn that the old file will be deleted permanently and ask for a typed confirmation.
- Check your Radarr version's Swagger docs for a replace or overwrite field on the ManualImport command.
- Jellyfin ties watch history and metadata to the item's path, so a replacement with a different name or extension may appear as a new item. Radarr's naming normally keeps the name the same; test on one film first.
- Only items tracked by Radarr (or Sonarr for episodes) can be replaced. Anything else has to be imported into Radarr first. Replacing an episode works the same way through Sonarr.

**Step: normalise the folder.** Replace keeps the film's existing folder, so a year-less folder such as `Elf` stays that way even though the new file is named `Elf (2003) - ...`. As agreed, Replace includes this step by default, with a toggle to turn it off. It asks Radarr to move the film to its correct folder name, `{Movie Title} ({Release Year})`, after the import, so folder and file names line up and later second versions group. Check your Radarr version's Swagger docs for the folder name its movie record carries and for moving files on update. Expect Jellyfin to treat a moved film as a new item, which resets its watch history.

**Second-version naming (movies)**

Jellyfin groups versions when the files sit in one movie folder and each extra file's name is the folder name, then `-`, then a label. For example, in a folder `Movie (2010)`: `Movie (2010) - 1080p.mkv` and `Movie (2010) - Extended Cut.mkv`.

- **Base name:** the movie folder's name exactly as it is on disk, read from the movie's path in Radarr. Many existing folders (Elf, Home) have no year, so Radarr's folder format cannot be assumed.
- **Label:** source plus cut. The source (DVD, Blu-ray, remux) is suggested from the disc type; the cut is free text and can be blank, for example "Extended Cut".
- **Risk:** if your Radarr file format adds text after the title with no `-` (a quality tag, say), Jellyfin will not group the existing file with the new one. Your current movie file format is exactly this case (see the paragraph after this list). Fallbacks: label the existing file too, or call Jellyfin's merge-versions endpoint (`POST /Videos/MergeVersions`) once both items exist; check it against your Jellyfin version.
- **Refresh:** the service always triggers a Jellyfin library refresh after adding a version, even if the global refresh setting is off, because Radarr never sees the file.
- **Radarr side effects:** do not ManualImport the second file, because Radarr would treat it as a replacement. Radarr will not rename or upgrade the extra file, and deleting the movie in Radarr with files removed deletes the whole folder, second version included.
- **TV episodes:** available as an option, off by default. Treat it as experimental: as reported in a Jellyfin discussion about 10.8, episode versions group only when each such episode sits in its own directory, which does not fit Sonarr's Season folders. Before relying on it, check Jellyfin's TV naming docs for your version first.

**Your current formats, and what to change**

Jellyfin's docs say every version must sit in the movie's folder, and each file name must start with the folder name exactly, then a space, a hyphen, a space and a label.

- **Current movie format:** `{Movie Title} ({Release Year}) {Quality Full}` gives `Movie (2010) Bluray-1080p.mkv`. There is no hyphen after the title, so it will not group with `Movie (2010) - Blu-ray Extended Cut.mkv`.
- **Agreed change:** set the Standard Movie Format to `{Movie Title} ({Release Year}) - {Quality Full}`. Radarr still reads quality from the name, and the original file becomes a labelled version that groups with the service's files. Alternative: drop the quality tag (`{Movie Title} ({Release Year})`), at the cost of Radarr no longer reading quality from the name on rescans.
- **Folders without a year:** many existing folders (Elf, Home) are named without a year, so a Radarr-named file `Elf (2003) - ...` does not start with the folder name `Elf`. Because you plan to re-rip most films, there is no bulk rename; the Replace flow can move the folder instead (see the folder step in the Replace section). Otherwise, rename those folders to Radarr's format first (Radarr's movie editor can move files to match), or group them with Jellyfin's merge-versions.
- **Eligibility check:** before adding a second version, the service checks that the existing file's name starts with the folder name followed by a space, hyphen, space. If not, it offers three fixes: rename the folder through Radarr, merge in Jellyfin, or cancel.
- **Sonarr:** its episode format already puts a hyphen after the series name. That matters only once TV second versions are switched on.

Sources (found through search; pages not opened in full): [Jellyfin docs, movies](https://jellyfin.org/docs/general/server/media/movies) and [Jellyfin discussion on TV multiple versions](https://github.com/jellyfin/jellyfin/discussions/7900).

## Burning

Burning supports every source you listed: an uploaded ISO, a file from the library, and FLAC files for audio CDs. Media targets are CD-R/RW and DVD-R/RW (and DVD+R/RW); there is no Blu-ray burning.

| Burn type | Source | Tools |
| --- | --- | --- |
| Pre-made video DVD | ISO made elsewhere (menus included) | `growisofs -dvd-compat -Z /dev/srX=file.iso`, or `xorriso -as cdrecord` |
| Authored video DVD | Video file from the library or an upload | ffmpeg (PAL DVD target, AC-3 audio) → dvdauthor → ISO → burn. Plays straight through on insert, no menus. |
| Audio CD | FLAC files | ffmpeg (44.1 kHz, 16-bit WAV) → cdrdao (CD-TEXT, gapless) |
| Data CD/DVD | Files or an ISO | xorriso |

**Why a pre-made ISO works.** A DVD-Video disc is a filesystem holding a `VIDEO_TS` folder, and menus are just files in it. An ISO made by DVDStyler or similar burns as-is.

**Rules the UI enforces.**

- Compare ISO or source size with the media's capacity (from `dvd+rw-mediainfo`) before starting, and fail early.
- RW discs are erased first. DVD-RW is formatted with `dvd+rw-format -force` for restricted-overwrite mode; DVD+RW needs nothing special.
- Offer a read-back verify with a checksum after every burn.
- A library file that does not fit a 4.7 GB disc is transcoded to PAL DVD first, and the UI shows the expected size.

**Compatibility note.** Many standalone and car players reject CD-RW audio and DVD-RW video discs. RW is fine for tests; use -R media for discs that must play everywhere.

Burn jobs go through the same per-drive queues as rips, so a drive cannot burn and rip at once.

## Security and file permissions

The container can drive your optical drives and run shell tools, so a compromised UI is close to a compromised box. Network limits alone are not enough.

- One admin password on the UI, even behind Tailscale (sessions via a signed cookie).
- Reachable only on the LAN and over Tailscale; never port-forwarded. Whether Tailscale runs on the Unraid host or as a sidecar container is still to decide.
- Devices passed with `--device`, no `--privileged`, no Docker socket mount.
- Jellyfin, Radarr and Sonarr API keys stored in config, never returned by the API or written to logs.
- Child processes run with fixed argument lists, never through a shell built from user input (disc labels and titles are untrusted text).
- Output files owned by `nobody:users` (99:100).

## Suggested build order

Each step gives something usable on its own, so you can test on real discs early.

1. **Skeleton.** Container image, FastAPI backend, SQLite in WAL mode, login, drive discovery by device ID, a basic UI shell.
2. **Detection and queues.** Disc probing, one queue per drive, progress streamed to the UI.
3. **Audio CD rip.** cyanrip, FLAC to the staging folder, approval screen, Jellyfin refresh.
4. **DVD and Blu-ray rip.** MakeMKV title scan, largest-title default, 5-minute cooldown, eject handling.
5. **Identification and import.** Radarr and Sonarr lookups, manual identify screen, Jellyfin duplicate check, ManualImport hand-off, then replace and second-version options.
6. **Encoding.** ffmpeg pool, preset builder, Jellyfin throttle, shutdown guard and `/api/busy`.
7. **Burning.** ISO burn first, then audio CD, authored DVD and data discs; RW erase and verify.
8. **Polish.** TV episode mapping, extras, optional Jellyfin refresh setting.

## Open items and reminders

These are still undecided or waiting on information from you.

- [ ] **Reminder: import method.** Use Radarr/Sonarr ManualImport with explicit IDs, not DownloadedMoviesScan (see Integrations).
- [ ] **Reminder: scheduler JSON.** Share the shutdown scheduler's JSON format and file path so the busy check and shutdown guard can be designed against it.
- [ ] **Preset defaults.** Choose default audio and subtitle languages, and whether the default is "smallest reasonable" or an exact size target.
- [ ] **Mover.** Decide whether the work area goes on a cache-only share, or jobs avoid the mover window.
- [ ] **Cache budget.** Decide how much cache the work area may use; this becomes a config setting.
- [ ] **Staging locations.** Confirm where "awaiting identification" and music staging live, and that neither is inside a Jellyfin library.
- [ ] **Duplicate hit.** Now covered by the skip, replace or second-version choice. Still to decide: what happens if you make no choice (proposed: the rip stays in Awaiting and nothing is imported).
- [ ] **Jellyfin refresh.** Choose between Radarr/Sonarr Connect notifications, a service setting, or staying manual.
- [ ] **Blu-ray drive.** Confirm it is a writer (`cat /proc/sys/dev/cdrom/info`); a writer is a third burner for CDs and DVDs.
- [ ] **Path mappings.** Confirm `/data` maps to the same share in the service, Radarr, Sonarr and Jellyfin.
- [ ] **VAAPI check.** Test hardware encoding on the new build before settling the default engine.

* [ ] **Front end.** Pick htmx (least work) or SvelteKit/React (slicker) before step 1 of the build order.

- [ ] **Hardware encoder default.** Choose VAAPI (works in Jellyfin today) or QSV (crashed the kernel before) as the default hardware engine.
- [ ] **Tailscale.** Decide between the Unraid host plugin and a sidecar container.

* [ ] **Change Radarr's movie format.** In Settings, Media Management, set the Standard Movie Format to `{Movie Title} ({Release Year}) - {Quality Full}` before the re-rip starts.
