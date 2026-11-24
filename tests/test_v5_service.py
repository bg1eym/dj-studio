"""v5 在应用里的整条路：文件夹（谱 + 原曲）→ 导入 → 一键分析 → 演出（调度器）→ 停下 / 继续 → 演出包 → 换地方再导入。

音频是合成的（已知 BPM / 第 1 小节）；引擎用独立模拟器 sim5（DJSTUDIO_ENGINE_BIN=none）。
整场录成 MP3 要真的声音引擎（模拟器不出 MP3）：那几个测试只在设了 DJSTUDIO_ENGINE_BIN 时跑，Studio 那一半的流程另有不用引擎的测试。
"""
import hashlib
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from djstudio.config import Config
from djstudio.library.audio import have_ffmpeg
from djstudio.mixxx import fakedb
from djstudio.studio import Studio, StudioError
from djstudio.synth import techno, write_wav
from djstudio.v5.link import SIM_DIR
from djstudio.v5.service import MANIFEST
from djstudio.web.app import create_app

from .v5util import ENGINE_BIN, HAVE_ENGINE

pytestmark = pytest.mark.skipif(
    not have_ffmpeg() or not (SIM_DIR / "sim5.cpp").is_file() or not (shutil.which("c++") or shutil.which("g++")),
    reason="需要 ffmpeg 和 C++ 编译器")

LAYOUT = (("intro", 8), ("groove", 16), ("breakdown", 8), ("drop", 16), ("outro", 8))  # 56 小节
SPECS = {"alpha.wav": (128.0, 0.25, 0), "beta.wav": (130.0, 0.40, 2), "perc.wav": (132.0, 0.10, 1)}


def bar1(name: str) -> float:
    bpm, first, db = SPECS[name]
    return first + db * 60.0 / bpm


SCORE = f"""# 服务测试

- 规范：v5
- 主速：128

## 素材

| ID | 曲名 | 文件名 | 原 BPM | 第 1 小节（秒） | 小节数 |
|---|---|---|---|---|---|
| A1 | Alpha | alpha.wav | 128 | {bar1('alpha.wav'):.3f} | 56 |
| A2 | Beta | beta.wav | 130 | {bar1('beta.wav'):.3f} | 56 |
| A3 | Perc | perc.wav | 132 | {bar1('perc.wav'):.3f} | 56 |

## 片段

| ID | 素材 | Deck | 原曲起点 | 全场起播 | 全场停止 | 角色 | 推子 | LOW |
|---|---|---|---|---|---|---|---|---|
| C1 | A1 | D1 | 9.1 | 1.1 | 33.1 | 主曲 | 10 | 12点 |
| C2 | A2 | D2 | 9.1 | 17.1 | 41.1 | 进曲 | 5 | 7点 |
| C3 | A3 | D3 | 9.1 | 9.1 | 25.1 | 节奏层 | 6 | 7点 |

## 场景 1：交接（17.1 – 33.1）

- 作用：第二首接管
- 保留：D1 的主题
- 前景：第 25 小节以后是 D2
- LOW：第 25 小节同一拍对换
- MID/HIGH：不动
- 角色：D1 出曲，D2 进曲，D3 节奏层
- 有意：中频叠加

### Loop 与跳段

| 片段 | 位置 | 做什么 | 说明 |
|---|---|---|---|
| C3 | 9.1 | loop 2小节，到 25.1 出 | |

### C2 参数

| 位置 | LOW | 推子 | 说明 |
|---|---|---|---|
| 24.1 | | 保持 | |
| 25.1 | 跳 12点 | 10 | 换低频 |

### C1 参数

| 位置 | LOW | 说明 |
|---|---|---|
| 25.1 | 跳 7点 | 换低频 |

### 效果

| ID | 类型 | 路由 | 目标 | 开始 | 结束 | 参数 | 说明 |
|---|---|---|---|---|---|---|---|
| F1 | Delay | 发送 | C1 | 31.1 | 33.1 | 拍长 1/2拍；送出 1/2；反馈 0.4 | |
"""


def make_studio(root: Path) -> Studio:
    (root / "mixxx").mkdir(parents=True, exist_ok=True)
    fakedb.create(root / "mixxx" / "mixxxdb.sqlite")
    return Studio(Config(home=root / "home", library=root / "lib", mixxx_dir=root / "mixxx", browse_roots=[root],
                         engine="builtin"))


def wait_job(studio: Studio, timeout: float = 180.0):
    t0 = time.time()
    while (studio.job is None or studio.job.state == "running") and time.time() - t0 < timeout:
        time.sleep(0.1)
    job = studio.job
    assert job is not None and job.state == "ok", "\n".join(job.log[-25:]) if job else "没有任务"
    return job


def wait_stage(studio: Studio, stages: tuple[str, ...], timeout: float = 90.0) -> str:
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = studio.conductor.progress.stage if studio.conductor else ""
        if st in stages:
            return st
        time.sleep(0.05)
    raise AssertionError(f"没等到 {stages}：{studio.conductor.progress.to_dict() if studio.conductor else None}")


