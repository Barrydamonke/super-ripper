from disc_ripper.web import auth_routes

from .conftest import TEST_PASSWORD


def _setup(client, pw=TEST_PASSWORD):
    return client.post("/setup", data={"password": pw, "confirm": pw})


def test_healthz_open(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_static_open(client):
    r = client.get("/static/htmx.min.js")
    assert r.status_code == 200
    assert "htmx" in r.text[:2000]


def test_first_boot_redirects_to_setup(client, config):
    r = client.get("/drives")
    assert r.status_code == 303
    assert r.headers["location"] == "/setup"
    assert config.db_path.exists()
    assert config.secret_key_path.exists()
    r = client.get("/login")
    assert r.headers["location"] == "/setup"
    assert client.get("/setup").status_code == 200


def test_setup_validation(client):
    r = client.post("/setup", data={"password": "short", "confirm": "short"})
    assert r.status_code == 400
    assert "at least 8" in r.text
    r = client.post("/setup", data={"password": "long-enough-1", "confirm": "long-enough-2"})
    assert r.status_code == 400
    assert "do not match" in r.text
    assert client.get("/drives").headers["location"] == "/setup"


def test_setup_then_login_flow(client):
    r = _setup(client)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    assert client.get("/setup").headers["location"] == "/login"
    # posting setup again cannot overwrite the password
    r = client.post("/setup", data={"password": "attacker-pass", "confirm": "attacker-pass"})
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    r = client.get("/drives")
    assert r.headers["location"] == "/login?next=/drives"
    assert client.get("/login").status_code == 200
    r = client.post("/login", data={"password": TEST_PASSWORD, "next": "/drives"})
    assert r.status_code == 303
    assert r.headers["location"] == "/drives"
    assert "dr_session" in r.cookies
    assert client.get("/drives").status_code == 200


def test_wrong_password(client, monkeypatch):
    monkeypatch.setattr(auth_routes, "FAILED_LOGIN_DELAY", 0)
    _setup(client)
    r = client.post("/login", data={"password": "not-the-password", "next": "/"})
    assert r.status_code == 401
    assert "Wrong password." in r.text
    assert "dr_session" not in r.cookies


def test_open_redirect_blocked(client):
    _setup(client)
    for bad in ("//evil.example", "https://evil.example", r"/\evil.example", ""):
        client.cookies.clear()
        r = client.post("/login", data={"password": TEST_PASSWORD, "next": bad})
        assert r.headers["location"] == "/", bad


def test_htmx_request_gets_hx_redirect(client):
    _setup(client)
    r = client.post("/drives/rescan", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert r.headers["HX-Redirect"] == "/login"
    assert r.text == ""


def test_forged_cookie_rejected(client):
    _setup(client)
    client.cookies.set("dr_session", "forged")
    assert client.get("/drives").status_code == 303


def test_root_is_dashboard_when_logged_in(logged_in_client):
    r = logged_in_client.get("/")
    assert r.status_code == 200
    assert "Dashboard" in r.text


def test_logout(logged_in_client):
    r = logged_in_client.post("/logout")
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    r = logged_in_client.get("/drives")
    assert r.headers["location"] == "/login?next=/drives"


def test_cross_origin_post_rejected(logged_in_client):
    # Another web UI on the same box (other port) is same-site, so SameSite=Lax alone doesn't stop it.
    r = logged_in_client.post("/logout", headers={"Origin": "http://testserver:8080"})
    assert r.status_code == 403
    r = logged_in_client.post("/drives/rescan", headers={"Sec-Fetch-Site": "same-site"})
    assert r.status_code == 403
    r = logged_in_client.post("/login", data={"password": "x"}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    # still logged in: nothing above went through
    assert logged_in_client.get("/settings").status_code == 200


def test_same_origin_post_allowed(logged_in_client):
    r = logged_in_client.post(
        "/drives/rescan",
        headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
    )
    assert r.status_code == 200


def test_frames_denied(client):
    assert client.get("/setup").headers["X-Frame-Options"] == "DENY"
