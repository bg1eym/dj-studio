"""端到端：真正的 DJStudio.js 跑在假 Mixxx（Node）里，由 conductor 指挥整套 set。"""
import shutil
import threading
import time

import pytest

from djstudio.compiler import compile_set
from djstudio.engine.client import EngineClient
from djstudio.engine.conductor import Conductor
from djstudio.engine.link import SimLink
from djstudio.setfile import SetFile

from .conftest import lanes_mismatch, make_info

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="需要 node")

SPEED = 40

LIB = {
    "a": make_info("a", 128.0, 150.0, 0.10),
    "b": make_info("b", 130.0, 160.0, 0.25),
    "c": make_info("c", 132.0, 130.0, 0.05, 1),
}


def sim_library():
    return [{"id": k, "dur": v.grid.duration, "bpm": v.bpm, "first_beat": v.grid.first_beat} for k, v in LIB.items()]


def make_compiled():
    s = SetFile.model_validate({
        "schema": "djset/2", "id": "e2e",
        "tracks": [{"id": "a", "title": "A"}, {"id": "b", "title": "B"}, {"id": "c", "title": "C"}],
        "transitions": [
            {"technique": "longhand", "start": 33, "b_in": 1, "bars": 24},
            {"technique": "instant", "start": 41, "b_in": 17},
        ],
        "ending": {"mode": "fade", "bar": 40, "bars": 8},
    })
    c = compile_set(s, lambda t: LIB.get(t.id))
    assert c.ok, [str(i) for i in c.issues]
    return c


@pytest.fixture(params=["wall", "engine"])
def rig(request):
    """模拟器 + DJStudio.js。两种时钟各跑一遍：wall = 脚本自己 20ms 一次的定时器（演出）；engine = 引擎每处理 20ms 音频
    给一个 tick、计时按引擎时间（录 MP3 时，倍速放出来和原速一样）。"""
    import os

    env = {**os.environ, "DJSTUDIO_CLOCK": request.param} if request.param == "engine" else None
    link = SimLink(sim_library(), speed=SPEED, env=env)
    client = EngineClient(link)
    assert client.wait_until(client.online, 5)
    st = link.sim({"cmd": "state"})
    if request.param == "engine":
        assert st["clocked"] == 1 and st["timers"] == 0  # tick 由引擎给，脚本自己不开定时器
    else:
        assert st["clocked"] == 0 and st["timers"] >= 1
    link.sim({"cmd": "queue", "ids": ["a", "b", "c"]})
    yield link, client
    link.close()


def run(cond, timeout=60):
    cond.start()
    cond.join(timeout)
    assert not cond.running, "conductor 没有在时限内结束"


def test_full_set(rig):
    link, client = rig
    compiled = make_compiled()
    cond = Conductor(client, compiled)

    # 在第一段过渡进行中采样两边的时间线拍位，检查对齐
    samples = []

    def sampler():
        p1 = compiled.programs[0]
        while cond.running or not samples:
            if cond.progress.stage == "transition" and cond.progress.program == "tr1":
                st = link.sim({"cmd": "state"})
                d1, d2 = st["decks"]
                if d1["playing"] and d2["playing"]:
                    samples.append((d1["beat"] - p1["a"]["start"]) - (d2["beat"] - p1["b"]["in"]))
            time.sleep(0.05)
            if len(samples) > 20:
                break

    th = threading.Thread(target=sampler, daemon=True)
    cond.start()
    th.start()
    cond.join(90)
    log = "\n".join(cond.progress.log)
    assert cond.progress.stage == "done", log + f"\n{cond.progress.error}"
    assert "装错" not in log and "失联" not in log and "小节错位" not in log, log
    assert samples, "没采到过渡中的样本"
    assert max(abs(x) for x in samples) < 0.05, samples

    st = link.sim({"cmd": "state"})
    assert not st["decks"][0]["playing"] and not st["decks"][1]["playing"]
    # 终曲淡出到 0
    assert st["decks"][0]["volume"] == pytest.approx(0, abs=1e-3)

    # 主控 BPM：过渡 1 结束时 b 被带到 130，c 在瞬时切换后是 132
    h = link.sim({"cmd": "history", "keys": ["bpm"]})["h"]
    bpms_d2 = [v for _, g, k, v in h if g == "[Channel2]"]
    assert bpms_d2 and abs(bpms_d2[-1] - 130) < 0.01
    bpms_d1 = [v for _, g, k, v in h if g == "[Channel1]"]
    assert abs(bpms_d1[-1] - 132) < 0.01