@pytest.fixture(scope="module")
def folder(tmp_path_factory) -> Path:
    """用户交过来的那个文件夹：一份谱 + 谱上写的原曲。"""
    d = tmp_path_factory.mktemp("pack") / "服务测试"
    d.mkdir()
    for k, (name, (bpm, first, db)) in enumerate(SPECS.items()):
        write_wav(d / name, techno(bpm, first, db, LAYOUT, seed=k + 11))
    (d / "服务测试.md").write_text(SCORE, encoding="utf-8")
    return d


@pytest.fixture(scope="module")
def ready(folder, tmp_path_factory):
    """导入文件夹并跑完一键分析的 Studio（整个模块共用：分析要十几秒）。"""
    mp = pytest.MonkeyPatch()
    mp.setenv("DJSTUDIO_ENGINE_BIN", "none")
    mp.setenv("DJSTUDIO_NO_UPDATE", "1")
    mp.setenv("DJSTUDIO_FIND_DIRS", "")
    studio = make_studio(tmp_path_factory.mktemp("studio"))
    res = studio.v5.import_folder(folder)
    job = wait_job(studio)
    yield studio, res, job
    studio.shutdown()
    mp.undo()


def test_folder_import_runs_the_whole_pipeline(ready):
    studio, res, job = ready
    assert res["assets"] == 3 and res["added"] == 3 and res["score"] == "服务测试.md"
    r = job.result
    assert r["v5"] and r["set"] == res["project"] and r["bars"] == 40 and 70 < r["duration"] < 80
    assert [s["no"] for s in r["scenes"]] == [1]
    st = studio.plan_status()
    assert st["v5"] and st["n_tracks"] == 3 and st["n_clips"] == 3 and all(t["track"] for t in st["tracks"])
    assert studio.plans()[0]["v5"] and studio.plans()[0]["set"] == r["set"]
    c = studio.v5.compiled(r["set"])
    assert c.ok and c.plan["exec"] and len(c.library_rows()) == 3
    # 原曲起点 9.1 = 这首自己的第 9 小节：按谱上写的「第 1 小节（秒）」数（Beta 的第 1 小节不在文件开头）
    beta = c.clip("C2")
    assert beta.info.beat_time(0.0) == pytest.approx(bar1("beta.wav"), abs=0.03)
    k2 = next(k for k in c.plan["clips"] if k["id"] == "C2")
    assert k2["segs"][0]["src"] == pytest.approx(bar1("beta.wav") + 32 * 60 / beta.info.grid.bpm, abs=0.03)
    assert abs(beta.info.grid.bpm - 130) < 0.1


def test_play_stop_resume_and_engine_state(ready):
    studio, res, job = ready
    pid = res["project"]
    studio.play(pid, speed=30)
    assert wait_stage(studio, ("running",)) == "running"
    t0 = time.time()
    es: dict = {}
    while time.time() - t0 < 20:
        es = studio.engine_state()
        if es.get("status") and es["status"]["state"] == "running" and es["status"]["beat"] > 70:
            break
        time.sleep(0.05)
    assert es["v5"] and es["online"] and es["set"] == pid and es["link"] == "sim5"
    decks = es["status"]["decks"]
    assert [d["deck_id"] for d in decks] == [1, 2, 3, 4] and decks[0]["clip_id"] == "C1" and decks[1]["clip_id"] == "C2"
    assert es["receipts"]["C1.start"]["st"] == "ok" and es["receipts"]["C2.start"]["st"] == "ok"
    studio.stop()
    assert wait_stage(studio, ("stopped",)) == "stopped"
    rp = studio.resume_point
    assert rp and rp["v5"] and rp["set"] == pid and 70 <= rp["beat"] < 160
    time.sleep(0.3)
    studio.resume()
    assert wait_stage(studio, ("done",), 60) == "done"
    log = "\n".join(studio.conductor.progress.log)
    assert "整场放完" in log and "!!" not in log, log
    assert f"从 {rp['label']} 开始" in log


def test_seek_pause_and_takeover_through_the_runner(ready):
    studio, res, job = ready
    studio.play(res["project"], speed=8)
    wait_stage(studio, ("running",))
    r = studio.v5.runner()
    r.seek(96.0)  # 第 25 小节：换低频的那一拍
    t0 = time.time()
    while time.time() - t0 < 20 and not (r.from_beat == 96.0 and (r.link.status or {}).get("state") == "running"
                                         and r.beat() > 97):
        time.sleep(0.05)
    assert r.from_beat == 96.0 and 97 < r.beat() < 130
    r.takeover(2, "lo", 0.5)
    r.pause()
    assert wait_stage(studio, ("paused",), 10) == "paused"
    b = r.beat()
    time.sleep(0.3)
    assert r.beat() == b and r.link.status["decks"][1]["manual"] == ["lo"]
    r.resume()
    r.give_back(2, "lo", "beat", 1.0)
    studio.stop()
    wait_stage(studio, ("stopped",))


