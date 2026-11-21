"""One plain-text log file per job, kept in /config/logs, so a rip can be looked into later.

A job's log holds the command that ran, the tool's own messages, progress every 10%,
and how the job ended. Progress chatter (MakeMKV's PRGV lines, cyanrip's per-second
progress) and MakeMKV's per-stream listing are left out, so a log stays small enough
to read. Writing a log must never break a rip: every error here is swallowed.
"""

from __future__ import annotations

import logging
import re
import shutil
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

LOG_DIRNAME = "logs"
_NOISE = re.compile(r"^(PRGV:|SINFO:)|^Ripping (and encoding )?track \d+, progress")


def path_for(config, job_id: int) -> Path:
    return config.config_dir / LOG_DIRNAME / f"job-{job_id}.log"


def is_noise(line: str) -> bool:
    return not line.strip() or bool(_NOISE.match(line))


class JobLog:
    def __init__(self, path: Path):
        self.path = path
        self._next_milestone = 10

    def write(self, text: str) -> None:
        stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                for line in text.splitlines() or [""]:
                    f.write(f"{stamp} {line}\n")
        except OSError as exc:
            log.debug("joblog: can't write %s: %s", self.path, exc)

    def output(self, line: str) -> None:
        """A line the tool printed."""
        if not is_noise(line):
            self.write(f"| {line.rstrip()}")

    def progress(self, fraction: float) -> None:
        """Note each 10% the job passes."""
        percent = int(max(0.0, min(fraction, 1.0)) * 100 + 1e-9)
        reached = None
        while self._next_milestone <= 100 and percent >= self._next_milestone:
            reached = self._next_milestone
            self._next_milestone += 10
        if reached is not None:
            self.write(f"progress {reached}%")

    def attach(self, name: str, src: Path) -> None:
        """Keep a copy of a tool's own log file next to this one (job-<id>-<name>.log)."""
        dest = self.path.with_name(f"{self.path.stem}-{name}.log")
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
        except OSError as exc:
            log.debug("joblog: can't keep %s: %s", src, exc)
            return
        self.write(f"kept {name}'s own log as {dest.name}")
