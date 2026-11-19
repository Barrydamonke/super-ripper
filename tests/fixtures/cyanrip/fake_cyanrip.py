"""Stand-in for cyanrip in tests. Replays recorded output and writes fake FLACs.

FAKE_CYANRIP_MODE: ok (default) | fail | hang.  FAKE_CYANRIP_TRACKS: track count (3).
"""
import os
import struct
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent


def flac_bytes():
    bits = (44100 << 44) | (1 << 41) | (15 << 36) | 44100
    info = struct.pack(">HH", 4096, 4096) + b"\x00" * 6 + bits.to_bytes(8, "big") + b"\x00" * 16
    return b"fLaC" + bytes([0x80]) + len(info).to_bytes(3, "big") + info


def main():
    args = sys.argv[1:]
    folder = Path(args[args.index("-D") + 1]) if "-D" in args else Path("rip")
    folder.mkdir(parents=True, exist_ok=True)
    (Path.cwd() / "argv.txt").write_text("\n".join(args), encoding="utf-8")
    mode = os.environ.get("FAKE_CYANRIP_MODE", "ok")
    tracks = int(os.environ.get("FAKE_CYANRIP_TRACKS", "3"))
    stdout = (HERE / "stdout.txt").read_text(encoding="utf-8").split("\n", 1)[1]
    if mode == "hang":
        sys.stdout.write("\rRipping and encoding track 1, progress - 1.00%")
        sys.stdout.flush()
        time.sleep(3600)
    if mode == "fail":
        sys.stdout.write("cyanrip 0.9.3 (release)\nError ripping: Input/output error\n")
        sys.stdout.flush()
        sys.exit(1)
    for chunk in stdout.split("\r"):
        sys.stdout.write(chunk + "\r")
        sys.stdout.flush()
    for n in range(1, tracks + 1):
        name = f"{n:02d}" if tracks > 9 else str(n)
        (folder / f"{name}.flac").write_bytes(flac_bytes())
    log = (HERE / "log.txt").read_text(encoding="utf-8").split("\n", 1)[1]
    if os.environ.get("FAKE_CYANRIP_VERIFIED"):  # every track accurately ripped
        log = "".join(
            f"Track {n} ripped and encoded successfully!\n"
            "  Accurip:       disc found in database (max confidence: 9)\n"
            f"    Accurip v1:  0000000{n} (accurately ripped, confidence 9)\n"
            for n in range(1, tracks + 1)
        )
    (folder / "log.log").write_text(log, encoding="utf-8")
    sys.exit(0)


main()
