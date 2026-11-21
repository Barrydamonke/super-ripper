"""Stand-in for makemkvcon in tests.

FAKE_MAKEMKV_MODE: ok (default) | fail_old | hang | short | long | two_files | no_audio.
FAKE_MAKEMKV_INFO: fixture replayed by `info` (default info_dvd.txt).
"""
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent


def fixture(name):
    return (HERE / name).read_text(encoding="utf-8").split("\n", 1)[1]


def main():
    args = sys.argv[1:]
    (Path.cwd() / "argv.txt").write_text("\n".join(args), encoding="utf-8")
    (Path.cwd() / "env_home.txt").write_text(os.environ.get("HOME", ""), encoding="utf-8")
    mode = os.environ.get("FAKE_MAKEMKV_MODE", "ok")
    if mode == "hang":
        sys.stdout.write("PRGV:100,100,65536\n")
        sys.stdout.flush()
        time.sleep(3600)
    if "info" in args:
        if mode == "fail_old":
            sys.stdout.write(fixture("info_too_old.txt"))
            sys.stdout.flush()
            sys.exit(1)
        sys.stdout.write(fixture(os.environ.get("FAKE_MAKEMKV_INFO", "info_dvd.txt")))
        return
    # mkv <dev> <title> <out_dir>
    i = args.index("mkv")
    title, out_dir = int(args[i + 2]), Path(args[i + 3])
    out_dir.mkdir(parents=True, exist_ok=True)
    for pct in (0, 32768, 65536):
        sys.stdout.write(f"PRGV:{pct},{pct},65536\n")
        sys.stdout.flush()
    seconds = {"short": 30, "long": 9000}.get(mode, 6730)
    audio = 0 if mode == "no_audio" else 2
    if mode == "two_files":
        (out_dir / f"title_t{title:02d}_b.mkv").write_text(
            json.dumps({"seconds": seconds, "video": 1, "audio": audio}), encoding="utf-8")
    (out_dir / f"title_t{title:02d}.mkv").write_text(
        json.dumps({"seconds": seconds, "video": 1, "audio": audio}), encoding="utf-8")


if __name__ == "__main__":
    main()
