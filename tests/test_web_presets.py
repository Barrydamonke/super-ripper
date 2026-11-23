"""The Presets page: list, edit form, greying and preview, the After ripping default."""

import pytest
from fastapi.testclient import TestClient

from disc_ripper import db, presets
from disc_ripper.presets import PresetSettings
from disc_ripper.app import create_app

from .conftest import TEST_PASSWORD


@pytest.fixture
def client(config):
    with TestClient(create_app(config), follow_redirects=False) as c:
        assert c.post("/setup", data={"password": TEST_PASSWORD, "confirm": TEST_PASSWORD}).status_code == 303
        assert c.post("/login", data={"password": TEST_PASSWORD, "next": "/"}).status_code == 303
        yield c


@pytest.fixture
def conn(client, config):
    c = db.connect(config.db_path)
    yield c
    c.close()


def form_for(i, **changes):
    name, d = presets.SEEDS[i]
    out = {"name": name, **{k: v for k, v in d.items() if k not in ("audio_languages", "subtitle_languages",
                                                                    "keep_commentary", "max_height")}}
    out["audio_languages"] = ", ".join(d["audio_languages"])
    out["subtitle_languages"] = ", ".join(d["subtitle_languages"])
    out["max_height"] = "none"
    return {**{k: str(v) for k, v in out.items()}, **{k: str(v) for k, v in changes.items()}}


def fast_form(**changes):
    return form_for(2, **changes)


def test_list_shows_seeds_and_rough_estimates(client):
    r = client.get("/presets")
    assert "Standard" in r.text and "Copy only" in r.text and "(rough)" in r.text


def test_nav_link(client):
    assert 'href="/presets"' in client.get("/").text


def test_copy_prefills(client):
    r = client.get("/presets/new?from=2")
    assert 'value="Fast copy"' in r.text


def test_save_new_and_edit(client, conn):
    r = client.post("/presets/save", data=form_for(1, name="Anime", audio_languages="jpn, eng"))
    assert r.status_code == 303 and r.headers["location"] == "/presets"
    assert "Anime" in client.get("/presets").text
    anime = next(p for p in presets.list_presets(conn) if p.name == "Anime")
    assert anime.settings.audio_languages == ("jpn", "eng")
    r = client.post("/presets/save", data={**form_for(1, name="Anime", audio_languages="jpn, eng", quality_bluray=22),
                                           "id": str(anime.id)})
    assert r.status_code == 303
    again = presets.get_preset(conn, anime.id)
    assert again.settings.quality_bluray == 22 and len(presets.list_presets(conn)) == 4


def test_save_rejects_duplicate_and_bad_combo(client):
    r = client.post("/presets/save", data=form_for(1, name="standard"))
    assert r.status_code == 200 and "already" in r.text and 'value="standard"' in r.text
    r = client.post("/presets/save", data=form_for(1, name="Odd", engine="handbrake", mode="hardware"))
    assert r.status_code == 200 and "Needs ffmpeg" in r.text and 'value="Odd"' in r.text


def test_save_keeps_omitted_disabled_fields(client, conn):
    before = presets.get_preset(conn, 2).settings
    data = fast_form(name="Fast", quality_dvd=20)
    del data["speed"]  # browsers don't post disabled inputs
    assert client.post("/presets/save", data={**data, "id": "2"}).status_code == 303
    after = presets.get_preset(conn, 2).settings
    assert after.speed == before.speed and after.quality_dvd == 20


def test_preview_greys_and_shows_command(client):
    r = client.post("/presets/preview", data={**fast_form(), "codec": "h264_vaapi"})
    assert "h264_vaapi" in r.text and "-qp 24" in r.text and "disabled" in r.text and "no speed setting" in r.text
    assert "French" not in r.text.split("Keeps:")[1].split("</")[0]  # Fast keeps English only


def test_preview_shows_error_inline(client):
    r = client.post("/presets/preview", data=fast_form(engine="handbrake"))
    assert r.status_code == 200 and "Needs ffmpeg" in r.text and "Keeps:" not in r.text


def test_delete_refused_in_use(client, conn):
    assert presets.set_default_choice(conn, "2") is None
    r = client.post("/presets/2/delete")
    assert "Fast is in use by After ripping" in r.text
    assert presets.get_preset(conn, 2) is not None


def test_delete_unused(client, conn):
    pid = presets.create_preset(conn, "Temp", presets.STANDARD, "2026-01-01T00:00:00+00:00")
    assert client.post(f"/presets/{pid}/delete").status_code == 303
    assert presets.get_preset(conn, pid) is None


def test_after_ripping_default(client, conn):
    client.post("/presets/default", data={"choice": "off"})
    assert presets.default_choice(conn) == "off"


def test_edit_form_wiring_and_ids(client):
    import re
    html = client.get("/presets/1").text
    assert 'hx-post="/presets/preview"' in html and 'hx-trigger="change"' in html and 'hx-sync="this:replace"' in html
    form = html.split('id="preset-form"')[1].split("</form>")[0]
    controls = re.findall(r"<(?:input|select)\b[^>]*>", form)
    visible = [c for c in controls if 'type="hidden"' not in c]
    assert visible and all(re.search(r'\bid="f-\w+"', c) for c in visible)


def test_speed_control_itself_is_disabled(client):
    import re
    r = client.post("/presets/preview", data=fast_form())
    speed = re.search(r'<select[^>]*name="speed"[^>]*>', r.text).group(0)
    assert "disabled" in speed


def test_name_is_escaped_on_list(client, conn):
    presets.create_preset(conn, "A(b)", presets.STANDARD, "2026-01-01T00:00:00+00:00")
    conn.execute("UPDATE presets SET name = '<b>x</b>' WHERE name = 'A(b)'"); conn.commit()
    t = client.get("/presets").text
    assert "<b>x</b>" not in t and "&lt;b&gt;x&lt;/b&gt;" in t


def test_unticked_keep_commentary_stays_unticked_after_error(client, conn):
    s = PresetSettings.from_dict({**presets.SEEDS[1][1], "keep_commentary": True})
    pid = presets.create_preset(conn, "Chatty", s, "2026-01-01T00:00:00+00:00")
    r = client.post("/presets/save", data={**form_for(1, name="Chatty", quality_bluray=999), "id": str(pid)})
    assert 'name="keep_commentary"' in r.text
    assert "checked" not in r.text.split('name="keep_commentary"')[1].split(">")[0]


def test_huge_ids_do_not_500(client):
    big = "9" * 30
    assert client.get(f"/presets/new?from={big}").status_code == 200
    assert client.post("/presets/save", data={**form_for(1), "id": big}).status_code == 200
    assert client.post("/presets/save", data={**form_for(1, name="Y"), "from": big}).status_code in (200, 303)
    assert client.post(f"/presets/{big}/delete").status_code in (200, 404)


def test_delete_button_confirms_with_the_name(client):
    html = client.get("/presets").text
    assert "onsubmit='return confirm(&#34;Delete preset Standard?&#34;)'" in html or         "onsubmit='return confirm(\"Delete preset Standard?\")'" in html
