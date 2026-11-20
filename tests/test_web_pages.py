from .conftest import TEST_PASSWORD

SERIAL = b"\x05\x80\x00\x06ABC123"
VALID = {
    "work_budget_gb": "100",
    "cache_min_free_gb": "20",
    "auto_start_rips": "on",
    "shutdown_grace_hours": "3",
    "raw_retention_hours": "24",
    "iso_dir": "/data/ISOs",
    "cd_rip_mode": "secure",
    "cd_retries": "10",
    "video_min_title_seconds": "120",
    "cd_approval": "odd_ones",
    "cd_cover_art": "on",
}


def test_drives_page_lists_drive(logged_in_client, config, add_sg):
    add_sg(config.sysfs_root, 1, vpd_serial=SERIAL)
    r = logged_in_client.post("/drives/rescan", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "BD-RE WH16NS40" in r.text
    assert "serial:ABC123" in r.text
    assert "<html" not in r.text  # partial only
    page = logged_in_client.get("/drives")
    assert page.status_code == 200
    assert "serial:ABC123" in page.text
    assert "/static/htmx.min.js" in page.text


def test_drives_page_empty(logged_in_client):
    r = logged_in_client.get("/drives")
    assert r.status_code == 200
    assert "No optical drives found" in r.text


def test_drive_page_shows_node_problems(logged_in_client, config, add_sg):
    add_sg(config.sysfs_root, 1, sr=0, vpd_serial=SERIAL)
    r = logged_in_client.post("/drives/rescan")
    assert "Not passed into the container" in r.text
    (config.dev_root / "sg1").write_text("")
    (config.dev_root / "sr0").write_text("")
    r = logged_in_client.post("/drives/rescan")
    assert "Not passed into the container" not in r.text


def test_absent_drive_marked(logged_in_client, config, add_sg):
    sg = add_sg(config.sysfs_root, 1, vpd_serial=SERIAL)
    logged_in_client.post("/drives/rescan")
    (sg / "device" / "type").write_text("0\n")  # no longer an optical drive
    r = logged_in_client.post("/drives/rescan")
    assert "Not detected at last scan." in r.text


def test_update_drive_label(logged_in_client, config, add_sg):
    add_sg(config.sysfs_root, 1, vpd_serial=SERIAL)
    logged_in_client.post("/drives/rescan")
    r = logged_in_client.post(
        "/drives/update", data={"drive_id": "serial:ABC123", "label": "Blu-ray", "enabled": "on"}
    )
    assert r.status_code == 200
    assert 'value="Blu-ray"' in r.text
    r = logged_in_client.post("/drives/rescan")
    assert 'value="Blu-ray"' in r.text


def test_update_drive_escapes_label(logged_in_client, config, add_sg):
    add_sg(config.sysfs_root, 1, vpd_serial=SERIAL)
    logged_in_client.post("/drives/rescan")
    r = logged_in_client.post(
        "/drives/update", data={"drive_id": "serial:ABC123", "label": '"><script>x</script>'}
    )
    assert r.status_code == 200
    assert "<script>x</script>" not in r.text
    assert "&lt;script&gt;x&lt;/script&gt;" in r.text


def test_update_unknown_drive(logged_in_client):
    r = logged_in_client.post("/drives/update", data={"drive_id": "serial:nope", "label": "x"})
    assert r.status_code == 404
    assert "Unknown drive." in r.text


def test_settings_page_shows_defaults(logged_in_client):
    r = logged_in_client.get("/settings")
    assert r.status_code == 200
    assert 'name="work_budget_gb"' in r.text
    assert 'value="60"' in r.text
    assert 'value="/data/ISOs"' in r.text


def test_settings_save_and_errors(logged_in_client):
    r = logged_in_client.post("/settings", data=VALID)
    assert r.status_code == 200
    assert "Settings saved." in r.text
    assert 'value="100"' in logged_in_client.get("/settings").text

    bad = {**VALID, "work_budget_gb": "abc", "iso_dir": "/somewhere/else"}
    r = logged_in_client.post("/settings", data=bad)
    assert r.status_code == 400
    assert "Must be a whole number." in r.text
    assert 'value="abc"' in r.text
    assert 'value="/somewhere/else"' in r.text
    assert "Settings saved." not in r.text
    # nothing from the bad post was stored
    assert 'value="100"' in logged_in_client.get("/settings").text


def test_settings_never_shows_secrets(logged_in_client, config):
    r = logged_in_client.get("/settings")
    assert r.status_code == 200
    assert "scrypt$" not in r.text
    assert config.secret_key_path.read_text().strip() not in r.text


def test_change_password_keeps_this_session(logged_in_client):
    old_cookie = logged_in_client.cookies["dr_session"]
    r = logged_in_client.post(
        "/settings/password",
        data={"current": TEST_PASSWORD, "new": "new-password-1", "confirm": "new-password-1"},
    )
    assert r.status_code == 200
    assert "Password changed." in r.text
    assert logged_in_client.cookies["dr_session"] != old_cookie
    assert logged_in_client.get("/settings").status_code == 200

    logged_in_client.cookies.set("dr_session", old_cookie)
    assert logged_in_client.get("/settings").status_code == 303


def test_change_password_errors(logged_in_client):
    r = logged_in_client.post(
        "/settings/password", data={"current": "wrong", "new": "new-password-1", "confirm": "new-password-1"}
    )
    assert r.status_code == 400
    assert "Current password is wrong." in r.text
    r = logged_in_client.post(
        "/settings/password", data={"current": TEST_PASSWORD, "new": "short", "confirm": "short"}
    )
    assert r.status_code == 400
    assert "at least 8" in r.text
    r = logged_in_client.post(
        "/settings/password", data={"current": TEST_PASSWORD, "new": "new-password-1", "confirm": "other-pass-1"}
    )
    assert r.status_code == 400
    assert "do not match" in r.text
    # the original password still works
    assert logged_in_client.get("/settings").status_code == 200


def test_secret_never_rendered(logged_in_client):
    r = logged_in_client.post("/settings", data={**VALID, "jellyfin_api_key": "s3cr3t-key"})
    assert r.status_code == 200
    assert "s3cr3t-key" not in r.text
    page = logged_in_client.get("/settings").text
    assert "s3cr3t-key" not in page
    assert "Set — leave blank to keep" in page
    # saving again with the field blank keeps it
    logged_in_client.post("/settings", data=VALID)
    from disc_ripper import db, settings

    conn = db.connect(logged_in_client.app.state.config.db_path)
    try:
        assert settings.get(conn, "jellyfin_api_key") == "s3cr3t-key"
    finally:
        conn.close()


def test_read_offset_update(logged_in_client, config, add_sg):
    add_sg(config.sysfs_root, 1, vpd_serial=SERIAL)
    logged_in_client.post("/drives/rescan")
    r = logged_in_client.post(
        "/drives/update", data={"drive_id": "serial:ABC123", "label": "BD", "enabled": "on", "read_offset": "667"}
    )
    assert r.status_code == 200
    assert 'value="667"' in r.text
    r = logged_in_client.post(
        "/drives/update", data={"drive_id": "serial:ABC123", "label": "BD", "enabled": "on", "read_offset": "99999"}
    )
    assert r.status_code == 400
    assert 'value="667"' in r.text


def test_settings_page_shows_groups(logged_in_client):
    text = logged_in_client.get("/settings").text
    assert text.count("<h2>DVD and Blu-ray</h2>") == 1
    assert text.index("<h2>DVD and Blu-ray</h2>") < text.index('name="makemkv_key"')
    assert "app_DefaultSelectionString" in text and "--profile" not in text
