"""v5 现场效果（P1）：处理组的路由、分频、Release、Roll / 采样的时间、从中间开始的恢复、能力拒绝。

对应需求里的 A13–A17、A22。声音是模拟器的假 deck：每拍一个脉冲（看时间），或 −12 dBFS RMS 的白噪声 / 扫频（看电平和残差）。
"""
import shutil
from pathlib import Path

import numpy as np
import pytest

from djstudio.v5 import score
from djstudio.v5.caps import BASIC, BUILTIN
from djstudio.v5.link import SIM_DIR
from djstudio.v5.writer import write
from tests.v5util import SR, Run, compiled, onsets, project

pytestmark = pytest.mark.skipif(not (SIM_DIR / "sim5.cpp").is_file() or not (shutil.which("c++") or shutil.which("g++")),
                                reason="需要 C++ 编译器（现编模拟器）")

BPM = 128.0  # = 素材 A1 的速度：deck 速率正好是 1，噪声夹具逐位可重复
SPB = SR * 60.0 / BPM  # 一拍多少帧
FULL = {"vol": 1.0}


def db(v: np.ndarray) -> float:
    return 20.0 * np.log10(max(1e-12, float(np.sqrt(np.mean(np.square(v, dtype=np.float64))))))


def clip(cid: str, asset: str, deck: int, start: str = "1.1", stop: str = "9.1", **kw):
    return {"id": cid, "asset": asset, "deck": deck, "start": start, "stop": stop, "init": FULL, **kw}


def fx(fid: str, kind: str, route: str, targets: list[str], start: str, end: str, **params):
    tail = params.pop("tail", None)
    out = {"id": fid, "kind": kind, "route": route, "targets": targets, "start": start, "end": end,
           "params": {k: str(v) for k, v in params.items()}}
    if tail is not None:
        out["tail"] = tail
    return out


def render(tmp_path: Path, clips: list[dict], effects: list[dict], waves: dict[str, str] | None = None, beats: float = 32.0,
           start: float = 0.0, name: str = "x", tempo: float = BPM) -> tuple[np.ndarray, "Run", list[dict]]:
    """放一遍，返回从全场第 start 拍起的主输出（左声道）、回执。start > 0：按编译结果算的预跑起点从中间开始。"""
    c = compiled(project(clips, tempo=[{"at": "1.1", "bpm": tempo}], fx=effects))
    with Run(c, speed=40, record=tmp_path / f"{name}.f32") as r:
        for asset, wave in (waves or {}).items():
            r.e.call({"cmd": "sim.set", "wave": wave, "asset": asset})
        r.start(start, c.prerun_from(start) if start else None)
        r.wait_state("done", 90)
        x = r.audio()
        f0 = int(round(r.frame_of(start)))
        n = int((beats - start) * SR * 60.0 / tempo)
        return x[f0:f0 + n], r, r.rc()


def beat_peaks(x: np.ndarray, n_beats: int, first: int = 0) -> list[float]:
    """每一拍开头 6 ms 里的峰值（脉冲在不在、多大）。x 从全场第 first 拍起。"""
    w = int(0.006 * SR)
    out = []
    for k in range(n_beats):
        i = int(round(k * SPB))
        out.append(float(np.abs(x[max(0, i - 4):i + w]).max()) if i + w <= len(x) else 0.0)
    return out


# ---------------------------------------------------------------- A13 路由

