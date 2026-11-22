"""v5 在应用里的整条路：文件夹（谱 + 原曲）→ 导入 → 一键分析 → 演出（调度器）→ 停下 / 继续 → 演出包 → 换地方再导入。

音频是合成的（已知 BPM / 第 1 小节）；引擎用独立模拟器 sim5（DJSTUDIO_ENGINE_BIN=none）。
"""
import hashlib
import json
import shutil
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
