from disc_ripper.makemkv import Title
from disc_ripper.titles import hold_reasons, is_unclear, looks_like_tv, main_title, should_eject


def T(n, secs, size=None, segs="", chapters=10):
    return Title(
        number=n, seconds=secs, size=size if size is not None else secs * 1000,
        chapters=chapters, source="", output_name=f"t{n:02d}.mkv", segments=segs, name="",
    )


def flags(**kw):
    base = dict(auto_start=True, hold_pressed=False, already_ripped=None, from_insert=True)
    base.update(kw)
    return base


def test_plain_film_starts():
    titles = [T(0, 6720), T(1, 300), T(2, 120), T(3, 90)]
    main = main_title(titles)
    assert main.number == 0
    assert not is_unclear(titles, main)
    assert not looks_like_tv(titles)
    assert hold_reasons(titles, **flags()) == []


def test_main_title_tie_goes_to_larger_size_and_empty():
    assert main_title([]) is None
    assert main_title([T(0, 100, size=5), T(1, 100, size=9)]).number == 1


def test_decoy_playlists_unclear():
    titles = [T(0, 8164, segs="1,2,3"), T(1, 8162, segs="4,5,6")]
    assert is_unclear(titles, main_title(titles))
    same = [T(0, 8164, segs="1,2,3"), T(1, 8162, segs="1,2,3")]
    assert not is_unclear(same, main_title(same))


def test_widescreen_fullscreen_dvd_unclear():
    titles = [T(0, 6730), T(1, 6712)]
    assert is_unclear(titles, main_title(titles))


def test_tv_disc_with_play_all():
    titles = [T(0, 42 * 60), T(1, 44 * 60), T(2, 46 * 60), T(3, 43 * 60), T(4, 176 * 60)]
    assert looks_like_tv(titles)


def test_film_with_many_extras_not_tv():
    titles = [T(0, 120 * 60)] + [T(i, m * 60) for i, m in enumerate([5, 8, 12, 15, 18, 20], 1)]
    assert not looks_like_tv(titles)


def test_tv_blocked_by_unrelated_long_title():
    titles = [T(0, 42 * 60), T(1, 44 * 60), T(2, 46 * 60), T(3, 100 * 60)]
    assert not looks_like_tv(titles)


def test_reasons_order_and_flags():
    titles = [T(0, 7000, segs="1"), T(1, 6990, segs="2")]
    assert hold_reasons(
        titles, auto_start=False, hold_pressed=True, already_ripped="Film (2001)", from_insert=False
    ) == [
        "two titles of about the same length (decoy playlists?)",
        "Hold pressed",
        "auto-start is off",
        "already ripped (Film (2001))",
        "disc was in the drive at start-up",
    ]
    tv = [T(0, 42 * 60), T(1, 44 * 60), T(2, 46 * 60)]
    assert "looks like a TV disc" in hold_reasons(tv, **flags())
    assert hold_reasons([], **flags()) == ["no titles found"]


def test_should_eject():
    assert should_eject([(0, 5, "done")])
    assert should_eject([(0, 5, "failed"), (0, 7, "done")])
    assert not should_eject([(0, 5, "done"), (3, 6, "queued")])
    assert not should_eject([(0, 5, "failed")])
    assert not should_eject([(0, 5, "done"), (1, 6, "running")])
    assert not should_eject([])
