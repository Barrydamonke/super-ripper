# super-ripper

A single Docker container for Unraid that rips, encodes and burns discs from its own web UI. The design is in [Disc Ripper – Design Plan.md](Disc%20Ripper%20%E2%80%93%20Design%20Plan.md).

Current state: **step 2 (detection, queues and the shutdown guard)**.
- **Dashboard** (the home page): shows each enabled drive live, as empty, tray open or disc in, with the disc type and label. It has Eject and Close tray buttons, and lets you queue a *test job*: a pretend job with a chosen length and size that exercises the queue, progress and cache-space check.
- **Drives page:** labels and enables drives.
- **Shutdown guard:** reads the Pi's power schedule and tells unraid-backend, through `/api/busy`, to wait while a job runs, for at most the grace period after the scheduled end.

Ripping comes in later steps.

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

## Update

Docker tab → the container → Check for updates (or Force update). Settings and the database live in `/config`, so they survive updates.

## Develop

```
python -m venv .venv
.venv\Scripts\pip install -e .[dev]     # Linux/macOS: .venv/bin/pip
.venv\Scripts\pytest
```

Run locally (Windows PowerShell):

```
$env:CONFIG_DIR="$PWD\.devconfig"; $env:SYSFS_ROOT="$PWD\.devconfig\sys"; $env:DEV_ROOT="$PWD\.devconfig\dev"; $env:WORK_DIR="$PWD\.devconfig\work"
.venv\Scripts\uvicorn disc_ripper.app:create_app --factory --port 8580
```
