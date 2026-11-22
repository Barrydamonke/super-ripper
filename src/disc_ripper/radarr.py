"""A small Radarr v3 API client (written against Radarr 7.0.3).

Every failure becomes a RadarrError with a readable message; the API key is
never included in a message.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

from disc_ripper.filmid import Candidate

TIMEOUT = 15.0

Fetcher = Callable[[str, str, dict[str, str], "bytes | None", float], bytes]


class RadarrError(Exception):
    pass


class HttpStatusError(Exception):
    """Raised by the default fetcher for an HTTP error answer: carries status and body."""

    def __init__(self, status: int, body: bytes):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body


def http_request(method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers=headers, method=method, data=body)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (admin-set URL)
            return response.read(20_000_000)
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read(100_000)
        except Exception:
            detail = b""
        raise HttpStatusError(exc.code, detail) from None


def _body_message(body: bytes) -> str:
    """Radarr error bodies are {"message": ...} or a list of {"errorMessage": ...}."""
    try:
        data = json.loads(body)
    except Exception:
        return ""
    if isinstance(data, dict):
        return str(data.get("message") or data.get("errorMessage") or "")
    if isinstance(data, list):
        return "; ".join(str(i["errorMessage"]) for i in data if isinstance(i, dict) and i.get("errorMessage"))
    return ""


class Radarr:
    def __init__(self, url: str, api_key: str, fetch: Fetcher = http_request):
        self.base = url.rstrip("/")
        self._headers = {"X-Api-Key": api_key, "Accept": "application/json"}
        self._fetch = fetch
        self.on_call: Callable[[str, str, str], None] | None = None  # (method, path, "ok" or the error), for logs
        parts = urllib.parse.urlsplit(self.base)
        # never echo user:pass@ from the address
        self._shown_url = self.base if "@" not in parts.netloc else self.base.replace(parts.netloc, parts.netloc.rpartition("@")[2], 1)

    def _note(self, method: str, path: str, outcome: str) -> None:
        if self.on_call is not None:
            try:
                self.on_call(method, path, outcome)
            except Exception:  # a logging hook must never break a call
                pass

    def _call(self, method: str, path: str, payload: object = None):
        try:
            result = self._request(method, path, payload)
        except RadarrError as exc:
            self._note(method, path, str(exc))
            raise
        self._note(method, path, "ok")
        return result

    def _request(self, method: str, path: str, payload: object = None):
        headers = dict(self._headers)
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        try:
            raw = self._fetch(method, self.base + path, headers, body, TIMEOUT)
        except HttpStatusError as exc:
            if exc.status == 401:
                raise RadarrError("Radarr refused the API key") from None
            detail = _body_message(exc.body)
            raise RadarrError(f"Radarr: {exc.status} {detail}".rstrip()) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, ValueError, http.client.HTTPException):
            raise RadarrError(f"Radarr didn't answer at {self._shown_url}") from None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            raise RadarrError("Radarr sent an answer that is not JSON; is the address right?") from None

    # lookups
    def lookup(self, term: str) -> list[dict]:
        return self._call("GET", "/api/v3/movie/lookup?" + urllib.parse.urlencode({"term": term}))

    def lookup_tmdb(self, tmdb_id: int) -> dict:
        return self._call("GET", f"/api/v3/movie/lookup/tmdb?tmdbId={int(tmdb_id)}")

    def lookup_imdb(self, imdb_id: str) -> dict:
        return self._call("GET", "/api/v3/movie/lookup/imdb?" + urllib.parse.urlencode({"imdbId": imdb_id}))

    # library
    def movie_by_tmdb(self, tmdb_id: int) -> dict | None:
        found = self._call("GET", f"/api/v3/movie?tmdbId={int(tmdb_id)}")
        return found[0] if found else None

    def movie(self, movie_id: int) -> dict:
        return self._call("GET", f"/api/v3/movie/{int(movie_id)}")

    def add_movie(self, lookup: dict, root_folder: str, profile_id: int) -> dict:
        payload = dict(lookup)
        payload.update(
            monitored=False,
            rootFolderPath=root_folder,
            qualityProfileId=profile_id,
            addOptions={"searchForMovie": False},
        )
        return self._call("POST", "/api/v3/movie", payload)

    def update_movie(self, movie: dict, move_files: bool = False) -> dict:
        flag = "true" if move_files else "false"
        return self._call("PUT", f"/api/v3/movie/{int(movie['id'])}?moveFiles={flag}", movie)

    def delete_movie(self, movie_id: int, delete_files: bool = False) -> None:
        flag = "true" if delete_files else "false"
        self._call("DELETE", f"/api/v3/movie/{int(movie_id)}?deleteFiles={flag}&addImportExclusion=false")

    # settings
    def root_folders(self) -> list[dict]:
        return self._call("GET", "/api/v3/rootfolder")

    def quality_profiles(self) -> list[dict]:
        return self._call("GET", "/api/v3/qualityprofile")

    def quality_definitions(self) -> list[dict]:
        return self._call("GET", "/api/v3/qualitydefinition")

    def recycle_bin(self) -> tuple[str, int]:
        config = self._call("GET", "/api/v3/config/mediamanagement") or {}
        return config.get("recycleBin") or "", int(config.get("recycleBinCleanupDays") or 0)

    # importing
    def manual_import_view(self, folder: str) -> list[dict]:
        query = urllib.parse.urlencode({"folder": folder, "filterExistingFiles": "false"})
        return self._call("GET", "/api/v3/manualimport?" + query)

    def command(self, name: str, **body) -> int:
        return int(self._call("POST", "/api/v3/command", {"name": name, **body})["id"])

    def command_status(self, command_id: int) -> str:
        return str(self._call("GET", f"/api/v3/command/{int(command_id)}").get("status", ""))


def to_candidate(lookup: dict) -> Candidate:
    return Candidate(
        tmdb_id=int(lookup["tmdbId"]),
        title=lookup.get("title") or "",
        year=lookup.get("year") or None,
        runtime_min=lookup.get("runtime") or None,
        poster=lookup.get("remotePoster") or "",
        in_library="id" in lookup,
        folder=lookup.get("folder") or "",
    )


def parse_resolution(text: str | None) -> tuple[int, int] | None:
    """Radarr 7 mediaInfo.resolution is "WxH"."""
    try:
        width, height = str(text).lower().split("x")
        return int(width), int(height)
    except ValueError:
        return None


def parse_runtime(text: str | None) -> int | None:
    """Radarr 7 mediaInfo.runTime is "H:MM:SS"; returns seconds."""
    try:
        hours, minutes, seconds = str(text).split(":")
        return int(hours) * 3600 + int(minutes) * 60 + int(float(seconds))
    except ValueError:
        return None
