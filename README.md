# super-ripper

A single Docker container for Unraid that rips, encodes and burns discs from its own web UI. The design is in [Disc Ripper – Design Plan.md](Disc%20Ripper%20%E2%80%93%20Design%20Plan.md).

Current state: **step 3 (audio CD ripping)**, on top of step 2 (detection, queues and the shutdown guard).
- **Dashboard** (the home page): shows each enabled drive live, as empty, tray open or disc in, with the disc type and label. It has Eject and Close tray buttons, and lets you queue a *test job*: a pretend job with a chosen length and size that exercises the queue, progress and cache-space check.
- **Drives page:** labels and enables drives.
- **Audio CDs:**
  - Insert a CD and it rips to FLAC (cyanrip), checked against AccurateRip, identified on MusicBrainz, tagged, and given its cover.
  - A clean rip files itself into `/data/MEDIA/Music/<Album Artist>/<Album> (<Year>)/`.
  - Anything that needs a decision waits on the **Music** page: no match, several possible releases, unverified tracks, or an album you already have.
- **Shutdown guard:** reads the Pi's power schedule and tells unraid-backend, through `/api/busy`, to wait while a job runs, for at most the grace period after the scheduled end.

DVD, Blu-ray and data-disc ripping come in later steps.

## Install on Unraid

1. **Make the image pullable.** The GitHub workflow pushes `ghcr.io/barrydamonke/super-ripper:latest` on every push to `main`. GitHub makes new packages private, so either:
   - set the package to public (GitHub → your profile → Packages → super-ripper → Package settings → Change visibility), or
   - log Unraid in to ghcr.io with a personal access token that has `read:packages`: `docker login ghcr.io -u <github user>`.
2. **Create the work share.** Shares → Add Share → name `disc-ripper-work`, Primary storage **Cache**, Secondary storage **None**. With no secondary storage, Unraid 7 makes it an exclusive share (no FUSE layer), and the mover never touches it.
3. **Add the container.** Copy `unraid/disc-ripper.xml` to `/boot/config/plugins/dockerMan/templates-user/my-disc-ripper.xml` on the flash drive, then Docker → Add Container → pick `disc-ripper` from the template list. Check the paths and `TZ`, then Apply.
4. **First start.** Open `http://<server>:8580`, set the admin password, log in, and open Drives. Both drives should show their model, an ID starting with `serial:` (or a `wwid:`/`port:` fallback), and node status `ok`.

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
4. **Where files go.** Rips are made in `/work/<job>/` and then wait in `/data/downloads/disc-ripper/<job>/`. They're filed into `/data/MEDIA/Music`. Set `MUSIC_DIR` or `DOWNLOADS_DIR` on the container to change those two.

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

## Update

Docker tab → the container → Check for updates (or Force update). Settings and the database live in `/config`, so they survive updates.

### Updating MakeMKV

MakeMKV is compiled from makemkv.com source at a pinned version. To bump it, edit the three `ARG` lines at the top of the `Dockerfile` (`MAKEMKV_VERSION`, `MAKEMKV_OSS_SHA256`, `MAKEMKV_BIN_SHA256`). Take the version from https://www.makemkv.com/download/ and the checksums from its `makemkv-sha-<version>.txt`, and check them by hand. The build fails if a checksum does not match.

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