def test_status_lags_behind_done(rig):
    """「done」事件比反映 A 已停的状态先到（状态每 100ms 一次；机器忙时真引擎测试台上出现过）：
    conductor 不能因为缓存里 A 还在播就报错，要等新状态。"""
    link, client = rig
    orig = client._dispatch
    hold = {"until": 0.0}

    def lagging(msg):
        if msg.get("t") == "ev" and msg.get("e") == "done":
            hold["until"] = time.monotonic() + 0.3
        if msg.get("t") == "st" and time.monotonic() < hold["until"]:
            return
        orig(msg)

    client._dispatch = lagging
    cond = Conductor(client, make_compiled())
    run(cond, 90)
    log = "\n".join(cond.progress.log)
    assert cond.progress.stage == "done", log + f"\n{cond.progress.error}"


def test_status_reports_mixer_following_lanes(rig):
    """引擎 0.4.0：状态里的旋钮读数（从控件读回）跟着自动化曲线走——网页混音台显示的就是这些值。"""
    link, client = rig
    compiled = make_compiled()
    p1 = compiled.programs[0]
    sts = []
    client.add_listener(lambda m: sts.append(m) if m.get("t") == "st" else None)
    cond = Conductor(client, compiled)
    run(cond, 90)
    assert cond.progress.stage == "done", "\n".join(cond.progress.log)
    checked, bad = lanes_mismatch(sts, p1)
    assert checked > 50 and bad <= checked * 0.03, (checked, bad)
    # 电平表：在播的 deck 有电平，停着的为 0；主输出有电平
    live = [st for st in sts if st["d"][0][1] and not st["d"][1][1]]
    assert live and all(st["m"][0][6] > 0 and st["m"][1][6] == 0 for st in live[:20])
    assert any(st["mv"] > 0 for st in live)
    # 装载时 AutoDJ 会把 crossfader 甩向在播一侧（引擎随后拨回）；两边都在播时必须在中间
    both = [st for st in sts if st["d"][0][1] and st["d"][1][1]]
    assert both and all(st["x"] == 0 for st in both)


def test_engine_lost(rig):
    link, client = rig
    compiled = make_compiled()
    cond = Conductor(client, compiled, lost_timeout=1.5)
    cond.start()
    assert client.wait_until(lambda: cond.progress.stage in ("armed", "solo", "transition"), 20)
    link.sim({"cmd": "freeze"})
    cond.join(15)
    log = "\n".join(cond.progress.log)
    assert cond.progress.stage == "error", log
    assert "引擎失联" in log
    assert not cond.progress.engine_ok


def test_wrong_track_detected(rig):
    link, client = rig
    link.sim({"cmd": "queue", "ids": ["a", "c", "b"]})  # 队列顺序和 set 不一致
    cond = Conductor(client, make_compiled())
    run(cond, 30)
    assert cond.progress.stage == "error"
    assert "装错曲" in cond.progress.error


def test_manual_stop_fades_out(rig):
    link, client = rig
    cond = Conductor(client, make_compiled())
    cond.start()
    assert client.wait_until(lambda: cond.progress.stage == "armed", 20)
    cond.request_stop()
    cond.join(20)
    assert cond.progress.stage == "stopped"
    st = link.sim({"cmd": "state"})
    assert not any(d["playing"] for d in st["decks"])


def test_incoming_knobs_preset_when_loaded(rig):
    """引擎 0.4.3：B 一装好、过渡就位（还没开始）时，B 的 EQ 就已经在过渡开头的位置——不等过渡开始才一下子拧过去。"""
    link, client = rig
    compiled = make_compiled()
    p1 = compiled.programs[0]
    b_lo0 = p1["lanes"]["B.lo"][0][1]
    assert b_lo0 < 0.5  # 长交接：B 的低频先切掉（-15 dB）
    sts = []
    client.add_listener(lambda m: sts.append(m) if m.get("t") == "st" else None)
    cond = Conductor(client, compiled)
    run(cond, 90)
    assert cond.progress.stage == "done", "\n".join(cond.progress.log)
    armed = [st for st in sts if (st.get("pg") or {}).get("id") == p1["id"] and st["pg"]["st"] == "armed" and st["d"][1][0]]
    assert len(armed) > 5, len(armed)
    settled = armed[2:]  # 下发后第一两帧可能还是下发前的
    assert all(abs(st["m"][1][2] - b_lo0) < 0.01 for st in settled), [st["m"][1][2] for st in settled[:10]]
    assert all(st["d"][0][1] and not st["d"][1][1] for st in settled)  # 这时 A 还在独奏，B 没在放


