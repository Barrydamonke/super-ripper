import asyncio
import json
import shutil

from disc_ripper import jobs, video_rips

from .test_videorip_rip import make_services, probe, rip_state, run_free  # noqa: F401
from .test_videorip_scan import finished, rip_jobs, run, scan_job, setup  # noqa: F401


def scanned_disc(config, setup):
    """Scan the disc without auto-starting (as if it was in at start-up); return (services, fingerprint)."""
    j = scan_job(setup, from_insert=False)
    services = make_services(config)
    run(config, services, finished(setup, j))
    assert rip_jobs(setup) == []
    return services, video_rips.disc_in_drive(setup, "serial:A")["fingerprint"]


def start(services, fp, **kw):
    kw.setdefault("main", 0)
    kw.setdefault("extras", [])
    kw.setdefault("again", False)
    return asyncio.run(services.start_video_rip("serial:A", fp, **kw))


def params_of(conn):
    return [json.loads(r["params"]) for r in conn.execute("SELECT params FROM jobs WHERE kind='rip_title' ORDER BY id")]


def test_start_with_extras_order(config, setup):
    services, fp = scanned_disc(config, setup)
    rip = start(services, fp, main=0, extras=[2, 1, 2, 0])
    ps = params_of(setup)
    assert [(p["title"], p["role"]) for p in ps] == [(0, "main"), (2, "extra"), (1, "extra")]
    assert {p["rip_id"] for p in ps} == {rip} == {rip_jobs(setup)[0]["id"]}
    assert rip_state(setup, rip)["main_title"] == 0


def test_start_refuses_unknown_titles(config, setup):
    services, fp = scanned_disc(config, setup)
    assert start(services, fp, main=9) is None
    assert start(services, fp, main=0, extras=[7]) is None
    assert start(services, fp, main=None, extras=[]) is None
    assert rip_jobs(setup) == []


def test_manual_start_clears_hold(config, setup):
    services, fp = scanned_disc(config, setup)
    with setup:
        setup.execute("UPDATE drives SET video_hold = 1")
    assert start(services, fp) is not None
    assert setup.execute("SELECT video_hold FROM drives").fetchone()[0] == 0


def test_tv_titles_are_peers(config, setup):
    services, fp = scanned_disc(config, setup)
    with setup:  # the fake writes 6730 s for every title
        scan = json.loads(setup.execute("SELECT scan FROM video_discs").fetchone()[0])
        for t in scan["titles"]:
            t["seconds"] = 6730
        setup.execute("UPDATE video_discs SET scan = ?", (json.dumps(scan),))
    rip = start(services, fp, main=None, extras=[1, 2, 0])
    assert {p["role"] for p in params_of(setup)} == {"title"}
    assert rip_state(setup, rip)["main_title"] is None
    run_free(config, services, lambda: all(r["state"] == "done" for r in rip_jobs(setup)))
    folder = config.downloads_dir / str(rip)
    assert sorted(p.name for p in folder.glob("*.mkv")) == ["title_t00.mkv", "title_t01.mkv", "title_t02.mkv"]
    assert not (folder / "extras").exists()


def test_double_start_refused(config, setup):
    services, fp = scanned_disc(config, setup)

    async def both():
        return await asyncio.gather(
            services.start_video_rip("serial:A", fp, main=0, extras=[1], again=False),
            services.start_video_rip("serial:A", fp, main=0, extras=[1], again=False))

    results = asyncio.run(both())
    assert sorted(r is None for r in results) == [False, True]
    assert len(rip_jobs(setup)) == 2


def test_already_ripped_needs_again(config, setup):
    services, fp = scanned_disc(config, setup)
    rip = start(services, fp)
    assert start(services, fp, again=True) is None  # still active: blocks even with again
    with setup:
        setup.execute("UPDATE jobs SET state = 'done'")
    video_rips.set_state(setup, rip, "ripped", "", "t")
    (config.downloads_dir / str(rip)).mkdir(parents=True)
    assert start(services, fp, again=False) is None
    again = start(services, fp, again=True)
    assert again is not None and again != rip
    with setup:
        setup.execute("UPDATE jobs SET state = 'done'")
    video_rips.set_state(setup, again, "ripped", "", "t")


def test_deleted_downloads_folder_unblocks(config, setup):
    services, fp = scanned_disc(config, setup)
    rip = start(services, fp)
    with setup:
        setup.execute("UPDATE jobs SET state = 'done'")
    video_rips.set_state(setup, rip, "ripped", "", "t")
    (config.downloads_dir / str(rip)).mkdir(parents=True)
    assert start(services, fp) is None
    shutil.rmtree(config.downloads_dir / str(rip))
    assert start(services, fp) is not None


def test_failed_rip_does_not_block(config, setup):
    services, fp = scanned_disc(config, setup)
    rip = start(services, fp)
    with setup:
        setup.execute("UPDATE jobs SET state = 'failed'")
    video_rips.set_state(setup, rip, "failed", "boom", "t")
    assert start(services, fp, again=False) is not None


def test_add_extra_once(config, setup):
    services, fp = scanned_disc(config, setup)
    rip = start(services, fp)
    extra = asyncio.run(services.add_video_extra(rip, 1))
    assert extra is not None
    p = json.loads(jobs.get(setup, extra)["params"])
    assert (p["rip_id"], p["role"], p["title"]) == (rip, "extra", 1)
    assert asyncio.run(services.add_video_extra(rip, 1)) is None
    assert asyncio.run(services.add_video_extra(rip, 0)) is None  # the main is already queued
    assert asyncio.run(services.add_video_extra(rip, 9)) is None  # not on the disc


def test_add_extra_to_tv_rip_is_a_title(config, setup):
    services, fp = scanned_disc(config, setup)
    rip = start(services, fp, main=None, extras=[1])
    extra = asyncio.run(services.add_video_extra(rip, 2))
    assert json.loads(jobs.get(setup, extra)["params"])["role"] == "title"


def test_add_extra_needs_this_disc(config, setup):
    services, fp = scanned_disc(config, setup)
    rip = start(services, fp)
    with setup:
        setup.execute("UPDATE jobs SET state = 'done'")
        setup.execute("UPDATE video_discs SET drive_id = NULL")
    assert asyncio.run(services.add_video_extra(rip, 1)) is None  # nothing active, disc not in the drive
    with setup:
        setup.execute("UPDATE video_discs SET drive_id = 'serial:A'")
    assert asyncio.run(services.add_video_extra(rip, 1)) is not None
    assert rip_state(setup, rip)["state"] == "ripping"


def test_set_as_film(config, setup):
    services, fp = scanned_disc(config, setup)
    video_rips.set_as_film(setup, fp, True)
    assert setup.execute("SELECT as_film FROM video_discs").fetchone()[0] == 1
