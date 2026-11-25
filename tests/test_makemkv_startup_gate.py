"""Two MakeMKVs starting together both scan every drive, and one can hang ("Scanning CD-ROM
devices" then silence). Only one MakeMKV at a time may be in that start-up drive scan."""

import asyncio
import types

from disc_ripper import runner, videorip


class FakeLog:
    def __init__(self):
        self.lines = []

    def write(self, text):
        self.lines.append(text)


def fake_ctx(services, events, name, *, drv=True, fail=False, delay=0.05):
    async def run_process(argv, *, cwd, on_line, env=None, idle_timeout=None, log_filter=None):
        events.append(f"{name} start")
        await on_line('MSG:1005,0,1,"MakeMKV v2.0.0 started"')
        await on_line('PRGT:5018,0,"Scanning CD-ROM devices"')
        await asyncio.sleep(delay)
        if fail:
            raise runner.ProcessStalled(600)
        if drv:
            await on_line('DRV:0,2,999,1,"BD-RE","LABEL","/dev/sr0"')
            events.append(f"{name} drives listed")
        await asyncio.sleep(delay)
        events.append(f"{name} end")
        return 0

    return types.SimpleNamespace(run_process=run_process, services=services, log=FakeLog(),
                                 config=types.SimpleNamespace(work_dir=".", config_dir=None))


async def _noop(line):
    pass


def test_second_makemkv_waits_for_the_first_drive_scan(monkeypatch):
    monkeypatch.setattr(videorip, "scan_env", lambda config: {})
    events = []
    services = types.SimpleNamespace()

    async def main():
        a = fake_ctx(services, events, "A")
        b = fake_ctx(services, events, "B")
        await asyncio.gather(videorip._run_makemkv(a, ["x"], _noop, 10),
                             videorip._run_makemkv(b, ["x"], _noop, 10))
        return b

    b = asyncio.run(main())
    assert events.index("B start") > events.index("A drives listed")  # B waited for A's drive scan
    assert events.index("B start") < events.index("A end")  # but they still run side by side
    assert any("waiting for the other drive" in line for line in b.log.lines)


def test_gate_released_when_makemkv_fails_during_its_scan(monkeypatch):
    monkeypatch.setattr(videorip, "scan_env", lambda config: {})
    monkeypatch.setattr(videorip, "STALL_UNIT_SECONDS", 1)
    events = []
    services = types.SimpleNamespace()

    async def main():
        a = fake_ctx(services, events, "A", fail=True)
        b = fake_ctx(services, events, "B")
        results = await asyncio.gather(videorip._run_makemkv(a, ["x"], _noop, 10),
                                       videorip._run_makemkv(b, ["x"], _noop, 10), return_exceptions=True)
        return results

    first, second = asyncio.run(main())
    assert isinstance(first, RuntimeError) and second == 0
    assert "B end" in events


def test_gate_released_after_the_cap_if_no_drive_list(monkeypatch):
    monkeypatch.setattr(videorip, "scan_env", lambda config: {})
    monkeypatch.setattr(videorip, "STARTUP_GATE_SECONDS", 0.1)
    events = []
    services = types.SimpleNamespace()

    async def main():
        a = fake_ctx(services, events, "A", drv=False, delay=0.5)
        b = fake_ctx(services, events, "B", delay=0.01)
        await asyncio.gather(videorip._run_makemkv(a, ["x"], _noop, 10),
                             videorip._run_makemkv(b, ["x"], _noop, 10))

    asyncio.run(main())
    assert events.index("B start") < events.index("A end")  # the cap let B go while A was still silent


def test_lines_still_reach_the_caller(monkeypatch):
    monkeypatch.setattr(videorip, "scan_env", lambda config: {})
    seen = []

    async def on_line(line):
        seen.append(line[:4])

    asyncio.run(videorip._run_makemkv(fake_ctx(types.SimpleNamespace(), [], "A"), ["x"], on_line, 10))
    assert seen == ["MSG:", "PRGT", "DRV:"]