def test_insert_bypass_is_the_same_signal_and_the_same_level(tmp_path):
    """分频的各段都不动：进组和直通是同一个信号（残差远低于 −60 dBFS），电平差 ≤ 0.1 dB——没有重复的干声。"""
    for wave in ("noise", "sweep"):
        ref, r0, _ = render(tmp_path, [clip("C1", "A1", 1)], [], {"A1": wave}, name="ref")
        neu, r1, _ = render(tmp_path, [clip("C1", "A1", 1)],
                            [fx("F1", "trans", "insert", ["C1"], "3.1", "7.1", 频段="HIGH", 深度=0)], {"A1": wave}, name="neu")
        assert r0.latency == 0 and r1.latency == BUILTIN.split_latency
        n = min(len(ref), len(neu))
        assert abs(db(ref[:n]) + 12.0) < 0.3, db(ref[:n])  # 夹具是 −12 dBFS RMS
        assert abs(db(neu[:n]) - db(ref[:n])) <= 0.1
        assert db(ref[:n] - neu[:n]) <= -60.0, (wave, db(ref[:n] - neu[:n]))


def test_send_takes_the_signal_after_the_insert(tmp_path):
    """插入组在推子前、发送取推子后：被 Trans 切掉的那几拍不在主输出里（没有另一条干声），也不会被送进 Delay。"""
    clips = [clip("C1", "A1", 1)]
    gate = fx("F1", "trans", "insert", ["C1"], "1.1", "9.1", 周期="2拍", 占空="1/2", 深度=1)  # 单数拍留、双数拍切
    delay = {"id": "F2", "kind": "delay", "route": "send", "targets": ["C1"], "start": "1.1", "end": "9.1",
             "params": {"beats": 0.25, "feedback": 0.0}}
    x, _, _ = render(tmp_path, clips, [gate, delay])
    peaks = beat_peaks(x, 28)
    assert all(p > 0.3 for p in peaks[0::2]), peaks  # 第 1、3 拍…（开着的半个周期）在
    assert all(p < 0.004 for p in peaks[1::2]), peaks  # 第 2、4 拍…整个没了：没有绕过处理组的干声
    q = int(round(0.25 * SPB))
    echoes = [float(np.abs(x[int(round(k * SPB)) + q - 4:int(round(k * SPB)) + q + 260]).max()) for k in range(1, 27)]
    assert all(e > 0.1 for e in echoes[1::2]) and all(e < 0.004 for e in echoes[0::2]), echoes


def test_group_insert_replaces_the_direct_paths_of_its_members_only(tmp_path):
    """组插入：成员不再各自直通（没有重复的那一份），组外的 Deck 不受影响。"""
    clips = [clip("C1", "A1", 1), clip("C2", "A1", 2), clip("C3", "A1", 3, src="1.3")]  # C3 的小节线错开两拍
    solo, _, _ = render(tmp_path, [clips[2]], [], name="solo")
    x, _, rc = render(tmp_path, clips, [fx("F1", "trans", "group-insert", ["C1", "C2"], "1.1", "9.1", 周期="2拍", 深度=1)])
    both, _, _ = render(tmp_path, clips, [], name="both")
    assert next(m for m in rc if m["id"] == "F1")["st"] == "ok"
    px, pb, ps = beat_peaks(x, 28), beat_peaks(both, 28), beat_peaks(solo, 28)
    for k in range(28):
        if k % 2 == 0:
            assert px[k] == pytest.approx(pb[k], rel=0.02), k  # 组开着：和不处理时一样大（两路各一份）
        else:
            assert px[k] == pytest.approx(ps[k], rel=0.02), k  # 组关着：只剩组外的 C3


def test_master_insert_processes_returns_and_samples_too(tmp_path):
    """总输出：效果返回和采样也在里面。总输出上切掉的那半拍里，Delay 的回声和采样都没有。"""
    clips = [clip("C1", "A1", 1)]
    effects = [
        {"id": "F1", "kind": "delay", "route": "send", "targets": ["C1"], "start": "1.1", "end": "9.1",
         "params": {"beats": 0.5, "feedback": 0.0}},
        fx("F2", "sample", "master-insert", ["rim"], "1.1", "9.1", 重复="1拍", 音量=1),
        fx("F3", "trans", "master-insert", [], "3.1", "9.1", 周期="1拍", 占空="1/4", 深度=1),
    ]
    x, _, _ = render(tmp_path, clips, effects)
    h = int(round(0.5 * SPB))

    def half(k: int) -> float:  # 第 k 拍后半拍开头（回声落在这）
        i = int(round(k * SPB)) + h
        return float(np.abs(x[i - 4:i + 260]).max())

    assert all(half(k) > 0.1 for k in range(1, 7))  # 总输出的 Trans 还没开：回声在
    assert all(half(k) < 0.004 for k in range(9, 27))  # 开了：后 3/4 拍是静音，回声也被切掉
    assert all(p > 0.5 for p in beat_peaks(x, 28)[8:])  # 拍点上（开着的 1/4）脉冲 + 采样都在


