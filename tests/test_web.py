"""网页 API 冒烟测试（不需要 ffmpeg / node / Mixxx）。"""
import pytest
from fastapi.testclient import TestClient

from djstudio.config import Config
from djstudio.mixxx import fakedb
from djstudio.studio import Studio
from djstudio.web.app import create_app


@pytest.fixture
def client(tmp_path):
    mixxx = tmp_path / "mixxx"
    mixxx.mkdir()
    fakedb.create(mixxx / "mixxxdb.sqlite")
    cfg = Config(home=tmp_path / "home", library=tmp_path / "lib", mixxx_dir=mixxx, browse_roots=[tmp_path])
    return TestClient(create_app(Studio(cfg)))


def test_index_and_state(client):
    assert "DJ Studio" in client.get("/").text
    st = client.get("/api/state").json()
    assert st["engine"]["connected"] is False and st["playing"] is False


def test_set_roundtrip_and_validate(client):
    body = {"schema": "djset/2", "id": "x1", "name": "测试", "tracks": [{"id": "nope"}], "transitions": []}
    r = client.put("/api/sets/x1", json=body)
    assert r.status_code == 200
    c = r.json()["compiled"]
    assert not c["ok"] and "找不到" in c["issues"][0]["msg"]
    assert client.get("/api/sets").json()[0]["id"] == "x1"
    bad = dict(body, transitions=[{"technique": "nope", "start": 1}])
    assert client.post("/api/sets/validate", json=bad).status_code == 422
    assert client.put("/api/sets/other", json=body).status_code == 400


def test_browse_outside_roots_forbidden(client):
    assert client.get("/api/browse", params={"path": "/etc"}).status_code == 403
    assert client.get("/api/browse").status_code == 200


def test_play_unprepared_rejected(client):
    body = {"schema": "djset/2", "id": "x2", "tracks": [], "transitions": []}
    client.put("/api/sets/x2", json=body)
    r = client.post("/api/sets/x2/play")
    assert r.status_code == 400 and "准备" in r.json()["error"]


def test_audio_only_for_known_tracks(client):
    assert client.get("/api/tracks/../../etc/passwd/audio").status_code == 404
    assert client.get("/api/tracks/deadbeef/audio").status_code == 404
