"""Film identification rules: pure functions, no I/O."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

_DROP_TOKENS = {"ws", "fs", "pal", "ntsc", "r2", "uk", "us"}
_DISC_TOKEN = re.compile(r"^(disc_?\d+|d\d)$", re.IGNORECASE)
_TITLE_OK = 0.9


@dataclass(frozen=True)
class Candidate:
    tmdb_id: int
    title: str
    year: int | None
    runtime_min: int | None
    poster: str
    in_library: bool
    folder: str


@dataclass(frozen=True)
class Scored:
    candidate: Candidate
    title_score: float
    runtime_ok: bool


def clean_label(label: str) -> str:
    text = re.sub(r"[_\s]+disc[_\s]*\d+\b", " ", label, flags=re.IGNORECASE)
    tokens = [t for t in re.split(r"[_\s]+", text) if t]
    tokens = [t for t in tokens if t.lower() not in _DROP_TOKENS]
    if len(tokens) > 1 and _DISC_TOKEN.match(tokens[-1]):
        tokens.pop()
    return " ".join(t.capitalize() for t in tokens)


def normalise(title: str) -> str:
    text = unicodedata.normalize("NFKD", title)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"^(the|a|an) ", "", text)


def _runtime_ok(c: Candidate, main_seconds: int) -> bool:
    if c.runtime_min is None:
        return False
    return abs(c.runtime_min * 60 - main_seconds) <= max(300, 0.06 * main_seconds)


def score(candidates: list[Candidate], names: list[str], main_seconds: int) -> list[Scored]:
    wanted = [normalise(n) for n in names if n]
    out = []
    for c in candidates:
        norm = normalise(c.title)
        best = max((SequenceMatcher(None, w, norm).ratio() for w in wanted), default=0.0)
        out.append(Scored(c, best, _runtime_ok(c, main_seconds)))

    def key(s: Scored):
        rt = s.candidate.runtime_min
        gap = abs(rt * 60 - main_seconds) if rt is not None else float("inf")
        return (-s.title_score, gap)

    return sorted(out, key=key)


def confident(scored: list[Scored]) -> Candidate | None:
    good = [s for s in scored if s.title_score >= _TITLE_OK and s.runtime_ok]
    if len(good) == 1:
        return good[0].candidate
    return None


def parse_ref(text: str) -> tuple[str, str] | None:
    text = (text or "").strip()
    m = re.search(r"\btt\d{5,}\b", text)
    if m:
        return ("imdb", m.group(0))
    if re.fullmatch(r"\d+", text):
        return ("tmdb", text)
    m = re.search(r"themoviedb\.org/movie/(\d+)", text)
    if m:
        return ("tmdb", m.group(1))
    return None


def quality_name(disc_type: str, preset: str) -> str:
    if disc_type == "dvd":
        return "DVD"
    return "Remux-1080p" if preset == "copy" else "Bluray-1080p"


def folder_name(candidate: Candidate) -> str:
    if candidate.folder:
        return candidate.folder
    name = f"{candidate.title} ({candidate.year})" if candidate.year else candidate.title
    return re.sub(r'[\/:*?"<>|]', "", name)
