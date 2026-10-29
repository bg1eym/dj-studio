"""服务层集成测试：导入音频 → 分析 → （假）Mixxx 库 → 编译 → 准备 → 在模拟器里播完整套。"""
import json
import sqlite3
from pathlib import Path
import shutil
import time

import pytest

from djstudio.config import Config
from djstudio.engine.link import SimLink
from djstudio.library.audio import have_ffmpeg
from djstudio.mixxx import process
from djstudio.setfile import SetFile
from djstudio.studio import Studio

from djstudio.mixxx import fakedb as fake_mixxxdb
from djstudio.synth import techno, write_wav

pytestmark = pytest.mark.skipif(not have_ffmpeg() or shutil.which("node") is None, reason="需要 ffmpeg 和 node")

LAYOUT = (("intro", 8), ("groove", 16), ("breakdown", 8), ("drop", 16), ("outro", 8))
SPECS = {  # 文件名 → (bpm, first_beat, downbeat)
    "a.wav": (128.0, 0.25, 0),
    "b.wav": (130.0, 0.40, 2),
    "c.wav": (132.0, 0.10, 1),
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    src = tmp_path / "src" / "Promo Pack"
    src.mkdir(parents=True)
    for name, (bpm, fb, db) in SPECS.items():
        write_wav(src / name, techno(bpm, fb, db, LAYOUT, seed=hash(name) % 100))
    mixxx_dir = tmp_path / "mixxx"
    mixxx_dir.mkdir()
    lib = tmp_path / "Music" / "DJStudio"
    fake_mixxxdb.create(mixxx_dir / "mixxxdb.sqlite", [lib])
    cfg = Config(home=tmp_path / "home", library=lib, mixxx_dir=mixxx_dir, browse_roots=[tmp_path])

    state = {"running": False, "sim": None}
    monkeypatch.setattr(process, "mixxx_running", lambda: state["running"])

    def launch():
        state["running"] = True

    def quit_(timeout=30.0):
        state["running"] = False
        return True

    monkeypatch.setattr(process, "launch_mixxx", launch)
    monkeypatch.setattr(process, "quit_mixxx", quit_)

    studio_box = {}

    def link_factory():
        st = studio_box["s"]
        rows = []
        for t in st.store.all():
            m = st._mixxx_tracks([t]).get(t.id)
            if m:
                rows.append({"id": str(m.id), "dur": m.duration, "bpm": m.grid.bpm, "first_beat": m.grid.first_beat})
        link = SimLink(rows, speed=40)
        link.sim({"cmd": "queue", "ids": [str(i) for i in st.mixxx.autodj_queue()]})
        state["sim"] = link
        return link

    studio = Studio(cfg, link_factory=link_factory)
    studio_box["s"] = studio
    yield studio, src, state
    studio.disconnect()


def wait_job(job, timeout=60):
    t0 = time.time()
    while job.state == "running" and time.time() - t0 < timeout:
        time.sleep(0.1)
    return job


def test_import_analyze_prepare_play(env):
    studio, src, state = env

    res = studio.library.import_paths([str(src)])
    assert len(res.added) == 3 and not res.failed
    again = studio.library.import_paths([str(src / "a.wav")])
    assert again.existing == [res.added[0]] or len(again.existing) == 1
    assert studio.library.wait_idle(60)

    by_name = {t.path.split("/")[-1]: t for t in studio.store.all()}
    assert set(by_name) == set(SPECS)
    for name, t in by_name.items():
        assert "Promo Pack" in t.path
        bpm, fb, db = SPECS[name]
        a = t.analysis
        assert a and a["grid_source"] == "own"
        assert abs(a["bpm"] - bpm) < 0.05
        # 第 1 小节起点 = 网格第 downbeat 拍
        assert t.bar1 == pytest.approx(fb + db * 60 / bpm, abs=0.03)
        assert studio.store.waveform_path(t.id).exists()

    # 还不在 Mixxx 库 → 编译报错
    s = SetFile.model_validate({
        "schema": "djset/2", "id": "it", "name": "集成测试",
        "tracks": [{"id": by_name[n].id, "title": n} for n in ("a.wav", "b.wav", "c.wav")],
        "transitions": [
            {"technique": "longhand", "start": 25, "b_in": 1, "bars": 24},
            {"technique": "instant", "start": 41, "b_in": 33},
        ],
        "ending": {"mode": "fade", "bar": 45, "bars": 8},
    })
    studio.save(s)
    c = studio.compile(s)
    assert not c.ok and any("Mixxx 曲库" in i.msg for i in c.issues)

    # 「Mixxx 扫描 + 分析」之后
    db = studio.cfg.mixxx_db
    for name, t in by_name.items():
        bpm, fb, _ = SPECS[name]
        fake_mixxxdb.add_track(db, t.path, t.duration, bpm, fb, title=name)
    c = studio.compile(s)
    assert c.ok, [str(i) for i in c.issues]

    job = wait_job(studio.prepare("it", settle=0.5))
    assert job.state == "ok", "\n".join(job.log)
    assert studio.prepared()["set"] == "it"
    assert len(studio.mixxx.autodj_queue()) == 3
    assert (studio.cfg.mixxx_controllers / "DJStudio.js").exists()
    assert list((studio.cfg.home / "backups").glob("*.sqlite"))

    studio.play("it")
    studio.conductor.join(90)
    p = studio.conductor.progress
    assert p.stage == "done", "\n".join(p.log) + f"\n{p.error}"
    logs = list(studio.cfg.logs_dir.glob("*-it.log"))
    assert logs and "整套播完" in logs[0].read_text()


def test_doctor_reports(env):
    studio, _, state = env
    rows = {r["name"]: r for r in studio.doctor()}
    assert rows["Mixxx 曲库"]["level"] == "ok"
    assert rows["Mixxx 曲库目录"]["level"] == "ok"
    assert rows["引擎脚本"]["level"] == "warn"
    assert rows["Mixxx 进程"]["level"] == "warn"


def test_browse_restricted(env, tmp_path):
    from djstudio.library.importer import browse

    studio, src, _ = env
    out = browse(str(src), studio.cfg.browse_roots)
    assert {f["name"] for f in out["files"]} == set(SPECS)
    with pytest.raises(PermissionError):
        browse("/etc", studio.cfg.browse_roots)


def test_sync_mixxx_analysis_loads_each_track_once(tmp_path, monkeypatch):
    """同步到 Mixxx：没分析的歌经 AutoDJ + skip_next 每首装载一次；之后等数据库出现真正的分析网格。"""
    import threading

    from djstudio.mixxx import fakedb as fdb

    lib = tmp_path / "Music" / "DJStudio"
    lib.mkdir(parents=True)
    mixxx_dir = tmp_path / "mixxx"
    mixxx_dir.mkdir()
    fdb.create(mixxx_dir / "mixxxdb.sqlite", [lib])
    cfg = Config(home=tmp_path / "home", library=lib, mixxx_dir=mixxx_dir, browse_roots=[tmp_path])
    state = {"running": False, "link": None}
    monkeypatch.setattr(process, "mixxx_running", lambda: state["running"])
    monkeypatch.setattr(process, "launch_mixxx", lambda: state.update(running=True))
    monkeypatch.setattr(process, "quit_mixxx", lambda timeout=30.0: state.update(running=False) or True)

    names = ["p.wav", "q.wav", "r.wav", "s.wav"]
    specs = {"p.wav": 126.0, "q.wav": 128.0, "r.wav": 130.0, "s.wav": 131.0}
    for n in names:
        write_wav(lib / n, techno(specs[n], 0.2, 0, (("groove", 8),), seed=len(n)))
    box = {}

    def link_factory():
        st = box["s"]
        rows = []
        for t in st.store.all():
            m = st._mixxx_tracks([t]).get(t.id)
            rows.append({"id": str(m.id), "dur": m.duration, "bpm": specs[Path(t.path).name], "first_beat": 0.2})
        link = SimLink(rows, speed=20)
        link.sim({"cmd": "queue", "ids": [str(i) for i in st.mixxx.autodj_queue()]})
        state["link"] = link
        return link

    studio = Studio(cfg, link_factory=link_factory)
    box["s"] = studio
    studio.library.import_paths([str(lib)], analyze_now=False)
    tracks = studio.store.all()
    mids = {}
    for t in tracks:  # 已扫描进库、没分析
        mids[fdb.add_track(cfg.mixxx_db, t.path, t.duration, None)] = t

    # 假的「Mixxx 分析器」：曲目被装载后把真正的网格写进库
    stop = threading.Event()

    def analyzer():
        done = set()
        while not stop.is_set():
            link = state["link"]
            if link:
                try:
                    loads = link.sim({"cmd": "state"})["loads"]
                except Exception:  # noqa: BLE001
                    loads = []
                for _, _, mid in loads:
                    mid = int(mid)
                    if mid not in done:
                        done.add(mid)
                        t = mids[mid]
                        with sqlite3.connect(cfg.mixxx_db) as c:
                            c.execute("UPDATE library SET bpm=?, beats=?, beats_version='BeatGrid-2.0', "
                                      "beats_sub_version='plugin_id=qm' WHERE id=?",
                                      (specs[Path(t.path).name], encode_grid_v2(specs[Path(t.path).name], 0.2, 44100), mid))
            time.sleep(0.1)

    import sqlite3
    from djstudio.mixxx.beats import encode_grid_v2

    th = threading.Thread(target=analyzer, daemon=True)
    th.start()
    job = wait_job(studio.sync_mixxx(settle=0.3), 90)
    stop.set()
    assert job.state == "ok", "\n".join(job.log)
    loads = [int(x[2]) for x in state["link"].sim({"cmd": "state"})["loads"]]
    assert set(loads) == set(mids), loads  # 每首至少装过一次（AutoDJ 可能重复装同一首）
    mx = studio._mixxx_tracks(tracks)
    assert all(m.analyzed for m in mx.values())
    studio.disconnect()


def test_offbeat_mixxx_grid_detected_and_fixed(env):
    """Mixxx 把网格放到反拍（真曲上实测到的情况）→ 网格存疑 → 校验报错 → 校正写库并锁定 → 通过。"""
    from djstudio.mixxx.beats import decode

    studio, src, state = env
    studio.library.import_paths([str(src)])
    assert studio.library.wait_idle(60)
    by_name = {t.path.split("/")[-1]: t for t in studio.store.all()}
    db = studio.cfg.mixxx_db
    ids = {}
    for name, t in by_name.items():
        bpm, fb, _ = SPECS[name]
        off = fb + 0.5 * 60 / bpm if name == "b.wav" else fb  # b 的 Mixxx 网格偏半拍
        ids[name] = fake_mixxxdb.add_track(db, t.path, t.duration, bpm, off, samplerate=22050, title=name)
    for t in by_name.values():
        studio.library.enqueue(t.id)
    assert studio.library.wait_idle(60)
    b = studio.store.get(by_name["b.wav"].id)
    gc = b.analysis["grid_check"]
    assert not gc["ok"] and "反拍" in gc["note"], gc
    assert studio.store.get(by_name["a.wav"].id).analysis["grid_check"]["ok"]

    s = SetFile.model_validate({"schema": "djset/2", "id": "g", "tracks": [{"id": by_name["a.wav"].id},
                                {"id": by_name["b.wav"].id}], "transitions": [{"technique": "instant", "start": 41}]})
    assert any("网格存疑" in i.msg for i in studio.compile(s).issues)

    studio.store.update(b.id, bar1_confirmed=True)  # 用户在反拍网格上确认过小节线
    plan = studio.grid_fix_plan()
    assert [p["title"] for p in plan] == [b.display]
    job = wait_job(studio.fix_grids(), 60)
    assert job.state == "ok", "\n".join(job.log)
    assert not studio.store.get(b.id).bar1_confirmed and any("重新核对小节线" in x for x in job.log)
    m = studio.mixxx.tracks_by_location([b.path])[b.path]
    assert m.bpm_lock and m.analyzed
    g = decode(sqlite3.connect(db).execute("SELECT beats FROM library WHERE id=?", (ids["b.wav"],)).fetchone()[0],
               "BeatGrid-2.0", 22050)
    beat = 60 / SPECS["b.wav"][0]
    ph = ((g.first_beat - SPECS["b.wav"][1]) / beat + 0.5) % 1 - 0.5
    assert abs(ph) < 0.05, ph  # 校正后网格线回到 kick 上
    assert studio.store.get(b.id).analysis["grid_check"]["ok"]
    assert not any("网格存疑" in i.msg for i in studio.compile(s).issues)
