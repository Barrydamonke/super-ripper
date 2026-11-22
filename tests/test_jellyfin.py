import json

import pytest

from disc_ripper import jellyfin

ITEMS = {
    "Items": [
        {
            "Name": "Heat",
            "ProviderIds": {"Tmdb": "949"},
            "MediaSources": [
                {"Path": "/media/Movies/Heat (1995)/Heat.mkv", "Size": 1234},
                {"Path": "/media/Movies/Heat (1995)/Heat - 4K.mkv", "Size": 5678},
                {"Path": "/media/Movies/Heat (1995)/nosize.mkv"},
            ],
        },
        {
            "Name": "Heat (other)",
            "ProviderIds": {"Tmdb": "9490"},
            "MediaSources": [{"Path": "/media/Movies/Other/Other.mkv", "Size": 1}],
        },
        {"Name": "No ids", "ProviderIds": None, "MediaSources": [{"Path": "/x.mkv", "Size": 2}]},
    ]
}


def test_movie_sources():
    seen = []

    def fetch(method, url, headers, timeout):
        seen.append((method, url))
        return json.dumps(ITEMS).encode()

    client = jellyfin.Jellyfin("http://jf:8096", "test-key", fetch=fetch)
    assert client.movie_sources(949, "Heat") == [
        ("/media/Movies/Heat (1995)/Heat.mkv", 1234),
        ("/media/Movies/Heat (1995)/Heat - 4K.mkv", 5678),
    ]
    method, url = seen[0]
    assert method == "GET" and url.startswith("http://jf:8096/Items?")
    assert "searchTerm=Heat" in url and "IncludeItemTypes=Movie" in url


def test_movie_sources_bad_json_raises():
    for body in (b"[]", b'{"Items": ["x"]}', b"not json"):
        client = jellyfin.Jellyfin("http://jf", "k", fetch=lambda m, u, h, t, body=body: body)
        with pytest.raises(jellyfin.JellyfinError):
            client.movie_sources(1, "x")
