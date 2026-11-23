"""Stand-in for HandBrakeCLI and ffmpeg (HANDBRAKE_BIN / FFMPEG_BIN).

FAKE_ENCODE_MODE: ok | fail | hang | short | no_audio | drop_audio (claims success, writes no audio streams).
FAKE_IDET: progressive | interlaced.  FAKE_CROP: what cropdetect runs answer (default 1920:1080:0:0).
FAKE_ARGV_FILE: append each run's argv (one JSON line).  FAKE_DAR: the output's display aspect (default 1.778).
FAKE_BLOCKS: replay the progress fixture this many times (log-size test).
Replays the progress fixtures and writes a JSON stub to the file named by -o, or the last argv item.
The stub keeps the source stub's streams that the argv mapped (--audio/--subtitle lists, -map 0:a:N / 0:s:N, or -map 0).
"""
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
mode = os.environ.get("FAKE_ENCODE_MODE", "ok")
argv = sys.argv[1:]


if os.environ.get("FAKE_ARGV_FILE"):
    with open(os.environ["FAKE_ARGV_FILE"], "a", encoding="utf-8") as f:
        f.write(json.dumps(argv) + "\n")


def say(text):
    print(text, flush=True)


if "-f" in argv and argv[argv.index("-f") + 1] == "null" and "cropdetect=round=2" not in argv:  # an idet run
    kind = os.environ.get("FAKE_IDET", "progressive")
    tff, prog = (400, 50) if kind == "interlaced" else (2, 500)
    say(f"[Parsed_idet_0 @ 0x1] Multi frame detection: TFF: {tff:5d} BFF: {1:5d} Progressive: {prog:5d} Undetermined: 0")
    sys.exit(0)

if "cropdetect=round=2" in argv:
    say(f"[Parsed_cropdetect_0 @ 0x1] x1:0 x2:1919 crop={os.environ.get('FAKE_CROP', '1920:1080:0:0')}")
    sys.exit(0)


def read_source():
    try:
        return json.loads(Path(argv[argv.index("-i") + 1]).read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}


def mapped(source_streams):
    """The source streams this run keeps: audio and subtitles, each re-indexed within its kind."""
    audio = [s for s in source_streams if s["kind"] == "audio"]
    subs = [s for s in source_streams if s["kind"] == "subtitle"]
    if "-o" in argv:  # HandBrake: 1-based lists, or none
        def pick(flag, pool):
            if flag not in argv or argv[argv.index(flag) + 1] == "none":
                return []
            return [pool[int(n) - 1] for n in argv[argv.index(flag) + 1].split(",")]
        keep_a, keep_s = pick("--audio", audio), pick("--subtitle", subs)
    elif any(argv[i] == "-map" and argv[i + 1] == "0" for i in range(len(argv) - 1)):
        keep_a, keep_s = audio, subs
    else:
        def pick_ff(kind, pool):
            return [pool[int(a.rsplit(":", 1)[1])] for a in argv if a.startswith(f"0:{kind}:")]
        keep_a, keep_s = pick_ff("a", audio), pick_ff("s", subs)
    out = []
    for kept in (keep_a, keep_s):
        for i, s in enumerate(kept):
            out.append({**s, "index": i})
    return out


if "-o" in argv:
    out = argv[argv.index("-o") + 1]
    fixture = HERE / "handbrake_progress.txt"
else:
    out = argv[-1]
    fixture = HERE / "ffmpeg_progress.txt"

if mode == "hang":
    say("started")
    time.sleep(3600)

lines = [x for x in fixture.read_text(encoding="utf-8").splitlines() if not x.startswith("#")]
for line in lines:
    say(line)
for _ in range(int(os.environ.get("FAKE_BLOCKS", "1")) - 1):  # more progress chatter, no more headers
    for line in lines:
        if line.startswith((" ", "Progress: {", "}")):
            say(line)

if mode == "fail":
    say("Encode failed")
    sys.exit(3)

seconds = 5.0 if mode == "short" else 6035.0
source = read_source()
streams = mapped(source.get("streams", []))
if mode in ("no_audio", "drop_audio"):
    streams = [s for s in streams if s["kind"] != "audio"]
audio = 0 if mode == "no_audio" else 1
Path(out).write_text(json.dumps(
    {"seconds": seconds, "video": 1, "audio": audio, "audio_channels": [6] if audio else [],
     "dar": float(os.environ.get("FAKE_DAR", "1.778")), "sar": [1, 1],
     "width": source.get("width", 1920), "height": source.get("height", 1080), "streams": streams}),
    encoding="utf-8")
