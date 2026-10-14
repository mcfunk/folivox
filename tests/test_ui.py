import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("starlette")
pytest.importorskip("httpx")

from starlette.testclient import TestClient  # noqa: E402

from folivox.pipeline import Cancelled  # noqa: E402
from folivox.ui.app import create_app  # noqa: E402

TOKEN = "t0ken"
BOOK = ("Chapter 1\n\n" + "It was a quiet morning in the village. " * 12 + "\n\nChapter 2\n\n"
        + "The river was cold and bright. " * 12 + "\n").encode()


def fake_render(book, opts, events):
    """Stands in for pipeline.render: emits the same events without any model."""
    events.stage("plan", "Preparing text")
    events.plan(100, 0, 2, 2)
    for i in range(2):
        if events.cancelled():
            raise Cancelled
        chunk = SimpleNamespace(source="x" * 50, chapter=0, idx=i, key=f"key{i}", text="x")
        events.chunk_done(chunk, SimpleNamespace(duration=2.0, synth_seconds=1.0, score=0.0), [0.0])
        time.sleep(0.05)
    work = opts.resolved_work_dir() / "chapters"
    work.mkdir(parents=True, exist_ok=True)
    (work / "001.flac").write_bytes(b"fLaC")
    events.chapter_built(0, "Chapter 1", 2.0)
    return opts.output


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FOLIVOX_CONFIG", str(tmp_path / "config.toml"))
    from folivox import config

    config.load.cache_clear()
    app = create_app(token=TOKEN, port=8765, data_dir=tmp_path / "ui", render=fake_render)
    c = TestClient(app, base_url="http://127.0.0.1:8765", headers={"X-Folivox-Token": TOKEN})
    yield c
    config.load.cache_clear()


def upload(client):
    r = client.post("/api/books", files={"file": ("tale.txt", BOOK, "text/plain")})
    assert r.status_code == 200, r.text
    return r.json()


def wait_done(client, job_id):
    for _ in range(100):
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["state"] != "running":
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_auth_and_host_guard(client):
    assert client.get("/").status_code == 200  # the page itself needs no token
    assert client.get("/api/voices", headers={"X-Folivox-Token": "wrong"}).status_code == 401
    assert client.get("/api/voices", headers={"Host": "evil.example"}).status_code == 403


def test_book_upload_and_chapters(client):
    b = upload(client)
    assert b["title"] and b["chapters"] and b["chapters"][0]["n"] == 1
    r = client.get(f"/api/books/{b['book_id']}/chapters?depth=1&lang=de")
    assert r.json()["lang"] == "de"
    assert client.post("/api/books", files={"file": ("x.xyz", b"?", "text/plain")}).status_code == 415


def test_voices_listing_and_sample(client):
    voices = client.get("/api/voices").json()["voices"]
    assert voices
    r = client.get(f"/api/voices/{voices[0]['id']}/sample")
    assert r.status_code == 200 and r.headers["content-type"] == "audio/flac"
    bundled = next(v for v in voices if v["bundled"])
    assert client.delete(f"/api/voices/{bundled['id']}").status_code == 404  # bundled voices cannot be removed


def test_render_job_events_and_chapter_audio(client, tmp_path):
    b = upload(client)
    body = {"kind": "render", "book_id": b["book_id"], "chapters": [1],
            "options": {"output_dir": str(tmp_path / "out"), "qa": False}}
    r = client.post("/api/jobs", json=body)
    assert r.status_code == 202, r.text
    job = r.json()["id"]
    assert wait_done(client, job)["state"] == "done"
    types = [line for line in client.get(f"/api/jobs/{job}/events").text.splitlines() if line.startswith("data:")]
    joined = "\n".join(types)
    for t in ("stage", "plan", "progress", "chapter", "done"):
        assert f'"type": "{t}"' in joined
    assert client.get(f"/api/jobs/{job}/chapters/1").content == b"fLaC"
    assert client.get(f"/api/jobs/{job}/chapters/9").status_code == 404


