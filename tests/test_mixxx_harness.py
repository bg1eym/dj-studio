"""端到端：conductor 驱动**真 Mixxx 2.5.6**（tools/mixxx-harness 编进 mixxx-test 的测试台）。

需要先按 tools/mixxx-harness/README.md 构建，并设置 DJSTUDIO_MIXXX_HARNESS=<mixxx-src>/build/mixxx-test，
否则跳过。DJSTUDIO_HARNESS_RECORD=<路径.wav> 时把整套主输出录下来。
"""
import os
import threading
import time
from pathlib import Path

import pytest

from djstudio.compiler import TrackInfo, compile_set
from djstudio.engine.client import EngineClient
from djstudio.engine.conductor import Conductor
from djstudio.engine.link import HarnessLink
from djstudio.grid import Grid
from djstudio.setfile import SetFile
from djstudio.synth import techno, write_wav

from .conftest import lanes_mismatch

BIN = os.environ.get("DJSTUDIO_MIXXX_HARNESS")
pytestmark = pytest.mark.skipif(not BIN or not Path(BIN).exists(), reason="没有构建真 Mixxx 测试台")

SPEED = 3.0
SPECS = {  # id: (bpm, first_beat, downbeat, 小节数)
    "a": (128.0, 0.10, 0, 80),
    "b": (130.0, 0.25, 2, 80),
    "c": (132.0, 0.05, 1, 64),
}


@pytest.fixture(scope="module")
def tracks(tmp_path_factory):
    d = tmp_path_factory.mktemp("harness_tracks")
    out = {}
    for k, (bpm, fb, db, bars) in SPECS.items():
        p = d / f"{k}.wav"
        layout = (("intro", 8), ("groove", bars // 2 - 8), ("breakdown", 8), ("drop", bars // 2 - 16), ("outro", 8))
        write_wav(p, techno(bpm, fb, db, layout, seed=ord(k)))
        out[k] = p
    return out


def run_set(tracks, record: str | None = None):
    link = HarnessLink(Path(BIN), [], speed=SPEED)
    client = EngineClient(link)
    assert client.wait_until(client.online, 30)
    lib = link.sim({"cmd": "library", "tracks": [
        {"id": k, "path": str(p), "bpm": SPECS[k][0], "first_beat": SPECS[k][1]} for k, p in tracks.items()]},
        timeout=60)["tracks"]
    infos = {t["id"]: TrackInfo(t["id"], t["id"], Grid(t["bpm"], SPECS[t["id"]][1], 0, t["duration"])) for t in lib}
    s = SetFile.model_validate({
        "schema": "djset/2", "id": "harness",
        "tracks": [{"id": "a", "title": "A"}, {"id": "b", "title": "B", "downbeat": 2},
                   {"id": "c", "title": "C", "downbeat": 1}],
        "transitions": [
            {"technique": "longhand", "start": 41, "b_in": 1, "bars": 24},
            {"technique": "instant", "start": 49, "b_in": 33},
        ],
        "ending": {"mode": "fade", "bar": 48, "bars": 8},
    })
    compiled = compile_set(s, lambda t: infos.get(t.id))
    assert compiled.ok, [str(i) for i in compiled.issues]
    link.sim({"cmd": "queue", "ids": ["a", "b", "c"]})
    if record:
        link.sim({"cmd": "record", "path": record})
    cond = Conductor(client, compiled, stall_timeout=8 * SPEED)
    samples: list[float] = []
    statuses: list[dict] = []
    client.add_listener(lambda m: statuses.append(m) if m.get("t") == "st" else None)

    def sampler():
        p1 = compiled.programs[0]
        while cond.running or not cond.progress.finished_at:
            if cond.progress.stage == "transition" and cond.progress.program == "tr1":
                st = link.sim({"cmd": "state"})
                d1, d2 = st["decks"]
                if d1["playing"] and d2["playing"]:
                    samples.append((d1["beat"] - p1["a"]["start"]) - (d2["beat"] - p1["b"]["in"]))
            time.sleep(0.1)

    cond.start()
    th = threading.Thread(target=sampler, daemon=True)
    th.start()
    cond.join(600)
    if record:
        link.sim({"cmd": "record_stop"}, timeout=30)
    state = link.sim({"cmd": "state"})
    link.sim({"cmd": "quit"})
    time.sleep(0.5)
    link.close()
    return cond, samples, state, link, statuses, compiled


def kick_flams(wav: Path, t0: float, t1: float) -> tuple[int, int]:
    """录音里 [t0, t1) 秒的 kick 起音数与「双击」数（两次起音相隔 < 80ms = 两个 kick 没对齐）。"""
    import numpy as np

    from djstudio.library import analysis as A
    from djstudio.library.audio import decode_mono

    x = decode_mono(wav, 44100)
    fr = A.stft_bands(x, 44100)
    k = A._norm(fr.low_flux)
    t = A.frame_time(np.arange(len(k)), 44100)
    pk = [i for i in range(2, len(k) - 2) if k[i] > 0.25 and k[i] == k[i - 2:i + 3].max()]
    on = t[pk]
    on = on[(on >= t0) & (on < t1)]
    return len(on), int((np.diff(on) < 0.08).sum())


def test_full_set_on_real_mixxx(tracks, tmp_path):
    record = os.environ.get("DJSTUDIO_HARNESS_RECORD") or str(tmp_path / "set.wav")
    cond, samples, state, link, statuses, compiled = run_set(tracks, record)
    log = "\n".join(cond.progress.log)
    assert cond.progress.stage == "done", log + f"\n{cond.progress.error}\n" + "\n".join(link.stderr_lines[-20:])
    assert "装错" not in log and "失联" not in log and "小节错位" not in log, log
    assert samples, "没采到过渡中两边都在播的样本"
    # 引擎报告的位置：B 起播靠 quantize 相位同步，两边时间线拍位应几乎一致
    assert max(abs(x) for x in samples) < 0.05, samples
    assert not any(d["playing"] for d in state["decks"])
    assert state["xFaderMode"] == 0
    # 网页混音台的读数：真 Mixxx 控件读回的 EQ / 滤波跟着曲线走；电平表是 Mixxx 按真实音频算的
    checked, bad = lanes_mismatch(statuses, compiled.programs[0])
    assert checked > 50 and bad <= checked * 0.03, (checked, bad)
    solo = [st for st in statuses if st["d"][0][1] and not st["d"][1][1]]
    idle_vu = sorted(st["m"][1][6] for st in solo)
    assert solo and max(st["m"][0][6] for st in solo) > 0.2 and idle_vu[len(idle_vu) // 2] < 0.01, idle_vu[-30:]
    assert max(st["mv"] for st in solo) > 0.2
    # 真正听到的声音：过渡 1 两个 kick 都响的那一段（A 第 41 小节起 + 6..18 小节）不能有双击
    a_bar = 60 * 4 / SPECS["a"][0]
    t0 = SPECS["a"][1] + 40 * 4 * 60 / SPECS["a"][0] + 6 * a_bar
    n, flams = kick_flams(Path(record), t0, t0 + 12 * a_bar)
    assert n > 20 and flams == 0, (n, flams)
