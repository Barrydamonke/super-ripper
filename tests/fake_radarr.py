"""An in-memory Radarr with the same public methods as disc_ripper.radarr.Radarr (not the HTTP layer).

Paths are real folders under tmp_path, so ManualImport can really move files.
"""

from __future__ import annotations

import itertools
import json
import shutil
import threading
from pathlib import Path

from disc_ripper import filmid
from disc_ripper.radarr import RadarrError

FIX = Path(__file__).parent / "fixtures" / "radarr"

LEGO = {"title": "The Lego Movie", "year": 2014, "runtime": 100, "tmdbId": 137106, "imdbId": "tt1490017",
        "remotePoster": "https://image.test/lego.jpg", "folder": "The Lego Movie (2014)"}
LION_1994 = {"title": "The Lion King", "year": 1994, "runtime": 89, "tmdbId": 8587, "imdbId": "tt0110357",
             "remotePoster": "", "folder": "The Lion King (1994)"}
LION_2019 = {"title": "The Lion King", "year": 2019, "runtime": 118, "tmdbId": 420818, "imdbId": "tt6105098",
             "remotePoster": "", "folder": "The Lion King (2019)"}


class FakeRadarr:
    def __init__(self, base: Path, catalog: list[dict] | None = None):
        self.root = base / "library" / "Movies"
        self.other_root = base / "library" / "Other movies"
        self.recycle = base / "library" / "recycle"
        for folder in (self.root, self.other_root, self.recycle):
            folder.mkdir(parents=True)
        self.catalog = list(catalog if catalog is not None else [LEGO])
        self.movies: dict[int, dict] = {}
        self.commands: dict[int, dict] = {}
        self.calls: list[str] = []
        self.on_call = None
        self.fail_once: set[str] = set()
        self.block_on: str | None = None  # a method that waits (in its thread) until `release` is set
        self.entered = threading.Event()
        self.release = threading.Event()
        self._ids = itertools.count(1)

    # -- plumbing -----------------------------------------------------------------

    def _enter(self, name: str, method: str, path: str) -> None:
        self.calls.append(name)
        if name == self.block_on:
            self.entered.set()
            self.release.wait(10)
        if name in self.fail_once:
            self.fail_once.discard(name)
            if self.on_call:
                self.on_call(method, path, "Radarr didn't answer at http://radarr.test")
            raise RadarrError("Radarr didn't answer at http://radarr.test")
        if self.on_call:
            self.on_call(method, path, "ok")

    # -- lookups -------------------------------------------------------------------

    def lookup(self, term: str) -> list[dict]:
        self._enter("lookup", "GET", "/api/v3/movie/lookup?term=" + term.replace(" ", "+"))
        wanted = filmid.normalise(term)
        found = [self._with_id(c) for c in self.catalog
                 if wanted and (wanted in filmid.normalise(c["title"]) or filmid.normalise(c["title"]) in wanted)]
        return found

    def _with_id(self, lookup: dict) -> dict:
        out = dict(lookup)
        existing = self._by_tmdb(lookup["tmdbId"])
        if existing:
            out["id"] = existing["id"]
        return out

    def lookup_tmdb(self, tmdb_id: int) -> dict:
        self._enter("lookup_tmdb", "GET", f"/api/v3/movie/lookup/tmdb?tmdbId={tmdb_id}")
        for c in self.catalog:
            if c["tmdbId"] == int(tmdb_id):
                return self._with_id(c)
        raise RadarrError("Radarr: 404 Not found")

    def lookup_imdb(self, imdb_id: str) -> dict:
        self._enter("lookup_imdb", "GET", f"/api/v3/movie/lookup/imdb?imdbId={imdb_id}")
        for c in self.catalog:
            if c["imdbId"] == imdb_id:
                return self._with_id(c)
        raise RadarrError("Radarr: 404 Not found")

    # -- library -------------------------------------------------------------------

    def _by_tmdb(self, tmdb_id: int) -> dict | None:
        return next((m for m in self.movies.values() if m["tmdbId"] == int(tmdb_id)), None)

    def movie_by_tmdb(self, tmdb_id: int) -> dict | None:
        self._enter("movie_by_tmdb", "GET", f"/api/v3/movie?tmdbId={tmdb_id}")
        return self._by_tmdb(tmdb_id)

    def movie(self, movie_id: int) -> dict:
        self._enter("movie", "GET", f"/api/v3/movie/{movie_id}")
        return json.loads(json.dumps(self.movies[int(movie_id)]))

    def add_movie(self, lookup: dict, root_folder: str, profile_id: int) -> dict:
        self._enter("add_movie", "POST", "/api/v3/movie")
        movie = dict(lookup)
        movie.update(id=next(self._ids), monitored=False, rootFolderPath=root_folder, qualityProfileId=profile_id,
                     path=str(Path(root_folder) / lookup["folder"]), hasFile=False,
                     addOptions={"searchForMovie": False})
        self.movies[movie["id"]] = movie
        return json.loads(json.dumps(movie))

    def update_movie(self, movie: dict, move_files: bool = False) -> dict:
        self._enter("update_movie", "PUT", f"/api/v3/movie/{movie['id']}?moveFiles={str(move_files).lower()}")
        self.movies[movie["id"]] = json.loads(json.dumps(movie))
        return movie

    def delete_movie(self, movie_id: int, delete_files: bool = False) -> None:
        self._enter("delete_movie", "DELETE", f"/api/v3/movie/{movie_id}")
        self.movies.pop(int(movie_id), None)

    # -- settings ------------------------------------------------------------------

    def root_folders(self) -> list[dict]:
        self._enter("root_folders", "GET", "/api/v3/rootfolder")
        return [{"id": 1, "path": str(self.root)}, {"id": 2, "path": str(self.other_root)}]

    def quality_profiles(self) -> list[dict]:
        self._enter("quality_profiles", "GET", "/api/v3/qualityprofile")
        return json.loads((FIX / "qualityprofile.json").read_text())

    def quality_definitions(self) -> list[dict]:
        self._enter("quality_definitions", "GET", "/api/v3/qualitydefinition")
        return json.loads((FIX / "qualitydefinition.json").read_text())

    def recycle_bin(self) -> tuple[str, int]:
        self._enter("recycle_bin", "GET", "/api/v3/config/mediamanagement")
        return str(self.recycle), 7

    # -- importing -----------------------------------------------------------------

    def manual_import_view(self, folder: str) -> list[dict]:
        self._enter("manual_import_view", "GET", "/api/v3/manualimport?folder=" + folder)
        return [{"path": str(f), "name": f.stem, "size": f.stat().st_size,
                 "languages": [{"id": 1, "name": "English"}], "rejections": []}
                for f in sorted(Path(folder).glob("*.mkv"))]

    def command(self, name: str, **body) -> int:
        self._enter("command", "POST", "/api/v3/command")
        command_id = next(self._ids)
        self.commands[command_id] = {"name": name, **body}
        if name == "ManualImport":
            for entry in body["files"]:
                movie = self.movies[entry["movieId"]]
                source = Path(entry["path"])
                folder = Path(movie["path"])
                folder.mkdir(parents=True, exist_ok=True)
                target = folder / source.name
                shutil.move(str(source), str(target))
                movie["hasFile"] = True
                movie["movieFile"] = {"path": str(target), "size": target.stat().st_size,
                                      "mediaInfo": {"resolution": "1920x1080", "runTime": "1:40:00"}}
        return command_id

    def command_status(self, command_id: int) -> str:
        self._enter("command_status", "GET", f"/api/v3/command/{command_id}")
        return "completed"
