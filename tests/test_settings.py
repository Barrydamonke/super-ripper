import pytest

from disc_ripper import db, settings


@pytest.fixture
def conn(config):
    c = db.connect(config.db_path)
    db.migrate(c)
    yield c
    c.close()


VALID_FORM = {
    "work_budget_gb": "100",
    "cache_min_free_gb": "20",
    "shutdown_grace_hours": "2.5",
    "raw_retention_hours": "24",
    "iso_dir": "/data/ISOs",
}


def test_defaults_when_empty(conn):
    values = settings.get_all(conn)
    assert values["work_budget_gb"] == 60
    assert values["cache_min_free_gb"] == 20
    assert values["auto_start_rips"] is True
    assert values["shutdown_grace_hours"] == 3
    assert values["raw_retention_hours"] == 24
    assert values["iso_dir"] == "/data/ISOs"


def test_parse_form_valid():
    values = settings.parse_form(VALID_FORM)
    assert values["auto_start_rips"] is False
    assert values["shutdown_grace_hours"] == 2.5
    assert values["work_budget_gb"] == 100
    assert settings.parse_form({**VALID_FORM, "auto_start_rips": "on"})["auto_start_rips"] is True


def test_parse_form_collects_all_errors():
    form = {**VALID_FORM, "work_budget_gb": "abc", "cache_min_free_gb": "-1", "iso_dir": "data/../x"}
    with pytest.raises(settings.SettingsError) as exc:
        settings.parse_form(form)
    assert set(exc.value.errors) == {"work_budget_gb", "cache_min_free_gb", "iso_dir"}


def test_parse_form_rejects_dotdot_in_absolute_path():
    with pytest.raises(settings.SettingsError) as exc:
        settings.parse_form({**VALID_FORM, "iso_dir": "/data/../etc"})
    assert set(exc.value.errors) == {"iso_dir"}


def test_save_round_trip(conn):
    settings.save(conn, settings.parse_form(VALID_FORM))
    assert settings.get(conn, "work_budget_gb") == 100
    assert settings.get(conn, "auto_start_rips") is False
    with pytest.raises(KeyError):
        settings.get(conn, "nope")


def test_internal_keys_hidden(conn):
    settings.set_internal(conn, "internal.x", 1)
    assert settings.get_internal(conn, "internal.x") == 1
    assert settings.get_internal(conn, "internal.missing", "d") == "d"
    assert "internal.x" not in settings.get_all(conn)
    with pytest.raises(ValueError):
        settings.set_internal(conn, "x", 1)


@pytest.mark.parametrize(
    "raw,expected",
    [("", ""), ("  ", ""), ("http://10.0.0.5:3001/status", "http://10.0.0.5:3001/status"),
     (" https://pi.local:8090/schedule ", "https://pi.local:8090/schedule")],
)
def test_url_setting_valid(raw, expected):
    assert settings.parse_form({**VALID_FORM, "schedule_status_url": raw})["schedule_status_url"] == expected


@pytest.mark.parametrize("raw", ["ftp://x", "10.0.0.5:3001", "http://", "http://a b"])
def test_url_setting_invalid(raw):
    with pytest.raises(settings.SettingsError) as exc:
        settings.parse_form({**VALID_FORM, "schedule_pi_url": raw})
    assert exc.value.errors == {"schedule_pi_url": "Must be blank or an http:// or https:// URL."}


def test_schedule_urls_default_blank(conn):
    values = settings.get_all(conn)
    assert values["schedule_status_url"] == ""
    assert values["schedule_pi_url"] == ""


def test_grace_max():
    assert settings.parse_form({**VALID_FORM, "shutdown_grace_hours": "3.5"})["shutdown_grace_hours"] == 3.5
    with pytest.raises(settings.SettingsError) as exc:
        settings.parse_form({**VALID_FORM, "shutdown_grace_hours": "4"})
    assert exc.value.errors["shutdown_grace_hours"] == "Must be at most 3.5."
