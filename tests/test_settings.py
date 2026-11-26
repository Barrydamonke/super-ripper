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
    "cd_rip_mode": "secure",
    "cd_retries": "10",
    "video_min_title_seconds": "120",
    "makemkv_stall_minutes": "10",
    "cd_approval": "odd_ones",
    "cd_cover_art": "on",
    "encode_slots": "1",
    "raw_keep_hours": "24",
    "encode_stall_minutes": "10",
    "radarr_url": "",
    "radarr_api_key": "",
    "ask_before_replace": "on",
    "tidy_folders_on_replace": "on",
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


def test_stored_value_above_max_is_clamped(conn):
    # Step 1 allowed a grace of up to 11 h; the guard must never exceed 3.5 h.
    conn.execute("INSERT INTO settings (key, value) VALUES ('shutdown_grace_hours', '8')")
    conn.commit()
    assert settings.get(conn, "shutdown_grace_hours") == 3.5
    assert settings.get_all(conn)["shutdown_grace_hours"] == 3.5


def test_url_with_bad_port_rejected():
    with pytest.raises(settings.SettingsError):
        settings.parse_form({**VALID_FORM, "schedule_status_url": "http://backend:30o1/status"})


def test_choice_setting():
    assert settings.parse_form({**VALID_FORM, "cd_rip_mode": "fast"})["cd_rip_mode"] == "fast"
    with pytest.raises(settings.SettingsError) as exc:
        settings.parse_form({**VALID_FORM, "cd_rip_mode": "turbo"})
    assert exc.value.errors["cd_rip_mode"] == "Must be one of: secure, fast."


def test_secret_blank_keeps_current():
    form = {**VALID_FORM, "jellyfin_api_key": ""}
    assert settings.parse_form(form, current={"jellyfin_api_key": "abc"})["jellyfin_api_key"] == "abc"
    form = {**VALID_FORM, "jellyfin_api_key": "  new  "}
    assert settings.parse_form(form, current={"jellyfin_api_key": "abc"})["jellyfin_api_key"] == "new"
    assert settings.parse_form({**VALID_FORM})["jellyfin_api_key"] == ""


def test_cd_defaults(conn):
    values = settings.get_all(conn)
    assert (values["cd_rip_mode"], values["cd_retries"], values["cd_approval"], values["cd_cover_art"]) == (
        "secure", 10, "odd_ones", True,
    )
    assert values["jellyfin_url"] == "" and values["jellyfin_api_key"] == ""


def test_text_setting_round_trip(conn):
    form = {**VALID_FORM, "video_track_selection": "  -sel:all,+sel:eng  "}
    settings.save(conn, settings.parse_form(form))
    assert settings.get(conn, "video_track_selection") == "-sel:all,+sel:eng"
    with pytest.raises(settings.SettingsError) as exc:
        settings.parse_form({**VALID_FORM, "video_track_selection": "a\nb"})
    assert "video_track_selection" in exc.value.errors
    with pytest.raises(settings.SettingsError):
        settings.parse_form({**VALID_FORM, "video_track_selection": "x" * 2001})
    assert settings.get(conn, "video_min_title_seconds") == 120
    assert settings.get(conn, "makemkv_key") == ""


def test_encoding_settings_defaults(conn):
    values = settings.get_all(conn)
    assert values["encode_slots"] == 1
    assert "encode_after_rip" not in values and "fast_quality" not in values  # presets own these now
    assert values["raw_keep_hours"] == 0
    assert values["encode_stall_minutes"] == 10
    by_key = {s.key: s for s in settings.SETTINGS}
    assert all(by_key[k].group == "Encoding" for k in (
        "encode_slots", "raw_keep_hours", "encode_stall_minutes"))
    assert (by_key["encode_slots"].minimum, by_key["encode_slots"].maximum) == (1, 2)
    assert (by_key["raw_keep_hours"].minimum, by_key["raw_keep_hours"].maximum) == (0, 720)
    assert (by_key["encode_stall_minutes"].minimum, by_key["encode_stall_minutes"].maximum) == (1, 240)
