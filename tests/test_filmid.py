import pytest

from disc_ripper.filmid import (Candidate, clean_label, confident, folder_name, normalise,
                                parse_ref, score)


def cand(title, runtime, tmdb_id=1, year=2014, folder=""):
    return Candidate(tmdb_id=tmdb_id, title=title, year=year, runtime_min=runtime, poster="",
                     in_library=False, folder=folder)


@pytest.mark.parametrize("label,expected", [
    ("THE_LEGO_MOVIE", "The Lego Movie"),
    ("MADAGASCAR_DISC_1", "Madagascar"),
    ("FM1", "Fm1"),
    ("SHREK_2_WS", "Shrek 2"),
])
def test_clean_label_cases(label, expected):
    assert clean_label(label) == expected


def test_normalise():
    assert normalise("The Amélie: Poulain!") == "amelie poulain"
    assert normalise("A  Bug's   Life") == "bugs life"
    assert normalise("An Education") == "education"


def test_confident_exact_title_and_runtime():
    scored = score([cand("The Lego Movie", 100)], ["The Lego Movie"], 6035)
    assert confident(scored).title == "The Lego Movie"


def test_remake_with_different_runtime_is_resolved_by_runtime():
    a = cand("The Lion King", 89, tmdb_id=1, year=1994)
    b = cand("The Lion King", 118, tmdb_id=2, year=2019)
    scored = score([b, a], ["The Lion King"], 5340)
    assert confident(scored).tmdb_id == 1


def test_remake_with_similar_runtime_waits():
    a = cand("The Lion King", 88, tmdb_id=1)
    b = cand("The Lion King", 90, tmdb_id=2)
    assert confident(score([a, b], ["The Lion King"], 5340)) is None


def test_no_runtime_never_confident():
    assert confident(score([cand("The Lego Movie", None)], ["The Lego Movie"], 6035)) is None


def test_low_title_score_not_confident():
    assert confident(score([cand("Something Else", 100)], ["The Lego Movie"], 6000)) is None
    assert confident([]) is None


def test_parse_ref():
    assert parse_ref("tt0319343") == ("imdb", "tt0319343")
    assert parse_ref("https://www.imdb.com/title/tt0319343/") == ("imdb", "tt0319343")
    assert parse_ref("10719") == ("tmdb", "10719")
    assert parse_ref("https://www.themoviedb.org/movie/10719-elf") == ("tmdb", "10719")
    assert parse_ref("elf") is None
    assert parse_ref("") is None


def test_folder_name():
    assert folder_name(cand("X", 1, folder="Elf (2003)")) == "Elf (2003)"
    assert folder_name(cand('Who: What/Why?', 1, year=2001)) == "Who WhatWhy (2001)"


def test_sequel_number_must_match():
    assert confident(score([cand("Toy Story 3", 103)], ["Toy Story 2"], 6180)) is None
    assert confident(score([cand("Shrek 2", 93)], ["Shrek2"], 5580)) is not None


@pytest.mark.parametrize("label,expected", [
    ("US", "Us"),
    ("US_MARSHALS", "Us Marshals"),
    ("ELF_D1", "Elf"),
    ("SHREK_2_WS_PAL", "Shrek 2"),
])
def test_clean_label_trailing_only(label, expected):
    assert clean_label(label) == expected


def test_empty_names_score_zero():
    assert score([cand("The", 100)], ["A"], 6000)[0].title_score == 0.0
    assert score([cand("Elf", 100)], [""], 6000)[0].title_score == 0.0