def test_score_rules_for_routes_are_errors_with_line_numbers():
    base = [clip("C1", "A1", 1), clip("C2", "A2", 2)]
    cases = [
        ([fx("F1", "roll", "send", ["C1"], "3.1", "4.1")], "fx_route"),
        ([fx("F1", "roll", "insert", ["C1", "C2"], "3.1", "4.1")], "fx_target"),
        ([fx("F1", "roll", "group-insert", ["C1"], "3.1", "4.1")], "fx_target"),
        ([fx("F1", "trans", "insert", ["C1"], "3.1", "5.1"), fx("F2", "trans", "group-insert", ["C1", "C2"], "4.1", "6.1")],
         "bus_overlap"),
        ([fx("F1", "trans", "insert", ["C1"], "3.1", "5.1"), fx("F2", "flanger", "master-insert", [], "4.1", "6.1")],
         "bus_overlap"),
        ([fx("F1", "roll", "insert", ["C1"], "3.1", "5.1"), fx("F2", "roll", "insert", ["C1"], "4.1", "6.1")], "fx_overlap"),
        ([fx("F1", "trans", "insert", ["C1"], "3.1", "5.1", 频段="BASS")], "fx_param"),
        ([fx("F1", "filter", "insert", ["C1"], "3.1", "5.1")], "fx_param"),  # 没写截止
        ([fx("F1", "release", "insert", ["C1"], "3.1", "4.1", 方式="刹车")], "fx_param"),  # 没写收尾
        ([fx("F1", "sample", "master-insert", ["nope.wav"], "3.1", "4.1")], "sample_missing"),
        ([fx("F1", "sample", "master-insert", ["kick"], "3.1", "5.1"), fx("F2", "sample", "master-insert", ["hat"], "4.1", "6.1")],
         "sample_overlap"),
    ]
    for effects, code in cases:
        for i, f in enumerate(effects):
            f["line"] = 40 + i
        c = compiled(project(base, fx=effects))
        assert [i.code for i in c.errors] == [code], (code, [str(i) for i in c.issues])
        assert c.errors[0].line in (40, 41) and c.plan is None


# ---------------------------------------------------------------- A14 分频

def test_processing_one_band_leaves_the_others_alone(tmp_path):
    """只切 HIGH：1 kHz 的脉冲（在 MID 里）每一拍都原样在；宽带噪声里高频段在关着的半拍里没了、低中频还在。"""
    x, _, _ = render(tmp_path, [clip("C1", "A1", 1)], [fx("F1", "trans", "insert", ["C1"], "1.1", "9.1", 频段="HIGH", 周期="1拍", 深度=1)])
    ref, _, _ = render(tmp_path, [clip("C1", "A1", 1)], [], name="ref")
    for a, b in zip(beat_peaks(x, 28), beat_peaks(ref, 28)):
        assert a == pytest.approx(b, rel=0.06)
    y, _, _ = render(tmp_path, [clip("C1", "A1", 1)],
                     [fx("F1", "trans", "insert", ["C1"], "1.1", "9.1", 频段="HIGH", 周期="1拍", 深度=1)], {"A1": "noise"}, name="n")

    def band_db(seg: np.ndarray, lo: float, hi: float) -> float:
        sp = np.abs(np.fft.rfft(seg * np.hanning(len(seg)))) ** 2
        f = np.fft.rfftfreq(len(seg), 1.0 / SR)
        return 10.0 * np.log10(float(sp[(f >= lo) & (f < hi)].sum()) + 1e-20)

    n = int(0.3 * SPB)
    for k in range(2, 26):
        on = y[int(k * SPB + 0.1 * SPB):][:n]
        off = y[int(k * SPB + 0.6 * SPB):][:n]
        assert band_db(on, 5000, 15000) - band_db(off, 5000, 15000) > 40.0  # 高频：关着的半拍里没了
        assert abs(band_db(on, 40, 150) - band_db(off, 40, 150)) < 8.0  # 低频：一直在（窄带噪声自己就有几 dB 的起伏）
        assert abs(band_db(on, 500, 1500) - band_db(off, 500, 1500)) < 4.0  # 中频：一直在


