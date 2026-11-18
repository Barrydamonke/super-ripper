import os
from types import SimpleNamespace

from disc_ripper import space
from disc_ripper.space import GB


def checker(tmp_path, free_gb=100.0, used_gb=0.0, clock=None):
    calls = {"size": 0}

    def statvfs(path):
        return SimpleNamespace(f_bavail=int(free_gb * GB / 4096), f_frsize=4096)

    def size_fn(path):
        calls["size"] += 1
        return int(used_gb * GB)

    c = space.SpaceChecker(tmp_path, statvfs=statvfs, size_fn=size_fn, clock=clock or (lambda: 0.0))
    return c, calls


def test_zero_needed_always_fits(tmp_path):
    c, _ = checker(tmp_path / "missing", free_gb=0)
    assert c.check(0, 0, 60, 20) is None


def test_budget_counts_used_and_running(tmp_path):
    c, _ = checker(tmp_path, used_gb=40)
    msg = c.check(15 * GB, 10 * GB, 60, 20)
    assert msg == "Waiting for cache space: needs 15.0 GB, work area budget has 10.0 GB left."
    assert c.check(10 * GB, 10 * GB, 60, 20) is None


def test_min_free(tmp_path):
    c, _ = checker(tmp_path, free_gb=25)
    msg = c.check(10 * GB, 0, 60, 20)
    assert msg == "Waiting for cache space: needs 10.0 GB, cache would drop below the 20 GB minimum free."


def test_missing_work_dir(tmp_path):
    c, _ = checker(tmp_path / "nope")
    assert c.check(GB, 0, 60, 20) == f"Work area {tmp_path / 'nope'} not found."


def test_tree_size_cached(tmp_path):
    now = [0.0]
    c, calls = checker(tmp_path, clock=lambda: now[0])
    c.check(GB, 0, 60, 20)
    c.check(GB, 0, 60, 20)
    assert calls["size"] == 1
    now[0] = 61.0
    c.check(GB, 0, 60, 20)
    assert calls["size"] == 2
    c.invalidate()
    c.check(GB, 0, 60, 20)
    assert calls["size"] == 3


def test_tree_size(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "f").write_bytes(b"x" * 1000)
    (tmp_path / "g").write_bytes(b"x" * 24)
    assert space.tree_size(tmp_path) == 1024
