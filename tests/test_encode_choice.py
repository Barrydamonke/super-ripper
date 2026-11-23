"""Choosing a rip's encode preset while it scans or rips."""
import asyncio

from disc_ripper import db, drives, presets
from disc_ripper.services import Services

from .test_encode_services import encodes_of
from .test_encodepool import FakeSpace  # noqa: F401
from .test_videorip_rip import make_services, probe, rip_state, run_until_done, scanned  # noqa: F401
from .test_videorip_scan import setup  # noqa: F401


def choice(conn):
    return conn.execute("SELECT encode_choice FROM drives WHERE id = 'serial:A'").fetchone()[0]


def test_choice_before_the_rip_is_copied_to_it(config, setup):
    assert asyncio.run(Services(config).set_encode_choice("serial:A", "2")) is None
    services, rip = scanned(config, setup)
    assert rip_state(setup, rip)["encode_preset"] == "2"
    run_until_done(config, setup, services, rip)
    [row] = encodes_of(setup)
    assert (row["preset_id"], row["preset_name"]) == (2, "Fast")


def test_changing_the_choice_mid_rip(config, setup):
    services, rip = scanned(config, setup)
    assert rip_state(setup, rip)["encode_preset"] is None  # no choice made: the after-rip default decides
    assert asyncio.run(services.set_encode_choice("serial:A", "3")) is None
    assert rip_state(setup, rip)["encode_preset"] == "3"
    run_until_done(config, setup, services, rip)
    assert [r["preset_id"] for r in encodes_of(setup)] == [3]


def test_dont_encode_this_rip(config, setup):
    services, rip = scanned(config, setup)
    asyncio.run(services.set_encode_choice("serial:A", "off"))
    run_until_done(config, setup, services, rip)
    assert encodes_of(setup) == []


def test_default_still_decides_without_a_choice(config, setup):
    presets.set_default_choice(setup, "2")
    services, rip = scanned(config, setup)
    run_until_done(config, setup, services, rip)
    assert [r["preset_id"] for r in encodes_of(setup)] == [2]


def test_unknown_choice_refused(config, setup):
    assert asyncio.run(Services(config).set_encode_choice("serial:A", "99")) == "Unknown preset."
    assert asyncio.run(Services(config).set_encode_choice("serial:Z", "2")) == "Unknown drive."
    assert choice(setup) is None


def test_choice_cleared_when_the_disc_leaves(config, setup):
    asyncio.run(Services(config).set_encode_choice("serial:A", "2"))
    assert choice(setup) == "2"
    drives.set_media(setup, "serial:A", "empty", None, "", "t")
    assert choice(setup) is None


def test_finished_rip_keeps_its_choice_when_the_next_disc_differs(config, setup):
    services, rip = scanned(config, setup)
    asyncio.run(services.set_encode_choice("serial:A", "2"))
    run_until_done(config, setup, services, rip)
    drives.set_media(setup, "serial:A", "empty", None, "", "t")
    assert rip_state(setup, rip)["encode_preset"] == "2"