def test_busy_and_cancel(client, tmp_path, monkeypatch):
    def slow(book, opts, events):
        while not events.cancelled():
            time.sleep(0.02)
        raise Cancelled

    client.app.state.ui.manager._render = slow
    b = upload(client)
    body = {"kind": "preview", "book_id": b["book_id"], "chapters": [1], "options": {"qa": False}}
    job = client.post("/api/jobs", json=body).json()["id"]
    assert client.post("/api/jobs", json=body).status_code == 409
    client.post(f"/api/jobs/{job}/cancel")
    assert wait_done(client, job)["state"] == "cancelled"


def test_bad_options_are_rejected(client):
    b = upload(client)
    r = client.post("/api/jobs", json={"book_id": b["book_id"], "chapters": [1], "options": {"stress": "loud"}})
    assert r.status_code == 400
    r = client.post("/api/jobs", json={"book_id": b["book_id"], "voice": "nobody", "chapters": [1]})
    assert r.status_code == 400


def test_settings_roundtrip(client, tmp_path):
    s = client.get("/api/settings").json()
    assert set(s["kinds"]) == {"tts", "asr", "voicegen", "llm"}
    llm_names = [p["name"] for p in s["kinds"]["llm"]["providers"]]
    r = client.put("/api/settings", json={"providers": {"llm": llm_names[-1]}})
    assert r.status_code == 200 and r.json()["kinds"]["llm"]["selected"] == llm_names[-1]
    assert client.put("/api/settings", json={"providers": {"llm": "nope"}}).status_code == 400


def test_session_survives_reload_and_restart(client, tmp_path):
    assert client.get("/api/session").json() is None
    b = upload(client)
    saved = {"book_id": b["book_id"], "chapters": [2], "voice": "", "lang": "en", "depth": 2,
             "options": {"o-speed": "0.9"}}
    assert client.put("/api/session", json=saved).status_code == 200
    assert client.get("/api/session").json() == saved
    # a brand new server on the same data dir (a restart) re-reads the book from disk
    app2 = create_app(token=TOKEN, port=8765, data_dir=tmp_path / "ui", render=fake_render)
    c2 = TestClient(app2, base_url="http://127.0.0.1:8765", headers={"X-Folivox-Token": TOKEN})
    assert c2.get("/api/session").json() == saved
    assert c2.get(f"/api/books/{b['book_id']}/chapters").json()["chapters"]


def test_session_with_vanished_book_is_dropped(client):
    client.put("/api/session", json={"book_id": "gone"})
    assert client.get("/api/session").json() is None


@pytest.fixture
def keychain(monkeypatch):
    keyring = pytest.importorskip("keyring")
    from keyring.backend import KeyringBackend

    class Memory(KeyringBackend):
        priority = 1

        def __init__(self):
            self.data = {}

        def get_password(self, service, username):
            return self.data.get((service, username))

        def set_password(self, service, username, password):
            self.data[(service, username)] = password

        def delete_password(self, service, username):
            if (service, username) not in self.data:
                raise keyring.errors.PasswordDeleteError("missing")
            del self.data[(service, username)]

    previous = keyring.get_keyring()
    keyring.set_keyring(Memory())
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    yield
    keyring.set_keyring(previous)


def test_api_keys_in_keychain_are_masked_and_used(client, keychain, monkeypatch):
    from folivox import config

    keys = client.get("/api/keys").json()["keys"]
    openai = next(k for k in keys if k["name"] == "OPENAI_API_KEY")
    assert not openai["saved"] and openai["used_by"]
    secret = "sk-test-1234567890abcd"
    r = client.put("/api/keys/OPENAI_API_KEY", json={"value": secret})
    assert r.status_code == 200
    assert secret not in r.text and "abcd" in r.text  # only the last 4 characters come back
    assert secret not in client.get("/api/keys").text
    assert config.api_key("OPENAI_API_KEY", "x") == secret  # providers find it without an env var
    monkeypatch.setenv("OPENAI_API_KEY", "from-env")
    assert config.api_key("OPENAI_API_KEY", "x") == "from-env"  # the environment wins
    assert client.delete("/api/keys/OPENAI_API_KEY").status_code == 200
    assert not next(k for k in client.get("/api/keys").json()["keys"] if k["name"] == "OPENAI_API_KEY")["saved"]


