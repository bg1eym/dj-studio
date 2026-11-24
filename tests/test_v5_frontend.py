"""四 Deck 演出画面（live5.js）里不碰 DOM 的那几样：旋钮的单位、loop 里原曲小节线落在哪、波形取哪一格。
在 node 里跑（和网页用的是同一份文件）。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "djstudio" / "web" / "static"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="要 node")

# 速度图（全场拍, BPM）：平的、匀速变速的、一下换速的（同一拍两个点）、最后一个点以后
TEMPO = [(0.0, 132.0), (384.0, 132.0), (448.0, 134.0), (704.0, 134.0), (704.0, 138.0), (768.0, 136.0)]
BEATS = [0.0, 100.0, 384.0, 400.5, 448.0, 600.0, 704.0, 730.0, 768.0, 960.0]

SCRIPT = r"""
import { eqHours, eqDb, laneFrac, nativeOf, waveCol, eachSrcBar, globalOfSrc, clipSpans, clipPick, clipBands, secAt, clock } from "./live5.js";
import { eqHours as v4Hours, faderPos } from "./live.js";
const out = {};
const dbs = [-25, -16, -6, -2, 0, 3, 6];
out.hours = dbs.map((d) => +eqHours(d).toFixed(4));
out.v4 = dbs.map((d) => +v4Hours(Math.pow(10, d / 20)).toFixed(4));      // 两 Deck 的旋钮吃增益
out.native = dbs.map((d) => +v4Hours(nativeOf("lo", d)).toFixed(4));     // 谱上的 dB → 原生值 → 旋钮的钟点位
out.round = [-5, -3, -1, 0, 2, 5].map((h) => +eqHours(eqDb(h)).toFixed(4));
out.fader = [0, 0.3, 0.6, 1].map((v) => +faderPos(nativeOf("vol", v)).toFixed(4));
out.frac = { cut: laneFrac("lo", -25), noon: laneFrac("lo", 0), top: laneFrac("lo", 6), fOff: laneFrac("filter", 0), vol: laneFrac("vol", 0.6) };
// 片段：全场第 8 拍起播，从原曲第 2 小节（第 4 拍）放；全场 24–40 在原曲第 5–6 小节打一个 2 小节的 loop（转两圈）
const clip = { bpm: 120, t0: 0.5, start: 8, stop: 40, segs: [
  { g0: 8, g1: 24, src: 4, lsrc: 4, loop: 0, kind: "start" }, { g0: 24, g1: 40, src: 16, lsrc: 16, loop: 8, kind: "loop" }, { g0: 40, g1: 40, src: 24, lsrc: 24, loop: 0, kind: "exit" }] };
