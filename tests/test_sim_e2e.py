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

from .conftest import make_info

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


@pytest.fixture
def rig():
    link = SimLink(sim_library(), speed=SPEED)
    client = EngineClient(link)
    assert client.wait_until(client.online, 5)
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
