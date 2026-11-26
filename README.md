# super-ripper

A single Docker container for Unraid that rips, encodes and burns discs from its own web UI. The design is in [Disc Ripper – Design Plan.md](Disc%20Ripper%20%E2%80%93%20Design%20Plan.md).

Current state: **steps 4 and 6 are built** (DVD and Blu-ray ripping, and encoding), on top of step 3 (audio CD ripping) and step 2 (detection, queues and the shutdown guard). **Step 5 (identification and import into the library) is built, and awaits testing on the server.**
- **Dashboard** (the home page): shows each enabled drive live, as empty, tray open or disc in, with the disc type and label. It has Eject and Close tray buttons, and lets you queue a *test job*: a pretend job with a chosen length and size that exercises the queue, progress and cache-space check.
- **Drives page:** labels and enables drives.
- **Audio CDs:**
  - Insert a CD and it rips to FLAC (cyanrip), checked against AccurateRip, identified on MusicBrainz, tagged, and given its cover.
  - A clean rip files itself into `/data/MEDIA/Music/<Album Artist>/<Album> (<Year>)/`.
  - Anything that needs a decision waits on the **Music** page: no match, several possible releases, unverified tracks, or an album you already have.
- **Shutdown guard:** reads the Pi's power schedule and tells unraid-backend, through `/api/busy`, to wait while a job runs, for at most the grace period after the scheduled end.
- **DVDs and Blu-rays:** insert a disc and it is scanned with MakeMKV, then the main title is ripped to MKV into the downloads folder and the disc ejects. Encoding and importing the film into the library are described below.

Data-disc ripping comes in a later step.

## Install on Unraid

1. **Make the image pullable.** The GitHub workflow pushes `ghcr.io/barrydamonke/super-ripper:latest` on every push to `main`. GitHub makes new packages private, so either:
   - set the package to public (GitHub → your profile → Packages → super-ripper → Package settings → Change visibility), or
   - log Unraid in to ghcr.io with a personal access token that has `read:packages`: `docker login ghcr.io -u <github user>`.