def test_gate_edges_do_not_clip_the_transient_on_the_beat(tmp_path):
    """整路 Trans（每拍开前半拍）：开门的沿在拍点之前走完，拍点上的脉冲和不处理时一样大；关着的半拍是静音。"""
    x, _, _ = render(tmp_path, [clip("C1", "A1", 1, src="1.1")], [fx("F1", "trans", "insert", ["C1"], "1.1", "9.1", 周期="1拍", 深度=1)],
                     {"A1": "click"})
    ref, _, _ = render(tmp_path, [clip("C1", "A1", 1)], [], name="ref")
    for a, b in zip(beat_peaks(x, 28), beat_peaks(ref, 28)):
        assert a == pytest.approx(b, rel=0.02)
    assert float(np.abs(np.diff(x)).max()) <= float(np.abs(np.diff(ref)).max()) * 1.02  # 没有比原信号更陡的跳变（爆音）


# ---------------------------------------------------------------- A15 Release

def _release_run(tmp_path, ending: str, name: str):
    clips = [clip("C1", "A1", 1, stop="11.1"), clip("C2", "A1", 2, stop="9.1")]
    effects = [
        fx("F1", "trans", "insert", ["C1"], "3.1", "9.1", 周期="2拍", 深度=1),  # C1：一直按两拍切
        fx("F2", "trans", "insert", ["C2"], "3.1", "9.1", 周期="2拍", 深度=1),  # C2：同样的，第 5 小节被 Release 收掉
        fx("F3", "release", "insert", ["C2"], "5.1", "6.1", 方式="刹车", 收尾=ending),
    ]
    solo, _, _ = render(tmp_path, [clips[0]], [effects[0]], beats=40, name="solo")
    x, _, rc = render(tmp_path, clips, effects, beats=40, name=name)
    return np.array(beat_peaks(x, 40)), np.array(beat_peaks(solo, 40)), rc


def test_release_resets_only_its_group_and_old_automation_stays_off(tmp_path):
    """Release 只收自己那一组：C1 的 Trans 照常；C2 的 Trans 被收掉以后不再回来（回原声 = 每一拍都在）。"""
    px, ps, rc = _release_run(tmp_path, "回原声", "back")
    assert {m["id"]: m["st"] for m in rc if m["id"] in ("F1", "F2", "F3")} == {"F1": "ok", "F2": "ok", "F3": "ok"}
    # 第 3–4 小节（拍 8–15）：两台一样地切，双数拍（关着的那半）是静音
    assert np.all(px[9:16:2] < 0.004) and np.allclose(px[8:16:2], 2 * ps[8:16:2], rtol=0.03)
    # Release 之后（第 6 小节起，拍 20–31）：C2 回到干声、不再被切；C1 照旧只在单数拍响
    back = px[20:32] - ps[20:32]
    ref = np.array([0.8, 0.4, 0.4, 0.4] * 3)  # C2 自己的脉冲：每一拍都在
    assert np.allclose(back, ref, rtol=0.06), back
    assert np.all(ps[21:32:2] < 0.004) and np.all(ps[20:32:2] > 0.3)
    # 片段停了就没有了（已停的片段不复活）：拍 32 起只剩 C1
    assert np.allclose(px[32:40], ps[32:40], atol=0.004)


