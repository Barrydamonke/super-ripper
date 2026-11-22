import io
import json
import urllib.error
from pathlib import Path

import pytest

from disc_ripper import radarr
from disc_ripper.radarr import Radarr, RadarrError

FIX = Path(__file__).parent / "fixtures" / "radarr"
KEY = "test-key"
URL = "http://radarr.test:7878"


def fx(name):
    return json.loads((FIX / name).read_text())


class Fake:
    def __init__(self, routes=None):
        self.calls = []
        self.routes = routes or {}

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, headers, body, timeout))
        for (m, suffix), answer in self.routes.items():
            if m == method and url.removeprefix(URL).startswith(suffix):
                if isinstance(answer, Exception):
                    raise answer
                return json.dumps(answer).encode()
        raise AssertionError(f"unexpected {method} {url}")


def client(routes):
    fake = Fake(routes)
    return Radarr(URL + "/", KEY, fetch=fake), fake


def test_lookup_and_candidates():
    c, fake = client({("GET", "/api/v3/movie/lookup?term=lego+movie"): fx("lookup_lego.json")})
    items = c.lookup("lego movie")
    cands = [radarr.to_candidate(i) for i in items]
    assert cands[0].tmdb_id == 137106 and cands[0].title == "The Lego Movie"
    assert cands[0].year == 2014 and cands[0].runtime_min == 100
    assert cands[0].folder == "The Lego Movie (2014)" and cands[0].poster.endswith("lego.jpg")
    assert cands[0].in_library is False and cands[1].in_library is True
    assert fake.calls[0][1] == URL + "/api/v3/movie/lookup?term=lego+movie"


def test_lookup_by_id_and_movie_lookups():
    elf = fx("movie_elf_with_file.json")
    c, fake = client({
        ("GET", "/api/v3/movie/lookup/tmdb?tmdbId=10719"): elf,
        ("GET", "/api/v3/movie/lookup/imdb?imdbId=tt0319343"): elf,
        ("GET", "/api/v3/movie?tmdbId=10719"): [elf],
        ("GET", "/api/v3/movie?tmdbId=1"): [],
        ("GET", "/api/v3/movie/12"): elf,
    })
    assert c.lookup_tmdb(10719)["id"] == 12
    assert c.lookup_imdb("tt0319343")["id"] == 12
    assert c.movie_by_tmdb(10719)["id"] == 12
    assert c.movie_by_tmdb(1) is None
    assert c.movie(12)["movieFile"]["mediaInfo"]["runTime"] == "1:32:45"


def test_add_movie_payload():
    lookup = fx("lookup_lego.json")[0]
    c, fake = client({("POST", "/api/v3/movie"): {"id": 50, **lookup}})
    c.add_movie(lookup, "/library/Movies", 1)
    method, url, headers, body, _ = fake.calls[0]
    sent = json.loads(body)
    assert url == URL + "/api/v3/movie"
    assert sent["monitored"] is False
    assert sent["addOptions"]["searchForMovie"] is False
    assert sent["rootFolderPath"] == "/library/Movies" and sent["qualityProfileId"] == 1
    assert sent["tmdbId"] == 137106
    assert headers["Content-Type"] == "application/json"
    assert "monitored" not in lookup  # input not mutated


def test_update_movie_move_files_query():
    elf = fx("movie_elf_with_file.json")
    c, fake = client({("PUT", "/api/v3/movie/12"): elf})
    c.update_movie(elf)
    c.update_movie(elf, move_files=True)
    assert fake.calls[0][1].endswith("/api/v3/movie/12?moveFiles=false")
    assert fake.calls[1][1].endswith("/api/v3/movie/12?moveFiles=true")
    assert json.loads(fake.calls[1][3])["id"] == 12


def test_delete_movie():
    c, fake = client({("DELETE", "/api/v3/movie/12"): {}})
    c.delete_movie(12)
    c.delete_movie(12, delete_files=True)
    assert "deleteFiles=false" in fake.calls[0][1]
    assert "deleteFiles=true" in fake.calls[1][1]


def test_lists_and_manual_import_view():
    c, fake = client({
        ("GET", "/api/v3/rootfolder"): fx("rootfolder.json"),
        ("GET", "/api/v3/qualityprofile"): fx("qualityprofile.json"),
        ("GET", "/api/v3/qualitydefinition"): fx("qualitydefinition.json"),
        ("GET", "/api/v3/manualimport"): fx("manualimport.json"),
    })
    assert [r["path"] for r in c.root_folders()] == ["/library/Movies", "/library/Other movies"]
    assert c.quality_profiles()[0]["name"] == "Any"
    assert {d["quality"]["name"] for d in c.quality_definitions()} >= {"DVD", "Remux-1080p"}
    assert c.manual_import_view("/downloads/Elf")[0]["movie"]["id"] == 12
    assert "folder=%2Fdownloads%2FElf" in fake.calls[-1][1]