2. **Create the work share.** Shares → Add Share → name `disc-ripper-work`, Primary storage **Cache**, Secondary storage **None**. With no secondary storage, Unraid 7 makes it an exclusive share (no FUSE layer), and the mover never touches it.
3. **Add the container.** Copy `unraid/disc-ripper.xml` to `/boot/config/plugins/dockerMan/templates-user/my-disc-ripper.xml` on the flash drive, then Docker → Add Container → pick `disc-ripper` from the template list. Check the paths and `TZ`, then Apply.
4. **First start.** Open `http://<server>:8580`, set the admin password, log in, and open Drives. Both drives should show their model, an ID starting with `serial:` (or a `wwid:`/`port:` fallback), and node status `ok`.
5. **Check the GPU.** The template passes `/dev/dri` through (`--device=/dev/dri`) for the Fast (hardware) encoding preset. Check it as the app user, not root (root can open a node the app can't): on the server run `docker exec disc-ripper setpriv --reuid=99 --regid=100 --groups=$(docker exec disc-ripper stat -c %g /dev/dri/renderD128) vainfo`. It must list `VAProfileHEVCMain : VAEntrypointEncSlice`. If it doesn't, Fast won't work. If the container log (`docker logs disc-ripper`) says `render node is root-only`, the host's `/dev/dri` permissions need fixing. `--device=/dev/dri` also means the container won't start on a host that has no `/dev/dri`; remove that line from the template there and use Standard or Copy only.

### How the drives get into the container

The template has no `--device` lines. Instead it grants access to the sr (block 11) and sg (char 21) device types with `--device-cgroup-rule`. At start-up the entrypoint (running as root) reads `/sys/class/scsi_generic`, creates `/dev/srX` and `/dev/sgX` only for the optical drives it finds (group `PGID`, mode 0660), then drops to `PUID:PGID`. Renumbered `sg` nodes after adding disks therefore need no template change; just restart the container. The container log lists each drive and node at start-up (`devnodes: ...`).

If a drive shows "Not passed into the container", check that the two `--device-cgroup-rule` entries are in Extra Parameters, and that `/dev/sg*` exists on the host (`ls /dev/sg*`; if not, load the `sg` module at boot with the User Scripts plugin: `modprobe sg`).

### Shutdown guard setup

1. **Template.** In the container template, set **Busy secret** (`BUSY_SECRET`) to a long random string. While it is blank, `/api/busy` answers 503.
2. **Schedule URLs.** In the ripper's Settings, set **Schedule source** to unraid-backend's status URL, `http://<unraid-ip>:3001/status`. Set **Schedule fallback** to the Pi's `http://<pi-ip>:8090/schedule`. The dashboard's schedule panel shows which one it read and when.
3. **unraid-backend.** Set `RIPPER_BUSY_URL=http://<unraid-ip>:8580/api/busy` and `RIPPER_BUSY_SECRET=<the same secret>` in its `.env`, then restart it. This needs the barry-mc-network change that adds the ripper busy check.
4. **Grace period.** Set in Settings (default 3 h, maximum 3.5 h). unraid-backend stops waiting for the ripper 4 hours after a shutdown request, whatever the ripper says.

Check the guard by hand: `curl -H "X-Shutdown-Secret: <secret>" http://<unraid-ip>:8580/api/busy`. It should report `"busy": false` when nothing is running, and `true` while a test job runs.

### Testing step 2 on the server

1. Insert and eject a CD, a DVD and a Blu-ray in each drive. Each card should update within about 5 seconds and show the right type and label.
2. Queue two test jobs on one drive and one on the other. The two drives should run at the same time, and each drive's queue should run in order.
3. Restart the container while a test job runs. The job should come back as "interrupted" with a Retry button.
4. Queue a test job whose size is bigger than the work-area budget. It should wait with a "Waiting for cache space" message.
5. On a real evening, start a test job that runs past the scheduled end. unraid-backend should hold the shutdown until the job finishes or the grace period ends.

### Audio CD setup

1. **Read offsets.** These are on the Drives page, pre-filled for an LG BH10LS38 (+667) and an iHAS124 A–F (+6), the AccurateRip database values. For any other drive, look its model up at accuraterip.com/driveoffsets.htm, or run `cyanrip -f` with a disc that AccurateRip knows. With a wrong offset, every track shows as "not verified".
2. **Jellyfin.** In Settings, set the **Jellyfin address** (e.g. `http://<unraid-ip>:8096`) and an **API key** from Jellyfin's Dashboard → API Keys. They're used for the "already in the library?" check and to refresh the library after filing. The key is never shown again once saved. Both are optional: without them the duplicate check uses only the ripper's own records.
3. **CD settings.** Settings has:
   - **rip mode:** secure, or fast for clean discs;
   - **retries** per bad sector;
   - **approval:** "only check the odd ones" files clean rips automatically; "always ask" sends every album to the Music page;
   - **cover art.**
4. **Where files go.** CD rips are made in `/work/<job>/`; DVD and Blu-ray rips are written straight into `/data/downloads/disc-ripper/<rip>/`. Both wait there in downloads. They're filed into `/data/MEDIA/Music`. Set `MUSIC_DIR` or `DOWNLOADS_DIR` on the container to change those two.

Library layout:

```
Music/<Album Artist>/<Album> (<Year>)/01 - <Title>.flac
Music/<Album Artist>/<Album> (<Year>)/1-01 - <Title>.flac          (multi-disc releases)
Music/Various Artists/<Album> (<Year>)/01 - <Artist> - <Title>.flac (compilations)
```

### Testing step 3 on the server

1. **A well-known CD.** It rips with "Track N of M" on its card, ejects, and files itself with `cover.jpg`. Jellyfin shows it after a refresh, about a minute later.
2. **An obscure or scratched CD.** It goes to Music → Needs a look. Fix it with a release, a MusicBrainz link, or by typing the details in.
3. **A 2-CD set.** Rip disc 1, then disc 2: both land in one folder.
4. **Re-insert a filed CD.** The card says "Already ripped" and offers Rip again.
5. **Cancel a rip midway** (the job shows cancelled), and **restart the container mid-rip** (it shows interrupted). Retry works with the same disc still in.
6. **Send me the first real rip's output.** I need it to confirm the progress and AccurateRip parsing against cyanrip's real output; the test copies are reconstructed from its source code.
   - The log: `docker exec disc-ripper sh -c 'cat /data/downloads/disc-ripper/*/rip.log'`, or the album's waiting folder.
   - The container log lines from that rip.

### DVD and Blu-ray setup

Settings has a **DVD and Blu-ray** group:
- **MakeMKV key:** paste the current beta key from the MakeMKV forum (it changes every month or two) or a purchased key. It is never shown again once saved.
- **Shortest title to list:** titles shorter than this (default 120 s) are ignored in the scan.
- **Track selection:** MakeMKV's default selection string (`app_DefaultSelectionString`). The default keeps every audio and subtitle track in every language, so a preset can pick any of them later. An upgrade changes the old English-only default to this one; a selection you edited by hand is left alone. Change it only if you know the MakeMKV syntax.

What happens on insert: the disc is scanned and the **longest title** is taken as the main one. It rips straight away and the disc ejects when done, unless the disc is **held** for you to choose. A disc is held when:
- the main title is unclear, or the disc looks like TV (several titles of similar length). A **DVD** held only because two titles are about the same length (usually two cuts of the film) waits 5 minutes, then rips the longest one. The card shows the time. Pick a title, press **Hold** (the card shows the button during the wait) or eject before then to stop it. A container restart during the wait leaves the disc held. A Blu-ray stays held until you choose, because decoy playlists are common there;
- you pressed **Hold** during the scan, or while the main-title rip was still queued;
- **auto-start** is off in Settings;
- it was already ripped (the card says "Already ripped" and offers Rip again), or its film is already in the library (the card says "Already in the library (Film)" and offers Rip again, even after the rip folder was cleaned up); a rip you skipped counts as already ripped;
- it was already in the drive when the container started.

A held card lists the titles; pick one and press **Rip**. While a rip runs you can tick **extras** on the card; they are ripped after the main title, and the eject waits for them. Cancelling an unwanted extra still lets the disc eject; a failed title keeps the disc in for Retry. **Eject** also cancels anything still queued for that disc, as does taking the disc out.

Where rips land: `/data/downloads/disc-ripper/<rip_id>/`, with extras in `extras/`. MakeMKV writes each title into a hidden `.partial/<job>/` folder inside the rip's folder, and the finished file is renamed into place. A rename needs no extra space, so a 40 GB Blu-ray needs 40 GB on the cache, not twice that. A failed or cancelled title's partial file is removed, and leftover `.partial` folders (from a crash, say) are swept at start-up and every hour, except a finished title that Move again still needs. A rip is checked against the free space of the downloads disk (plus the minimum free space setting), not the work-area budget, which covers CD rips and encodes only. A rip waiting for room says "Waiting for space on the downloads disk". Pressing Retry twice queues the title once. Files keep MakeMKV's own names and are never overwritten. Once a film is imported and Jellyfin shows it, its rip folder is cleaned up (see Importing films); otherwise delete a rip's folder to discard it. If a rename isn't possible, the file is copied instead. If that copy fails (for example the cache is short of space), the rip still counts as done and the disc ejects. The file stays in `.partial/<job>/`, and the drive's card keeps the message and a **Move again** button, with or without a disc in. If the file is gone by then, Move again says there is nothing left to move.

When the beta key or version expires, rips fail with an "Update MakeMKV" message. Paste the new beta key in Settings, or rebuild the image with a newer version (below).

- **Stop a silent MakeMKV after** (default 10 minutes): MakeMKV 2.0 on Linux sometimes stalls with LG drives, using a full CPU core and printing nothing. If it is silent this long, the app stops it and the job fails with "MakeMKV stopped responding". The app retries it by itself 3 minutes later, up to 2 more times, and the failed job's box says when. It skips the retry if you have pressed Retry, queued something else on that drive, or taken the disc out. After that, press **Retry** yourself, or eject and re-insert the disc. Only one MakeMKV at a time does its start-up drive scan, because two discs inserted together could hang one of them. MakeMKV prints progress all the way through a rip, so a working rip is never stopped by this.

### Job logs

Every job (scans, rips, test jobs) keeps a log in `/config/logs/job-<id>.log`: what ran, the tool's own messages, progress every 10% and how it ended. A CD rip also keeps cyanrip's own log beside it as `job-<id>-cyanrip.log`. Progress chatter is left out, so a log is usually 10–20 KB. Logs are kept until you delete them. Each job on a drive's card has a **log** link that opens the log in a new tab, and a failed job shows its reason in a box on the card with **Retry** and **View log**.

### Testing step 4 on the server

1. Insert a plain film DVD in each drive. It rips and ejects, and the MKV plays from downloads.
2. Insert a Blu-ray with decoy playlists, if you have one. It is held, then rips after you pick a title.
3. Tick an extra mid-rip. It lands in `extras/`, and the eject waits for it.
4. Press Hold during a scan. Check one rip's tracks with `ffprobe`: every audio and subtitle language is there, commentary included.
5. Cancel mid-rip. Restart the container mid-rip; Retry works.
6. Re-insert a ripped disc: "Already ripped". Rip again works.
7. Insert a disc too big for the remaining budget: "Waiting for cache space".
8. Capture real `makemkvcon -r` output (info and mkv) from a DVD and a Blu-ray into `tests/fixtures/makemkv/`, and check the parser against it. The current fixtures are written from MakeMKV's documented format, not captured.

### Encoding

After a rip, the main title is encoded to HEVC, and the encode lands **beside the raw file** as `<name> - <Preset>.mkv` (for example `The Lego Movie_t01 - Standard.mkv`) in the rip's downloads folder. Presets live on the **Presets** page (see below); you pick one per encode. Three come with the app:

- **Standard:** HandBrake, x265 10-bit, preset `slow`, quality RF 20 for Blu-ray and RF 18 for DVD. Slow but small and clean; DVDs are also deinterlaced when combing is detected.
- **Fast:** ffmpeg on the Intel GPU (`hevc_vaapi`, constant quality, starting at the value you had in the old `fast_quality` setting, default 24). Much faster, larger files. DVDs are checked for interlacing first.
- **Copy only:** a lossless remux into a new MKV, with no re-encoding.

The seeded presets give one English audio track, converted to E-AC-3: 5.1 at 640 kb/s when the source has 3 or more channels, otherwise stereo or mono at 224 kb/s. Every English subtitle track, the chapters and the metadata are kept. No subtitle track is marked default (forced flags are kept), so Jellyfin doesn't switch one on by itself and burn it into the picture. Encodes run at the lowest CPU and disk priority.

Settings has an **Encoding** group:
- **encode_slots:** encodes run at once, 1 or 2 (default 1). A change applies after the container restarts.
- **raw_keep_hours:** how long the raw file is kept after a good encode (default 24).
- **encode_stall_minutes:** stop an encoder that prints nothing for this long (default 10).

What a finished rip does next is set by **After ripping** on the Presets page. To pick differently for one disc, use **Encode with** on its drive card while it scans or rips; titles that finish after a change use the new choice. The Films page shows each rip's choice.

Encodes queue behind the shutdown guard (a running encode holds shutdown like a rip, and an encode that would run past the grace period waits for the next session) and behind the work-area space check. Stopping the container mid-encode marks the encode interrupted and it restarts at the next boot.

**Raw deletion.** The raw rip is deleted as soon as an encode of it passes its check (any preset, including Copy only), as long as the encode's output still exists and no other encode of that file is queued or running. A disc can always be ripped again. To keep raws for a while, set **Keep raw rips for** in Settings (in hours; 0, the default, means straight away). The **Delete raw now** button on the Films page does the same at once, after a confirmation, and is refused unless a good encode exists. The disc is the backup. The Films page then shows "raw deleted".

**Films page** (`/films`, in the nav): the running and queued encodes at the top (the dashboard's **Encoding** panel shows the same lines), then each rip, newest first, with its files. Each file has an **Encode** menu (and **Encode all** per rip), a failed encode shows its reason with **Retry** and **log**, and every encode has its own log (`/config/logs/`). It updates live.

**GPU setup.** The template passes `--device=/dev/dri`. The entrypoint keeps the render group itself, clears the other groups, and warns in the container log if the render node is root-only (then Fast cannot run as the app user). Check as the app user with the `setpriv ... vainfo` command in step 5 of the install (plain `docker exec ... vainfo` runs as root and proves nothing): it must list `VAProfileHEVCMain : VAEntrypointEncSlice`. Without it, use Standard or Copy only.

### Testing step 6 on the server

1. Encode The Lego Movie with Standard and Madagascar with Fast. Play both in Jellyfin.
2. Check Fast's quality and size, and tune the quality in the Fast preset.
3. Check Madagascar's Standard encode for combing (deinterlacing).
4. Press **Delete raw now** on one film. Let another raw expire after `raw_keep_hours` (or set it to 0 temporarily).
5. Stop the container mid-encode. The encode restarts at the next boot.
6. Start an encode that would run past the grace period. It waits for the next session.
7. Capture real encoder output for the parser fixtures (the current ones are synthetic). Use a short title and keep the files on the share:
   ```
   mkdir -p /mnt/user/Barry-server/fixtures
   docker exec disc-ripper sh -c 'HandBrakeCLI --json --stop-at seconds:60 --encoder x265_10bit --encoder-preset slow --quality 20 -i "/data/downloads/disc-ripper/<rip_id>/<file>.mkv" -o /tmp/hb-test.mkv' > /mnt/user/Barry-server/fixtures/handbrake.json.txt 2>&1
   docker exec disc-ripper sh -c 'ffmpeg -nostdin -y -vaapi_device /dev/dri/renderD128 -i "/data/downloads/disc-ripper/<rip_id>/<file>.mkv" -t 60 -vf format=nv12,hwupload -c:v hevc_vaapi -rc_mode CQP -qp 24 -c:a eac3 -b:a 640k -progress pipe:1 /tmp/ff-test.mkv' > /mnt/user/Barry-server/fixtures/ffmpeg-progress.txt 2>/mnt/user/Barry-server/fixtures/ffmpeg-stderr.txt
   docker exec disc-ripper rm -f /tmp/hb-test.mkv /tmp/ff-test.mkv
   ```
   Then copy them into `tests/fixtures/encode/` and check the parsers against them. Do this once for a DVD and once for a Blu-ray.

### Presets

The **Presets** page (`/presets`, in the nav) lists every preset with a plain summary, an estimate for a DVD and a Blu-ray, and **Edit**, **Copy** and **Delete**. **New preset** and **Copy** open the edit page; Copy starts from an existing preset. Rips keep every language on the disc; each preset decides which tracks go into its encode.

**After ripping** at the top picks the preset a finished rip queues on its own, or Off. It replaces the old `encode_after_rip` setting.

The edit page has three sections:
- **Video:** the engine (HandBrake or ffmpeg), the mode (software, hardware or copy), the codec, quality for DVD and for Blu-ray (lower is better), speed (software only), a height cap (scales down only, never up), deinterlacing (auto is DVDs only, off, or always) and crop (off, or auto to cut the black bars).
- **Audio:** the codec (E-AC-3, AC-3, AAC, Opus, or copy untouched), the channels (up to 5.1, stereo, or keep as is), bitrates for surround and for stereo, the languages in the order you want them, whether to keep the first track of each language or all of them, and whether to keep commentary. If no track matches your languages, the first audio track is kept and the log says so, so a film never ends up silent. Tracks with no language set count as a match.
- **Subtitles:** all, forced only, or none, within the languages you list.

Options the chosen engine and mode can't do are **greyed out**, with a short reason beside them. For example, hardware encoding has no speed, and copy mode greys out every video option.

Every change redraws two **previews** (an example DVD and an example Blu-ray, each with a few audio and subtitle tracks). Each shows the exact command that would run, which tracks it keeps, and the estimate. The previews use the same code as real encodes, so the command shown is the one that runs.

**Estimates** show size and time. A preset with no finished encodes shows a rough guess marked "(rough)". After a few encodes made with the same settings and disc type, it uses the average of the last five instead, and says how many ("from 4 encodes"). Changing a setting that affects the result starts the history again.

**Editing** a preset applies to encodes that start afterwards, including ones already queued. Finished encodes keep the settings they were made with. Renaming doesn't rename old files.

**Deleting** is refused while a queued or running encode, a drive's Encode with choice, a rip in progress, or After ripping uses the preset. At least one preset must always remain.

A rip made before this update holds English tracks only. A preset asking for another language on it keeps the first audio track, and the Films page says so.

### Testing the preset builder on the server

1. Force Update the container.
2. Open Presets and check the three seeds. Fast should show your old quality.
3. Copy Standard as "Anime" with `jpn, eng` audio, and check the Blu-ray example preview keeps Japanese first.
4. Rip a disc with a second language. The rip keeps it (check the log's track list).
5. Encode with the new preset, and check the tracks in Jellyfin.
6. Try a 720p cap preset, and check that Radarr imports a Blu-ray one as Bluray-720p (the cap applies to Blu-ray sources).
7. Try crop auto on a 2.39:1 film.
8. Try deleting a preset that's in use.

### Importing films

After a film's main title has a good encode, the app looks the film up in Radarr and files the encode into your library. TV-like discs (no single main title) are left alone: the Films page says "TV: not imported yet" and the rip stays in downloads.

**Setup.** Settings has a **Radarr** group. Without an address and key nothing is identified, and the Films page says "Set up Radarr in Settings to import films."
- **Radarr address:** for example `http://<server>:7878`.
- **Radarr API key:** Radarr → Settings → General → Security → API Key. It is never shown again once saved.
- **Ask before replacing** (default on): when the film is already in Radarr with a file, wait for your OK before replacing it.
- **Tidy folders on replace** (default on): after a replace, rename the film's folder to Radarr's own name for it, e.g. `Elf` to `Elf (2003)`.

Also turn on Radarr's recycle bin (Settings → Media Management → Recycling Bin) and point it at a folder under `/data`. Radarr and the ripper must both see that folder at the same path, which they do when both mount the share as `/data`. Without it, Replace asks you to type `REPLACE`, and Undo cannot bring the old file back.

**Finding the film.** The search uses MakeMKV's disc name, or the cleaned-up volume label if there is none. A match is imported automatically only when it is confident: the title is at least 90% similar, the runtime is within 5 minutes (or 6%) of the ripped title, and no other candidate also fits. Anything else waits on the Films page with the best five candidates and an **Identify** form: type a name (and year), or paste an IMDb or TMDb ID or link, press **Search**, then **Use this film**. A remake with the same title but a different length is told apart by its runtime; if two fit, it waits for you. A film that is not in Radarr yet is added under **Movies**, unmonitored, with Radarr's first quality profile (you can pick **Other movies** in the form), and Radarr's own naming files it. The film is always left unmonitored, so Radarr never goes looking for a "better" copy.

**Auto-import or wait.** A film that is new to Radarr, or in Radarr without a file, imports straight away once the match is confident. A film that is already in Radarr with a file is a **Replace**:
- With **Ask before replacing** on, the rip waits with the old file and the new one side by side (resolution, codecs, audio, size, length) and three choices: **Replace**, **Skip**, or **Choose a different film**.
- With it off, the replace happens on its own, but only if every safety check passes. Otherwise it waits anyway and the failed check is named. The checks are:
  - the new length is within 2% (or 5 minutes) of the old file's;
  - Radarr's recycle bin is set, and the ripper can see it;
  - the new resolution is not lower than the old one (a DVD over a 1080p file always waits).
- With no visible recycle bin, **Replace** needs the word `REPLACE` typed first.

Import steps run in the background and never hold the shutdown guard. The file is *moved* into the library by Radarr, which names it and picks its quality (DVD, Bluray-1080p, or Remux-1080p for a Blu-ray Copy only), and the app checks that Radarr's file really is the new one. Extras go into the film's `extras` folder. Only one rip of a film is imported at a time; a second waits ("Another rip of this film is being imported"). If Radarr is down or the key is wrong, the rip shows "Import failed" with the reason and **Retry**, which carries on from where it stopped. After a restart, an interrupted import shows "Interrupted by a restart; press Retry." Radarr calls wait 15 s for an answer, except the folder view for the import (180 s) and film look-ups (60 s); a call that times out says "Radarr took too long to answer ... press Retry." Each import has a log (`/config/logs/import-<rip_id>.log`, linked from the Films page) with every Radarr call; the key is never written to it.

When a second rip of the same film waits and then carries on, it goes through the replace rule. With Ask before replacing off and all checks passing, it replaces automatically.

Encodes that finished before Radarr was set up aren't imported automatically. Use Identify on the Films page, or re-encode.

**Undo, Skip and the folder tidy.**
- **Recently imported** at the top of the Films page lists the last imports, each with **Undo**.
- Undoing a new film moves its file back to the rip's downloads folder, removes the film from Radarr only if the ripper added it, and removes the folder if it is empty.
- Undoing a replace moves the new file back, restores the old file from Radarr's recycle bin, and moves the folder back to its old name if it was tidied. Radarr empties its recycle bin after `recycleBinCleanupDays` (default 7), and after that only the new file comes back. The page shows "Undo available until <date>" for replaces.
- Undo leaves the rip waiting for a film again, so "Choose a different film" is Undo and then Identify.
- **Skip** gives up on a rip: after a confirmation it deletes the rip's downloads folder.
- The folder tidy runs only on a replace. The target is the name Radarr itself gives the film; the film's current folder is the last part of its Radarr path.

**When the import starts.** Only once the whole disc is done: every title ripped, and no encode of the disc queued or running. A main title that finishes encoding before the extras have been ripped waits for them, so every extra goes into the film's extras folder. The import also waits while an automatic retry of one of the disc's titles is pending, and while a title's latest rip or encode attempt has failed: the Films page then says "Title N didn't rip. Retry it on the drive's card, or import without it." (or "Title N's encode failed. Retry it below, or import without it.") with an **Import anyway** button (a cancelled attempt doesn't block). The app also looks every 5 minutes, at start-up and after Settings are saved for a finished disc that never got imported (for example when its last encode failed, or Radarr was set up later), and imports it.

**Clean-up.** After an import, the ripper asks Jellyfin to refresh and checks every 5 minutes, for up to 24 hours, for the film. It counts as seen when Jellyfin has an item with the film's TMDb ID whose file name and size match the imported file (the name, not the whole path, because Jellyfin may mount the library elsewhere). Then the rip's downloads folder (raw, encodes and extras) is deleted and the Films page says "In Jellyfin; the rip's files were cleaned up." Without Jellyfin set up, the folder is deleted 24 hours after the import. Clean-up is postponed (and tried again later) while the disc still has work: a title queued, ripping or waiting for space, an encode queued or running, or a rip that is still ripping or couldn't be moved. Clean-up waits, and keeps the rip, if the library file is missing or still inside the rip folder: the page says "Check the library." The raw-file sweep leaves any rip that is being imported or was imported alone.

### Testing step 5 on the server

(In this test the preset dropdown shows your preset names, such as Standard, rather than the old fixed three.)

1. Open Radarr's Swagger page (`<radarr>/api/v3/…` docs) and check the endpoints in the design's Constraints.
2. Check Radarr → Settings → Media Management → Recycling Bin is set to a folder under `/data`. Turn it on before replacing.
3. Import one new film (one not in Radarr). It lands under Movies, unmonitored, and Jellyfin shows it.
4. Replace one film with **Ask before replacing** on. Check the side-by-side comparison, then press Replace. Check the old file is in the recycle bin and the folder tidy.
5. **Undo** that replace. The old file comes back, and Radarr shows it. Check the page says "Undo available until <date>" first.
6. Turn **Ask before replacing** off and replace another film. Also try a DVD over a 1080p file: it must still wait.
7. With the recycle bin off, check that Replace needs the typed word `REPLACE`.
8. Rip a remake with the same title as an older film. It must not import the wrong one.
9. Stop Radarr, start an import, and check the rip fails with a readable reason. Start Radarr and press Retry.
10. In a browser, type in the Identify search box and wait for an encode to progress; the text and results must stay.
11. Check Jellyfin's watch history on a replaced film, and that the rip's downloads folder is deleted once Jellyfin shows the film.

## Update

Docker tab → the container → Check for updates (or Force update). Settings and the database live in `/config`, so they survive updates.

### Updating MakeMKV

MakeMKV is compiled from makemkv.com source at a pinned version. To bump it, edit the three `ARG` lines at the top of the `Dockerfile` (`MAKEMKV_VERSION`, `MAKEMKV_OSS_SHA256`, `MAKEMKV_BIN_SHA256`). Take the version from https://www.makemkv.com/download/ and the checksums from its `makemkv-sha-<version>.txt`, and check them by hand. The build fails if a checksum does not match. Then rebuild and redeploy the image. A new beta key (Settings) is enough when only the key has expired.

## Develop

```
python -m venv .venv
.venv\Scripts\pip install -e .[dev]     # Linux/macOS: .venv/bin/pip
.venv\Scripts\pytest
```

Run locally (Windows PowerShell):

```
$env:CONFIG_DIR="$PWD\.devconfig"; $env:SYSFS_ROOT="$PWD\.devconfig\sys"; $env:DEV_ROOT="$PWD\.devconfig\dev"; $env:WORK_DIR="$PWD\.devconfig\work"; $env:MUSIC_DIR="$PWD\.devconfig\music"; $env:DOWNLOADS_DIR="$PWD\.devconfig\downloads"
.venv\Scripts\uvicorn disc_ripper.app:create_app --factory --port 8580
```