def test_api_key_rejects_unknown_names_and_empty_values(client, keychain):
    assert client.put("/api/keys/HOME", json={"value": "x"}).status_code == 404
    assert client.put("/api/keys/OPENAI_API_KEY", json={"value": "  "}).status_code == 400


def test_ui_events_carry_what_the_studio_view_needs():
    from folivox.pipeline import RenderOptions
    from folivox.ui.jobs import Job, UIEvents

    job = Job("render", "b", RenderOptions(output=Path("x.m4b"), voice_wav=Path("v.wav")))
    ev = UIEvents(job)
    ev.layout([{"index": 0, "title": "One", "fragments": [[10, "abc"], [20, "def"]], "done": "10"}])
    chunk = SimpleNamespace(chapter=0, idx=1, key="def" * 5, text="hello", source="x" * 20)
    ev.chunk_started(chunk)
    ev.step("qa", 1, 3)
    ev.chunk_done(chunk, SimpleNamespace(duration=2.0, synth_seconds=3.0, score=0.05), [0.5, 0.05])
    by_type = {e["type"]: e for e in job.events}
    assert by_type["layout"]["chapters"][0]["done"] == "10"
    assert by_type["fragment"]["key"] == "defdefdefd" and by_type["fragment"]["idx"] == 1
    assert (by_type["step"]["step"], by_type["step"]["attempts"]) == ("qa", 3)
    p = by_type["progress"]
    assert p["attempts"] == 2 and p["chars"] == 20 and p["retakes"] == 1 and p["key"] == "defdefdefd"


