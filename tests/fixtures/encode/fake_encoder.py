"""Stand-in for HandBrakeCLI and ffmpeg (HANDBRAKE_BIN / FFMPEG_BIN).

FAKE_ENCODE_MODE: ok | fail | hang | short | no_audio.  FAKE_IDET: progressive | interlaced.
Replays the progress fixtures and writes a JSON stub to the file named by -o, or the last argv item.
"""
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
mode = os.environ.get("FAKE_ENCODE_MODE", "ok")
argv = sys.argv[1:]


def say(text):
    print(text, flush=True)


if "-f" in argv and argv[argv.index("-f") + 1] == "null":  # an idet run
    kind = os.environ.get("FAKE_IDET", "progressive")
    tff, prog = (400, 50) if kind == "interlaced" else (2, 500)
    say(f"[Parsed_idet_0 @ 0x1] Multi frame detection: TFF: {tff:5d} BFF: {1:5d} Progressive: {prog:5d} Undetermined: 0")
    sys.exit(0)

if "-o" in argv:
    out = argv[argv.index("-o") + 1]
    fixture = HERE / "handbrake_progress.txt"
else:
    out = argv[-1]
    fixture = HERE / "ffmpeg_progress.txt"

if mode == "hang":
    say("started")
    time.sleep(3600)

for line in fixture.read_text(encoding="utf-8").splitlines():
    if not line.startswith("#"):
        say(line)

if mode == "fail":
    say("Encode failed")
    sys.exit(3)

seconds = 5.0 if mode == "short" else 6035.0
audio = 0 if mode == "no_audio" else 1
Path(out).write_text(json.dumps(
    {"seconds": seconds, "video": 1, "audio": audio, "audio_channels": [6] if audio else []}), encoding="utf-8")