def test_engine_process_dying_stops_the_show_and_is_not_restarted(ready):
    """引擎进程中途没了：这一场停在那里、报错，不自动重启重放；用户重新开始才会起新的引擎。"""
    studio, res, job = ready
    studio.play(res["project"], speed=8)
    wait_stage(studio, ("running",))
    run = studio.v5.run
    run["link"]._p.kill()
    assert wait_stage(studio, ("error",), 20) == "error"
    pr = studio.conductor.progress
    assert "引擎" in (pr.error or "") and pr.finished_at
    t0 = time.time()
    while studio.v5.run is not None and time.time() - t0 < 10:
        time.sleep(0.05)
    assert studio.v5.run is None and not studio.conductor.running  # 没有偷偷重开
    studio.play(res["project"], speed=8)  # 用户再点开始：新的引擎，照常
    wait_stage(studio, ("running",))
    assert studio.v5.run["link"] is not run["link"]
    studio.stop()
    wait_stage(studio, ("stopped",))


def test_a_start_point_that_cannot_be_restored_is_refused(ready):
    """抓取型的效果离得太远：不让从那里开始（不装成恢复了），原话告诉用户从哪之前开始。"""
    studio, res, job = ready
    c = studio.v5.compiled(res["project"])
    assert c.restore_problem(40.0) is None  # 这份谱里没有这样的效果：哪里都能开始
    c.live.append({"id": "FX", "kind": "roll", "name": "Roll", "route": "insert", "clips": ["C1"], "start": 4.0, "end": 600.0,
                   "tail": 0.0, "restore": "capture", "line": 1, "rc": "FX", "text": ""})
    assert "FX" in c.restore_problem(400.0)


def test_web_api_serves_the_chart_and_controls(ready):
    studio, res, job = ready
    pid = res["project"]
    client = TestClient(create_app(studio))
    plan = client.get("/api/plan").json()
    assert plan["v5"] and plan["project"] == pid
    body = client.get(f"/api/sets/{pid}").json()
    ch = body["chart"]
    assert body["v5"] and ch["ok"] and ch["decks"] == 4 and len(ch["clips"]) == 3 and ch["scenes"][0]["strategy"]["LOW"]
    assert client.get("/api/v5/caps").json()["max_decks"] == 4
    assert client.get(f"/api/v5/{pid}/state_at", params={"beat": 100}).json()["decks"][1]["clip"] == "C2"
    md = client.get(f"/api/v5/{pid}/score.md").text
    assert "- 规范：v5" in md and "| C2 | A2 | D2 |" in md
    assert client.post("/api/v5/pause").status_code == 400  # 没在演出
    r = client.post(f"/api/sets/{pid}/play", json={"speed": 16, "from": 1})
    assert r.status_code == 200 and r.json()["v5"], r.text
    wait_stage(studio, ("running",))
    assert studio.v5.runner().from_beat == 64.0  # 从场景 1 开始 = 第 17 小节
    st = client.get("/api/state").json()
    assert st["playing"] and st["engine"]["v5"] and st["progress"]["v5"]
    assert client.post("/api/v5/takeover", json={"deck": 1, "param": "hi", "value": -6}).status_code == 200
    assert client.post("/api/v5/return", json={"deck": 1, "param": "hi", "when": "bar"}).status_code == 200
    assert client.post("/api/v5/seek", json={"beat": 120}).status_code == 200
    assert client.post("/api/stop").status_code == 200
    wait_stage(studio, ("stopped", "done"))
    # 图谱里每份素材带着整首的长度和打击垫；Hot Cue 表能看、能下载
    ch = client.get(f"/api/sets/{pid}").json()["chart"]
    assert ch["assets"] and all(a["dur"] > 0 and a["track_id"] for a in ch["assets"] if a["hotcues"])
    assert any(h["name"] == "IN" for a in ch["assets"] for h in a["hotcues"])
    r = client.get(f"/api/v5/{pid}/cuesheet.md")
    assert r.status_code == 200 and "Hot Cue 表" in r.text and "| 1 | IN |" in r.text
    assert "attachment" in client.get(f"/api/v5/{pid}/cuesheet.md?download=1").headers["content-disposition"]


# ---------------------------------------------------------------- 整场录成 MP3


def wait_recording(studio: Studio, timeout: float = 300.0) -> dict:
    t0 = time.time()
    while studio.recording and studio.recording["state"] == "running" and time.time() - t0 < timeout:
        time.sleep(0.1)
    return studio.recording_state() or {}


def test_recording_needs_the_sound_engine(ready, monkeypatch):
    """没有声音引擎（模拟器没有声音）：不录，原话说清楚；不留下一个「正在录」的状态。"""
    monkeypatch.setenv("DJSTUDIO_ENGINE_BIN", "none")
    studio, res, job = ready
    before = studio.recording_state()
    with pytest.raises(StudioError, match="没有声音引擎，录不了"):
        studio.record(res["project"])
    r = TestClient(create_app(studio)).post(f"/api/sets/{res['project']}/record")
    assert r.status_code == 409 and "没有声音引擎，录不了" in r.json()["detail"]
    assert studio.recording_state() == before