def test_release_exit_keeps_the_group_silent(tmp_path):
    """退场和回原声的结果不一样：退场以后这一组一直静音，直到片段停。"""
    px, ps, _ = _release_run(tmp_path, "退场", "exit")
    assert np.allclose(px[20:40], ps[20:40], atol=0.004)  # Release 之后只剩 C1
    pb, _, _ = _release_run(tmp_path, "回原声", "back2")
    assert np.abs(pb[20:32] - px[20:32]).max() > 0.3


def test_release_echo_repeats_the_last_beats_and_ends_by_its_limit(tmp_path):
    """Release Echo：开始那一拍起干声收掉，延迟线里最后那一段带着反馈转；到声明的时长为止（尾音有上限）。"""
    x, _, _ = render(tmp_path, [clip("C1", "A1", 1)],
                     [fx("F1", "release", "insert", ["C1"], "5.1", "7.1", 方式="Echo", 收尾="退场", 拍长="1拍", 反馈=0.5)], beats=36)
    p = beat_peaks(x, 36)
    assert p[15] == pytest.approx(0.4, rel=0.05)  # 之前：干声
    assert p[16] == pytest.approx(0.4, rel=0.06) and p[17] == pytest.approx(0.2, rel=0.08)  # 第 16 拍 = 第 15 拍的回声，一次减半
    assert p[18] == pytest.approx(0.1, rel=0.1)
    assert max(p[24:]) < 0.004  # 到时长就没有了；退场：片段的干声也不回来


# ---------------------------------------------------------------- A16 Roll 和采样

def test_roll_captures_on_the_beat_and_repeats_on_the_grid(tmp_path):
    """Roll：在写的那一拍抓，之后每一遍的起点都对在 抓取点 + n × 拍长 上；改短以后对在新的格子上；到点回到现场，没有残留。"""
    x, r, rc = render(tmp_path, [clip("C1", "A1", 1)], [fx("F1", "roll", "insert", ["C1"], "3.1", "5.1", 拍长="1拍 → 1/2拍")])
    assert next(m for m in rc if m["id"] == "F1")["st"] == "ok"
    on = onsets(x, thr=0.05, gap=int(0.2 * SPB)) / SPB
    want = [float(k) for k in range(8)] + [8.0, 9.0, 10.0, 11.0] + [12.0 + 0.5 * k for k in range(8)] + [float(k) for k in range(16, 32)]
    assert len(on) == len(want), (list(np.round(on, 3)), want)
    assert np.abs(on - np.array(want)).max() * SPB <= 3.0  # 每一下都在 3 帧以内
    p = beat_peaks(x, 32)
    assert all(v == pytest.approx(0.8, rel=0.05) for v in p[8:16])  # 抓的是小节第 1 拍（0.8 的那一下），反复放它
    assert p[16] == pytest.approx(0.8, rel=0.05) and p[17] == pytest.approx(0.4, rel=0.05)  # 到点：回到现场


