"""The entrypoint's render-node gid detection (shell logic, run with sh)."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).resolve().parents[1] / "docker" / "entrypoint.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("sh") is None or not hasattr(os, "getgid"),
    reason="needs a POSIX sh",
)


def _render_gid(node: Path) -> str:
    env = {**os.environ, "ENTRYPOINT_LIB": "1", "RENDER_NODE": str(node)}
    result = subprocess.run(
        ["sh", "-c", f'. "{ENTRYPOINT.as_posix()}"; render_gid'],
        env=env, capture_output=True, text=True, check=True,
    )
    return result.stdout


def test_render_gid_prints_the_node_group(tmp_path):
    node = tmp_path / "renderD128"
    node.write_text("")
    assert _render_gid(node).strip() == str(node.stat().st_gid)


def test_render_gid_prints_nothing_without_a_node(tmp_path):
    assert _render_gid(tmp_path / "missing") == ""


def _sh(node: Path, fn: str):
    env = {**os.environ, "ENTRYPOINT_LIB": "1", "RENDER_NODE": str(node)}
    return subprocess.run(
        ["sh", "-c", f'. "{ENTRYPOINT.as_posix()}"; {fn}'],
        env=env, capture_output=True, text=True, check=True,
    )


def test_groups_option_uses_a_non_root_gid(tmp_path):
    node = tmp_path / "renderD128"
    node.write_text("")
    if node.stat().st_gid == 0:
        pytest.skip("test user's group is 0")
    assert _sh(node, "groups_option").stdout.strip() == f"--groups={node.stat().st_gid}"


def test_groups_option_clears_groups_without_a_node(tmp_path):
    assert _sh(tmp_path / "missing", "groups_option").stdout.strip() == "--clear-groups"


def _fake_root_node(tmp_path, mode: str):
    """Run groups_option/warn_render_node with `stat` faked as a gid-0 node."""
    node = tmp_path / "renderD128"
    node.write_text("")
    fn = (
        f'stat() {{ case "$2" in %g) echo 0;; %a) echo {mode};; esac; }}; '
        "groups_option; warn_render_node"
    )
    return _sh(node, fn)


def test_gid_zero_clears_groups_and_warns_when_root_only(tmp_path):
    result = _fake_root_node(tmp_path, "660")
    assert result.stdout.strip() == "--clear-groups"
    assert "render node is root-only" in result.stderr


def test_gid_zero_world_accessible_clears_groups_quietly(tmp_path):
    result = _fake_root_node(tmp_path, "666")
    assert result.stdout.strip() == "--clear-groups"
    assert result.stderr == ""