def test_record_flow_of_a_v5_project_without_the_engine(ready, monkeypatch):
    """Studio 那一半（录一遍的那一步换成假的，不用引擎）：v5 的项目转到 Service5.record_once；它说这一遍不能用（redo）
    就按下一档倍速重录；录的时候不能开始演出、不能再录一份；录好的文件加上 ID3（标题 = 谱名），放进「DJ Studio 录音」。"""
    studio, res, job = ready
    pid = res["project"]
    client = TestClient(create_app(studio))
    monkeypatch.setattr("djstudio.engine.bundled.engine_binary", lambda: Path("/bin/sh"))
    calls: list[tuple[float, bool]] = []
    gate = threading.Event()

    def fake_once(rec, c, speed, part, last):
        calls.append((speed, last))
        if len(calls) == 1:
            gate.wait(20)
            return {"redo": "放得太快，有动作没赶上（D2 起播 C2）", "cache_misses": 0}
        part.write_bytes(b"\xff\xfb\x90\x00" + bytes(1000))
        return {"seconds": c.duration, "cache_misses": 0, "late": 0, "rate": 7.5}

    monkeypatch.setattr(studio.v5, "record_once", fake_once)
    events: list[dict] = []

    def listen(kind, payload):
        if kind == "recording":
            events.append(dict(payload))

    studio.listeners.append(listen)
    try:
        r = client.post(f"/api/sets/{pid}/record")
        assert r.status_code == 200, r.text
        st = r.json()
        c = studio.v5.compiled(pid)
        assert st["state"] == "running" and st["set"] == pid and st["title"] == "服务测试" and "cancel" not in st
        assert st["duration"] == round(c.duration, 1) and st["progress"] == 0.0
        assert client.get("/api/state").json()["recording"]["state"] == "running"
        # 录的时候：不能再录一份，不能开始演出（从头、从场景、继续都不行）
        assert client.post(f"/api/sets/{pid}/record").status_code == 409
        with pytest.raises(StudioError, match="正在录 MP3"):
            studio.play(pid, speed=8)
        r = client.post(f"/api/sets/{pid}/play", json={"speed": 8, "from": 1})
        assert r.status_code >= 400 and "正在录 MP3" in r.text
        assert studio.v5.run is None
        gate.set()
        rec = wait_recording(studio, 30)
    finally:
        gate.set()
        studio.listeners.remove(listen)
    assert rec["state"] == "ok", rec
    assert calls == [(20.0, False), (8.0, False)]  # 第一遍不能用 → 放慢到第二档重录
    assert any("有动作没赶上" in e["detail"] and "放慢到 8 倍重录" in e["detail"] for e in events), [e["detail"] for e in events]
    assert events[0]["state"] == "running" and events[-1]["state"] == "ok"
    assert rec["speed"] == 8.0 and rec["rate"] == 7.5 and rec["cache_misses"] == 0 and rec["progress"] == 1.0
    assert rec["seconds"] == round(c.duration, 1) and rec["detail"].startswith("录好了")
    f = Path(rec["file"])
    assert f.parent == studio.recordings_dir and f.name == rec["file_name"] and f.name.startswith("服务测试 ") and f.suffix == ".mp3"
    data = f.read_bytes()
    assert data[:3] == b"ID3" and "服务测试".encode("utf-16-le") in data[:200] and rec["size"] == len(data)
    assert data.endswith(b"\xff\xfb\x90\x00" + bytes(1000))  # 标签后面是引擎录的那一份，原样
    assert [p.name for p in studio.recordings_dir.iterdir()] == [f.name]  # 临时文件没留下
    assert [x["name"] for x in client.get("/api/recordings").json()] == [f.name]
    # 录完了：演出照常能开始；反过来，演出的时候不能录
    studio.play(pid, speed=4)
    wait_stage(studio, ("running",))
    with pytest.raises(StudioError, match="正在演出"):
        studio.record(pid)
    assert studio.recording_state()["state"] == "ok"  # 上一次录的结果还在
    studio.stop()
    wait_stage(studio, ("stopped",))


def test_recording_is_refused_when_a_source_file_is_gone(ready, monkeypatch, tmp_path):
    """用到的原文件不在了：录之前就说是哪一个（不等录完才发现少了一台 Deck）。"""
    studio, res, job = ready
    monkeypatch.setattr("djstudio.engine.bundled.engine_binary", lambda: Path("/bin/sh"))
    c = studio.v5.compiled(res["project"])
    rows = c.library_rows()
    gone = [{**rows[0], "path": str(tmp_path / "搬走了.wav")}] + rows[1:]
    monkeypatch.setattr(type(c), "library_rows", lambda self: gone)
    before = studio.recording_state()
    with pytest.raises(StudioError, match=r"原文件不在了.*A1.*搬走了\.wav"):
        studio.record(res["project"])
    assert studio.recording_state() == before


