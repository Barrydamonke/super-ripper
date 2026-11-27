import asyncio

import pytest

from disc_ripper import db, drives, jobs, runner


class RecordingNotifier:
    def __init__(self):
        self.drives = []

    async def drive_changed(self, drive_id):
        self.drives.append(drive_id)

    async def guard_changed(self):
        pass


class FakeSpace:
    def __init__(self, answers=None):
        self.answers = list(answers or [])
        self.calls = []

    def check(self, needs_bytes, running_bytes, budget_gb, min_free_gb):
        self.calls.append((needs_bytes, running_bytes, budget_gb, min_free_gb))
        return self.answers.pop(0) if self.answers else None

    def invalidate(self):
        pass


@pytest.fixture
def conn(config, add_sg):
    add_sg(config.sysfs_root, 1, sr=0, vpd_serial=b"\x05\x80\x00\x01A")
    add_sg(config.sysfs_root, 2, sr=1, vpd_serial=b"\x05\x80\x00\x01B")
    c = db.connect(config.db_path)
    db.migrate(c)
    drives.sync(c, drives.discover(config.sysfs_root), "t0")
    yield c
    c.close()


def make_runner(config, space=None, handlers=None, **kw):
    return runner.JobRunner(
        config, RecordingNotifier(), space or FakeSpace(), handlers=handlers or runner.HANDLERS,
        time_scale=0.001, **kw,
    )


