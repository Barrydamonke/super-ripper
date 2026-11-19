"""Front cover from the Cover Art Archive. Missing art is normal, never an error."""

from __future__ import annotations

import logging
import urllib.error

from .musicbrainz import USER_AGENT, Fetcher, http_get

log = logging.getLogger(__name__)
URL = "https://coverartarchive.org/release/{release_id}/front-1200"


def fetch_front(release_id: str, fetch: Fetcher = http_get) -> bytes | None:
    try:
        data = fetch(URL.format(release_id=release_id), {"User-Agent": USER_AGENT}, 30.0)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            log.info("cover art for %s: HTTP %s", release_id, exc.code)
        return None
    except OSError as exc:
        log.info("cover art for %s: %s", release_id, exc)
        return None
    return data or None
