"""v5 在应用里的整条路：文件夹（谱 + 原曲）→ 导入 → 一键分析 → 演出（调度器）→ 停下 / 继续 → 演出包 → 换地方再导入。

音频是合成的（已知 BPM / 第 1 小节）；引擎用独立模拟器 sim5（DJSTUDIO_ENGINE_BIN=none）。
"""
import hashlib
import json
import shutil
import time
from pathlib import Path

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


def test_score_errors_reach_the_user_with_line_numbers(ready, tmp_path):
    studio, res, job = ready
    bad = tmp_path / "坏谱"
    bad.mkdir()
    text = SCORE.replace("| 25.1 | 跳 7点 | 换低频 |", "| 25.9 | 跳 7点 | 换低频 |")
    (bad / "坏谱.md").write_text(text, encoding="utf-8")
    line = next(i for i, ln in enumerate(text.splitlines(), 1) if "25.9" in ln)
    with pytest.raises(StudioError, match=f"第 {line} 行"):
        studio.v5.import_folder(bad)
