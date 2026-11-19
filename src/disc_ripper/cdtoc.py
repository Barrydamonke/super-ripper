"""A CD's table of contents, and its MusicBrainz disc ID.

See https://musicbrainz.org/doc/Disc_ID_Calculation. Offsets here are raw LBAs
(the first track usually starts at 0); MusicBrainz counts the 150-frame
lead-in, so 150 is added when hashing.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass

FRAMES_PER_SECOND = 75
LEAD_IN = 150
# Between the last audio track and a trailing data session (CD-Extra / enhanced CD):
# 11250 frames of lead-out/lead-in plus a 150-frame pre-gap.
DATA_SESSION_GAP = 11400


@dataclass(frozen=True)
class Toc:
    first: int
    last: int
    offsets: tuple[int, ...]  # LBA start of tracks first..last
    leadout: int
    data_tracks: frozenset[int]

    @property
    def audio_tracks(self) -> list[int]:
        return [t for t in range(self.first, self.last + 1) if t not in self.data_tracks]

    def _audio_span(self) -> tuple[int, int]:
        """(last audio track, end of the audio) — a trailing data track is left out."""
        if self.last in self.data_tracks and self.last > self.first:
            return self.last - 1, self.offsets[self.last - self.first] - DATA_SESSION_GAP
        return self.last, self.leadout

    @property
    def audio_seconds(self) -> float:
        _last, end = self._audio_span()
        return (end - self.offsets[0]) / FRAMES_PER_SECOND

    def to_json(self) -> str:
        return json.dumps(
            {
                "first": self.first,
                "last": self.last,
                "offsets": list(self.offsets),
                "leadout": self.leadout,
                "data_tracks": sorted(self.data_tracks),
            }
        )

    @classmethod
    def from_json(cls, text: str) -> Toc:
        data = json.loads(text)
        return cls(
            first=int(data["first"]),
            last=int(data["last"]),
            offsets=tuple(int(o) for o in data["offsets"]),
            leadout=int(data["leadout"]),
            data_tracks=frozenset(int(t) for t in data.get("data_tracks", [])),
        )


def musicbrainz_disc_id(toc: Toc) -> str:
    last, leadout = toc._audio_span()
    parts = [f"{toc.first:02X}", f"{last:02X}", f"{leadout + LEAD_IN:08X}"]
    for track in range(1, 100):
        if toc.first <= track <= last:
            parts.append(f"{toc.offsets[track - toc.first] + LEAD_IN:08X}")
        else:
            parts.append(f"{0:08X}")
    digest = hashlib.sha1("".join(parts).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii").replace("+", ".").replace("/", "_").replace("=", "-")
