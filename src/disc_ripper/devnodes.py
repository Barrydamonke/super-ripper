"""Create /dev nodes for the optical drives at container start (run as root).

The Unraid template grants cgroup access to sr (block 11) and sg (char 21)
devices instead of fixed --device entries, because sg numbers change when
disks are added. This module creates nodes only for the optical drives found
in sysfs, owned by group PGID, so the app (running as PUID:PGID) can use them.

Run: python -m disc_ripper.devnodes
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .drives import DiscoveredDrive, discover


@dataclass(frozen=True)
class DeviceNode:
    path: Path
    kind: Literal["c", "b"]
    major: int
    minor: int


def plan_nodes(found: list[DiscoveredDrive], dev_root: Path) -> list[DeviceNode]:
    nodes: list[DeviceNode] = []
    for drive in found:
        nodes.append(DeviceNode(dev_root / drive.sg_name, "c", *drive.sg_dev))
        if drive.sr_name and drive.sr_dev:
            nodes.append(DeviceNode(dev_root / drive.sr_name, "b", *drive.sr_dev))
    return nodes


def apply_nodes(nodes: list[DeviceNode], gid: int, mode: int = 0o660) -> list[str]:
    lines: list[str] = []
    for node in nodes:
        label = f"{node.path} ({node.kind} {node.major}:{node.minor})"
        try:
            if node.path.exists():
                action = "exists"
            else:
                file_type = stat.S_IFCHR if node.kind == "c" else stat.S_IFBLK
                os.mknod(node.path, mode | file_type, os.makedev(node.major, node.minor))
                action = "created"
            os.chown(node.path, 0, gid)
            os.chmod(node.path, mode)
            lines.append(f"{label}: {action}, group {gid}, mode {mode:o}")
        except (OSError, AttributeError) as exc:
            lines.append(f"{label}: failed ({exc})")
    return lines


def main() -> int:
    sysfs_root = Path(os.environ.get("SYSFS_ROOT", "/sys"))
    dev_root = Path(os.environ.get("DEV_ROOT", "/dev"))
    gid = int(os.environ.get("PGID", "100"))
    found = discover(sysfs_root)
    if not found:
        print("devnodes: no optical drives found in sysfs")
        return 0
    for drive in found:
        print(f"devnodes: {drive.vendor} {drive.model} -> {drive.id}")
    for line in apply_nodes(plan_nodes(found, dev_root), gid):
        print(f"devnodes: {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
