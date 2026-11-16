# Disc Ripper – Design Plan

Nov 13, 2026 · @Joseph · updated Nov 16, 2026

## Goals and scope

One container on the Unraid server that rips and encodes discs and burns audio CDs from its own web UI, hands finished films and shows to Radarr and Sonarr, and checks whether you already own a film before a rip is wasted on it.

**In scope**

- Rip DVD and Blu-ray (including BD-R) to MKV (MakeMKV), audio CDs to FLAC (cyanrip), data discs to ISO (ddrescue)
- Encode through a preset builder page, with HandBrakeCLI or ffmpeg as the engine, in software or hardware
- Burn audio CDs from custom tracklists built from the music library; erase CD-RW first
- Identify discs automatically through Radarr, Sonarr and MusicBrainz, with a manual identify screen as fallback
- Duplicate check (Radarr first, Jellyfin as fallback) with skip, replace-existing and add-as-second-version options; hand-off into Radarr and Sonarr for naming
- Optionally keep the lossless MKV as a second version, chosen per disc
- Movies and music first; TV discs supported through a manual episode-mapping screen

**Out of scope**

- DVD, data-disc and Blu-ray burning (only audio CDs are burned)
- Guaranteed UHD Blu-ray ripping (needs a compatible drive and firmware), game discs, LaserDisc, HD DVD
- Public exposure and multiple user accounts: admin only, on the LAN and over Tailscale
- Lidarr: music stays outside the *arr apps

## Environment and hardware

The service runs as one Docker container on the new Unraid build (Unraid 7.3.5), next to Jellyfin, Radarr and Sonarr.

| Item | Detail |
| --- | --- |
| Host | New Unraid build (Gigabyte Z370 HD3, Corsair Carbide 200R case), Unraid 7.3.5 |
| CPU / iGPU | Intel i5-8500 (6 cores, no Hyper-Threading), UHD 630 used for hardware encoding |
| Optical drives | Two drives (`sr0`, `sr1`) on real SATA ports; there are only enough SATA ports for two. The design supports any number of drives, so a third can be added later. |
| CD burning | `sr0` and `sr1` both write CD-R and CD-RW, so there are at least two burners |
| Cache | 256 GB pool, about 80 GB free today; the size will change, so the budget is a setting |
| Access | LAN and Tailscale (Tailscale runs on the Unraid host), with one password |
| Power | Weekly on/off schedule kept by a Raspberry Pi; shutdown goes through unraid-backend (see the shutdown section) |
| Workload | About 50 discs to re-rip |

**Device handling.** Each drive needs its `/dev/srX` node and its matching `/dev/sgX` node (MakeMKV needs the `sg` one). Both numbers can change between boots, `sg` nodes have no `/dev/disk/by-id` links, and every SATA hard disk has an `sg` node too, so fixed `--device` entries would go stale. Instead, the template grants cgroup access to the sr and sg device types (`--device-cgroup-rule='b 11:* rmw'` and `'c 21:* rmw'`). At start-up a root entrypoint reads `/sys/class/scsi_generic/*/device/`, creates nodes only for the optical drives (owned by group PGID, mode 0660), then drops to 99:100. Drives are identified by serial (falling back to WWID, then SATA port), and per-drive settings are keyed by that ID. Do not use `--privileged`. If `/dev/sg*` is missing on the host, load the `sg` module at boot with the User Scripts plugin.

**Device permissions.** The container runs as 99:100 (`nobody:users`). The optical nodes it creates are already owned by group 100, so they need no extra group. It needs `--group-add` only for the `render` group that owns `/dev/dri/renderD128` (added in the encoding step). Set `--stop-timeout` to about 60 seconds, because Unraid's default of 10 seconds is too short for a clean shutdown of child processes.

**Disc insert detection.** There is no udev inside the container, so the service polls each drive every few seconds with the `CDROM_DRIVE_STATUS` ioctl and reacts when a drive goes from empty to "disc OK".