out.bars = [];
eachSrcBar(clip, 0, 64, (g, k, head) => out.bars.push([g, k + 1, head]));
out.window = [];
eachSrcBar(clip, 30, 37, (g, k) => out.window.push([g, k + 1]));
out.beats = 0;
eachSrcBar(clip, 8, 16, () => { out.beats++; }, 1);
const wf = { rate: 50, n: 3000, low: new Uint8Array(3000), mid: new Uint8Array(3000), high: new Uint8Array(3000) };
wf.low[Math.floor((0.5 + 16 * 0.5) * 50)] = 255;  // 原曲第 16 拍（loop 的头）那一格：0.5 秒 + 16 拍 × 0.5 秒
wf.high[Math.floor((0.5 + 4 * 0.5) * 50)] = 128;  // 起播的那一格
out.cols = [8, 24, 28, 32, 39.99].map((g) => waveCol(clip, wf, g, g + 0.01));
out.outside = waveCol(clip, wf, 100000, 100001);
// 点整首波形：原曲第几拍 → 这套谱里第一次放到它是全场第几拍
out.g = [4, 10, 19.5, 16, 20, 23, 3, 24, 40].map((src) => globalOfSrc(clip, src));
// 从圈中间起播的片段：圈头要等转回来才到
const mid = { bpm: 120, t0: 0, start: 0, stop: 32, segs: [{ g0: 0, g1: 32, src: 6, lsrc: 4, loop: 8, kind: "loop", id: "L" }] };
out.gmid = [6, 11, 4, 5.5, 12, 3].map((src) => globalOfSrc(mid, src));
out.spans = clipSpans(clip).map((sp) => [sp.a, sp.b, sp.loop, sp.laps ?? null]);
out.pick = [clipPick(clip), clipPick(mid), clipPick({ segs: [] })];
// 要这首的哪些元素：LOW 从头切到尾 = 不要；先切着后来放进来 = 后进；先在后来切掉 = 后切
const L = (...vs) => vs.map((v, i) => ({ at: i * 16, v }));
out.bands = [
  clipBands({ lanes: { lo: L(-25, -25), mid: L(0, -6), hi: L(0) } }),
  clipBands({ lanes: { lo: L(-25, 0), mid: L(0), hi: L(0, -25) } }),
  clipBands({ lanes: {} }),
];
// set 的时间：全场第几拍 = 开演后第几秒
const tempo = JSON.parse(process.env.TEMPO || "[]");
out.sec = JSON.parse(process.env.BEATS || "[]").map((b) => secAt(tempo, b));
out.clock = [0, 59.99, 61, 3599, 3600, 3725].map(clock);
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def out(tmp_path_factory):
    d = tmp_path_factory.mktemp("live5")
    for name in ("live5.js", "live.js", "chart5.js", "wavedraw.js"):
        shutil.copy(STATIC / name, d / name)
    (d / "package.json").write_text('{"type": "module"}')
    (d / "t.js").write_text(SCRIPT)
    env = {**os.environ, "TEMPO": json.dumps([{"at": b, "bpm": v} for b, v in TEMPO]), "BEATS": json.dumps(BEATS)}
    r = subprocess.run(["node", "t.js"], cwd=d, capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_knob_units_match_the_two_deck_mixer(out):
    """谱上的 dB 换成钟点位，和两 Deck 调音台（live.js）是同一条曲线；经原生值走一圈也一样。7 点 = 切，12 点 = 0 dB，5 点 = +6。"""
    assert out["hours"] == out["v4"] == out["native"]
    assert out["hours"][0] == -5 and out["hours"][4] == 0 and out["hours"][-1] == 5
    assert out["round"] == [-5, -3, -1, 0, 2, 5]  # 拖旋钮：钟点位 → dB → 钟点位 不走样
    assert out["fader"] == [0, 0.3, 0.6, 1]  # 推子位置 → 增益 → 推子位置
    assert out["frac"] == {"cut": 0, "noon": 0.5, "top": 1, "fOff": 0.5, "vol": 0.6}


def test_source_bar_lines_follow_loops(out):
    """原曲的小节线画在全场的哪一拍：直放时一小节一条；loop 里每一圈从圈头的小节号重新数，圈头单独标出来。"""
    assert out["bars"] == [[8, 2, True], [12, 3, False], [16, 4, False], [20, 5, False],
                           [24, 5, True], [28, 6, False], [32, 5, True], [36, 6, False]]
    assert out["window"] == [[32, 5], [36, 6]]  # 只看一段（画面里的那几小节）也对
    assert out["beats"] == 9  # 按拍数：全场 8–16 两端都算，9 条拍线


def test_waveform_column_reads_the_source_position(out):
    """全场第 g 拍取的是原曲那一刻的波形：起播那一格、loop 每一圈的头是同一格、圈中间不是、片段外没有。"""
    c = out["cols"]
    assert c[0][2] == pytest.approx(128 / 255) and c[0][0] == 0
    assert c[1][0] == 1 and c[3][0] == 1 and c[2][0] == 0
    assert c[4] is not None and out["outside"] is None


def test_click_on_the_whole_track_maps_to_a_global_bar(out):
    """点整首波形上的一个位置：换成这套谱里第一次放到那里的全场拍；这套谱不放的那一段（起播点之前、停之后）点不了。"""
    # 片段：全场 8 起播、从原曲第 4 拍放到第 20 拍（全场 24），然后在原曲 16–24 拍转圈到全场 40
    assert out["g"] == [8, 14, 23.5, 20, 28, 31, None, None, None]  # 原曲 16–20 拍直放时先放到过一次（全场 20–24）
    assert out["gmid"] == [0, 5, 6, 7.5, None, None]  # 从圈中间（第 6 拍）起播：圈头第 4 拍要等转回来（全场第 6 拍）
    assert out["spans"] == [[4, 20, False, None], [16, 24, True, 2]]  # loop 的那一圈：原曲 16–24 拍，转 2 圈


def test_selected_span_and_needed_bands(out):
    """整首波形上框出来的「选用哪一段」= 放到的最前面到最后面；「要哪些元素」按三个频段的 EQ 从头到尾怎么摆。"""
    assert out["pick"] == [{"a": 4, "b": 24}, {"a": 4, "b": 12}, None]
    assert out["bands"] == [{"lo": "off", "mid": "on", "hi": "on"}, {"lo": "in", "mid": "on", "hi": "out"},
                            {"lo": "on", "mid": "on", "hi": "on"}]


def test_set_time_matches_the_compiler(out):
    """画面上的 Set 时间（全场第几拍是第几秒）和编译器的速度图是同一个算法。"""
    from djstudio.v5.compile import TempoMap

    tm = TempoMap(TEMPO)
    assert out["sec"] == pytest.approx([tm.sec_at(b) for b in BEATS], abs=1e-6)
    assert out["clock"] == ["0:00", "0:59", "1:01", "59:59", "1:00:00", "1:02:05"]