def test_jumps_and_loops(rig):
    """跳段和 loop（规范 v4）：独奏时 A 往后跳 8 小节、打 2 小节 loop 转 3 遍；过渡里 A 打 2 小节 loop 转 3 遍（旋钮照常按时间走）、
    B 打 1 小节 loop 转 2 遍；B 独奏时往回跳 4 小节再放一遍。全套照常跑完，过渡的时间线不被 loop 打乱，没有小节错位。"""
    link, client = rig
    s = SetFile.model_validate({
        "schema": "djset/2", "id": "moves",
        "tracks": [
            {"id": "a", "title": "A", "moves": [{"bar": 5, "to": 13}, {"bar": 17, "loop": 2, "times": 3}]},
            {"id": "b", "title": "B", "moves": [{"bar": 25, "to": 21}]},
            {"id": "c", "title": "C"},
        ],
        "transitions": [
            {"technique": "custom", "start": 33, "b_in": 1, "play_at": 8, "bars": 24, "params": {"a_end": "stop"},
             "lanes": {"A.vol": [[0, 1], [20, 1], [24, 0]], "B.vol": [[8, 0.5], [16, 1]], "A.lo": [[8, 0], [12, -15]]},
             "events": [[4, "A.loop:8"], [10, "A.loop_off"], [12, "B.loop:4"], [14, "B.loop_off"], [24, "A.stop"]]},
            {"technique": "instant", "start": 41, "b_in": 17},
        ],
        "ending": {"mode": "fade", "bar": 40, "bars": 8},
    })
    c = compile_set(s, lambda t: LIB.get(t.id))
    assert c.ok, [str(i) for i in c.issues]
    beat_a = LIB["a"].grid.bar_beat
    assert c.plans[0] == [{"at": beat_a(5), "jump": 32.0}, {"at": beat_a(17), "loop": 8.0, "times": 3}]
    assert c.plans[1] == [{"at": round(LIB["b"].grid.bar_beat(25), 4), "jump": -16.0}] and c.plans[2] == []
    # 过渡 1 的 loop 出口提前半遍（不和 loop 尾那一拍抢）；B 过渡后在自己的第 16 小节（loop 多转了 1 小节）
    p1 = c.programs[0]
    assert [e for e in p1["events"] if "loop" in e[1]] == [[16.0, "A.loop:8"], [36.0, "A.loop_off"],
                                                          [48.0, "B.loop:4"], [54.0, "B.loop_off"]]
    # 时间表：A 独奏 33-1 小节 - 8（跳过）+ 4（loop 多转 2 遍）；B 独奏 41-16 + 4（往回跳再放 4 小节）
    solos = [seg for seg in c.schedule if seg.kind == "solo"]
    assert abs(solos[0].t1 - solos[0].t0 - (32 - 8 + 4) * 4 * 60 / 128) < 1e-6
    assert abs(solos[1].t1 - solos[1].t0 - (41 - 16 + 4) * 4 * 60 / 130) < 1e-6

    sts, evs = [], []
    client.add_listener(lambda m: sts.append(m) if m.get("t") == "st" else evs.append(m) if m.get("t") == "ev" else None)
    cond = Conductor(client, c)
    run(cond, 120)
    log = "\n".join(cond.progress.log)
    assert cond.progress.stage == "done", log + f"\n{cond.progress.error}"
    assert "小节错位" not in log and "失联" not in log, log
    assert "跳段 +8 小节" in log and "跳段 -4 小节" in log and "出 loop" in log, log
    h = link.sim({"cmd": "history", "keys": ["beatjump", "beatloop_activate", "reloop_toggle"]})["h"]
    acts = [(g, k, v) for _, g, k, v in h if v]
    assert ("[Channel1]", "beatjump", 32) in acts and ("[Channel2]", "beatjump", -16) in acts
    assert sum(1 for g, k, _ in acts if g == "[Channel1]" and k == "beatloop_activate") == 2  # 独奏 + 过渡里
    assert sum(1 for g, k, _ in acts if g == "[Channel2]" and k == "beatloop_activate") == 1
    # 过渡里 A 在 loop 里转的时候，旋钮读数照常跟着时间线（loop 不把时间线拉回去）
    checked, bad = lanes_mismatch(sts, p1)
    assert checked > 30 and bad <= checked * 0.05, (checked, bad)
    tls = [st["pg"]["tl"] for st in sts if (st.get("pg") or {}).get("id") == "tr1" and st["pg"]["st"] == "running"]
    assert len(tls) > 30 and all(b >= a - 1e-6 for a, b in zip(tls, tls[1:])), "时间线往回走了"
    # 引擎报的「位置跳了」：A 在 loop 里跳回 2 次（每次 -8 拍）、B 1 次（-4 拍），都按整拍补进时间线
    wraps = [(e["side"], e["by"]) for e in evs if e.get("e") == "wrap" and e.get("id") == "tr1"]
    assert wraps == [("a", -8), ("a", -8), ("b", -4)], wraps
    # 过渡完（状态里 tr1 第一次是 done）B 在自己的第 16 小节附近（loop 多转的 1 小节没往前走；状态 100ms 一帧，差不到 1 拍）
    first_done = next(st for st in sts if (st.get("pg") or {}).get("id") == "tr1" and st["pg"]["st"] == "done")
    b_bar = LIB["b"].grid.beat_bar(LIB["b"].grid.time_beat(first_done["d"][1][2] / 1000))
    assert abs(b_bar - 16) < 0.3, b_bar