@pytest.fixture(scope="module")
def example(tmp_path_factory):
    """示例谱（docs/score_v5_example.md）和按它的素材表合成的五份 MP3，在声音引擎上分析好。录音的测试用。"""
    if not HAVE_ENGINE:
        pytest.skip("要声音引擎（DJSTUDIO_ENGINE_BIN）")
    from djstudio.v5.demo import make_folder

    mp = pytest.MonkeyPatch()
    mp.setenv("DJSTUDIO_ENGINE_BIN", ENGINE_BIN)
    mp.setenv("DJSTUDIO_NO_UPDATE", "1")
    mp.setenv("DJSTUDIO_FIND_DIRS", "")
    root = tmp_path_factory.mktemp("example")
    studio = make_studio(root)
    try:
        make_folder(root / "四 Deck 场景示例")
        res = studio.v5.import_folder(root / "四 Deck 场景示例")
        wait_job(studio)
    finally:
        mp.undo()  # 这个模块里别的测试用模拟器：环境变量不留着，用引擎的测试自己设
    yield studio, res["project"]
    studio.shutdown()


def decode(path: Path) -> np.ndarray:
    """MP3 → 立体声 float（44.1 kHz）。ffmpeg 按 LAME 的 Info 帧去掉编码器前后的补零：第 0 个采样就是录音的第 0 帧。"""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "2", "-ar", "44100", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype="<f4").reshape(-1, 2).astype(np.float64)


def recorded_files(studio: Studio) -> set[str]:
    d = studio.recordings_dir
    return {p.name for p in d.iterdir()} if d.is_dir() else set()


