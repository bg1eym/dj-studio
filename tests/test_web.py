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


def test_live_view_assets_and_rehearsal_api(client):
    assert "class LiveView" in client.get("/static/live.js").text
    body = {"schema": "djset/2", "id": "x3", "tracks": [{"id": "a"}, {"id": "b"}],
            "transitions": [{"technique": "instant", "start": 33, "b_in": 17}]}
    client.put("/api/sets/x3", json=body)
    # 预演从第 1 段过渡开始：截短的那份，A 从过渡前 8 小节起播
    r = client.get("/api/sets/x3", params={"rehearse_from": 1}).json()
    assert r["set"]["tracks"][0]["start"] == 25 and r["set"]["id"] == "x3-from1"
    assert client.get("/api/sets/x3", params={"rehearse_from": 5}).status_code == 400
    assert client.post("/api/sets/x3/rehearse", json={"speed": 100}).status_code == 400
    r = client.post("/api/sets/x3/rehearse", json={"speed": 2})
    assert r.status_code == 400 and "校验" in r.json()["error"]  # 曲目不在曲库：和真播一样先过校验


def test_upload_audio_and_reject_bad_files(client, tmp_path):
    import shutil as _sh

    from djstudio.library.audio import have_ffmpeg
    from djstudio.synth import techno, write_wav

    assert client.put("/api/upload", params={"name": "notes.txt"}, content=b"x").status_code == 400
    assert client.put("/api/upload", params={"name": "../x.mp3"}, content=b"").status_code == 400  # 空文件
    r = client.put("/api/upload", params={"name": "谱.pdf"}, content=b"%PDF-1.4 not really")
    assert r.status_code == 422 and "打碟谱" in r.json()["detail"]
    assert client.get("/api/plan").json() is None  # 读不了的谱不算当前打碟谱
    if not have_ffmpeg() or _sh.which("ffprobe") is None:
        return
    wav = tmp_path / "w.wav"
    write_wav(wav, techno(128.0, 0.1, 0, (("groove", 4),)))
    r = client.put("/api/upload", params={"name": "01_Song.wav"}, content=wav.read_bytes())
    assert r.status_code == 200 and r.json()["kind"] == "audio" and r.json()["track"]
    again = client.put("/api/upload", params={"name": "01_Song.wav"}, content=wav.read_bytes()).json()
    assert again["existing"] and again["track"] == r.json()["track"]
    assert [t["id"] for t in client.get("/api/tracks").json()] == [r.json()["track"]]
