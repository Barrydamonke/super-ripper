"""Check a job's space needs against the work-area budget and the cache's free space.

/work is an Unraid exclusive share, so statvfs on it reports the cache pool
itself. The work area's own size is a directory walk, cached for a minute.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path

GB = 1_000_000_000


def tree_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass  # deleted while we walked
    return total


class SpaceChecker:
    def __init__(
        self,
        work_dir: Path,
        *,
        statvfs: Callable = getattr(os, "statvfs", None),
        size_fn: Callable[[Path], int] = tree_size,
        clock: Callable[[], float] = time.monotonic,
        cache_seconds: float = 60.0,
    ):
        self.work_dir = work_dir
        self._statvfs = statvfs
        self._size_fn = size_fn
        self._clock = clock
        self._cache_seconds = cache_seconds
        self._cached: tuple[float, int] | None = None

    def invalidate(self) -> None:
        self._cached = None

    def _used(self) -> int:
        now = self._clock()
        if self._cached is None or now - self._cached[0] >= self._cache_seconds:
            self._cached = (now, self._size_fn(self.work_dir))
        return self._cached[1]

    def check(self, needs_bytes: int, running_bytes: int, budget_gb: float, min_free_gb: float) -> str | None:
        """None if the job fits; otherwise the reason it has to wait."""
        if needs_bytes <= 0:
            return None
        if not self.work_dir.is_dir():
            return f"Work area {self.work_dir} not found."
        needs = f"needs {needs_bytes / GB:.1f} GB"
        left = budget_gb * GB - self._used() - running_bytes
        if needs_bytes > left:
            return f"Waiting for cache space: {needs}, work area budget has {max(left, 0) / GB:.1f} GB left."
        if self._statvfs is None:
            return "Waiting for cache space: free space can't be read on this platform."
        st = self._statvfs(self.work_dir)
        # Running jobs may not have written their data yet; count it as already used.
        free = st.f_bavail * st.f_frsize - running_bytes
        if free - needs_bytes < min_free_gb * GB:
            return f"Waiting for cache space: {needs}, cache would drop below the {min_free_gb:g} GB minimum free."
        return None
