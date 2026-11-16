# super-ripper

A single Docker container for Unraid that rips, encodes and burns discs from its own web UI. The design is in [Disc Ripper – Design Plan.md](Disc%20Ripper%20%E2%80%93%20Design%20Plan.md).

Current state: **step 1 (skeleton)**. You can log in, see the optical drives (identified by serial number), label them, and edit settings. Ripping comes in later steps.

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
$env:CONFIG_DIR="$PWD\.devconfig"; $env:SYSFS_ROOT="$PWD\.devconfig\sys"; $env:DEV_ROOT="$PWD\.devconfig\dev"
.venv\Scripts\uvicorn disc_ripper.app:create_app --factory --port 8580
```