def test_loop_twice_then_jump_at_the_loop_end(rig):
    """「放两遍再往后跳」：第 5 小节 4 小节 loop 转 2 遍，第 9 小节（loop 结束那一小节）跳到 17。
    第一遍到 loop 尾时不能先跳（那时还在 loop 里，跳段会把 loop 挪走）；出了 loop、第二遍放到第 9 小节才跳。"""
    link, client = rig
    s = SetFile.model_validate({
        "schema": "djset/2", "id": "loopjump",
        "tracks": [{"id": "a", "title": "A", "moves": [{"bar": 5, "loop": 4, "times": 2}, {"bar": 9, "to": 17}]},
                   {"id": "b", "title": "B"}],
        "transitions": [{"technique": "instant", "start": 33, "b_in": 17}],
        "ending": {"mode": "fade", "bar": 40, "bars": 8},
    })
    c = compile_set(s, lambda t: LIB.get(t.id))
    assert c.ok, [str(i) for i in c.issues]
    solos = [seg for seg in c.schedule if seg.kind == "solo"]
    assert abs(solos[0].t1 - solos[0].t0 - (32 + 4 - 8) * 4 * 60 / 128) < 1e-6
    cond = Conductor(client, c)
    run(cond, 120)
    log = "\n".join(cond.progress.log)
    assert cond.progress.stage == "done", log + f"\n{cond.progress.error}"
    assert "小节错位" not in log, log
    h = link.sim({"cmd": "history", "keys": ["beatjump", "beatloop_activate", "reloop_toggle"]})["h"]
    acts = [(k, v) for _, g, k, v in h if v and g == "[Channel1]"]
    assert [k for k, _ in acts] == ["beatloop_activate", "reloop_toggle", "beatjump"], acts
    assert acts[-1] == ("beatjump", 32)


def test_loop_ending_where_the_transition_starts(rig):
    """独奏 loop 正好转到下一段过渡的第一小节（60 分钟 set 的 Soma：169 起 24 小节 × 2，193 出曲）：过渡要等 loop 转完第二遍才开始。"""
    link, client = rig
    s = SetFile.model_validate({
        "schema": "djset/2", "id": "loopout",
        "tracks": [{"id": "a", "title": "A", "moves": [{"bar": 25, "loop": 8, "times": 2}]}, {"id": "b", "title": "B"}],
        "transitions": [{"technique": "instant", "start": 33, "b_in": 17}],
        "ending": {"mode": "fade", "bar": 40, "bars": 8},
    })
    c = compile_set(s, lambda t: LIB.get(t.id))
    assert c.ok, [str(i) for i in c.issues]
    evs = []
    client.add_listener(lambda m: evs.append(m) if m.get("t") == "ev" else None)
    cond = Conductor(client, c)
    run(cond, 120)
    log = "\n".join(cond.progress.log)
    assert cond.progress.stage == "done", log + f"\n{cond.progress.error}"
    order = [e["e"] for e in evs if e.get("e") in ("loop_in", "loop_out", "started")]
    assert order[:3] == ["loop_in", "loop_out", "started"], order