def test_sample_fill_hits_on_the_frame_and_stops_at_its_end(tmp_path):
    """采样：每一下落在采样上；到「结束」那一拍还在响的收掉；不重复的只打一下。"""
    c = compiled(project([clip("C1", "A1", 1, init={"vol": 0.0})], tempo=[{"at": "1.1", "bpm": BPM}], fx=[
        fx("F1", "sample", "master-insert", ["rim"], "2.1", "3.1", 重复="1/2拍", 音量=1),
        fx("F2", "sample", "master-insert", ["crash"], "4.1", "4.3", 音量=1),
        fx("F3", "sample", "master-insert", ["kick"], "6.1", "6.1"),
    ]))
    assert c.ok and len(c.plan["samples"]) == 3 and len([h for h in c.plan["hits"] if not h.get("stop")]) == 8 + 1 + 1
    with Run(c, speed=40, record=tmp_path / "s.f32") as r:
        r.start()
        r.wait_state("done", 60)
        x = r.audio()
        f0 = r.frame_of(0)
        rc = {m["id"]: m for m in r.rc()}
    assert rc["F1"]["st"] == rc["F2"]["st"] == rc["F3"]["st"] == "ok"
    for b in [4.0 + 0.5 * k for k in range(8)] + [12.0, 20.0]:  # 每一下：到点之前是静音，到点那几帧就有声音
        i = int(round(f0 + b * SPB))
        assert float(np.abs(x[i - 300:i - 3]).max()) < 1e-4, b
        assert float(np.abs(x[i - 2:i + 30]).max()) > 0.05, b
    i = int(f0 + 14 * SPB)  # 吊镲有 1.5 秒，写的是两拍：到点（第 14 拍）收掉
    assert float(np.abs(x[i - 2000:i - 500]).max()) > 0.02 and float(np.abs(x[i + 200:i + 20000]).max()) < 1e-4


# ---------------------------------------------------------------- A17 从中间开始

LIVE_CASES = {
    "trans": (fx("F1", "trans", "insert", ["C1"], "3.1", "7.1", 频段="MID", 周期="1/2拍", 深度=1), "noise"),
    "filter": (fx("F1", "filter", "insert", ["C1"], "3.1", "7.1", 模式="高通", 截止="100Hz → 4kHz"), "noise"),
    "filter_echo": (fx("F1", "filter_echo", "insert", ["C1"], "3.1", "5.1", 截止="200Hz → 2kHz", 拍长="1/2拍", 反馈=0.7), "noise"),
    "spiral": (fx("F1", "spiral", "insert", ["C1"], "3.1", "5.1", 拍长="1/2拍", 音高="+12"), "noise"),
    "noise": (fx("F1", "noise", "insert", ["C1"], "3.1", "7.1", 音量="0 → 1/2"), "noise"),
    "flanger": (fx("F1", "flanger", "insert", ["C1"], "3.1", "7.1", 周期="2小节"), "noise"),
    "phaser": (fx("F1", "phaser", "insert", ["C1"], "3.1", "7.1", 周期="2小节"), "noise"),
    "release": (fx("F1", "release", "insert", ["C1"], "5.1", "7.1", 方式="Echo", 收尾="回原声", 拍长="1拍", 反馈=0.6), "noise"),
    "brake": (fx("F1", "release", "insert", ["C1"], "5.1", "6.1", 方式="刹车", 收尾="回原声"), "noise"),
    "sample": (fx("F1", "sample", "master-insert", ["crash"], "5.1", "7.1"), "noise"),
}


@pytest.mark.parametrize("kind", sorted(LIVE_CASES))
def test_every_live_effect_can_be_rebuilt_from_the_middle(tmp_path, kind):
    """每种效果：从它中间的一拍开始（按编译结果算的预跑起点），听到的和从头连续放到这里一样。"""
    effect, wave = LIVE_CASES[kind]
    clips = [clip("C1", "A1", 1)]
    full, _, _ = render(tmp_path, clips, [effect], {"A1": wave}, name="full")
    at = 21.0  # 第 6 小节第 2 拍：Roll 已经在转、Echo 的尾音在响、Release 放到一半
    mid, _, _ = render(tmp_path, clips, [effect], {"A1": wave}, start=at, name="mid")
    ref = full[int(round(at * SPB)):]
    n = min(len(ref), len(mid)) - 64
    assert n > 8 * SPB
    # 两遍的缓冲相位不一样（起点落在缓冲里的位置不同），允许差一帧以内的对齐
    best = min(db(ref[64 + s:n + s] - mid[64:n]) for s in (-1, 0, 1))
    assert best <= max(-60.0, db(ref[64:n]) - 40.0), (kind, best, db(ref[64:n]))