def test_record_the_example_to_mp3_in_the_sound_engine(example, monkeypatch):
    """「录成 MP3」，曲谱 v5：示例谱整场（7 分多钟：四台 Deck、发送效果、现场效果、采样、变速）在声音引擎里不出声地尽快放一遍，
    用 Mixxx 的 MP3 编码器录成 320 kbps。从第 0 拍录起（第一拍是完整的）、到全场结束；开头结尾没有多出来的静音；
    读盘没有跟不上，没有晚到的动作；标签里是谱名。"""
    monkeypatch.setenv("DJSTUDIO_ENGINE_BIN", ENGINE_BIN)
    studio, pid = example
    c = studio.v5.compiled(pid)
    assert c.ok and 400 < c.duration < 460
    had = recorded_files(studio)
    events: list[dict] = []

    def listen(kind, payload):
        if kind == "recording":
            events.append(dict(payload))

    studio.listeners.append(listen)
    t0 = time.time()
    try:
        st = studio.record(pid)
        assert st["state"] == "running" and st["title"] == "四 Deck 场景示例" and st["duration"] == round(c.duration, 1)
        with pytest.raises(StudioError, match="正在录 MP3"):
            studio.play(pid, speed=1.0)  # 录的时候不能开始演出
        rec = wait_recording(studio)
    finally:
        studio.listeners.remove(listen)
    wall = time.time() - t0
    assert rec["state"] == "ok", rec
    assert rec["cache_misses"] == 0 and rec["speed"] == Studio.RECORD_SPEEDS[0], rec  # 第一遍就录成了，没有放慢重录
    assert rec["rate"] > 3 and wall < c.duration / 3, f"{c.duration:.0f} 秒的一场录了 {wall:.1f} 秒（{rec['rate']} 倍速）"
    prog = [e["progress"] for e in events if e["state"] == "running"]
    assert len(prog) > 5 and prog == sorted(prog) and 0.3 < prog[-1] <= 0.99  # 网页上的进度一直往前走
    assert any("倍速，录到" in e["detail"] for e in events)
    f = Path(rec["file"])
    assert f.parent == studio.recordings_dir and f.name.startswith("四 Deck 场景示例 ") and f.suffix == ".mp3"
    assert recorded_files(studio) - had == {f.name}  # 只多了这一个：临时文件没留下
    # 标签：ID3v2.3，标题是谱名（UTF-16）；320 kbps
    head = f.read_bytes()[:200]
    assert head[:3] == b"ID3" and "四 Deck 场景示例".encode("utf-16-le") in head
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration,bit_rate:format_tags=title",
                            "-of", "json", str(f)], capture_output=True, text=True, check=True)
    fmt = json.loads(probe.stdout)["format"]
    assert fmt["tags"]["title"] == "四 Deck 场景示例" and 300_000 < float(fmt["bit_rate"]) < 340_000
    assert abs(float(fmt["duration"]) - rec["seconds"]) < 0.1
    # 长度 = 全场（第一个片段从 1.1 起播，最后一个片段在全场结束时停；效果的尾音都在全场长度以内）
    assert c.clips and min(k.start for k in c.clips) == 0.0 and max(k.stop for k in c.clips) == c.length
    assert c.duration - 0.5 < rec["seconds"] <= c.duration + 0.05, (rec["seconds"], c.duration)

    x = decode(f)
    sr = 44100
    assert abs(len(x) / sr - rec["seconds"]) < 0.05
    rms = float(np.sqrt((x ** 2).mean()))
    assert 0.03 < rms < 0.5 and 0.5 < float(np.abs(x).max()) < 1.5, (rms, float(np.abs(x).max()))
    # 第一拍没有被切掉：录音从第 0 拍所在的那一块（512 帧 = 11.6 ms）录起——开头是一小段静音，然后第 1 小节的重音从头响起；
    # 它和第 5 小节那一下（合成素材里同样的一下，这时旋钮都还没动）一样大
    env = np.abs(x).max(axis=1)
    onset = int(np.flatnonzero(env > 0.01)[0])
    assert onset < 512 and float(env[:256].max()) < 1e-3, (onset, float(env[:256].max()))
    beat = 60.0 / c.tempo.bpm_at(0.0)
    assert c.tempo.bpm_at(16.0) == c.tempo.bpm_at(0.0)
    win = int(0.4 * sr)
    bar1 = float(np.sqrt((x[:win] ** 2).mean()))
    bar5 = float(np.sqrt((x[int(16 * beat * sr):int(16 * beat * sr) + win] ** 2).mean()))
    assert bar1 > 0.01 and abs(bar1 - bar5) < 0.1 * bar5, (bar1, bar5)
    # 结尾：停在全场结束，不是硬切在一个响的采样上
    assert float(env[-64:].max()) < 0.02, float(env[-64:].max())
    # 中间没有整段没声音的地方：按开头速度的 4 小节（7 秒多）一段，每一段都有声音
    seg = int(16 * beat * sr)
    quiet = [i for i in range(len(x) // seg) if float(np.sqrt((x[i * seg:(i + 1) * seg] ** 2).mean())) < 0.003]
    assert not quiet, quiet


def test_recording_can_be_cancelled_through_the_web_api(example, monkeypatch):
    """网页上的那条路（v5 的项目用同一个接口）：开始录 → 状态里看得到 → 取消 → 不留下半截文件；之后演出照常能开始。"""
    monkeypatch.setenv("DJSTUDIO_ENGINE_BIN", ENGINE_BIN)
    studio, pid = example
    client = TestClient(create_app(studio))
    before = recorded_files(studio)
    r = client.post(f"/api/sets/{pid}/record")
    assert r.status_code == 200 and r.json()["state"] == "running", r.text
    r = client.post(f"/api/sets/{pid}/record")
    assert r.status_code == 409 and "已经在录了" in r.json()["detail"]
    t0 = time.time()
    while studio.recording["state"] == "running" and studio.recording["progress"] < 0.02 and time.time() - t0 < 60:
        time.sleep(0.05)
    st = client.get("/api/state").json()["recording"]
    assert st["state"] == "running" and st["progress"] >= 0.02 and "录到" in st["detail"], st
    assert any(name.endswith(".part.mp3") for name in recorded_files(studio) - before)  # 正在往临时文件里录
    assert client.post("/api/recording/cancel").json() == {"ok": True}
    rec = wait_recording(studio, 30)
    assert rec["state"] == "cancelled" and "file" not in rec, rec
    assert recorded_files(studio) == before
    r = client.post(f"/api/sets/{pid}/play", json={"speed": 8})
    assert r.status_code == 200, r.text
    wait_stage(studio, ("running",))
    assert studio.engine_state()["link"] == "mixxx-engine"
    studio.stop()
    wait_stage(studio, ("stopped",))


def test_recording_keeps_the_effect_tail_after_the_last_clip(example, monkeypatch):
    """最后一个片段停了以后还在响的尾音也录进去：尾音算在全场长度里，引擎放到那里才算放完（不是片段一停就收）。
    一个片段 4 小节，最后一小节送进 Echo（1/2 拍、反馈 0.6）：片段在第 16 拍停，尾音再响 8 拍，一拍比一拍小，最后淡完。"""
    from djstudio.v5.model import Project

    monkeypatch.setenv("DJSTUDIO_ENGINE_BIN", ENGINE_BIN)
    studio, pid = example
    a1 = next(a for a in studio.v5.load(pid).assets if a.id == "A1")
    studio.v5.save(Project.model_validate({
        "schema": "djset/3", "id": "tail-test", "name": "尾音", "tempo": [{"at": "1.1", "bpm": 132}],
        "assets": [json.loads(a1.model_dump_json())],
        "clips": [{"id": "C1", "asset": "A1", "deck": 1, "src": "25.1", "start": "1.1", "stop": "5.1"}],  # 原曲第 25 小节起：有 kick 的一段
        "fx": [{"id": "F1", "kind": "echo", "route": "send", "targets": ["C1"], "start": "4.1", "end": "5.1",
                "params": {"beats": 0.5, "feedback": 0.6}, "send": [{"at": "4.1", "value": 1.0, "step": True}]}]}))
    c = studio.v5.compiled("tail-test")
    assert c.ok, [str(i) for i in c.issues]
    assert max(k.stop for k in c.clips) == 16.0 and c.fx[0]["tail"] == 8.0 and c.length == 24.0
    studio.record("tail-test")
    rec = wait_recording(studio, 60)
    assert rec["state"] == "ok" and rec["cache_misses"] == 0, rec
    # 长度 = 片段 + 尾音（结尾淡到听不见的那一点点是数字静音，不录）
    assert c.duration - 0.5 < rec["seconds"] <= c.duration + 0.05, (rec["seconds"], c.duration)
    x = decode(Path(rec["file"]))
    beat = int(44100 * 60 / 132)
    db = [20 * np.log10(float(np.sqrt((x[b * beat:(b + 1) * beat] ** 2).mean())) + 1e-9) for b in range(23)]
    assert all(v > -25 for v in db[:16]), db  # 片段在放
    assert db[16] < db[15] - 5 and db[16] > -30, db  # 片段停了（干声没了），尾音还在
    assert all(db[b + 1] < db[b] - 5 for b in range(16, 22)) and db[22] < -60, db  # 一拍比一拍小，到结尾已经很轻：不是硬切


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_export_package_and_import_it_somewhere_else(ready, folder, tmp_path, monkeypatch):
    """A10：演出包 = 谱 + 原曲在同一层，字节不变、不出混音；挪到别处照样能导入；缺曲、内容被换都指得出是哪一个。"""
    monkeypatch.setenv("DJSTUDIO_ENGINE_BIN", "none")
    studio, res, job = ready
    out = studio.v5.export_package(res["project"], tmp_path / "out")
    pkg = Path(out["path"])
    names = sorted(f.name for f in pkg.iterdir())
    assert names == sorted(["服务测试.md", "alpha.wav", "beta.wav", "perc.wav", MANIFEST])  # 没有试听 / 录音
    for name in SPECS:
        assert sha(pkg / name) == sha(folder / name)
    man = json.loads((pkg / MANIFEST).read_text(encoding="utf-8"))
    assert {a["file"]: a["sha256"] for a in man["assets"]} == {n: sha(folder / n) for n in SPECS}
    assert man["caps"]["max_decks"] == 4 and man["exec"] and not list((tmp_path / "out").glob(".*"))  # 临时目录没留下
    # 整个包挪到别处，换一台「电脑」（全新的 Studio）导入
    moved = tmp_path / "别的电脑" / "包"
    shutil.copytree(pkg, moved)
    other = make_studio(tmp_path / "other")
    try:
        r2 = other.v5.import_folder(moved)
        assert r2["assets"] == 3
        wait_job(other)
        assert other.v5.compiled(r2["project"]).plan["clips"] == studio.v5.compiled(res["project"]).plan["clips"]
    finally:
        other.shutdown()
    # 缺一首：指出是哪一个；内容被换：指出是哪一个
    (moved / "beta.wav").unlink()
    third = make_studio(tmp_path / "third")
    try:
        with pytest.raises(StudioError, match=r"缺.*A2 beta\.wav"):
            third.v5.import_folder(moved)
        shutil.copyfile(moved / "alpha.wav", moved / "beta.wav")
        with pytest.raises(StudioError, match=r"内容不一样.*A2 beta\.wav"):
            third.v5.import_folder(moved)
    finally:
        third.shutdown()


def test_own_sample_files_travel_with_the_folder_and_the_package(tmp_path, monkeypatch):
    """谱旁边的采样文件：导入文件夹时带进来，编译时解码成引擎要的裸数据，导出演出包时和谱、原曲放同一层；缺了指出是哪一个。"""
    monkeypatch.setenv("DJSTUDIO_ENGINE_BIN", "none")
    monkeypatch.setenv("DJSTUDIO_NO_UPDATE", "1")
    monkeypatch.setenv("DJSTUDIO_FIND_DIRS", "")
    d = tmp_path / "带采样"
    d.mkdir()
    write_wav(d / "alpha.wav", techno(128.0, 0.25, 0, LAYOUT, seed=5))
    hit = np.zeros(4410, dtype=np.float32)
    hit[:2000] = 0.5 * np.sin(2 * np.pi * 900 * np.arange(2000) / 44100) * np.exp(-np.arange(2000) / 400)
    write_wav(d / "fill.wav", hit)
    (d / "带采样.md").write_text(f"""# 带采样

- 规范：v5
- 主速：128

## 素材

| ID | 曲名 | 文件名 | 原 BPM | 第 1 小节（秒） | 小节数 |
|---|---|---|---|---|---|
| A1 | Alpha | alpha.wav | 128 | {bar1('alpha.wav'):.3f} | 56 |

## 片段

| ID | 素材 | Deck | 原曲起点 | 全场起播 | 全场停止 |
|---|---|---|---|---|---|
| C1 | A1 | D1 | 9.1 | 1.1 | 17.1 |

## 效果

| ID | 类型 | 路由 | 目标 | 开始 | 结束 | 参数 | 说明 |
|---|---|---|---|---|---|---|---|
| F1 | 采样 | 采样 | fill.wav | 8.1 | 9.1 | 重复 1拍；音量 0.7 | 自己的采样 |
| F2 | 采样 | 采样 | clap | 12.1 | 13.1 | 重复 2拍 | 内置的 |
""", encoding="utf-8")
    studio = make_studio(tmp_path / "studio")
    try:
        res = studio.v5.import_folder(d)
        wait_job(studio)
        pid = res["project"]
        c = studio.v5.compiled(pid)
        assert c.ok, [str(i) for i in c.issues]
        smp = {x["id"]: x for x in c.plan["samples"]}
        # 采样文件是 22.05 kHz 的 4410 帧 = 0.2 秒：解码到引擎的 44.1 kHz 是 8820 帧，存成 float32 立体声
        assert smp["fill.wav"]["frames"] == 8820 and Path(smp["fill.wav"]["path"]).stat().st_size == 8820 * 8
        assert smp["clap"]["frames"] > 1000 and len([h for h in c.plan["hits"] if not h.get("stop")]) == 4 + 2
        out = studio.v5.export_package(pid, tmp_path / "out")
        pkg = Path(out["path"])
        assert sorted(f.name for f in pkg.iterdir()) == sorted(["带采样.md", "alpha.wav", "fill.wav", MANIFEST])
        assert sha(pkg / "fill.wav") == sha(d / "fill.wav")
        man = json.loads((pkg / MANIFEST).read_text(encoding="utf-8"))
        assert [x["file"] for x in man["samples"]] == ["fill.wav"] and man["samples"][0]["sha256"] == sha(d / "fill.wav")
        # 换一台电脑导入演出包：采样照样在
        other = make_studio(tmp_path / "other")
        try:
            r2 = other.v5.import_folder(pkg)
            wait_job(other)
            c2 = other.v5.compiled(r2["project"])
            assert c2.ok and c2.plan["hits"] == c.plan["hits"]
        finally:
            other.shutdown()
        # 缺了采样文件：导入时就说缺哪个
        (pkg / "fill.wav").unlink()
        third = make_studio(tmp_path / "third")
        try:
            with pytest.raises(StudioError, match=r"缺这些采样文件.*fill\.wav"):
                third.v5.import_folder(pkg)
        finally:
            third.shutdown()
    finally:
        studio.shutdown()


def test_score_errors_reach_the_user_with_line_numbers(ready, tmp_path):
    studio, res, job = ready
    bad = tmp_path / "坏谱"
    bad.mkdir()
    text = SCORE.replace("| 25.1 | 跳 7点 | 换低频 |", "| 25.9 | 跳 7点 | 换低频 |")
    (bad / "坏谱.md").write_text(text, encoding="utf-8")
    line = next(i for i, ln in enumerate(text.splitlines(), 1) if "25.9" in ln)
    with pytest.raises(StudioError, match=f"第 {line} 行"):
        studio.v5.import_folder(bad)


def test_demo5_check_needs_no_fake_mixxx_library(tmp_path, monkeypatch):
    """`demo5 --check`（构建机在打包好的应用里跑的那一步）：示例谱 → 合成素材 → 分析 → 编译 → 放 8 小节。
    不依赖假的 Mixxx 曲库：那份 schema 只在源码里、没有打进应用包，0.4.0 的 demo5 因此在应用里一运行就找不到文件。"""
    from djstudio.cli import main
    from djstudio.mixxx import fakedb

    monkeypatch.setenv("DJSTUDIO_ENGINE_BIN", "none")
    monkeypatch.setenv("DJSTUDIO_NO_UPDATE", "1")
    monkeypatch.setenv("DJSTUDIO_FIND_DIRS", "")
    monkeypatch.setattr(fakedb, "SCHEMA_SQL", tmp_path / "不在应用包里.sql")  # 应用包里的情况：读不到
    assert main(["demo5", "--check", "--dir", str(tmp_path / "demo")]) == 0


def test_data_files_the_app_reads_are_in_the_bundle():
    """djstudio/ 下不是 .py 的文件：要么在 pyproject 的 package-data 里（会打进应用包），要么是只给源码用的、
    并且应用里走得到的代码不碰它。新加了数据文件忘了打包，这里先红。"""
    import fnmatch
    import tomllib

    root = Path(__file__).resolve().parents[1]
    globs = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["setuptools"]["package-data"]["djstudio"]
    source_only = {"mixxx/schema_2_5_6.sql"}  # 假的 Mixxx 曲库的表结构：只有 v4 的 `djstudio demo`（源码里跑、要 node）和测试用
    pkg = root / "djstudio"
    for f in pkg.rglob("*"):
        if not f.is_file() or f.suffix in (".py", ".pyc") or "__pycache__" in f.parts:
            continue
        rel = f.relative_to(pkg).as_posix()
        packaged = any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(rel, g.replace("**/", "")) for g in globs)
        assert packaged or rel in source_only, f"{rel} 没有打进应用包（pyproject.toml 的 package-data）"
    # 用到那份表结构的只有 fakedb；用 fakedb 的只有 v4 的源码演示——四 Deck 的演示、应用本身都不碰
    users = sorted(f.relative_to(root).as_posix() for f in pkg.rglob("*.py") if "fakedb" in f.read_text(encoding="utf-8"))
    assert users == ["djstudio/demo.py"], users
