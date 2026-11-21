from disc_ripper import joblog


def read(path):
    return path.read_text(encoding="utf-8")


def test_output_keeps_messages_and_drops_progress_noise(tmp_path):
    log = joblog.JobLog(tmp_path / "job-1.log")
    log.output('MSG:5011,0,0,"Operation successfully completed","x"')
    log.output("PRGV:100,200,65536")
    log.output('SINFO:1,2,1,6202,"Audio"')
    log.output("Ripping and encoding track 3, progress - 41.50%, ETA - 2m")
    log.output("   ")
    log.output("Track 3 ripped and encoded successfully!")
    text = read(tmp_path / "job-1.log")
    assert "| MSG:5011" in text and "| Track 3 ripped" in text
    assert "PRGV" not in text and "SINFO" not in text and "progress - 41.50%" not in text
    assert len(text.splitlines()) == 2


def test_progress_milestones_every_ten_percent(tmp_path):
    log = joblog.JobLog(tmp_path / "job-1.log")
    for fraction in (0.0, 0.05, 0.12, 0.13, 0.35, 0.99, 1.0):
        log.progress(fraction)
    lines = [line.split(" ", 2)[2] for line in read(tmp_path / "job-1.log").splitlines()]
    assert lines == ["progress 10%", "progress 30%", "progress 90%", "progress 100%"]


def test_lines_are_timestamped(tmp_path):
    log = joblog.JobLog(tmp_path / "job-1.log")
    log.write("hello")
    date, time_, rest = read(tmp_path / "job-1.log").strip().split(" ", 2)
    assert len(date) == 10 and len(time_) == 8 and rest == "hello"


def test_writing_never_raises(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    log = joblog.JobLog(blocker / "logs" / "job-1.log")  # parent is a file: can't be created
    log.write("ignored")
    log.output("ignored")
    log.progress(0.5)
    log.attach("cyanrip", tmp_path / "missing.log")


def test_attach_copies_a_tool_log(tmp_path):
    src = tmp_path / "log.log"
    src.write_text("Tracks ripped accurately: 13/13\n", encoding="utf-8")
    log = joblog.JobLog(tmp_path / "logs" / "job-7.log")
    log.attach("cyanrip", src)
    assert read(tmp_path / "logs" / "job-7-cyanrip.log") == "Tracks ripped accurately: 13/13\n"
    assert "job-7-cyanrip.log" in read(tmp_path / "logs" / "job-7.log")


def test_path_for(config):
    assert joblog.path_for(config, 42) == config.config_dir / "logs" / "job-42.log"