def test_reveal_only_for_finished_jobs(client, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("subprocess.Popen", lambda cmd, *a, **k: calls.append(cmd))
    b = upload(client)
    out = tmp_path / "out"
    r = client.post("/api/jobs", json={"book_id": b["book_id"], "chapters": [1],
                                       "options": {"output_dir": str(out), "qa": False}})
    job = r.json()["id"]
    assert wait_done(client, job)["state"] == "done"
    assert client.post(f"/api/jobs/{job}/reveal").status_code == 404  # fake render wrote no output file
    (out / "tale.m4b").write_bytes(b"x")
    assert client.post(f"/api/jobs/{job}/reveal").status_code == 200 and calls



def test_result_download_and_reveal_without_a_file_manager(client, tmp_path, monkeypatch):
    def no_file_manager(cmd, *a, **k):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr("subprocess.Popen", no_file_manager)
    b = upload(client)
    out = tmp_path / "out"
    job = client.post("/api/jobs", json={"book_id": b["book_id"], "chapters": [1],
                                         "options": {"output_dir": str(out), "qa": False}}).json()["id"]
    assert wait_done(client, job)["state"] == "done"
    assert client.get(f"/api/jobs/{job}/result").status_code == 404  # nothing written yet
    (out / "tale.m4b").write_bytes(b"m4b-bytes")
    r = client.get(f"/api/jobs/{job}/result")
    assert r.status_code == 200 and r.content == b"m4b-bytes" and "tale.m4b" in r.headers["content-disposition"]
    assert client.post(f"/api/jobs/{job}/reveal").status_code == 409  # a container: no xdg-open


def test_result_download_zips_an_mp3_folder(client, tmp_path):
    import io
    import zipfile

    b = upload(client)
    out = tmp_path / "out"
    job = client.post("/api/jobs", json={"book_id": b["book_id"], "chapters": [1],
                                         "options": {"output_dir": str(out), "qa": False, "mp3": True}}).json()
    assert wait_done(client, job["id"])["state"] == "done"
    result = Path(client.get(f"/api/jobs/{job['id']}").json()["result"])
    result.mkdir(parents=True, exist_ok=True)
    (result / "01.mp3").write_bytes(b"one")
    r = client.get(f"/api/jobs/{job['id']}/result")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert names == [f"{result.name}/01.mp3"]
    assert not list((tmp_path / "ui").glob("*.zip"))  # the temporary zip is removed after sending


def test_allow_hosts_for_a_server(tmp_path):
    app = create_app(token=TOKEN, port=8765, data_dir=tmp_path / "ui", render=fake_render,
                     allow_hosts=["folivox.example.com", "nas:9000"])
    c = TestClient(app, base_url="http://127.0.0.1:8765", headers={"X-Folivox-Token": TOKEN})
    for host in ("folivox.example.com", "folivox.example.com:8765", "nas:9000", "localhost:8765"):
        assert c.get("/api/voices", headers={"Host": host}).status_code == 200, host
    for host in ("nas:8765", "evil.example", "folivox.example.com:1"):
        assert c.get("/api/voices", headers={"Host": host}).status_code == 403, host
    assert c.get("/api/voices", headers={"Host": "folivox.example.com", "X-Folivox-Token": "x"}).status_code == 401
    anything = create_app(token=TOKEN, port=8765, data_dir=tmp_path / "ui", render=fake_render, allow_hosts=["*"])
    c = TestClient(anything, base_url="http://127.0.0.1:8765", headers={"X-Folivox-Token": TOKEN})
    assert c.get("/api/voices", headers={"Host": "anything.lan"}).status_code == 200

def test_voices_carry_waveform_and_auto_choice(client):
    r = client.get("/api/voices").json()
    v = r["voices"][0]
    assert {"peaks", "seconds", "transcript", "lang"} <= set(v)
    assert len(v["peaks"]) == 64 and max(v["peaks"]) == 100  # soundfile is part of the test group
    assert r["auto"]["ru"] in {x["id"] for x in r["voices"]}


def test_favorites_roundtrip(client, tmp_path):
    assert client.get("/api/voices").json()["favorites"] == []
    assert client.put("/api/favorites", json={"ids": ["russlib", "xenia", "russlib"]}).status_code == 200
    assert client.get("/api/voices").json()["favorites"] == ["russlib", "xenia"]
    assert client.put("/api/favorites", json={"ids": "nope"}).status_code == 400
    # survives a restart: stored next to the session
    app2 = create_app(token=TOKEN, port=8765, data_dir=tmp_path / "ui", render=fake_render)
    c2 = TestClient(app2, base_url="http://127.0.0.1:8765", headers={"X-Folivox-Token": TOKEN})
    assert c2.get("/api/voices").json()["favorites"] == ["russlib", "xenia"]


def test_token_link_sets_a_cookie_and_the_clean_url_works(tmp_path):
    app = create_app(token=TOKEN, port=8765, data_dir=tmp_path / "ui", render=fake_render)
    c = TestClient(app, base_url="http://127.0.0.1:8765", follow_redirects=False)
    assert c.get("/api/settings").status_code == 401
    assert c.get("/?token=wrong").status_code == 401 and not c.cookies
    r = c.get(f"/?token={TOKEN}")
    assert r.status_code == 303 and r.headers["location"] == "/"  # the token is not left in the address bar
    sc = r.headers["set-cookie"].lower()
    assert "httponly" in sc and "samesite=strict" in sc and TOKEN not in r.headers["set-cookie"]
    assert c.get("/api/settings").status_code == 200  # cookie only, no header, no query
    other = TestClient(app, base_url="http://127.0.0.1:8765", cookies={"folivox_session_8765": "guess"})
    assert other.get("/api/settings").status_code == 401


def test_ui_token_is_stable_and_private(tmp_path, monkeypatch):
    import stat

    monkeypatch.setattr("platformdirs.user_data_dir", lambda *a, **k: str(tmp_path))
    from folivox.ui import ui_token

    first = ui_token()
    assert len(first) >= 24 and ui_token() == first
    assert stat.S_IMODE((tmp_path / "ui_token").stat().st_mode) == 0o600


def test_ui_token_from_the_deployment(tmp_path, monkeypatch):
    monkeypatch.setattr("platformdirs.user_data_dir", lambda *a, **k: str(tmp_path))
    monkeypatch.delenv("FOLIVOX_UI_TOKEN", raising=False)
    from folivox.ui import read_token, ui_token

    secret = tmp_path / "secret"
    secret.write_text("from-a-docker-secret-123\n")
    monkeypatch.setenv("FOLIVOX_UI_TOKEN_FILE", str(secret))
    assert ui_token() == read_token() == "from-a-docker-secret-123"
    monkeypatch.setenv("FOLIVOX_UI_TOKEN", "from-the-environment-456")  # the variable wins over the file
    assert ui_token() == "from-the-environment-456"
    assert not (tmp_path / "ui_token").exists()  # nothing generated or written
    monkeypatch.setenv("FOLIVOX_UI_TOKEN", "short")
    with pytest.raises(SystemExit):
        ui_token()


def png_bytes():
    """A valid 6x8 grey PNG without any imaging library."""
    import struct
    import zlib

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    rows = b"".join(b"\x00" + b"\xc8" * 6 for _ in range(8))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 6, 8, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def test_custom_cover_upload_use_and_reset(client):
    b = upload(client)
    bid = b["book_id"]
    assert b["cover"] == "" and client.get(f"/api/books/{bid}/cover").status_code == 404
    bad = client.put(f"/api/books/{bid}/cover", files={"file": ("x.txt", b"not an image", "text/plain")})
    assert bad.status_code == 415
    r = client.put(f"/api/books/{bid}/cover", files={"file": ("c.png", png_bytes(), "image/png")})
    assert r.status_code == 200 and r.json() == {"cover": "custom"}
    got = client.get(f"/api/books/{bid}/cover")
    assert got.status_code == 200 and got.content[:4] == b"\x89PNG"
    assert client.get(f"/api/books/{bid}/chapters").json()["cover"] == "custom"
    seen = {}

    def spy(book, opts, events):
        seen["cover"], seen["no_cover"] = opts.cover, opts.no_cover
        return fake_render(book, opts, events)

    client.app.state.ui.manager._render = spy
    job = client.post("/api/jobs", json={"kind": "preview", "book_id": bid, "chapters": [1]}).json()["id"]
    wait_done(client, job)
    assert seen["cover"].read_bytes()[:4] == b"\x89PNG" and seen["no_cover"] is False
    assert client.put(f"/api/books/{bid}/cover", json={"mode": "none"}).json() == {"cover": "none"}
    assert client.get(f"/api/books/{bid}/cover").status_code == 404
    job = client.post("/api/jobs", json={"kind": "preview", "book_id": bid, "chapters": [1]}).json()["id"]
    wait_done(client, job)
    assert seen["cover"] is None and seen["no_cover"] is True
    assert client.put(f"/api/books/{bid}/cover", json={"mode": "bad"}).status_code == 400


def test_last_uploaded_book_survives_restarts_and_a_lost_session(tmp_path):
    def open_app():
        app = create_app(token=TOKEN, port=8765, data_dir=tmp_path / "ui", render=fake_render)
        return TestClient(app, base_url="http://127.0.0.1:8765", headers={"X-Folivox-Token": TOKEN})

    c = open_app()
    assert c.get("/api/session").json() is None  # nothing uploaded yet
    first = upload(c)["book_id"]
    time.sleep(0.02)
    second = upload(c)["book_id"]
    # a restarted server, no PUT /api/session was ever sent: the last upload is selected
    assert open_app().get("/api/session").json() == {"book_id": second}
    # the session file is lost or corrupted: fall back to the newest upload
    (tmp_path / "ui" / "session.json").write_text("{broken", encoding="utf-8")
    assert open_app().get("/api/session").json() == {"book_id": second}
    (tmp_path / "ui" / "session.json").unlink()
    assert open_app().get("/api/session").json() == {"book_id": second}
    # the saved session points at a deleted book: the next newest one is used
    import shutil

    shutil.rmtree(tmp_path / "ui" / "uploads" / second)
    assert open_app().get("/api/session").json() == {"book_id": first}
    # a real session is kept as it is
    c2 = open_app()
    c2.put("/api/session", json={"book_id": first, "chapters": [2], "voice": "russlib"})
    assert open_app().get("/api/session").json()["chapters"] == [2]
    assert not list((tmp_path / "ui").glob("session.tmp"))


def test_book_details_are_editable_saved_and_sent_to_the_render(client):
    b = upload(client)
    bid = b["book_id"]
    r = client.get(f"/api/books/{bid}/meta").json()
    assert r["meta"]["title"] == b["title"] and r["meta"]["genre"] == "Audiobook" and r["meta"]["narrator"] == ""
    saved = client.put(f"/api/books/{bid}/meta", json={"title": "  My   Title ", "narrator": "Anna", "series": "Saga",
                                                         "series_part": "2", "description": "Line one.\nLine two."})
    assert saved.json()["meta"]["title"] == "My Title" and saved.json()["meta"]["description"] == "Line one.\nLine two."
    assert client.get(f"/api/books/{bid}/meta").json()["meta"]["series"] == "Saga"  # kept on the server
    assert client.put(f"/api/books/{bid}/meta", json={"title": "x" * 400}).status_code == 400
    seen = {}

    def spy(book, opts, events):
        seen.update(opts.meta)
        return fake_render(book, opts, events)

    client.app.state.ui.manager._render = spy
    wait_done(client, client.post("/api/jobs", json={"kind": "preview", "book_id": bid, "chapters": [1]}).json()["id"])
    assert seen["title"] == "My Title" and seen["narrator"] == "Anna" and seen["series_part"] == "2"
    back = client.put(f"/api/books/{bid}/meta", json={"reset": True}).json()
    assert back["meta"] == back["defaults"] and back["meta"]["narrator"] == ""


def test_pronunciations_are_edited_checked_saved_and_sent_to_the_render(client):
    bid = upload(client)["book_id"]
    assert client.get(f"/api/books/{bid}/pronounce").json() == {"text": "", "count": 0, "warnings": []}
    text = "# names\nKvothe = Quothe\nTolkien* = Tolkeen\nbroken line\nFoo = /Q Z/\n"
    saved = client.put(f"/api/books/{bid}/pronounce", json={"text": text}).json()
    assert saved["text"] == text and saved["count"] == 2 and len(saved["warnings"]) == 2
    assert saved["warnings"][0].startswith("line 4:") and ".txt" not in saved["warnings"][0]
    assert client.get(f"/api/books/{bid}/pronounce").json()["text"] == text  # kept on the server
    assert client.get(f"/api/books/{bid}/chapters").status_code == 200  # the lexicon is not mistaken for the book
    assert client.put(f"/api/books/{bid}/pronounce", json={"text": "x" * 200_000}).status_code == 400
    assert client.put(f"/api/books/{bid}/pronounce", json={"text": 5}).status_code == 400
    assert client.get("/api/books/nope/pronounce").status_code == 404
    seen = {}

    def spy(book, opts, events):
        seen["files"] = [p.read_text(encoding="utf-8") for p in opts.pronounce]
        return fake_render(book, opts, events)

    client.app.state.ui.manager._render = spy
    wait_done(client, client.post("/api/jobs", json={"kind": "preview", "book_id": bid, "chapters": [1]}).json()["id"])
    assert seen["files"] == [text]
    assert client.put(f"/api/books/{bid}/pronounce", json={"text": "  \n"}).json()["count"] == 0
    wait_done(client, client.post("/api/jobs", json={"kind": "preview", "book_id": bid, "chapters": [1]}).json()["id"])
    assert seen["files"] == []  # an emptied lexicon is not passed on


def test_cookie_survives_a_server_restart(tmp_path):
    """The cookie is derived from the saved token, so restarting `folivox ui` does not log the browser out."""
    def boot():
        return TestClient(create_app(token=TOKEN, port=8765, data_dir=tmp_path / "ui", render=fake_render),
                          base_url="http://127.0.0.1:8765", follow_redirects=False)

    first = boot()
    r = first.get(f"/?token={TOKEN}")
    cookie = r.cookies.get("folivox_session_8765")
    assert cookie and TOKEN not in cookie and "max-age" in r.headers["set-cookie"].lower()
    again = boot()
    again.cookies.set("folivox_session_8765", cookie)
    assert again.get("/api/session").status_code == 200
    assert boot().get("/api/session").status_code == 401  # a browser without the cookie is still refused


@pytest.mark.parametrize("target,state,needle", [("ok", "done", None), ("fail", "error", "no voice"),
                                                 ("crash", "error", "cudnnGetLibConfig")])
def test_render_process_reports_result_failure_and_native_crash(tmp_path, target, state, needle):
    from folivox.book import Book
    from folivox.pipeline import RenderOptions
    from folivox.ui.jobs import JobManager

    m = JobManager(process_target=f"render_fakes:{target}")
    job = m.start("render", "b", Book(title="t"), RenderOptions(output=tmp_path / "x.m4b"))
    deadline = time.time() + 60
    while job.state == "running" and time.time() < deadline:
        time.sleep(0.1)
    assert job.state == state
    if needle:
        assert needle in job.error
    if target != "fail":
        assert any(e["type"] == "stage" for e in job.events)


def test_render_process_cancels(tmp_path):
    from folivox.book import Book
    from folivox.pipeline import RenderOptions
    from folivox.ui.jobs import JobManager

    job = JobManager(process_target="render_fakes:slow").start("render", "b", Book(title="t"),
                                                                RenderOptions(output=tmp_path / "x.m4b"))
    deadline = time.time() + 60
    while not job.events and time.time() < deadline:
        time.sleep(0.1)
    job.cancel_requested.set()
    while job.state == "running" and time.time() < deadline:
        time.sleep(0.1)
    assert job.state == "cancelled"


def test_pronunciation_sample_is_picked_from_the_book_and_played_as_a_preview(client):
    bid = upload(client)["book_id"]
    assert client.get(f"/api/books/{bid}/pronounce/sample").json() == {"text": "", "missing": []}  # no entries yet
    client.put(f"/api/books/{bid}/pronounce", json={"text": "river = rivver\nvillage = vilidge\nghost = gost\n"})
    picked = client.get(f"/api/books/{bid}/pronounce/sample").json()
    assert picked["missing"] == ["ghost"]
    assert picked["text"].index("river") < picked["text"].index("village") < picked["text"].index("ghost")  # file order
    seen = {}

    def spy(book, opts, events):
        seen["text"] = book.sections[0].blocks[0].text
        seen["pronounce"] = opts.pronounce
        return fake_render(book, opts, events)

    client.app.state.ui.manager._render = spy
    job = {"kind": "preview", "book_id": bid, "chapters": [1], "sample_text": "  My own  sentence about the river. "}
    wait_done(client, client.post("/api/jobs", json=job).json()["id"])
    assert seen["text"] == "My own sentence about the river." and len(seen["pronounce"]) == 1
    for bad in ({**job, "kind": "render"}, {**job, "sample_text": "  "}, {**job, "sample_text": "x" * 601}):
        assert client.post("/api/jobs", json=bad).status_code == 400