**GPU history.** Jellyfin was moved from QSV back to VAAPI after kernel crashes under load. VAAPI (through ffmpeg) is therefore the default hardware path. QSV, through either ffmpeg or HandBrake, is selectable but marked experimental and must be tested on this box before relying on it.

**Image build.** MakeMKV is compiled from source, so the Dockerfile uses a multi-stage build (or borrows from an existing MakeMKV image such as jlesage's). The image also needs:

- ffmpeg with VAAPI and QSV support, and the Intel media driver (iHD)
- HandBrakeCLI built with QSV support. HandBrake uses Intel's oneVPL, and on an 8th-gen chip it probably needs Intel's older Media SDK runtime; confirm during the build.
- cyanrip, ddrescue, cdrdao, dvd+rw-tools (for disc probing) and cd-discid
- `/dev/dri` passed in for the iGPU

The container's `TZ` must match the Pi's local clock, because schedule times are in Pi local time.

## Architecture

One FastAPI (Python) backend runs one job queue per drive and shells out to command-line tools, so a drive can never do two things at once.

&#91;embedded content: architecture · 3 layers\]

State lives in SQLite (WAL mode) under `/config`, and a job table doubles as the queue, so no Redis is needed. Tool progress is parsed per tool and pushed to the browser over SSE. The front end is htmx with its SSE extension. A single admin password protects the UI.

## Storage layout and path mapping

Two rules shape the layout: the cache-only work share holds only jobs in progress, and anything finished leaves it straight away. The `Barry-server` share is mounted as `/data` in the service, Radarr, Sonarr and Jellyfin, so every path under `/data` means the same thing to all of them.

| Purpose | Container path | Notes |
| --- | --- | --- |
| Config and SQLite DB | `/config` → `/mnt/cache/appdata/disc-ripper` | Cache only. SQLite in WAL mode. |
| Work area (in-progress rips and encodes) | `/work/<jobId>/` → new share `disc-ripper-work` | Cache only, set up as an Unraid exclusive share (no FUSE layer). The mover never touches it. Mounted only in this container. Size limited by the cache budget. |
| Finished output | `/data/downloads/disc-ripper/<jobId>/` | On `Barry-server` (cache, then mover). Holds encodes, kept lossless copies, FLAC awaiting approval and ISOs while they wait for identification, a duplicate decision or import. ManualImport reads from here. Outside every Jellyfin library path. |
| Movies | `/data/MEDIA/All Movies/Movies` and `/data/MEDIA/All Movies/Other movies` | Radarr root folders |
| TV | `/data/MEDIA/All TV Shows/Tv_shows` and `/data/MEDIA/All TV Shows/Other Tv` | Sonarr root folders |
| Music | `/data/MEDIA/Music/<Artist>/<Album>` | Moved here by the service after you approve the tags |
| Data-disc ISOs | `/data/ISOs` (a setting) | Created by the service if missing |

**What leaves the work area, and when.**

- **Encode:** once it passes the ffprobe check, it moves to `/data/downloads/disc-ripper/<jobId>/`.
- **Lossless MKV, "keep lossless" ticked:** moves out with the encode, and later becomes a second version.
- **Lossless MKV, not kept (the usual case):** stays in `/work` until the encode is confirmed in Jellyfin, then it is deleted. If the job is still waiting for identification or a duplicate decision 24 hours (a setting) after the encode passed its check, the raw file is deleted anyway. The encode is safe, and the disc itself is the backup.
- **FLAC and ISO:** move out as soon as the rip finishes.

Moving from `/work` to `/data` crosses two mounts, so it is a copy and then a delete, not a rename. Both are on the cache SSD, so an encode moves in seconds and a kept lossless file in a few minutes.

**Cache budget.** Two settings: the work-area budget (default 60 GB) and a minimum free space on the cache pool (default 20 GB, read from the pool itself). Before a rip starts, the size MakeMKV reports for the selected titles must fit both. If it doesn't, the disc waits in its drive as "waiting for cache space" and starts when encodes finish and raw files are deleted. The free-space floor matters because `Barry-server` writes also land on the cache until the mover runs.

**Mover schedule.** The mover runs daily at 21:00, inside the usual power-on window. On a day when the server isn't on at 21:00 the mover doesn't run, so finished files stay on the cache until the next run. The free-space floor covers this. The mover never touches the work share, so its 21:00 run can't disturb a running job.

**Ownership.** Files are written as `nobody:users` (99:100) so Radarr, Sonarr and Jellyfin can read, move and rename them.

**Existing ARM output.** `MEDIA/unidentified` holds leftovers from Automatic Ripping Machine. ARM must not run against the same drives at the same time as this service.

## Disc lifecycle

There is no fixed cooldown. A disc starts ripping as soon as it is probed and scanned, unless there is a decision to make first.

1. **Insert.** The poll sees a new disc; the service probes the type (audio, DVD-Video, Blu-ray, data).
2. **Scan.** `makemkvcon info` lists the titles (for video discs), and the volume label and Blu-ray title metadata are read.
3. **Identify.** The service searches Radarr, Sonarr or MusicBrainz for a match (see Identification).
4. **Duplicate check.** If there is a confident match, the service checks whether you already own it.
5. **Decide: start or hold.** A disc is held, with a notice in the UI, when any of these is true:
   - a duplicate was found (you choose skip, replace or second version)
   - the main title is unclear: several titles are within a few percent of the longest one, as on Blu-rays with obfuscated playlists, or the runtime Radarr expects points to a different title than the largest
   - it looks like a TV disc (a Sonarr match, or many titles of similar length)
   - you pressed "Hold" on that drive, or turned off the "Auto-start rips" setting

   Otherwise the main title starts ripping immediately.
6. **Rip.** While it rips, you can tick extra titles; they queue on the same drive after the main title. You can also tick "keep lossless" for this disc.
7. **Eject.** The drive ejects when its queue for that disc is empty. A "force eject now" button is always available.
8. **Encode, identify if still needed, import, clean up** (see the sections below).

If identification only succeeds after the rip has started (for example after a manual identify), the duplicate check runs then. The rip finishes and only the import waits for your choice.

## Identification

The service guesses automatically. When it cannot, the disc lands in an "Awaiting identification" list with a screen modelled on Jellyfin's Identify form. Ripping does not wait for identification unless the disc is held for one of the reasons above.

1. **At insert**, read the volume label (and the disc's own title metadata on Blu-ray) and clean it up: underscores, `DISC_1`, region codes.
2. **Movies and TV**: search Radarr (`/api/v3/movie/lookup`) and Sonarr (`/api/v3/series/lookup`), so no separate TMDb key is needed. Rank candidates by title similarity and by how close the main title's runtime is to the candidate's runtime. The same runtime match is used to pick the main title on the disc.
3. **Audio CDs**: cyanrip looks the disc up on MusicBrainz and tags the files.
4. **Confident match**: preselect it, but still show it for confirmation. **No match**: queue it for manual identification.
5. **Duplicate check**: as soon as a confident guess exists:
   - **Films and episodes:** ask Radarr (or Sonarr) whether the item exists and has a file, matched on TMDb or TVDB ID. Radarr's file record also supplies the details for the Replace comparison.
   - **Anything Radarr or Sonarr don't track:** fall back to Jellyfin.
   - **Albums:** ask Jellyfin by MusicBrainz release ID, and check whether the `Music/<Artist>/<Album>` folder exists.

**Manual identify screen (films and TV).** Fields: Name, Year, IMDb ID, plus TMDb ID for movies or TVDB ID for TV. Results show poster, year and runtime so the right candidate is easy to pick. A destination picker chooses the Radarr or Sonarr root folder (Movies, Other movies, Tv\_shows, Other Tv).

**Manual identify screen (audio CDs).** Paste a MusicBrainz release URL, or type album, artist, year and track titles by hand.

**Multi-disc albums.** One album folder. The disc number goes in the tags and in the file names (`1-01 Title.flac`, `2-01 Title.flac`).

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

**Title selection.** For movies, the main title is the one whose runtime best matches the Radarr candidate. If there is no candidate, the largest title is used. If that choice is unclear, the disc is held (see Disc lifecycle).

**Extras.** Not ripped by default. When ticked, they are kept in the film's `extras` folder for Jellyfin, which Radarr's import does not do for you. Radarr should ignore an `extras` subfolder when it scans; confirm on your version.

**Auto detection order.** Audio tracks present: audio CD. `VIDEO_TS` present: DVD-Video. `BDMV` present: Blu-ray. Anything else with a filesystem: data disc. You can override the guess in the UI.

**MakeMKV licence.** It needs the beta key (changes periodically) or a purchased licence, stored in config, not baked into the image.

## Encoding and the preset builder

All encodes are driven by presets you build on a preset-builder page. Each preset picks an engine (HandBrakeCLI or ffmpeg) and a mode (software or hardware). The default goal is small files with good quality. The lossless MKV is kept only when you tick "keep lossless" for that disc.

| Engine | Software | Hardware | Notes |
| --- | --- | --- | --- |
| HandBrakeCLI | x265, x264 | QSV (experimental) | Built-in deinterlacing (decomb), detelecine, auto-crop and a foreign-audio subtitle scan. HandBrake has no VAAPI on Linux, so its only Intel hardware option is QSV. |
| ffmpeg | libx265, libx264 | VAAPI (`hevc_vaapi`, `h264_vaapi`), QSV (experimental) | VAAPI is the default hardware path. Deinterlacing and cropping are built by the service (see below). |

**Default presets** (change them on the preset page):

- **Standard (software):** HandBrakeCLI, x265, preset slow, RF 20 for Blu-ray and RF 18 for DVD. On the i5-8500 this is roughly 3 to 4 hours per 1080p film, which is fine for about 50 discs.
- **Fast (hardware):** ffmpeg, `hevc_vaapi`, constant quality. The quality value is set after testing on the real box, because VAAPI's numbers are not comparable to x265's RF.
- **Copy only (remux):** re-encodes nothing; only drops unwanted audio and subtitle tracks.

**Default audio and subtitles.**

- **Audio:** keep English and the film's original language. The main track is converted to E-AC-3 5.1 at 640 kb/s; commentary tracks are dropped.
- **Subtitles:** keep English and every forced track. Image subtitles (PGS, VobSub) are copied as they are. On some clients they make Jellyfin transcode to burn them in, which is accepted.
- **Lossless copy:** when kept, it keeps every track untouched.

**Preset builder options.** Engine and mode; codec; quality value (RF, CRF or QP); resolution cap; audio codec, bitrate and channel layout; audio and subtitle languages to keep; deinterlacing; crop detection; and the copy-only mode. There are no file-size targets and no two-pass. Options an engine does not support are greyed out, and the page shows the exact command each preset produces.

**Estimates.** The preset builder shows an estimated file size and encode time per preset. Estimates come from two sources:

- **History:** the average megabytes per minute of source, and frames per second, for past encodes with that preset, split by DVD and Blu-ray.
- **Test on a sample:** encodes three 30-second clips from a ripped file you choose, and extrapolates.

Until a preset has history, it uses a conservative default speed (a setting) and the estimate is marked as rough. The same estimate is shown on each queued encode and used by the shutdown guard.

**What the ffmpeg path must cover itself** (HandBrake does these already):

- **Interlacing.** PAL DVDs are often interlaced; detect with ffmpeg's `idet` filter and deinterlace (`bwdif`) when needed.
- **Black bars.** Off by default. When on, run `cropdetect` at several points through the film and use the largest box, so films whose aspect ratio changes are not over-cropped.

**Music.** CDs rip to FLAC only. No MP3 copies are made; Jellyfin transcodes audio when a client needs it.

**Concurrency and priority.** Encodes run in a global pool, separate from the per-drive rip queues. One encode at a time by default; a setting allows two (useful only to pair one hardware with one software encode), and queued encodes can be re-ordered. Every encode runs at `nice 19` and `ionice` idle, so Jellyfin and the rest of the server always come first. There is no pause-while-Jellyfin-transcodes throttle. Progress comes from ffmpeg's `-progress` output and HandBrakeCLI's `--json` output.

## Scheduling and the shutdown guard

### How the server shuts down today

The Raspberry Pi keeps the weekly schedule. At the end of each day's window it sends `POST /power/shutdown-request` to unraid-backend. unraid-backend waits until no players are online, then writes the poweroff flag. A pending request expires after 12 hours. Full details are in `schedule info.md`.

### What changes

The ripper delays shutdown for at most 3 hours past the scheduled end. If its work finishes before then, the server shuts down as soon as it is idle.

**Reading the schedule.** The service reads the schedule from unraid-backend's `/status` (the `schedule` field), and falls back to the Pi's `GET /schedule` on port 8090. It refreshes every 5 minutes and applies the same rules as `schedule_util.is_scheduled_online`. That includes `endNextDay` and overnight windows that end under yesterday's entry. A day with blank times has no scheduled end. If no schedule can be read, the guard is off and the UI shows a warning.

**The grace period.** A setting, default 3 hours past the scheduled end. The ripper is the single owner of this number.

**Before the scheduled end:**

- An encode starts only if its estimate says it will finish before the scheduled end plus the grace period. Otherwise it waits for the next session.
- Rips are never refused, because you are there. If a rip is expected to run past the end of the grace period, the UI warns you.

**After the scheduled end (draining):**

- No new encodes start.
- Running jobs carry on.
- Rips you start still run, with the same warning.

**`GET /api/busy`.** It returns whether any job is running, plus each job's estimated finish. It is protected by a shared secret header, like `X-Shutdown-Secret`. Once the grace period has passed, it reports not busy even if a job is still running, so shutdown goes ahead.

**Change to unraid-backend.** When a shutdown request arrives, unraid-backend also polls `/api/busy` (every minute, with a short timeout). It writes the poweroff flag only when no players are online *and* the ripper reports not busy. If the ripper can't be reached, it counts as not busy. As a safety net, unraid-backend ignores the ripper 4 hours after the request arrives, in case the ripper misbehaves. This cap must stay longer than the ripper's grace period, so raise it if you ever raise the grace setting.

**Players.** The players rule is unchanged: unraid-backend waits until no players are online, for up to the request's 12-hour expiry. The 3-hour limit applies only to the ripper. If players keep the server on past the grace period, the ripper starts nothing new. Any job still running can be cut off when the last player leaves, and it restarts at the next boot.

**On SIGTERM** (container stop during shutdown), the service stops child processes cleanly and marks their jobs as interrupted. MakeMKV rips and encodes cannot resume, so interrupted jobs restart from the beginning:

- **Encodes:** re-queued automatically at the next boot.
- **Rips:** retried automatically if the same disc (same disc ID) is still in the drive; otherwise they show a "retry" button.

**Per-drive queues.** Each drive runs one job at a time, so a drive can never rip and burn at once. The queue lives in SQLite and survives restarts.

## Integrations: Jellyfin, Radarr, Sonarr

Radarr and Sonarr own naming and the final library folders; the service identifies the disc, delivers the file, and tells them what it is. Check every endpoint below against the Swagger docs of your installed versions before building.

| Service | Used for | Calls |
| --- | --- | --- |
| Radarr | Identification, duplicate check, adding the film, import and renaming, unmonitoring | `GET /api/v3/movie/lookup`, `GET /api/v3/movie`, `POST /api/v3/movie`, `PUT /api/v3/movie/{id}`, ManualImport command |
| Sonarr | Same for TV, using TVDB IDs | `GET /api/v3/series/lookup`, `GET /api/v3/series`, `POST /api/v3/series`, ManualImport command |
| Jellyfin | Fallback duplicate check, music duplicate check, confirming an import arrived, refresh after music and second versions | `GET /Items` (by provider ID or search term; check which filters your version supports), `POST /Library/Refresh` |

**Use ManualImport, not DownloadedMoviesScan.** `DownloadedMoviesScan` parses the filename and can reject files it cannot match. Because you identify the film yourself, import with Radarr's ManualImport command, the explicit movie ID and an explicit quality (DVD, Bluray-1080p, Remux-1080p and so on, from the disc type and preset). For Sonarr, pass the series ID and the episode IDs from your manual mapping screen.

**Hand-off steps for a film.**

1. Check the film is in Radarr (`GET /api/v3/movie`, matched on TMDb ID). If not, add it unmonitored, with search-on-add disabled, so Radarr does not go looking for a download.
2. Send the ManualImport command with the file's path under `/data/downloads/disc-ripper/<jobId>/` and the movie ID.
3. Radarr renames and moves the file into the chosen root folder.
4. Set the film to unmonitored in Radarr. This stops Radarr (through qBittorrent) from grabbing a download to "upgrade" your rip, for example because the quality profile's cutoff is higher or a custom format scores x265 low.
5. Wait until Jellyfin shows the new file, then delete the raw rip from `/work`, unless "keep lossless" was ticked (then add it as a second version).

**Path rule.** The path in the command must be the path inside Radarr's container. Since all containers mount the share as `/data`, one path works for everyone; confirm each container's mapping before building.

**Jellyfin refresh.** Turn on Radarr's and Sonarr's Connect notification for Jellyfin, so imports refresh the library with no code here. The service calls `POST /Library/Refresh` itself only for music and second versions, because Radarr and Sonarr never see those files.

**Naming and extras.** Radarr or Sonarr renames the ripped file, honouring your naming format. Renaming is enabled in both. The movie folder format is {Movie Title} ({Release Year}); series and season folders are {Series Title} and Season {season}. The Standard Movie Format changes to {Movie Title} ({Release Year}) - {Quality Full} (see second versions). The Standard Episode Format is {Series Title} - S{season:00}E{episode:00} - {Episode Title} {Quality Full}. The service puts extras into the film's `extras` folder.

## Replacing a film or adding a second version

Second versions come up in two ways: the duplicate check finds a film you already own and you choose "add as second version", or you ticked "keep lossless" for a disc. Both use the same naming code. When the duplicate check finds a film, the disc is held until you choose.

| Choice | What happens | How | Risk |
| --- | --- | --- | --- |
| Skip | Nothing is ripped; the disc ejects. (If the duplicate was found only after the rip, the output is deleted or stays in Awaiting, your pick.) | None | None |
| Replace | The new rip takes the old file's place. The film keeps its Radarr entry and folder. | Radarr ManualImport with the existing movie ID; Radarr renames and deals with the old file | Destructive: the old file is recycled or deleted, depending on Radarr's recycle-bin setting |
| Add as second version | Both files stay and Jellyfin shows a version picker. | Service copies the file into the film's folder using Jellyfin's version naming; Radarr is not told | Naming must match what Jellyfin groups; Radarr tracks only one file per movie |

**Keep lossless.** The encode is imported through Radarr as the main file, which Radarr tracks. The lossless MKV is then added beside it as a second version labelled with its source, for example `Movie (2010) - Blu-ray Remux.mkv`. Movies only; TV episodes never keep a lossless copy.

**Safeguards for Replace**

- Before anything is touched, show old and new side by side: resolution, codec, size, duration, audio tracks and subtitles. The old file's details come from Radarr's file record.
- Act only after the new file has finished and passed an ffprobe check: its duration must be within a set tolerance of the old file's (tolerance is a setting).
- Read Radarr's recycle-bin setting. Radarr's recycle bin is currently set, so a replaced file can be recovered. If it is ever empty, warn that the old file will be deleted permanently and ask for a typed confirmation.
- Check your Radarr version's Swagger docs for a replace or overwrite field on the ManualImport command.
- Watch history: Jellyfin ties an item to its path, so a replacement with a different name or extension may appear as a new item. Jellyfin probably also keys watched state on the TMDb and IMDb IDs, which would carry it across. Test on one film first.
- Only items tracked by Radarr (or Sonarr for episodes) can be replaced. Anything else has to be imported into Radarr first. Replacing an episode works the same way through Sonarr.

**Step: normalise the folder.** Replace keeps the film's existing folder, so a year-less folder such as `Elf` stays that way even though the new file is named `Elf (2003) - ...`. Replace therefore includes this step by default, with a toggle to turn it off. After the import, it asks Radarr to move the film to its correct folder name, `{Movie Title} ({Release Year})`, so folder and file names line up and later second versions group. Check your Radarr version's Swagger docs for the folder name its movie record carries and for moving files on update. The same watch-history caveat applies.

**Second-version naming (movies)**

Jellyfin groups versions when the files sit in one movie folder and each file's name is the folder name, then ` - `, then a label. For example, in a folder `Movie (2010)`: `Movie (2010) - Bluray-1080p.mkv` and `Movie (2010) - Blu-ray Remux.mkv`.

- **Base name:** the movie folder's name exactly as it is on disk, read from the movie's path in Radarr. Many existing folders (Elf, Home) have no year, so Radarr's folder format cannot be assumed.
- **Label:** source plus cut. The source (DVD, Blu-ray, Remux) is suggested from the disc type; the cut is free text and can be blank, for example "Extended Cut".
- **Format change (done):** Radarr's Standard Movie Format is now `{Movie Title} ({Release Year}) - {Quality Full}`. The old format, `{Movie Title} ({Release Year}) {Quality Full}`, has no hyphen after the title, so Jellyfin would not group its files with a second version. Radarr still reads quality from the name with the new format.
- **Eligibility check:** before adding a second version, the service checks that the existing file's name starts with the folder name followed by a space, hyphen, space. If not, it offers three fixes: rename the folder through Radarr, merge in Jellyfin (`POST /Videos/MergeVersions`, check it against your version), or cancel.
- **Refresh:** the service always triggers a Jellyfin library refresh after adding a version, because Radarr never sees the file.
- **Default version:** check which version Jellyfin plays by default when a film has an encode and a lossless copy. The encode should be the default, so clients don't transcode the large file.
- **Radarr side effects:** do not ManualImport the second file, because Radarr would treat it as a replacement. Radarr will not rename or upgrade the extra file, and deleting the movie in Radarr with files removed deletes the whole folder, second version included.
- **TV episodes:** second versions are available as an option, off by default and experimental. As reported in a Jellyfin discussion about 10.8, episode versions group only when each such episode sits in its own directory, which does not fit Sonarr's Season folders. Check Jellyfin's TV naming docs for your version before relying on it.

Sources (found through search; pages not opened in full): [Jellyfin docs, movies](https://jellyfin.org/docs/general/server/media/movies) and [Jellyfin discussion on TV multiple versions](https://github.com/jellyfin/jellyfin/discussions/7900).

## Burning audio CDs

Only audio CDs are burned, mostly from custom tracklists for the car or a CD player. Media targets are CD-R and CD-RW.

**Tracklist builder.**

- Search and browse `/data/MEDIA/Music` by artist, album and track, and add tracks from any album.
- Drag to re-order. A running total shows against the disc's capacity, read from the blank disc (74 or 80 minutes). The page blocks burning when the list does not fit.
- Tracklists are saved, so a CD can be burned again later.

**Burn settings.**

- **Gaps:** 2 seconds between tracks by default; no gap between tracks that come one after another on the same album, so gapless albums stay gapless.
- **Volume levelling:** applies each track's ReplayGain value, so tracks from different albums play at a similar volume. On by default for tracklists; the tracks need ReplayGain tags or the service calculates them.
- **CD-TEXT:** artist and title per track, from the tags, for players that show it.

**Pipeline.** ffmpeg decodes each track to 44.1 kHz, 16-bit WAV (applying the volume levelling), then cdrdao writes the disc in disc-at-once mode from a generated TOC file with CD-TEXT.

**Rules the UI enforces.**

- A CD-RW is blanked first (`cdrdao blank`).
- Only drives that report they can write CD-R are offered as burners.
- There is no read-back verify: comparing audio CDs is unreliable because of drive read offsets, and a test play does the job.

**Compatibility note.** Many car and standalone players reject CD-RW. Use CD-R for discs that must play everywhere; CD-RW is fine for tests.

Burn jobs go through the same per-drive queues as rips, so a drive cannot burn and rip at once.

## Security and file permissions

The container can drive your optical drives and run shell tools, so a compromised UI is close to a compromised box. Network limits alone are not enough.

- One admin password on the UI, even behind Tailscale (sessions via a signed cookie).
- Reachable only on the LAN and over Tailscale (host plugin); never port-forwarded.
- `/api/busy` uses its own shared secret, separate from the admin password.
- Devices passed with `--device`, no `--privileged`, no Docker socket mount.
- Jellyfin, Radarr and Sonarr API keys stored in config, never returned by the API or written to logs.
- Child processes run with fixed argument lists, never through a shell built from user input (disc labels, titles and music tags are untrusted text).
- Output files owned by `nobody:users` (99:100).

## Suggested build order

Each step gives something usable on its own, so you can test on real discs early.

1. **Skeleton.** Container image, FastAPI backend, SQLite in WAL mode, login, settings page, drive discovery by serial, a basic htmx UI shell.
2. **Detection, queues and the shutdown guard.** Insert polling, disc probing, one queue per drive, progress over SSE, cache budget checks, schedule reading, `/api/busy`, and the unraid-backend change. This comes early because every later step creates jobs that a shutdown could interrupt.
3. **Audio CD rip.** cyanrip, FLAC to the finished-output folder, approval screen, manual identify for CDs, Jellyfin refresh.
4. **DVD and Blu-ray rip.** MakeMKV title scan, main-title choice, auto-start and hold rules, eject handling.
5. **Identification and import.** Radarr and Sonarr lookups, manual identify screen, Radarr-first duplicate check, ManualImport hand-off, unmonitoring, raw-file clean-up after Jellyfin confirms.
6. **Encoding.** Both engines, the preset builder with estimates and sample tests, encode pool, low-priority scheduling, encode estimates in the shutdown guard.
7. **Replace and second versions.** Replace safeguards, folder normalisation, second-version naming, keep lossless.
8. **Audio CD burning.** Tracklist builder, volume levelling, CD-TEXT, CD-RW blanking.
9. **Polish.** TV episode mapping, extras.

## Open items and reminders

**Waiting on you**

- [x] **Optical drives.** Two drives, `sr0` and `sr1`; only two SATA ports are free.
- [x] **Radarr's movie format** changed to `{Movie Title} ({Release Year}) - {Quality Full}`.

**Defaults chosen in this plan; change any that are wrong**

- [ ] **HandBrake and ffmpeg.** Read as "each preset chooses either engine, in software or hardware". HandBrake's hardware path is QSV only.
- [ ] **Audio and subtitles.** English plus original-language audio, main track to E-AC-3 5.1 at 640 kb/s, commentary dropped, English and forced subtitles kept.
- [ ] **Quality.** x265 preset slow at RF 20 (Blu-ray) and RF 18 (DVD), roughly 4 to 8 GB per 1080p film.
- [ ] **Unmonitor after import**, so Radarr never replaces a rip with a download.
- [ ] **Raw-file clean-up.** Deleted once Jellyfin has the encode, or 24 hours after the encode passed its check if the job is still waiting.
- [ ] **Cache budget.** 60 GB for the work area and at least 20 GB kept free on the pool.

**To test on the real box**

- [ ] **VAAPI.** Hardware encoding on the new build, and the quality value for the Fast preset.
- [ ] **HandBrake QSV on the UHD 630,** treated as experimental after the earlier kernel crashes.
- [ ] **Path mappings.** Confirm `/data` maps to the same share in the service, Radarr, Sonarr and Jellyfin.
- [ ] **Jellyfin.** Which version it plays by default; whether watch history survives a Replace; which `/Items` filters your version supports.
- [ ] **Radarr Swagger.** ManualImport fields (quality, replace or overwrite), and moving a film's folder on update.
