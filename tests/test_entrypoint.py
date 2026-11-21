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