async def wait_until(predicate, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out waiting")
        await asyncio.sleep(0.005)


def states(conn, *ids):
    return [jobs.get(conn, i)["state"] for i in ids]


def test_runs_jobs_one_at_a_time_in_order(config, conn):
    a1 = jobs.enqueue(conn, "serial:A", "test", {"duration_s": 2}, now="t")
    a2 = jobs.enqueue(conn, "serial:A", "test", {"duration_s": 2}, now="t")
    seen_both_running = []

    async def main():
        r = make_runner(config)
        r.sync_workers({"serial:A"})

        def check():
            s = states(conn, a1, a2)
            if s == ["running", "running"]:
                seen_both_running.append(True)
            return s == ["done", "done"]

        await wait_until(check)
        await r.stop()

    asyncio.run(main())
    assert not seen_both_running
    first, second = jobs.get(conn, a1), jobs.get(conn, a2)
    assert second["started_at"] >= first["finished_at"]
    assert first["progress"] == 1.0


def test_two_drives_run_in_parallel(config, conn):
    async def slow(ctx):
        await asyncio.sleep(0.2)

    a = jobs.enqueue(conn, "serial:A", "slow", {}, now="t")
    b = jobs.enqueue(conn, "serial:B", "slow", {}, now="t")

    async def main():
        r = make_runner(config, handlers={"slow": slow})
        r.sync_workers({"serial:A", "serial:B"})
        await wait_until(lambda: states(conn, a, b) == ["running", "running"])
        await wait_until(lambda: states(conn, a, b) == ["done", "done"])
        await r.stop()

    asyncio.run(main())


def test_waiting_space_blocks_queue_then_runs(config, conn):
    big = jobs.enqueue(conn, "serial:A", "test", {"duration_s": 1}, needs_bytes=50, now="t")
    small = jobs.enqueue(conn, "serial:A", "test", {"duration_s": 1}, now="t")
    space = FakeSpace(["Waiting for cache space: needs 50"])

    async def main():
        r = make_runner(config, space=space, recheck_seconds=60)
        r.sync_workers({"serial:A"})
        await wait_until(lambda: states(conn, big)[0] == "waiting_space")
        assert jobs.get(conn, big)["message"] == "Waiting for cache space: needs 50"
        await asyncio.sleep(0.05)
        assert states(conn, small) == ["queued"]  # stays behind the waiting job
        r.wake("serial:A")
        await wait_until(lambda: states(conn, big, small) == ["done", "done"])
        await r.stop()

    asyncio.run(main())
    assert space.calls[0] == (50, 0, 60, 20)


def test_waiting_space_rechecks_on_timer(config, conn):
    big = jobs.enqueue(conn, "serial:A", "test", {"duration_s": 1}, needs_bytes=50, now="t")

    async def main():
        r = make_runner(config, space=FakeSpace(["wait"]), recheck_seconds=0.05)
        r.sync_workers({"serial:A"})
        await wait_until(lambda: states(conn, big) == ["done"])
        await r.stop()

    asyncio.run(main())


def test_cancel_running_job(config, conn):
    long = jobs.enqueue(conn, "serial:A", "test", {"duration_s": 100000}, now="t")
    nxt = jobs.enqueue(conn, "serial:A", "test", {"duration_s": 1}, now="t")

    async def main():
        r = make_runner(config)
        r.sync_workers({"serial:A"})
        await wait_until(lambda: states(conn, long) == ["running"])
        assert await r.cancel(long) is True
        await wait_until(lambda: states(conn, long, nxt) == ["cancelled", "done"])
        assert await r.cancel(long) is False
        await r.stop()

    asyncio.run(main())


def test_cancel_queued_job(config, conn):
    j = jobs.enqueue(conn, "serial:A", "test", {}, now="t")

    async def main():
        r = make_runner(config)
        assert await r.cancel(j) is True

    asyncio.run(main())
    assert states(conn, j) == ["cancelled"]


def test_handler_error_marks_failed(config, conn):
    async def boom(ctx):
        raise RuntimeError("boom")

    j = jobs.enqueue(conn, "serial:A", "boom", {}, now="t")
    unknown = jobs.enqueue(conn, "serial:A", "nonsense", {}, now="t")

    async def main():
        r = make_runner(config, handlers={"boom": boom})
        r.sync_workers({"serial:A"})
        await wait_until(lambda: states(conn, j, unknown) == ["failed", "failed"])
        await r.stop()

    asyncio.run(main())
    assert jobs.get(conn, j)["error"] == "boom"
    assert jobs.get(conn, unknown)["error"] == "Unknown job kind: nonsense"


def test_stop_marks_running_interrupted(config, conn):
    j = jobs.enqueue(conn, "serial:A", "test", {"duration_s": 100000}, now="t")

    async def main():
        r = make_runner(config)
        r.sync_workers({"serial:A"})
        await wait_until(lambda: states(conn, j) == ["running"])
        await r.stop()

    asyncio.run(main())
    row = jobs.get(conn, j)
    assert row["state"] == "interrupted"
    assert row["message"] == jobs.INTERRUPTED_MESSAGE


def test_sync_workers_removes_drive(config, conn):
    j = jobs.enqueue(conn, "serial:A", "test", {"duration_s": 100000}, now="t")

    async def main():
        r = make_runner(config)
        r.sync_workers({"serial:A"})
        await wait_until(lambda: states(conn, j) == ["running"])
        r.sync_workers(set())
        await wait_until(lambda: states(conn, j) == ["interrupted"])
        await r.stop()

    asyncio.run(main())


def test_progress_throttled(config, conn, monkeypatch):
    writes = []
    real = jobs.set_progress

    def counting(*args, **kwargs):
        writes.append(args[2])
        return real(*args, **kwargs)

    monkeypatch.setattr(jobs, "set_progress", counting)

    async def chatty(ctx):
        for i in range(100):
            await ctx.report(i / 100, f"step {i}")
        await ctx.report(1.0, "done")

    j = jobs.enqueue(conn, "serial:A", "chatty", {}, now="t")

    async def main():
        r = make_runner(config, handlers={"chatty": chatty})
        r.sync_workers({"serial:A"})
        await wait_until(lambda: states(conn, j) == ["done"])
        await r.stop()

    asyncio.run(main())
    assert len(writes) <= 3
    assert writes[-1] == 1.0


def test_test_job_reports_estimate(config, conn):
    j = jobs.enqueue(conn, "serial:A", "test", {"duration_s": 200}, now="t")

    async def main():
        r = make_runner(config)
        r.sync_workers({"serial:A"})
        await wait_until(lambda: jobs.get(conn, j)["estimated_finish"] is not None)
        row = jobs.get(conn, j)
        assert row["message"].startswith("Test job: 3m")
        await r.stop()

    asyncio.run(main())


class SlowSpace(FakeSpace):
    def __init__(self, delay, answers=None, raise_first=None):
        super().__init__(answers)
        self.delay = delay
        self.raise_first = raise_first

    def check(self, *args):
        import time

        time.sleep(self.delay)
        if self.raise_first is not None:
            error, self.raise_first = self.raise_first, None
            raise error
        return super().check(*args)


def test_cancel_while_worker_is_picking_up_job(config, conn):
    j = jobs.enqueue(conn, "serial:A", "test", {"duration_s": 2}, now="t")

    async def main():
        r = make_runner(config, space=SlowSpace(0.3))
        r.sync_workers({"serial:A"})
        await asyncio.sleep(0.1)  # the worker is inside the space check
        assert await r.cancel(j) is True
        await asyncio.sleep(0.6)
        await r.stop()

    asyncio.run(main())
    row = jobs.get(conn, j)
    assert row["state"] == "cancelled"
    assert row["started_at"] is None


def test_worker_survives_space_check_error(config, conn):
    j = jobs.enqueue(conn, "serial:A", "test", {"duration_s": 1}, now="t")

    async def main():
        r = make_runner(config, space=SlowSpace(0, raise_first=OSError(5, "EIO")), recheck_seconds=0.05)
        r.sync_workers({"serial:A"})
        await wait_until(lambda: states(conn, j) == ["done"])
        await r.stop()

    asyncio.run(main())


def test_worker_survives_notifier_error_after_start(config, conn):
    first = jobs.enqueue(conn, "serial:A", "test", {"duration_s": 1}, now="t")
    second = jobs.enqueue(conn, "serial:A", "test", {"duration_s": 1}, now="t")

    class FlakyNotifier(RecordingNotifier):
        failed = False

        async def drive_changed(self, drive_id):
            if not self.failed:
                self.failed = True
                raise RuntimeError("template blew up")

    async def main():
        r = runner.JobRunner(config, FlakyNotifier(), FakeSpace(), time_scale=0.001, recheck_seconds=0.05)
        r.sync_workers({"serial:A"})
        await wait_until(lambda: states(conn, second) == ["done"])
        await r.stop()

    asyncio.run(main())
    assert states(conn, first)[0] in ("done", "failed")


def test_space_check_and_start_are_atomic(config, conn, tmp_path):
    from types import SimpleNamespace

    from disc_ripper.space import GB, SpaceChecker

    a = jobs.enqueue(conn, "serial:A", "test", {"duration_s": 2}, needs_bytes=40 * GB, now="t")
    b = jobs.enqueue(conn, "serial:B", "test", {"duration_s": 2}, needs_bytes=40 * GB, now="t")
    real = SpaceChecker(
        tmp_path, statvfs=lambda p: SimpleNamespace(f_bavail=10**9, f_frsize=4096), size_fn=lambda p: 0
    )

    class Slowed:
        def check(self, *args):
            import time

            time.sleep(0.1)  # widen the race between the two drives
            return real.check(*args)

        def invalidate(self):
            real.invalidate()

    both_running = []

    async def main():
        r = make_runner(config, space=Slowed(), recheck_seconds=0.05)
        r.sync_workers({"serial:A", "serial:B"})

        def check():
            if states(conn, a, b) == ["running", "running"]:
                both_running.append(True)
            return states(conn, a, b) == ["done", "done"]

        await wait_until(check, timeout=10)
        await r.stop()

    asyncio.run(main())
    assert not both_running  # 40 + 40 GB never fits the default 60 GB budget


def test_cancel_between_start_and_task_creation(config, conn):
    j = jobs.enqueue(conn, "serial:A", "test", {"duration_s": 2}, now="t")

    class SlowNotifier(RecordingNotifier):
        async def drive_changed(self, drive_id):
            await asyncio.sleep(0.3)

    async def main():
        r = runner.JobRunner(config, SlowNotifier(), FakeSpace(), time_scale=0.001)
        r.sync_workers({"serial:A"})
        await wait_until(lambda: states(conn, j) == ["running"])
        assert await r.cancel(j) is True  # the handler task doesn't exist yet
        await wait_until(lambda: states(conn, j) == ["cancelled"], timeout=5)
        await r.stop()

    asyncio.run(main())


def test_cancel_running_job_returns_after_state_is_written(config, conn):
    j = jobs.enqueue(conn, "serial:A", "test", {"duration_s": 100000}, now="t")

    async def main():
        r = make_runner(config)
        r.sync_workers({"serial:A"})
        await wait_until(lambda: states(conn, j) == ["running"])
        assert await r.cancel(j) is True
        # The cancel route renders the card right after this; it must not show "running".
        assert states(conn, j) == ["cancelled"]
        await r.stop()

    asyncio.run(main())


def test_run_process_env(conn, config, tmp_path):
    import sys

    lines = []

    async def on_line(line):
        lines.append(line)

    async def main():
        r = runner.JobRunner(config, RecordingNotifier(), FakeSpace())
        ctx = runner.JobContext(r, 1, "serial:A", {})
        code = await ctx.run_process(
            [sys.executable, "-c", "import os; print(os.environ['X_TEST'])"],
            cwd=tmp_path, on_line=on_line, env={"X_TEST": "1"},
        )
        assert code == 0

    asyncio.run(main())
    assert lines == ["1"]


def test_run_process_logs_command_output_and_exit(conn, config, tmp_path):
    import sys

    async def on_line(line):
        pass

    async def main():
        r = runner.JobRunner(config, RecordingNotifier(), FakeSpace())
        ctx = runner.JobContext(r, 5, "serial:A", {})
        await ctx.run_process([sys.executable, "-c", "print('hello there')"], cwd=tmp_path, on_line=on_line)

    asyncio.run(main())
    text = (config.config_dir / "logs" / "job-5.log").read_text(encoding="utf-8")
    assert "$ " in text and "-c" in text
    assert "| hello there" in text
    assert "exit code 0" in text


def test_run_process_stops_a_silent_child(conn, config, tmp_path):
    import sys

    async def on_line(line):
        pass

    async def main():
        r = runner.JobRunner(config, RecordingNotifier(), FakeSpace())
        ctx = runner.JobContext(r, 6, "serial:A", {})
        with pytest.raises(runner.ProcessStalled) as stalled:
            await ctx.run_process(
                [sys.executable, "-c", "import time; print('working', flush=True); time.sleep(60)"],
                cwd=tmp_path, on_line=on_line, idle_timeout=0.5,
            )
        assert stalled.value.seconds == 0.5

    asyncio.run(main())
    assert not runner.LIVE_PROCESSES
    text = (config.config_dir / "logs" / "job-6.log").read_text(encoding="utf-8")
    assert "| working" in text and "no output for" in text


def test_job_log_has_start_and_outcome(config, conn):
    async def boom(ctx):
        await ctx.report(0.5, "half")
        raise RuntimeError("boom")

    j = jobs.enqueue(conn, "serial:A", "boom", {"title": 3}, now="t")

    async def main():
        r = make_runner(config, handlers={"boom": boom})
        r.sync_workers({"serial:A"})
        await wait_until(lambda: states(conn, j) == ["failed"])
        await r.stop()

    asyncio.run(main())
    text = (config.config_dir / "logs" / f"job-{j}.log").read_text(encoding="utf-8")
    assert f"job #{j} (boom) started on serial:A" in text
    assert '"title": 3' in text
    assert "progress 50%" in text
    assert text.rstrip().endswith("finished: failed — boom")


def _real_space(config, downloads_free_gb, work_free_gb=1000, used_gb=0):
    from types import SimpleNamespace

    from disc_ripper import space

    def statvfs(path):
        free = downloads_free_gb if str(path) == str(config.downloads_dir) else work_free_gb
        return SimpleNamespace(f_bavail=int(free * space.GB / 4096), f_frsize=4096)

    return space.SpaceChecker(config.work_dir, statvfs=statvfs, size_fn=lambda p: int(used_gb * space.GB))


def test_rip_space_checked_on_downloads_not_work_budget(config, conn):
    from disc_ripper.space import GB

    # A 70 GB rip with the default 60 GB work budget (and /work nearly full) starts when downloads has room.
    j = jobs.enqueue(conn, "serial:A", "rip_title", {"rip_id": 1, "title": 0}, needs_bytes=70 * GB,
                     uses_drive=True, now="t")
    r = make_runner(config, space=_real_space(config, downloads_free_gb=500, work_free_gb=1, used_gb=59))
    assert r._space_check(conn, 70 * GB, "rip_title") is None
    # The same size as any other kind of job still hits the budget.
    assert "work area budget" in r._space_check(conn, 70 * GB, "test")
    assert j  # (the job row only documents the scenario)


def test_rip_waits_for_downloads_space(config, conn):
    from disc_ripper.space import GB

    r = make_runner(config, space=_real_space(config, downloads_free_gb=75))
    msg = r._space_check(conn, 70 * GB, "rip_title")  # default floor: 20 GB spare
    assert msg == "Waiting for space on the downloads disk: needs 70.0 GB, 75.0 GB free (keeping 20 GB spare)."
    # A running rip's not-yet-written bytes count against the free space.
    other = jobs.enqueue(conn, "serial:B", "rip_title", {"rip_id": 2, "title": 0}, needs_bytes=10 * GB, now="t")
    jobs.start(conn, other, "t")
    r2 = make_runner(config, space=_real_space(config, downloads_free_gb=105))
    assert r2._space_check(conn, 70 * GB, "rip_title") is None
    jobs.finish(conn, other, "done", "t")
    running = jobs.enqueue(conn, "serial:B", "rip_title", {"rip_id": 3, "title": 0}, needs_bytes=20 * GB, now="t")
    jobs.start(conn, running, "t")
    assert "Waiting for space on the downloads disk" in r2._space_check(conn, 70 * GB, "rip_title")


def test_terminate_gives_up_when_child_will_not_die(monkeypatch):
    monkeypatch.setattr(runner, "KILL_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(runner, "KILL_WAIT_SECONDS", 0.05)

    class Stuck:
        returncode = None
        pid = 4242

        def terminate(self):
            pass

        def kill(self):
            pass

        async def wait(self):
            await asyncio.Event().wait()  # never returns

    async def go():
        await asyncio.wait_for(runner._terminate(Stuck(), False), 5)

    with pytest.raises(runner.ProcessUnkillable, match="could not be stopped; eject the disc or restart"):
        asyncio.run(go())
