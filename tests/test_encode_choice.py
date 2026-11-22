"""Choosing a rip's encode preset while it scans or rips."""
import asyncio

from disc_ripper import db, drives
from disc_ripper.services import Services

from .test_encode_services import encodes_of, set_setting
from .test_encodepool import FakeSpace  # noqa: F401
from .test_videorip_rip import make_services, probe, rip_state, run_until_done, scanned  # noqa: F401
from .test_videorip_scan import setup  # noqa: F401


def choice(conn):
    return conn.execute("SELECT encode_choice FROM drives WHERE id = 'serial:A'").fetchone()[0]


def test_choice_before_the_rip_is_copied_to_it(config, setup):
    assert asyncio.run(Services(config).set_encode_choice("serial:A", "fast")) is None
    services, rip = scanned(config, setup)
    assert rip_state(setup, rip)["encode_preset"] == "fast"
    run_until_done(config, setup, services, rip)
    [row] = encodes_of(setup)
    assert row["preset"] == "fast"


def test_changing_the_choice_mid_rip(config, setup):
    services, rip = scanned(config, setup)
    assert rip_state(setup, rip)["encode_preset"] is None  # no choice made: the setting decides
    assert asyncio.run(services.set_encode_choice("serial:A", "copy")) is None
    assert rip_state(setup, rip)["encode_preset"] == "copy"
    run_until_done(config, setup, services, rip)
    assert [r["preset"] for r in encodes_of(setup)] == ["copy"]


def test_dont_encode_this_rip(config, setup):
    services, rip = scanned(config, setup)
    asyncio.run(services.set_encode_choice("serial:A", "off"))
    run_until_done(config, setup, services, rip)
    assert encodes_of(setup) == []


def test_setting_still_decides_without_a_choice(config, setup):
    set_setting(setup, "encode_after_rip", "fast")
    services, rip = scanned(config, setup)
    run_until_done(config, setup, services, rip)
    assert [r["preset"] for r in encodes_of(setup)] == ["fast"]


def test_unknown_choice_refused(config, setup):
    assert asyncio.run(Services(config).set_encode_choice("serial:A", "turbo")) == "Unknown preset."
    assert asyncio.run(Services(config).set_encode_choice("serial:Z", "fast")) == "Unknown drive."
    assert choice(setup) is None


def test_choice_cleared_when_the_disc_leaves(config, setup):
    asyncio.run(Services(config).set_encode_choice("serial:A", "fast"))
    assert choice(setup) == "fast"
    drives.set_media(setup, "serial:A", "empty", None, "", "t")
    assert choice(setup) is None


def test_finished_rip_keeps_its_choice_when_the_next_disc_differs(config, setup):
    services, rip = scanned(config, setup)
    asyncio.run(services.set_encode_choice("serial:A", "fast"))
    run_until_done(config, setup, services, rip)
    drives.set_media(setup, "serial:A", "empty", None, "", "t")
    assert rip_state(setup, rip)["encode_preset"] == "fast"
