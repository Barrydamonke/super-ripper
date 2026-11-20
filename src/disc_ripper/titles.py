"""Title choice and the rules for starting a rip or holding the disc."""

from __future__ import annotations

from statistics import median

from .makemkv import Title

UNCLEAR_TOLERANCE = 0.03
EPISODE_MIN = 18 * 60
EPISODE_MAX = 75 * 60
EPISODE_SPREAD = 0.20
PLAY_ALL_MIN = 90 * 60
PLAY_ALL_TOLERANCE = 0.10
MIN_EPISODES = 3

_ACTIVE = {"queued", "waiting_space", "running"}


def main_title(titles: list[Title]) -> Title | None:
    """The longest title; a tie goes to the larger size."""
    if not titles:
        return None
    return max(titles, key=lambda t: (t.seconds, t.size))


def is_unclear(titles: list[Title], main: Title) -> bool:
    """Another title is within 3% of the main one's duration (and is not the same playlist)."""
    for t in titles:
        if t is main or t.number == main.number:
            continue
        if main.segments and t.segments == main.segments:
            continue
        if abs(t.seconds - main.seconds) <= UNCLEAR_TOLERANCE * main.seconds:
            return True
    return False


def looks_like_tv(titles: list[Title]) -> bool:
    candidates = [t for t in titles if EPISODE_MIN <= t.seconds <= EPISODE_MAX]
    if len(candidates) < MIN_EPISODES:
        return False
    mid = median(t.seconds for t in candidates)
    episodes = [t for t in candidates if abs(t.seconds - mid) <= EPISODE_SPREAD * mid]
    if len(episodes) < MIN_EPISODES:
        return False
    total = sum(t.seconds for t in episodes)
    for t in titles:
        if t.seconds > PLAY_ALL_MIN and abs(t.seconds - total) > PLAY_ALL_TOLERANCE * total:
            return False
    return True


def hold_reasons(
    titles: list[Title],
    *,
    auto_start: bool,
    hold_pressed: bool,
    already_ripped: str | None,
    from_insert: bool,
) -> list[str]:
    """Reasons to hold the disc for the owner; an empty list means start."""
    reasons: list[str] = []
    main = main_title(titles)
    if main is not None and is_unclear(titles, main):
        reasons.append("two titles of about the same length (decoy playlists?)")
    if looks_like_tv(titles):
        reasons.append("looks like a TV disc")
    if hold_pressed:
        reasons.append("Hold pressed")
    if not auto_start:
        reasons.append("auto-start is off")
    if already_ripped:
        reasons.append(f"already ripped ({already_ripped})")
    if not from_insert:
        reasons.append("disc was in the drive at start-up")
    if main is None:
        reasons.append("no titles found")
    return reasons


def should_eject(attempts: list[tuple[int, int, str]]) -> bool:
    """Eject when nothing is pending and each title's latest attempt is done."""
    if not attempts or any(state in _ACTIVE for _, _, state in attempts):
        return False
    latest: dict[int, tuple[int, str]] = {}
    for title, job_id, state in attempts:
        if title not in latest or job_id > latest[title][0]:
            latest[title] = (job_id, state)
    return all(state == "done" for _, state in latest.values())
