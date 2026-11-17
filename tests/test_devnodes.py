import os

from disc_ripper import devnodes
from disc_ripper.devnodes import DeviceNode
from disc_ripper.drives import DiscoveredDrive


def _drive(sg=(21, 2), sr=(11, 0)):
    return DiscoveredDrive(
        id="serial:A", id_source="serial", vendor="v", model="m", revision="r",
        sg_name=f"sg{sg[1]}", sr_name=f"sr{sr[1]}" if sr else None, sg_dev=sg, sr_dev=sr,
    )


def test_plan_nodes(tmp_path):
    assert devnodes.plan_nodes([_drive()], tmp_path) == [
        DeviceNode(tmp_path / "sg2", "c", 21, 2),
        DeviceNode(tmp_path / "sr0", "b", 11, 0),
    ]


def test_plan_skips_missing_sr(tmp_path):
    assert devnodes.plan_nodes([_drive(sr=None)], tmp_path) == [
        DeviceNode(tmp_path / "sg2", "c", 21, 2)
    ]


def test_apply_reports_failure_and_continues(tmp_path, monkeypatch):
    calls = []

    def fake_mknod(path, mode=0o600, device=0):
        calls.append(str(path))
        if len(calls) == 1:
            raise PermissionError("nope")

    monkeypatch.setattr(os, "mknod", fake_mknod, raising=False)
    monkeypatch.setattr(os, "makedev", lambda a, b: (a << 8) | b, raising=False)
    monkeypatch.setattr(os, "chown", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
    nodes = devnodes.plan_nodes([_drive()], tmp_path)
    lines = devnodes.apply_nodes(nodes, gid=100)
    assert len(lines) == 2
    assert "failed" in lines[0]
    assert "failed" not in lines[1]
    assert len(calls) == 2


def test_apply_skips_existing_node(tmp_path, monkeypatch):
    (tmp_path / "sg2").write_text("")
    monkeypatch.setattr(os, "mknod", lambda *a, **k: (_ for _ in ()).throw(AssertionError("mknod called")), raising=False)
    monkeypatch.setattr(os, "chown", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
    [line] = devnodes.apply_nodes([DeviceNode(tmp_path / "sg2", "c", 21, 2)], gid=100)
    assert "exists" in line


def test_main_with_no_drives(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SYSFS_ROOT", str(tmp_path / "sys"))
    monkeypatch.setenv("DEV_ROOT", str(tmp_path / "dev"))
    assert devnodes.main() == 0
    assert "no optical drives" in capsys.readouterr().out
