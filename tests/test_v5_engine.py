"""v5 原生调度器（tools/mixxx-engine/djs5）端到端：Python 编出的计划 → 调度器 → 录下主输出，在采样上量。

默认跑独立模拟器 sim5（调度器是同一份代码，deck 是假的：每拍出一个脉冲）；设了 DJSTUDIO_ENGINE_BIN 时
带 engine 的那几个测试在真正的声音引擎（Mixxx 2.5.6 的 deck）上再跑一遍。需求文档第 13 节的验收编号写在各测试上。
"""
import shutil
import time

import numpy as np
import pytest

from djstudio.compiler import fader_gain
from djstudio.v5.caps import BUILTIN, Caps
from djstudio.v5.link import Engine5Error, SIM_DIR, open_engine, sim5_binary

from .v5util import HAVE_ENGINE, SPECS, Run, click_wav, compiled, onsets, peak_at, project

pytestmark = pytest.mark.skipif(
    not (SIM_DIR / "sim5.cpp").is_file() or not (shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")),
    reason="没有 C++ 编译器（模拟器 sim5 要现编）")

BACKENDS = ["sim"] + (["engine"] if HAVE_ENGINE else [])
AMP = 0.045


@pytest.fixture(scope="module")
def wavs(tmp_path_factory):
    """声音引擎用的素材：和假 deck 同样的脉冲，写成 WAV。"""
    if not HAVE_ENGINE:
        return None
    d = tmp_path_factory.mktemp("v5wav")
    out = {}
    for k, (bpm, first) in SPECS.items():
        out[k] = d / f"{k}.wav"
        click_wav(out[k], bpm, first, 150.0)
    return out


def four(vol: float = 1.0, **extra):
    """四首素材：C1 从头放，另外三首在全场第 3 小节（第 8 拍）同一拍进。"""
    init = {"vol": vol}
    return project([
        {"id": "C1", "asset": "A1", "deck": 1, "start": "1.1", "stop": "17.1", "init": init},
        {"id": "C2", "asset": "A2", "deck": 2, "src": "5.1", "start": "3.1", "stop": "17.1", "init": init},
        {"id": "C3", "asset": "A3", "deck": 3, "src": "2.1", "start": "3.1", "stop": "13.1", "init": init},
        {"id": "C4", "asset": "A4", "deck": 4, "src": "9.1", "start": "3.1", "stop": "11.1", "init": init},
    ], **extra)


def test_caps_match_the_builtin_table():
    """编译时用的能力表（caps.BUILTIN）和调度器自己报的一样。"""
    with Run(compiled(four())) as r:
        got = Caps.from_engine(r.e.call({"cmd": "v5.caps"}))
    assert got.signature() == BUILTIN.signature(), (got, BUILTIN)


@pytest.mark.parametrize("backend", BACKENDS)
def test_four_decks_start_on_the_same_sample(backend, wavs, tmp_path):
    """A05 / A06：同一拍的起播在同一帧生效；四路计划重合的输出起点对齐。
    四首网格 BPM、相位都不同（128 / 130 / 132 / 126），全场 132：第 8 拍三首同时进，之后每拍四个脉冲叠在同一帧——
    幅度正好是四个之和（1 kHz 的脉冲错开 2 个采样就叠不满）。起播那一拍的脉冲是完整的（没有被起播的渐入削掉）。"""
    sim = backend == "sim"
    vol = 1.0 if sim else 0.25  # 声音引擎录成 16 位 WAV：叠起来不能超过满幅
    g = fader_gain(vol)
    # 模拟器：脉冲是算出来的，帧 ±1.5、幅度 4.5%（起点落在两个采样之间的哪里，采到的峰值在 0.968–1 之间变）。
    # 声音引擎：素材经过 Mixxx 的变速重采样和 16 位录音，帧 ±3（68 微秒；A06 的目标是 5 毫秒）、幅度 9%
    ftol, amp = (1.5, AMP) if sim else (3.0, 0.09)
    c = compiled(four(vol), dur=150.0, paths=None if sim else wavs)
    rec = tmp_path / ("out.f32" if sim else "out.wav")
    with Run(c, speed=40 if sim else 8, sim=sim, record=rec) as r:
        r.start()
        worst = 0.0
        while r.state() != "done":
            r.e.wait(lambda: False, 0.05)
            worst = max([worst] + [abs(d["phase_ms"]) for d in (r.e.status or {}).get("decks", [])])
        assert worst < 0.03, worst  # 每台 Deck 的实际位置和计划位置差不到 1.5 个采样
        assert [x["st"] for x in r.rc() if x["id"].endswith(".start")] == ["ok"] * 4
        assert not [x for x in r.rc() if "resync" in x["id"]]
        assert r.e.status["late"] == 0
        x = r.audio()

        def onset_err(b):
            want = r.frame_of(b)
            near = onsets(x[int(want) - 300:int(want) + 300], thr=0.3 * 0.4 * g)
            assert len(near) == 1, (b, near)
            return near[0] + int(want) - 300 - want

        for b in range(0, 8):  # 只有 C1：脉冲落在速度图算出来的那一帧
            assert abs(onset_err(b)) <= ftol, (b, onset_err(b))
        for b in (8, 12, 16, 20, 24, 28):  # 小节第 1 拍：四首各 0.8，叠在同一帧
            assert peak_at(x, r.frame_of(b)) == pytest.approx(3.2 * g, rel=amp), b
            assert abs(onset_err(b)) <= ftol, (b, onset_err(b))
        for b in (9, 10, 11, 13):  # 其余拍：各 0.4
            assert peak_at(x, r.frame_of(b)) == pytest.approx(1.6 * g, rel=amp), b
        # C4 在第 40 拍停：那一拍上它的脉冲不出来（门在停止点已经关完）；C3 第 48 拍停
        assert peak_at(x, r.frame_of(39)) == pytest.approx(1.6 * g, rel=amp)
        assert peak_at(x, r.frame_of(40)) == pytest.approx(0.8 * 3 * g, rel=amp)
        assert peak_at(x, r.frame_of(48)) == pytest.approx(0.8 * 2 * g, rel=amp)
        if not sim:  # 声音引擎数得到读盘没跟上的次数：一次就是某台 Deck 一块静音
            assert r.e.call({"cmd": "v5.perf"})["cache_misses"] == 0 and r.e.status["misses"] == 0


@pytest.mark.skipif(not HAVE_ENGINE, reason="要声音引擎（读盘线程是 Mixxx 的）")
def test_catching_up_at_high_speed_does_not_outrun_the_reader(wavs, tmp_path):
    """倍速（不出声）时引擎一口气补很多块：要让着 Mixxx 的读盘线程，不然会跑到它前面，那台 Deck 这一块是静音——
    四个脉冲的拍上只叠得出三个（Mac 构建机上出过）。100 倍速放完：读盘没跟上 0 次，每个小节第 1 拍四个脉冲都在。"""
    g = fader_gain(0.25)
    c = compiled(four(0.25), dur=150.0, paths=wavs)
    with Run(c, speed=100, sim=False, record=tmp_path / "out.wav") as r:
        r.start()
        r.wait_state("done", 120)
        perf = r.e.call({"cmd": "v5.perf"})
        assert perf["cache_misses"] == 0 and r.e.status["misses"] == 0, perf
        x = r.audio()
        for b in (8, 12, 16, 20, 24, 28, 32, 36):
            assert peak_at(x, r.frame_of(b)) == pytest.approx(3.2 * g, rel=0.09), b


@pytest.mark.skipif(not HAVE_ENGINE, reason="要声音引擎（装载是 Mixxx 的）")
@pytest.mark.parametrize("speed", [4, 100])
def test_a_deck_that_gets_another_track_waits_for_the_real_load(speed, wavs, tmp_path):
    """同一台 Deck 放完一首、换另一首：要等新曲真的装完才算装好。以前只看 track_loaded 和 deck 上挂的曲目——换曲的那一刻
    前者还是旧曲留下的 1、后者一交给 deck 就是新的，于是「0 毫秒就装好了」，预备点的定位被随后真正的装载清掉；倍速录音时
    连起播的定位也可能被清掉，那一段先从错的地方放、过几块再跳回来。
    这里量两样：起播之前 deck 停在预备点上（起播点往前 4 块）；起播以后不用重新定位，每个小节第 1 拍两个脉冲叠在同一帧。"""
    p = project([
        {"id": "C1", "asset": "A1", "deck": 1, "start": "1.1", "stop": "17.1", "init": {"vol": 0.25}},
        {"id": "C2", "asset": "A2", "deck": 2, "start": "1.1", "stop": "5.1", "init": {"vol": 0.25}},
        {"id": "C3", "asset": "A3", "deck": 2, "src": "9.1", "start": "9.1", "stop": "17.1", "init": {"vol": 0.25}},
    ])
    g = fader_gain(0.25)  # 录成 16 位：两个脉冲叠起来不能超过 1
    c = compiled(p, dur=150.0, paths=wavs)
    cue = SPECS["A3"][1] + 32 * 60.0 / SPECS["A3"][0] - 4 * 512 / 44100
    with Run(c, speed=speed, sim=False, record=tmp_path / "out.wav") as r:
        r.start()
        if speed <= 4:
            assert r.e.wait(lambda: r.state() == "running" and r.deck(2)["clip_id"] == "C3" and r.deck(2)["state"] == "ready", 30)
            time.sleep(0.4)  # 真正的装载这时早完了（几十毫秒）：位置要还在预备点
            d = r.deck(2)
            assert d["state"] == "ready" and d["source_sec"] == pytest.approx(cue, abs=0.002), d
        r.wait_state("done", 120)
        assert not r.rc("C3.resync"), r.rc("C3.resync")
        start = r.rc("C3.start")[0]
        assert start["st"] in ("ok", "late"), start
        perf = r.e.call({"cmd": "v5.perf"})
        assert perf["cache_misses"] == 0, perf
        x = r.audio()
        first = 32 if start["st"] == "ok" else 36  # 倍速下装得晚就晚进，位置照样在计划上
        for b in range(first, 64, 4):
            assert peak_at(x, r.frame_of(b)) == pytest.approx(1.6 * g, rel=0.09), (b, start)


def test_master_limiter_keeps_the_output_under_full_scale(tmp_path):
    """主输出的限幅器（dsp.hpp MasterLimiter）。四台 Deck 满推子叠在同一拍上是 3.2 倍满刻度：不压，声卡和录音就把它削平。
    开着限幅器：输出绝不超过 0.985；状态里报压了多少；过载计数照旧数的是进限幅器之前超的采样（看得出排得太满）。
    小电平时开不开都一样（逐位相同）。模拟器默认关（别的测试量的是没压过的电平），声音引擎默认开。"""
    def run(vol, limiter, name):
        with Run(compiled(four(vol)), record=tmp_path / f"{name}.f32") as r:
            got = r.e.call({"cmd": "v5.limiter", "on": limiter})
            assert got["on"] is limiter
            r.start()
            r.wait_beat(30.0)
            st = r.e.status["master"]
            r.wait_state("done", 60)
            f0 = int(round(r.frame_of(0.0)))  # 从第 0 拍起取一样长的一段（录音头尾的静音每遍不一样长）
            return r.audio()[f0:f0 + int(56 * 44100 * 60 / 132)], st

    loud, st = run(1.0, True, "loud")
    raw, st0 = run(1.0, False, "raw")
    assert np.abs(raw).max() == pytest.approx(3.2, rel=0.09) and st0["limiter"] is False and st0["limit_db"] == 0
    assert np.abs(loud).max() <= 0.985 + 1e-6
    assert st["limiter"] is True and st["limit_db"] < -0.5 and st["over"] > 0  # 脉冲很稀：读状态的那一刻正在放开，压得没有峰上那么深
    quiet_on, st2 = run(0.25, True, "quiet_on")
    quiet_off, _ = run(0.25, False, "quiet_off")
    assert np.array_equal(quiet_on, quiet_off) and st2["limit_db"] == 0 and st2["over"] == 0


@pytest.mark.skipif(not HAVE_ENGINE, reason="要声音引擎（默认开着限幅器的是它）")
def test_the_sound_engine_limits_by_default(wavs, tmp_path):
    """声音引擎默认开着限幅器：四台满推子叠着放，录下来的峰值不到满刻度（16 位的录音里没有削平的采样）。"""
    c = compiled(four(1.0), dur=150.0, paths=wavs)
    with Run(c, speed=20, sim=False, record=tmp_path / "out.wav") as r:
        r.start()
        r.wait_beat(30.0)
        assert r.e.status["master"]["limiter"] is True
        r.wait_state("done", 120)
        x = r.audio()
    assert 0.9 < np.abs(x).max() <= 0.986 and int((np.abs(x) >= 0.999).sum()) == 0


def test_runner_reports_reader_misses_once_in_a_while():
    """引擎报的 misses 涨了，Runner5 记一行日志（最多两秒一行）；没在放、或者引擎不报（模拟器）就不记；重新开始后从 0 数。"""
    from djstudio.v5.runner import Runner5

    r = Runner5(compiled(four()), link=None)  # type: ignore[arg-type]
    for st in ({"state": "idle", "misses": 3}, {"state": "running"}, {"state": "running", "misses": 0}):
        r._watch_misses(st)
    assert not r.progress.log
    r._watch_misses({"state": "running", "misses": 2})
    assert "读盘没跟上" in r.progress.log[-1] and "2 次" in r.progress.log[-1]
    r._watch_misses({"state": "running", "misses": 3})
    assert len(r.progress.log) == 1  # 刚写过
    r._miss_logged -= 3.0
    r._watch_misses({"state": "running", "misses": 3})
    assert len(r.progress.log) == 2 and "3 次" in r.progress.log[-1]
    r._watch_misses({"state": "prerun", "misses": 0})  # 重新开始：引擎从 0 数
    r._miss_logged -= 3.0
    r._watch_misses({"state": "running", "misses": 1})
    assert len(r.progress.log) == 3 and "1 次" in r.progress.log[-1]


def test_decks_are_independent(tmp_path):
    """A01：四段不同素材各放各的；动一台 Deck 的 EQ、推子、Loop 不改别的 Deck。"""
    p = four(lanes=[
        {"clip": "C2", "param": "lo", "points": [{"at": "5.1", "value": -25, "step": True}]},
        {"clip": "C2", "param": "vol", "points": [{"at": "6.1", "value": 0.5, "step": True}]},
        {"clip": "C4", "param": "filter", "points": [{"at": "4.1", "value": 0.0}, {"at": "6.1", "value": 0.6}]},
    ], loops=[{"id": "L1", "clip": "C3", "at": "5.1", "length": "2拍", "until": "9.1"}])
    with Run(compiled(p), speed=30) as r:
        r.start()
        st = r.wait_beat(26.0)
        decks = {d["deck"]: d for d in r.e.call({"cmd": "sim.decks"})["decks"]}
        assert [decks[d]["asset"] for d in (1, 2, 3, 4)] == ["A1", "A2", "A3", "A4"]
        assert decks[1]["params"] == {"lo": 1, "mid": 1, "hi": 1, "filter": 0.5}
        assert decks[2]["params"]["lo"] == pytest.approx(0.05623, abs=1e-4) and decks[2]["params"]["mid"] == 1
        assert decks[3]["params"] == {"lo": 1, "mid": 1, "hi": 1, "filter": 0.5}
        assert decks[4]["params"]["filter"] == pytest.approx(0.8, abs=0.01) and decks[4]["params"]["lo"] == 1
        assert [decks[d]["loop"] for d in (1, 2, 3, 4)] == [False, False, True, False]
        vols = [d["controls"]["vol"] for d in st["decks"]]
        assert vols[0] == pytest.approx(1.0, abs=1e-3) and vols[1] == pytest.approx(0.24025, abs=2e-3)
        assert vols[2] == pytest.approx(1.0, abs=1e-3) and vols[3] == pytest.approx(1.0, abs=1e-3)
        assert st["decks"][2]["state"] == "looping" and st["decks"][0]["state"] == "playing"


@pytest.mark.parametrize("backend", BACKENDS)
def test_loop_100_laps_and_a_jump_only_move_their_own_clip(backend, wavs, tmp_path):
    """A03：loop 转 100 遍以后源位置回绕正确，全场位置一直往前；跳段只改目标片段。"""
    sim = backend == "sim"
    vol = 1.0 if sim else 0.25
    g = fader_gain(vol)
    amp = AMP if sim else 0.09
    p = project([
        {"id": "C1", "asset": "A1", "deck": 1, "start": "1.1", "stop": "34.1", "init": {"vol": vol}},
        {"id": "C3", "asset": "A3", "deck": 3, "src": "2.1", "start": "2.1", "stop": "34.1", "init": {"vol": vol}},
    ], loops=[{"id": "L1", "clip": "C3", "at": "3.1", "length": "1拍", "until": "28.1"}],  # 100 遍
        jumps=[{"id": "J1", "clip": "C3", "at": "30.1", "to": "9.1"}])
    c = compiled(p, dur=150.0, paths=None if sim else wavs)
    assert c.ok, c.issues
    k3 = c.clip("C3")
    assert k3.src_beat_at(107.999) == pytest.approx(8.999, abs=1e-6)  # loop 里：原曲第 3 小节第 1 拍那一拍
    assert k3.src_beat_at(108.0) == 9.0 and k3.src_beat_at(116.0) == 32.0  # 出来接着放；跳到 9.1（第 32 拍）
    rec = tmp_path / ("out.f32" if sim else "out.wav")
    with Run(c, speed=40 if sim else 8, sim=sim, record=rec) as r:
        r.start()
        st = r.wait_beat(50.0, 120)
        assert st["decks"][2]["state"] == "looping" and st["decks"][2]["loop_lap"] >= 40
        st = r.wait_beat(120.0, 120)
        d1, d3 = st["decks"][0], st["decks"][2]
        assert d3["state"] == "playing"
        # 源位置 = 计划上的位置（跳段以后）；C1 没被动过
        assert d3["source_sec"] == pytest.approx(k3.info.beat_time(k3.src_beat_at(st["beat"])), abs=0.003)
        assert d1["source_sec"] == pytest.approx(c.clip("C1").info.beat_time(st["beat"]), abs=0.003)
        assert abs(d3["phase_ms"]) < 0.1 and abs(d1["phase_ms"]) < 0.1
        r.wait_state("done", 120)
        ids = [x["id"] for x in r.rc()]
        assert "L1" in ids and "L1.out" in ids and "J1" in ids
        assert not [i for i in ids if "resync" in i], ids  # 100 遍下来位置没有漂到要重新定位
        assert [x["st"] for x in r.rc() if x["id"] in ("L1", "L1.out", "J1")] == ["ok"] * 3
        x = r.audio()
        # loop 里 C3 每拍都是同一个脉冲（原曲第 8 拍 = 小节第 1 拍，0.8），和 C1 叠在同一帧
        for b in (20, 50, 90, 107):
            want = 0.8 + (0.8 if b % 4 == 0 else 0.4)
            assert peak_at(x, r.frame_of(b)) == pytest.approx(want * g, rel=amp), b
        assert peak_at(x, r.frame_of(109)) == pytest.approx((0.4 + 0.4) * g, rel=amp)  # 出 loop 以后：原曲第 10 拍
        assert peak_at(x, r.frame_of(116)) == pytest.approx((0.8 + 0.8) * g, rel=amp)  # 跳到原曲第 32 拍
        for b in (106, 107, 108, 109, 115, 116, 117):  # 出 loop、跳段前后脉冲都在拍线上
            want = r.frame_of(b)
            near = onsets(x[int(want) - 300:int(want) + 300], thr=0.3 * 0.4 * g)
            assert len(near) == 1 and abs(near[0] + int(want) - 300 - want) <= (1.5 if sim else 3.0), (b, near)


def test_tempo_map_and_audio_clock_agree(tmp_path):
    """A04（短的一段）：132 → 134 → 136 的全场速度图，脉冲落在 Python 的速度图算出来的那一帧——
    引擎和编译器是同一张图，秒数按 BPM 曲线积分。"""
    p = four(tempo=[{"at": "1.1", "bpm": 132}, {"at": "3.1", "bpm": 132}, {"at": "7.1", "bpm": 134, "ramp": True},
                    {"at": "9.1", "bpm": 134}, {"at": "13.1", "bpm": 136, "ramp": True}])
    c = compiled(p)
    with Run(c, record=tmp_path / "out.f32") as r:
        r.start()
        r.wait_state("done")
        assert not [x for x in r.rc() if "resync" in x["id"]]
        x = r.audio()
        for b in range(0, 60):
            want = r.frame_of(b)
            lo = int(want) - 200
            near = onsets(x[lo:lo + 400], thr=0.05)
            assert len(near) == 1 and abs(near[0] + lo - want) <= 2.0, (b, near, want - lo)
        for b in (16, 24, 36, 40):  # 升速途中四首仍然叠在同一帧
            assert peak_at(x, r.frame_of(b)) == pytest.approx(3.2 if b < 40 else 2.4, rel=AMP), b


def test_sixty_minutes_without_drift(tmp_path):
    """A04：60 分钟的排程相对音频时钟不累计漂移（模拟器里快进）。两台 Deck 从头放到尾，中间几次升速；
    最后一分钟的脉冲仍然落在速度图算出来的那一帧，位置差一直在 1 个采样左右。"""
    bars = 2040  # 约 61 分钟
    p = project([
        {"id": "C1", "asset": "A1", "deck": 1, "start": "1.1", "stop": f"{bars}.1"},
        {"id": "C2", "asset": "A2", "deck": 2, "src": "3.1", "start": "2.1", "stop": f"{bars}.1"},
    ], tempo=[{"at": "1.1", "bpm": 132}, {"at": "400.1", "bpm": 132}, {"at": "432.1", "bpm": 134, "ramp": True},
              {"at": "1200.1", "bpm": 134}, {"at": "1232.1", "bpm": 136, "ramp": True}])
    c = compiled(p, dur=4200.0)
    assert c.ok and c.duration > 3600
    with Run(c, speed=1) as r:
        r.start()
        r.wait_state("running", 10)
        worst = 0.0
        t0 = time.time()
        end = (bars - 1) * 4
        for speed, until in ((2000, end - 2500), (150, end - 200)):  # 先猛跑，快到头了放慢，停在最后一分钟以前
            r.e.call({"cmd": "sim.set", "speed": speed})
            while r.beat() < until and time.time() - t0 < 300:
                r.e.wait(lambda: False, 0.03)
                for d in r.e.status["decks"][:2]:
                    worst = max(worst, abs(d["phase_ms"]))
        r.e.call({"cmd": "sim.set", "speed": 20})
        rec = tmp_path / "tail.f32"
        r.rec_frame0 = r.e.call({"cmd": "sim.record", "path": str(rec)})["frame"]
        r.record = rec
        b0 = int(r.beat_of_frame(r.rec_frame0)) + 3  # 录音开始以后的第 3 拍起量
        r.wait_beat(b0 + 40, 60)
        x = r.audio()
        for b in range(b0, b0 + 32):
            want = r.frame_of(b)
            lo = int(want) - 200
            near = onsets(x[lo:lo + 400], thr=0.05)
            assert len(near) == 1 and abs(near[0] + lo - want) <= 2.0, (b, near, want - lo)
        assert worst < 0.1, worst
        assert not [x for x in r.rc() if "resync" in x["id"]]


def test_late_and_failed_loads(tmp_path):
    """A02 / A21：装得晚的片段晚进但位置照样对在计划上（回执标 late）；装不上的片段标失效，别的 Deck 照常放完。"""
    p = project([
        {"id": "C1", "asset": "A1", "deck": 1, "start": "1.1", "stop": "13.1"},
        {"id": "C2", "asset": "A2", "deck": 2, "start": "5.1", "stop": "13.1", "prep": "1小节"},
        {"id": "C3", "asset": "A3", "deck": 3, "start": "5.1", "stop": "13.1"},
    ])
    c = compiled(p)
    assert c.ok, c.issues
    with Run(c, speed=30, record=tmp_path / "out.f32") as r:
        r.e.call({"cmd": "sim.set", "load_ms": 2600})  # C2 只留了 1 小节（1.8 秒）准备
        r.e.call({"cmd": "sim.set", "fail": "A3"})
        r.start()
        r.wait_state("done")
        start2 = r.rc("C2.start")[0]
        assert start2["st"] == "late" and 16.0 < start2["at"] < 20.0
        assert [x["st"] for x in r.rc("C3.load")] == ["failed"] and [x["st"] for x in r.rc("C3.start")] == ["failed"]
        assert [x["st"] for x in r.rc("C1.stop")] == ["ok"]
        x = r.audio()
        assert peak_at(x, r.frame_of(16)) == pytest.approx(0.8, rel=AMP)  # C2 还没进、C3 没装上：只有 C1
        assert peak_at(x, r.frame_of(24)) == pytest.approx(1.6, rel=AMP)  # C2 晚进以后和 C1 叠在同一帧
        assert peak_at(x, r.frame_of(40)) == pytest.approx(1.6, rel=AMP)


def test_stale_load_does_not_touch_the_new_clip():
    """A02：迟到的装载回执和旧命令不影响新片段——同一台 Deck 刚要装 C2，马上跳到后面要装 C5 的地方。"""
    p = project([
        {"id": "C1", "asset": "A1", "deck": 1, "start": "1.1", "stop": "41.1"},
        {"id": "C2", "asset": "A2", "deck": 2, "start": "3.1", "stop": "9.1"},
        {"id": "C5", "asset": "A4", "deck": 2, "start": "21.1", "stop": "41.1"},
    ])
    c = compiled(p)
    with Run(c, speed=30) as r:
        r.e.call({"cmd": "sim.set", "load_ms": 1500})
        r.start()
        r.e.wait(lambda: r.state() in ("preparing", "running"), 5)
        r.e.call({"cmd": "v5.stop"})
        r.wait_state("idle", 5)
        r.start(beat=84.0)
        st = r.wait_beat(90.0)
        d2 = st["decks"][1]
        assert d2["clip_id"] == "C5" and d2["load_generation"] == c.clip("C5").gen and d2["state"] == "playing"
        assert r.e.call({"cmd": "sim.decks"})["decks"][1]["asset"] == "A4"
        assert not [x for x in r.rc("C2.start") if x["frame"] > r.rc("C5.load")[0]["frame"]]


@pytest.mark.parametrize("backend", BACKENDS)
def test_send_fx_instances_are_independent(backend, wavs, tmp_path):
    """A09：每个发送效果是自己的实例。D4 的 Delay 关送出、换片段，不改 D2 的 Echo；D4 原来的尾音不会采到 D4 的新片段。"""
    sim = backend == "sim"
    vol = 1.0 if sim else 0.25
    g = fader_gain(vol)
    p = project([
        {"id": "C2", "asset": "A2", "deck": 2, "start": "1.1", "stop": "9.1", "init": {"vol": vol}},
        {"id": "C4", "asset": "A4", "deck": 4, "start": "1.1", "stop": "3.1", "init": {"vol": vol}},
        {"id": "C5", "asset": "A3", "deck": 4, "start": "4.1", "stop": "9.1", "prep": "3拍", "init": {"vol": vol}},
    ], fx=[
        # D4：Delay 1/2 拍，反馈 0.5（一串回声，尾音拖进 C5 起播以后）
        {"id": "F1", "kind": "delay", "targets": ["C4"], "start": "1.1", "end": "3.1", "params": {"beats": 0.5, "feedback": 0.5}},
        # D2：Echo 1/4 拍（反馈 0：只回一下，落在拍 + 1/4 上，不和 D4 的拍 + 1/2 混在一起），一直开到后面
        {"id": "F2", "kind": "echo", "targets": ["C2"], "start": "2.1", "end": "8.1", "params": {"beats": 0.25, "feedback": 0.0}},
    ])
    c = compiled(p, dur=150.0, paths=None if sim else wavs)
    assert c.ok, c.issues
    f1, f2 = c.fx
    assert f1["slot"] != f2["slot"]
    rec = tmp_path / ("out.f32" if sim else "out.wav")
    with Run(c, speed=30 if sim else 8, sim=sim, record=rec) as r:
        r.start()
        r.wait_state("done", 120)
        x = r.audio()

        def pk(b):
            return peak_at(x, r.frame_of(b), 150)

        # D4 的 Delay：C4 在 0–7 拍，回声落在拍 + 1/2 上（第一下 = 1 倍，D2 的 Echo 那时还没开）
        assert pk(0.5) == pytest.approx(0.8 * g, rel=0.1) and pk(1.5) > 0.3 * g
        # D2 的 Echo 开了以后回声落在拍 + 1/4 上；D4 关送出（第 8 拍）、换片段之后它照样在
        assert pk(4.25) == pytest.approx(0.8 * 0.7079 * g, rel=0.12)
        assert pk(12.25) == pytest.approx(0.8 * 0.7079 * g, rel=0.12) and pk(20.25) > 0.4 * g
        assert pk(0.25) < 0.02 * g  # 开之前没有
        # C5 第 12 拍起播：它的脉冲只在拍上；拍 + 1/2 上只剩 C4 的尾音在衰减（要是尾音采到了 C5，这里会有 0.8）
        assert pk(12.5) < 0.06 * g and pk(16.5) < 0.02 * g and pk(20.5) < 0.02 * g
        assert pk(16.0) > 0.7 * g


def test_takeover_one_knob_and_return(tmp_path):
    """A12：接管一个旋钮只停那一个旋钮的自动化；回谱只恢复它，用一条短斜线走到那时的谱值，不回放过期的点。"""
    p = four(lanes=[
        {"clip": "C1", "param": "hi", "points": [{"at": "2.1", "value": 0}, {"at": "16.1", "value": -11.3}]},
        {"clip": "C1", "param": "lo", "points": [{"at": "4.1", "value": -25, "step": True}]},
        {"clip": "C2", "param": "vol", "points": [{"at": "4.1", "value": 0.5, "step": True},
                                                  {"at": "6.1", "value": 1.0, "step": True}]},
    ])
    c = compiled(p)
    with Run(c, speed=12) as r:
        r.start()
        r.wait_beat(9.0)
        r.e.call({"cmd": "v5.takeover", "deck": 1, "param": "hi", "value": 2.0})
        r.e.call({"cmd": "v5.takeover", "deck": 2, "param": "vol", "value": 0.1})
        st = r.wait_beat(24.0)
        d1, d2 = st["decks"][0], st["decks"][1]
        assert d1["controls"]["hi"] == pytest.approx(2.0) and d1["manual"] == ["hi"]
        assert d1["controls"]["lo"] == pytest.approx(0.05623, abs=1e-4)  # 同一台 Deck 的 LOW 照谱在第 4 小节切了
        assert d2["controls"]["vol"] == pytest.approx(0.1, abs=1e-3) and d2["manual"] == ["vol"]  # 谱上 4.1 / 6.1 的跳变没生效
        assert st["decks"][2]["manual"] == [] and st["decks"][2]["controls"]["vol"] == pytest.approx(1.0, abs=1e-3)
        r.e.call({"cmd": "v5.return", "deck": 1, "param": "hi", "when": "bar", "ramp": 4.0})
        r.e.call({"cmd": "v5.return", "deck": 2, "param": "vol", "when": "beat", "ramp": 1.0})
        st = r.wait_beat(30.2)
        hi = st["decks"][0]["controls"]["hi"]
        assert 0.3 < hi < 2.0 and st["decks"][0]["manual"] == ["hi"]  # 28 拍起走 4 拍的斜线，还在路上
        assert st["decks"][1]["manual"] == [] and st["decks"][1]["controls"]["vol"] == pytest.approx(1.0, abs=1e-3)
        st = r.wait_beat(34.0)
        want = 10 ** (-11.3 * (st["beat"] - 4) / 56 / 20)  # 那一刻谱上的值（4 拍到 60 拍从 0 到 -11.3 dB）
        assert st["decks"][0]["manual"] == [] and st["decks"][0]["controls"]["hi"] == pytest.approx(want, abs=0.03)


def test_pause_freezes_and_resume_keeps_the_beat(tmp_path):
    """第 9 节：全场暂停时四台 Deck 和效果一起冻住；继续时保持原来的全场拍。"""
    c = compiled(four())
    with Run(c, speed=20, record=tmp_path / "out.f32") as r:
        r.start()
        r.wait_beat(14.0)
        r.e.call({"cmd": "v5.pause"})
        r.wait_state("paused", 5)
        b0, f0 = r.beat(), r.e.status["frame"]
        time.sleep(0.4)
        assert r.beat() == b0 and r.e.status["frame"] == f0
        pos = [d["source_sec"] for d in r.e.status["decks"]]
        r.e.call({"cmd": "v5.resume"})
        r.wait_beat(b0 + 1.0)
        assert all(d["source_sec"] >= p0 for d, p0 in zip(r.e.status["decks"], pos))
        r.wait_state("done")
        x = r.audio()
        for b in (int(b0) + 4, int(b0) + 9, 36):  # 继续以后脉冲还在原来那条拍线上
            want = r.frame_of(b)
            lo = int(want) - 200
            near = onsets(x[lo:lo + 400], thr=0.05)
            assert len(near) == 1 and abs(near[0] + lo - want) <= 2.0, (b, near)


def _mid_project():
    return project([
        {"id": "C1", "asset": "A1", "deck": 1, "start": "1.1", "stop": "25.1"},
        {"id": "C2", "asset": "A2", "deck": 2, "src": "5.1", "start": "3.1", "stop": "25.1", "init": {"vol": 0.5, "lo": -25}},
        {"id": "C3", "asset": "A3", "deck": 3, "src": "2.1", "start": "3.1", "stop": "21.1"},
        {"id": "C4", "asset": "A4", "deck": 4, "src": "9.1", "start": "3.1", "stop": "12.1"},
    ], tempo=[{"at": "1.1", "bpm": 132}, {"at": "9.1", "bpm": 132}, {"at": "17.1", "bpm": 136, "ramp": True}],
        lanes=[{"clip": "C2", "param": "lo", "points": [{"at": "5.1", "value": -25}, {"at": "17.1", "value": 0}]},
               {"clip": "C2", "param": "vol", "points": [{"at": "7.1", "value": 1.0, "step": True}]},
               {"clip": "C1", "param": "hi", "points": [{"at": "5.1", "value": 0}, {"at": "21.1", "value": -11.3}]}],
        loops=[{"id": "L1", "clip": "C3", "at": "5.1", "length": "2拍", "until": "15.1"}],
        jumps=[{"id": "J1", "clip": "C3", "at": "17.1", "to": "31.1"}],
        fx=[{"id": "F1", "kind": "echo", "targets": ["C4"], "start": "9.1", "end": "12.1",
             "params": {"beats": 0.5, "feedback": 0.7}}])


@pytest.mark.parametrize("at", [36.0, 45.0, 70.0])
def test_start_in_the_middle_matches_a_continuous_run(at, tmp_path):
    """A11：在 loop 里（36）、升速和 Echo 尾音途中（45）、跳段以后（70）开始，四台 Deck 的位置、旋钮和
    一直放下来时一样；Echo 的尾音由静音预运行补出来（不是静音冒充）。"""
    c = compiled(_mid_project())
    assert c.ok, c.issues
    pre = c.prerun_from(at)
    assert (pre < at) == (at < 70.0)  # 36：Echo 开着；45：尾音还在；70：早就没了
    with Run(c, speed=40, record=tmp_path / "full.f32") as full:
        full.start()
        full.wait_state("done")
        xa = full.audio()
        with Run(c, speed=40, record=tmp_path / "mid.f32") as mid:
            mid.start(beat=at, prerun=pre)
            st = mid.wait_beat(at + 3.0)
            want = c.state_at(st["beat"])
            for d, w in zip(st["decks"], want["decks"]):
                if w["state"] in ("playing", "looping"):
                    k = c.clip(w["clip"])
                    assert d["clip_id"] == w["clip"] and d["state"] == w["state"], (d, w)
                    sec = k.info.beat_time(k.src_beat_at(st["beat"]))
                    assert abs(d["source_sec"] - sec) < 0.004 or w["state"] == "looping", (d, sec)
                    assert abs(d["phase_ms"]) < 0.2
                    native = {p: v for p, v in mid.c.plan["clips"][k.gen - 1]["init"].items()}
                    for prm in ("lo", "mid", "hi", "filter"):
                        pts = mid.c.plan["clips"][k.gen - 1]["curves"].get(prm)
                        exp = np.interp(st["beat"], [q[0] for q in pts], [q[1] for q in pts]) if pts else native[prm]
                        assert d["controls"][prm] == pytest.approx(exp, abs=0.04), (d["deck_id"], prm)
                else:
                    assert d["state"] in ("idle", "ready", "loading", "stopped"), (d, w)
            mid.wait_state("done")
            xb = mid.audio()
            assert peak_at(xb, mid.frame_of(at - 1.0)) < 1e-4  # 开始以前没有声音（预运行不出声）
            for b in np.arange(at + 1.0, 94.0, 0.5):  # 拍上的脉冲和拍 + 1/2 上的 Echo 尾音都和一直放下来的一样
                pa, pb = peak_at(xa, full.frame_of(b), 150), peak_at(xb, mid.frame_of(b), 150)
                # 拍上的脉冲容 6%（采样相位）；拍 + 1/2 上是好几下回声叠起来的（延迟取整到帧，彼此差零点几帧），容 15%
                tol = 0.06 if b == int(b) else 0.15
                assert pb == pytest.approx(pa, abs=0.01 + tol * pa), (b, pa, pb)
            if at == 45.0:  # 真的有尾音可比：45.5 那一下回声全是第 44 拍以前送进去的声音
                assert peak_at(xb, mid.frame_of(45.5), 150) > 0.05


def test_engine_rejects_what_it_cannot_do():
    """A22 的引擎一侧：计划里有引擎做不了的东西（Deck 太多、不认识的效果、实例超了）→ 整份拒绝，不会略过了照放。"""
    c = compiled(four())
    e = open_engine(c.library_rows(), speed=10, sim=True, decks=2)
    try:
        with pytest.raises(Engine5Error, match="Deck"):
            e.call({"cmd": "v5.plan", "plan": c.plan})
        ok = compiled(project([{"id": "C1", "asset": "A1", "deck": 1, "start": "1.1", "stop": "9.1"}]))
        bad = {**ok.plan, "fx": [{"id": "F9", "kind": "roll", "clip": "C1", "slot": 0, "start": 0, "end": 4, "tail": 0,
                                  "p": {}, "send": [[0, 1]], "wet": [[0, 1]]}]}
        with pytest.raises(Engine5Error, match="roll"):
            e.call({"cmd": "v5.plan", "plan": bad})
        bad["fx"][0].update(kind="echo", slot=99)
        with pytest.raises(Engine5Error, match="实例"):
            e.call({"cmd": "v5.plan", "plan": bad})
        e.call({"cmd": "v5.plan", "plan": ok.plan})
    finally:
        e.close()


def test_sim_binary_is_cached():
    a = sim5_binary()
    t0 = time.time()
    assert sim5_binary() == a and time.time() - t0 < 0.5
