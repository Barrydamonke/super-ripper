"""Jellyfin: "do I already own this album?" and "please rescan the library".

Both are best-effort: a wrong address or key must never stop an album being
filed. Errors are kept for the Music page.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse
import urllib.request
from collections.abc import Callable

log = logging.getLogger(__name__)
TIMEOUT = 10.0

Fetcher = Callable[[str, str, dict[str, str], float], bytes]


class JellyfinError(Exception):
    pass


def http_request(method: str, url: str, headers: dict[str, str], timeout: float) -> bytes:
    request = urllib.request.Request(url, headers=headers, method=method, data=b"" if method == "POST" else None)
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (admin-set URL)
        return response.read(5_000_000)


class Jellyfin:
    def __init__(self, url: str, api_key: str, fetch: Fetcher = http_request):
        self.base = url.rstrip("/")
        self._headers = {"Authorization": f'MediaBrowser Token="{api_key}"', "Accept": "application/json"}
        self._fetch = fetch

    def _call(self, method: str, path: str) -> bytes:
        try:
            return self._fetch(method, self.base + path, self._headers, TIMEOUT)
        except Exception as exc:  # HTTP errors, timeouts, bad URLs
            raise JellyfinError(str(exc)) from exc

    def has_album(self, release_id: str, album: str) -> bool:
        """Is there a music album with this MusicBrainz release ID?

        Searches by album name and compares ProviderIds, which works on Jellyfin
        versions without provider-ID filtering.
        """
        query = urllib.parse.urlencode(
            {"Recursive": "true", "IncludeItemTypes": "MusicAlbum", "Fields": "ProviderIds", "searchTerm": album}
        )
        try:
            items = json.loads(self._call("GET", f"/Items?{query}")).get("Items", [])
        except ValueError as exc:
            raise JellyfinError(f"unexpected answer: {exc}") from exc
        return any((item.get("ProviderIds") or {}).get("MusicBrainzAlbum") == release_id for item in items)

    def refresh(self) -> None:
        self._call("POST", "/Library/Refresh")


class RefreshDebouncer:
    """One library refresh `delay` seconds after the last filing, so a stack of CDs
    causes one scan. trigger() must be called on the event loop."""

    def __init__(self, get_client: Callable[[], Jellyfin | None], delay: float = 60.0):
        self._get_client = get_client
        self.delay = delay
        self._task: asyncio.Task | None = None
        self.last_error: str | None = None

    def trigger(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = asyncio.get_running_loop().create_task(self._later())

    async def _later(self) -> None:
        await asyncio.sleep(self.delay)
        client = self._get_client()
        if client is None:
            return
        try:
            await asyncio.to_thread(client.refresh)
            self.last_error = None
        except JellyfinError as exc:
            self.last_error = f"Jellyfin refresh failed: {exc}"
            log.warning("%s", self.last_error)