def test_command_and_status():
    c, fake = client({
        ("POST", "/api/v3/command"): fx("command_queued.json"),
        ("GET", "/api/v3/command/99"): fx("command_completed.json"),
    })
    assert c.command("ManualImport", files=[{"path": "x"}], importMode="move") == 99
    sent = json.loads(fake.calls[0][3])
    assert sent["name"] == "ManualImport" and sent["importMode"] == "move"
    assert c.command_status(99) == "completed"


def test_recycle_bin():
    c, _ = client({("GET", "/api/v3/config/mediamanagement"): fx("mediamanagement.json")})
    assert c.recycle_bin() == ("/library/recycle/", 7)
    c, _ = client({("GET", "/api/v3/config/mediamanagement"): {"recycleBin": None}})
    assert c.recycle_bin() == ("", 0)


def test_parsers():
    assert radarr.parse_resolution("720x552") == (720, 552)
    assert radarr.parse_runtime("1:32:45") == 5565
    assert radarr.parse_resolution("") is None and radarr.parse_runtime(None) is None
    assert radarr.parse_runtime("garbage") is None


def test_errors_are_readable():
    route = ("GET", "/api/v3/rootfolder")
    for exc, expect in [
        (radarr.HttpStatusError(401, b""), "Radarr refused the API key"),
        (ConnectionRefusedError("boom"), f"Radarr didn't answer at {URL}"),
        (TimeoutError(), f"Radarr didn't answer at {URL}"),
        (urllib.error.URLError("nope"), f"Radarr didn't answer at {URL}"),
        (radarr.HttpStatusError(400, b'{"message": "Path is bad"}'), "Radarr: 400 Path is bad"),
        (radarr.HttpStatusError(400, b'[{"errorMessage": "Bad A"}, {"errorMessage": "Bad B"}]'), "Radarr: 400 Bad A; Bad B"),
        (radarr.HttpStatusError(500, b"<html>oops</html>"), "Radarr: 500"),
    ]:
        c, _ = client({route: exc})
        with pytest.raises(RadarrError) as err:
            c.root_folders()
        assert str(err.value).startswith(expect), str(err.value)
        assert KEY not in str(err.value)


def test_bad_json_is_readable():
    c = Radarr(URL, KEY, fetch=lambda *a: b"<html>")
    with pytest.raises(RadarrError):
        c.root_folders()


def test_api_key_header():
    c, fake = client({("GET", "/api/v3/rootfolder"): []})
    c.root_folders()
    assert fake.calls[0][2]["X-Api-Key"] == KEY
    assert fake.calls[0][1] == URL + "/api/v3/rootfolder"


def test_default_http_request_raises_status(monkeypatch):
    def boom(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 400, "Bad", {}, io.BytesIO(b'{"message": "x"}'))

    monkeypatch.setattr(radarr.urllib.request, "urlopen", boom)
    with pytest.raises(radarr.HttpStatusError) as err:
        radarr.http_request("GET", URL + "/x", {}, None, 1.0)
    assert err.value.status == 400 and err.value.body == b'{"message": "x"}'


def test_scheme_less_url_is_readable():
    c = Radarr("radarr:7878", KEY)  # default http_request path
    with pytest.raises(RadarrError) as err:
        c.root_folders()
    assert str(err.value) == "Radarr didn't answer at radarr:7878"


def test_http_exception_is_readable():
    import http.client

    c, _ = client({("GET", "/api/v3/rootfolder"): http.client.BadStatusLine("gone")})
    with pytest.raises(RadarrError) as err:
        c.root_folders()
    assert str(err.value) == f"Radarr didn't answer at {URL}"


def test_credentials_in_url_are_not_echoed():
    def refuse(*args):
        raise ConnectionRefusedError("x")

    c = Radarr("http://bob:s3cret@radarr.test:7878/", KEY, fetch=refuse)
    with pytest.raises(RadarrError) as err:
        c.root_folders()
    assert str(err.value) == "Radarr didn't answer at http://radarr.test:7878"
    assert "s3cret" not in str(err.value) and "bob" not in str(err.value)


def test_on_call_hook_reports_outcomes_without_key():
    c, fake = client({("GET", "/api/v3/rootfolder"): [], ("GET", "/api/v3/qualityprofile"): RadarrError("Radarr: 500")})
    seen = []
    c.on_call = lambda method, path, outcome: seen.append((method, path, outcome))
    c.root_folders()
    with pytest.raises(RadarrError):
        c.quality_profiles()
    assert seen == [("GET", "/api/v3/rootfolder", "ok"), ("GET", "/api/v3/qualityprofile", "Radarr: 500")]
    assert KEY not in repr(seen)