def test_a_roll_started_in_the_middle_repeats_the_same_slice(tmp_path):
    """Roll 抓取的起点落在采样上，两遍之间可以差一帧（缓冲相位不同）：不逐位比，比每 1/4 拍上有没有脉冲、多大。"""
    effect = fx("F1", "roll", "insert", ["C1"], "3.1", "7.1", 拍长="1拍 → 1/2拍 → 1/4拍")
    clips = [clip("C1", "A1", 1)]
    full, _, _ = render(tmp_path, clips, [effect], name="full")
    at = 21.0
    mid, _, _ = render(tmp_path, clips, [effect], start=at, name="mid")
    assert compiled(project(clips, tempo=[{"at": "1.1", "bpm": BPM}], fx=[effect])).prerun_from(at) == pytest.approx(7.75)
    w = int(0.006 * SR)

    def quarters(x: np.ndarray) -> np.ndarray:
        return np.array([float(np.abs(x[max(0, int(round(q * SPB / 4)) - 4):int(round(q * SPB / 4)) + w]).max()) for q in range(40)])

    a, b = quarters(full[int(round(at * SPB)):]), quarters(mid)
    assert a[:12].min() > 0.5  # 还在 roll 里：每 1/4 拍一下，都是抓到的那个小节第 1 拍
    assert np.allclose(a, b, atol=0.03), (a, b)


def test_a_capture_too_far_back_refuses_the_start_point():
    """抓取型的效果离得太远（超过引擎回算的上限）：不能装成恢复了，要说从哪之前开始。"""
    c = compiled(project([clip("C1", "A1", 1, stop="100.1")], fx=[fx("F1", "roll", "insert", ["C1"], "3.1", "90.1", 拍长="1拍")]))
    assert c.ok
    assert c.restore_problem(40.0) is None and c.prerun_from(40.0) == pytest.approx(7.75)
    msg = c.restore_problem(300.0)
    assert msg and "F1" in msg and "3.1" in msg
    assert c.restore_problem(4.0) is None and c.restore_problem(380.0) is None  # 之前 / 之后都没事


# ---------------------------------------------------------------- A22 能力拒绝

def test_an_engine_without_live_effects_names_the_missing_ability_and_the_line():
    effects = [fx("F1", "roll", "insert", ["C1"], "3.1", "4.1"), fx("F2", "release", "insert", ["C1"], "5.1", "6.1", 方式="刹车", 收尾="退场")]
    effects[0]["line"], effects[1]["line"] = 57, 58
    c = compiled(project([clip("C1", "A1", 1)], fx=effects), caps=BASIC)
    assert not c.ok and c.plan is None
    assert [(i.code, i.line) for i in c.errors] == [("caps_fx", 57), ("caps_fx", 58)]
    assert "Roll" in c.errors[0].msg and "Release" in c.errors[1].msg
    assert compiled(project([clip("C1", "A1", 1)], fx=effects)).ok  # 现在的引擎会


# ---------------------------------------------------------------- 谱上的写法

SCORE = """# 现场效果

- 规范：v5
- 主速：128

## 素材

| ID | 曲名 | 文件名 | 原 BPM | 第 1 小节（秒） | 小节数 |
|---|---|---|---|---|---|
| A1 | One | one.mp3 | 128 | 0.100 | 64 |
| A3 | Perc | perc.mp3 | 132 | 0.050 | 64 |

## 片段

| ID | 素材 | Deck | 原曲起点 | 全场起播 | 全场停止 | 角色 | 推子 |
|---|---|---|---|---|---|---|---|
| C1 | A1 | D1 | 1.1 | 1.1 | 17.1 | 主曲 | 10 |
| C3 | A3 | D3 | 1.1 | 1.1 | 17.1 | 节奏层 | 8 |

## 场景 1：效果（1.1 – 17.1）

- 状态：草稿

### 效果

| ID | 类型 | 路由 | 目标 | 开始 | 结束 | 参数 | 说明 |
|---|---|---|---|---|---|---|---|
| F1 | Trans | 插入 | C3 | 3.1 | 5.1 | 频段 HIGH；周期 1/4拍；占空 1/2；深度 1 | 只切高频 |
| F2 | Roll | 插入 | C3 | 5.1 | 6.1 | 频段 MID+HIGH；拍长 1拍 → 1/2拍 → 1/4拍 | 越切越碎 |
| F3 | Filter Echo | 插入 | C1 | 7.1 | 9.1 | 模式 高通；截止 100Hz → 2kHz；拍长 1/2拍；反馈 0.6；尾音 2小节 | |
| F4 | Release | 插入 | C3 | 6.1 | 7.1 | 方式 Echo；拍长 1/2拍；退场 | Roll 完接 Release |
| F5 | 采样 | 采样 | snare | 8.3 | 9.1 | 重复 1/4拍；音量 0.6 | fill |
| F6 | Flanger | 组插入 | C1+C3 | 12.1 | 13.1 | 周期 2小节；深度 0.8 | |
| F7 | Noise | 总输出 | 总输出 | 15.1 | 17.1 | 音量 0 → 1/3；频率 400Hz → 8kHz | 上行的噪声 |
| F8 | Spiral | 插入 | C1 | 13.3 | 14.1 | 音高 +12；拍长 1/2拍；尾音 1拍 | |
| F9 | Phaser | 插入 | C3 | 9.1 | 10.1 | 周期 1小节 | 退场以后重新启用 |
"""


def test_live_effects_in_a_score_compile_and_write_back():
    r = score.parse_text(SCORE, "live")
    assert not r.errors, [str(i) for i in r.errors]
    c = compiled(r.project)
    assert c.ok, [str(i) for i in c.errors]
    buses = {b["id"]: b for b in c.plan["buses"]}
    assert [(b["route"], b["clips"]) for b in buses.values()] == [
        ("insert", ["C3"]), ("insert", ["C1"]), ("group", ["C1", "C3"]), ("master", [])]
    units = {u["id"]: u for b in buses.values() for u in b["units"]}
    assert units["F1"]["bands"] == [0, 0, 1] and units["F1"]["p"] == {"period": 0.25, "duty": 0.5, "depth": 1.0}
    assert units["F2"]["bands"] == [0, 1, 1] and units["F2"]["ev"] == [[pytest.approx(17.333333), 0.5], [pytest.approx(18.666667), 0.25]]
    assert units["F3"]["kind"] == "filter" and units["F3.echo"]["kind"] == "echo" and units["F3.echo"]["tail"] == 8.0  # 宏展开成两件
    assert units["F4"]["kind"] == "rel_echo" and units["F4"]["back"] is False
    assert units["F4"]["until"] == 32.0  # 退场：静音到这一组下一次显式启用（F9，第 9 小节）
    hits = c.plan["hits"]  # 8.3 起每 1/4 拍一下，到 9.1 收
    assert [h["beat"] for h in hits] == [30.0 + 0.25 * k for k in range(8)] + [32.0] and hits[-1]["stop"] and hits[0]["id"] == "F5"
    live = {x["id"]: x for x in c.live}
    assert "宏" not in live["F3"]["text"] and "Echo" in live["F3"]["text"]
    acts = [a for a in __import__("djstudio.v5.compile", fromlist=["actions"]).actions(c) if a["kind"].startswith("fx")]
    assert any(a["rc"] == "F2" and "Roll" in a["text"] and a["line"] for a in acts)
    # 另存：写回谱再读，编出来的计划一样
    again = score.parse_text(write(r.project), "live")
    assert not again.errors, [str(i) for i in again.errors]
    c2 = compiled(again.project)
    assert c2.plan["buses"] == c.plan["buses"] and c2.plan["hits"] == c.plan["hits"]
