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
import { eqHours, eqDb, laneFrac, nativeOf, waveCol, eachSrcBar, globalOfSrc, clipSpans, clipPick, clipBands, secAt, clock, pageStart, glide,
  overlayFrac, elemLevel, filterPass, GRID_NOON, passThrough, cellLook, LAMP, gridY, fxCat } from "./live5.js";
import { eqHours as v4Hours, faderPos } from "./live.js";
import { overviewColumns, stackColumn } from "./wavedraw.js";
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
// 曲线一页一页地翻：一页 64 拍（16 小节），每一拍落在哪一页
out.pages = [0, 10, 47.9, 48, 95.9, 96, 400].map((b) => pageStart(b, 64));
out.scenePages = [320, 330, 415.9, 416].map((b) => pageStart(b, 128, 320)); // 按场景看、场景比一页长：从场景的开头数页
// 游标匀着走：132 BPM（每毫秒 0.0022 拍），每帧 16.7 毫秒，引擎每 250 毫秒报一次、每次差 ±0.04 拍
{
  const rate = 132 / 60000, dt = 1000 / 60;
  let shown = 100, truth = 100, target = 100, minStep = 1e9, maxStep = 0;
  for (let i = 1; i <= 600; i++) {
    truth += dt * rate;
    if (i % 15 === 0) target = truth + (i % 30 === 0 ? 0.04 : -0.04); else target += dt * rate;
    const next = glide(shown, target, dt, rate);
    minStep = Math.min(minStep, next - shown); maxStep = Math.max(maxStep, next - shown);
    shown = next;
  }
  out.glide = { min: minStep / (dt * rate), max: maxStep / (dt * rate), off: Math.abs(shown - truth) };
  out.glideSnap = [glide(100, 108, dt, rate), glide(100, 99.5, dt, rate) >= 100, glide(100, 100, 5000, rate) - 100 < 0.6];
}
// 整首波形（Prime 4 的 Track Overview 那种画法）：三段的音量按真实比例加在一起，从底边往上摞；整首最高的那一列画满
{
  // 8 帧、每秒 1 帧、整首 8 秒，画 4 列（一列 2 帧）。不另外抹平（smooth = 0）
  const ow = { rate: 1, n: 8, low: Uint8Array.from([200, 100, 100, 100, 0, 0, 40, 40]), mid: Uint8Array.from([40, 20, 50, 50, 0, 0, 10, 10]), high: Uint8Array.from([10, 0, 0, 0, 0, 0, 0, 0]) };
  out.ovCols = overviewColumns(ow, 4, 8, 0);
  out.ovOut = overviewColumns(ow, 4, 16, 0);           // 画布比曲子长一倍：后一半没有
  out.ovSmooth = overviewColumns(ow, 8, 8, 4)[0];      // 抹平 4 秒：第一列看的是前后各 2 秒
  out.ovStack = stackColumn([0.5, 0.25, 0.125], 60, 40); // 底边 y = 60，满高 40
}
// 元素格子图：旋钮线叠在格子上用的纵轴、某一小节有多满、过完旋钮还剩多少
{
  const flat = { lo: 0, mid: 0, hi: 0, filter: 0, vol: 1 };
  const KICK = [0.8, 0.15, 0.05], HAT = [0, 0.1, 0.9], BASS = [0.85, 0.15, 0];
  out.noon = GRID_NOON;
  out.ovl = [overlayFrac("lo", -25), overlayFrac("lo", eqDb(-3)), overlayFrac("lo", 0), overlayFrac("hi", 6), overlayFrac("vol", 1), overlayFrac("vol", 0.5), overlayFrac("vol", 0)];
  const spans = [[24, 72, 1], [96, 168, 0.6]];
  out.lvl = [23, 24, 71, 72, 96, 167, 168].map((b) => elemLevel(spans, b)).concat([elemLevel(undefined, 3)]);
  out.aud = {
    full: passThrough(KICK, flat),
    kickLowCut: passThrough(KICK, { ...flat, lo: -25 }), hatLowCut: passThrough(HAT, { ...flat, lo: -25 }),
    hatHighDown: passThrough(HAT, { ...flat, hi: -6 }), boost: passThrough(KICK, { ...flat, lo: 6 }),
    faderDown: passThrough(KICK, { ...flat, vol: 0 }), faderHalf: passThrough(KICK, { ...flat, vol: 0.5 }),
    bassHpf: passThrough(BASS, { ...flat, filter: 0.5 }), hatLpf: passThrough(HAT, { ...flat, filter: -0.5 }), hatHpf: passThrough(HAT, { ...flat, filter: 0.5 }),
  };
  out.fpass = [filterPass(0), filterPass(0.25), filterPass(1), filterPass(-1)];
  // 格子的样子：高 = 原曲里有多满；亮 = 旋钮放过去多少（像一盏带调光的灯）
  const look = (lv, w, g) => cellLook(lv, passThrough(w, g));
  out.lamp = LAMP;
  out.look = {
    full: look(1, KICK, flat), weak: look(0.3, KICK, flat), mid: look(0.6, KICK, flat),
    kickLowCut: look(1, KICK, { ...flat, lo: -25 }), bassLowCut: look(1, BASS, { ...flat, lo: -25 }), hatLowCut: look(1, HAT, { ...flat, lo: -25 }),
    hatHighCut: look(1, HAT, { ...flat, hi: -25 }), kickLow9: look(1, KICK, { ...flat, lo: eqDb(-3) }), kickLow11: look(1, KICK, { ...flat, lo: eqDb(-1) }),
    fader0: look(1, KICK, { ...flat, vol: 0 }), fader2: look(1, KICK, { ...flat, vol: 0.2 }), fader5: look(1, KICK, { ...flat, vol: 0.5 }), fader8: look(1, KICK, { ...flat, vol: 0.8 }),
    bassHpf: look(1, BASS, { ...flat, filter: 0.5 }), hatLpf: look(1, HAT, { ...flat, filter: -0.5 }),
  };
  out.ramp = [0, 0.1, 0.2, 0.35, 0.5, 0.65, 0.8, 1].map((t) => cellLook(1, t).a);
  // 叠在格子上的四条线上下错开：同一个值，HIGH 最上、推子最下，隔得一样远；不带参数 = 正中（右边的刻度）
  const row = { y: 100, h: 110 };
  out.gy = { hi: gridY(row, "hi", 0.5), mid: gridY(row, "mid", 0.5), lo: gridY(row, "lo", 0.5), vol: gridY(row, "vol", 0.5), mid0: gridY(row, null, 0.5),
    top: gridY(row, "hi", 1), bottom: gridY(row, "vol", 0), box: [100 + 11, 100 + 110 - 2] };
  out.cat = [fxCat({ kind: "echo", route: "send" }), fxCat({ kind: "roll", route: "insert" }), fxCat({ kind: "trans", route: "group" }), fxCat({ kind: "sample", route: "master" })];
}
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


def test_curve_pages_stay_put_and_flip_with_look_ahead(out):
    """EQ 曲线一页一页地看：一页里的线不动（同一页里的拍都落在同一个页头），游标走到最后四分之一页才翻，
    新的一页正好从游标那里开始——后面至少四分之一页一直看得到。"""
    assert out["pages"] == [0, 0, 0, 48, 48, 96, 384]  # 16 小节一页：每 12 小节翻一次
    assert out["scenePages"] == [320, 320, 320, 416]  # 场景从第 320 拍起、一屏 32 小节：从场景的开头数


def test_cursor_glides_instead_of_stopping_and_jumping(out):
    """引擎的拍一块一块报上来（每次和画面差几十毫秒）：游标照样匀着走——每一帧走的拍数和正常速度差不到两成，
    不停、不往回走，也不越走越偏；差得多了（卡住、跳转）才直接过去。"""
    g = out["glide"]
    assert 0.8 < g["min"] <= g["max"] < 1.2
    assert g["off"] < 0.06
    assert out["glideSnap"] == [108, True, True]  # 差 8 拍：直接过去；目标在后面：停着不退；隔了很久才画下一帧：不会一下窜出去


def test_whole_track_waveform_is_stacked_from_the_bottom_like_engine(out):
    """整首波形照 Prime 4 的 Track Overview：一列 = 这一小段时间里三段各自的平均，三段按真实的比例加在一起（不各自拉满），
    从底边往上摞——低频贴着底边，中频接在上面，高频在最上面；整首统一一个比例，最高的那一列正好画满。"""
    c = out["ovCols"]
    # 第一列：low (200+100)/2 = 150，mid 30，high 5 → 一共 185，是最高的一列 → 画满
    assert c[0] == pytest.approx([150 / 185, 30 / 185, 5 / 185]) and sum(c[0]) == pytest.approx(1)
    assert c[1] == pytest.approx([100 / 185, 50 / 185, 0])  # 三段的比例是真实的：高频没有就没有，不拉满
    assert c[2] == [0, 0, 0] and c[3] == pytest.approx([40 / 185, 10 / 185, 0])  # 静音 = 0
    assert out["ovOut"][1] is not None and out["ovOut"][2] is None and out["ovOut"][3] is None  # 曲子以外没有
    assert out["ovSmooth"][0] > out["ovSmooth"][1] > out["ovSmooth"][2] > 0  # 抹平以后还是低 > 中 > 高
    (y_lo, h_lo), (y_mid, h_mid), (y_hi, h_hi) = out["ovStack"]
    assert (h_lo, h_mid, h_hi) == (20, 10, 5)
    assert (y_lo, y_mid, y_hi) == (40, 30, 25)  # 低频从底边 60 往上 20，中频接在它上面，高频接在中频上面


def test_element_grid_axis_and_what_is_left_after_the_knobs(out):
    """元素格子图（曲谱规范 v5 第 14 节）。旋钮线叠在格子上：7 点在最下面，12 点和推子 10 在同一条线上，5 点到顶，中间按钟点位匀着排。
    旋钮放过去多少 =（每个频段的份额 × 那个频段过 EQ、Filter 以后剩多少，加起来）× 推子的位置；和这个声部本来有多满无关。"""
    noon = out["noon"]
    assert out["ovl"] == pytest.approx([0, noon * 0.4, noon, 1, noon, noon / 2, 0])  # 7 点、9 点、12 点、5 点；推子 10 / 5 / 0
    assert out["lvl"] == [0, 1, 1, 0, 0.6, 0.6, 0, 0]  # 小节从 0 数、不含末尾；没写 = 没有
    a = out["aud"]
    assert a["full"] == pytest.approx(1)
    assert a["kickLowCut"] == pytest.approx(0.2)  # 切了 LOW：kick 只剩它在中、高频的那一点（0.15 + 0.05）
    assert a["hatLowCut"] == pytest.approx(1)  # 镲不在低频：切 LOW 对它没影响
    assert a["hatHighDown"] == pytest.approx(0.1 + 0.9 * 10 ** (-6 / 20))  # HIGH 退 6 dB：高频那九成剩一半
    assert a["boost"] == pytest.approx(1)  # 12 点以上按全放过去算
    assert a["faderDown"] == 0 and a["faderHalf"] == pytest.approx(0.5)  # 推子在底 = 没有；按位置算，推到一半 = 一半
    assert a["bassHpf"] == pytest.approx(0.15) and a["hatLpf"] == pytest.approx(0.1) and a["hatHpf"] == pytest.approx(1)  # Filter 往右先吃低频，往左先吃高频
    assert out["fpass"] == [[1, 1, 1], [0.5, 1, 1], [0, pytest.approx(0.2), 1], [1, pytest.approx(0.2), 0]]


def test_cells_light_up_and_go_dark_with_the_knobs(out):
    """格子的亮灭只看旋钮放过去多少（EQ、Filter、推子），高矮只看原曲里有多满——两样不混。
    切掉 LOW：kick、bass 灭，闭镲照样全亮；切掉 HIGH 反过来。亮度是调光灯式的：剩不到两成算灭，八成以上全亮，中间一路变亮。"""
    k = out["look"]
    assert out["lamp"] == {"off": 0.2, "full": 0.8}
    # 高矮：满 / 中 / 弱，和旋钮无关
    assert [k[n]["h"] for n in ("full", "mid", "weak")] == pytest.approx([1.0, 0.68, 0.44])
    assert k["kickLowCut"]["h"] == k["full"]["h"] == 1.0
    # 亮灭：原曲里弱不弱不影响亮度
    assert k["full"]["a"] == k["weak"]["a"] == pytest.approx(0.95) and k["full"]["lit"]
    assert not k["kickLowCut"]["lit"] and not k["bassLowCut"]["lit"] and k["kickLowCut"]["a"] == 0.05
    assert k["hatLowCut"]["lit"] and k["hatLowCut"]["a"] == pytest.approx(0.95)  # 切 LOW 碰不到镲
    assert not k["hatHighCut"]["lit"]
    assert k["kickLowCut"]["a"] < k["kickLow9"]["a"] < k["kickLow11"]["a"] <= k["full"]["a"]  # LOW 一点一点放回来：一路变亮
    assert k["kickLow9"]["lit"] and 0.3 < k["kickLow9"]["a"] < 0.8
    assert not k["fader0"]["lit"] and not k["fader2"]["lit"]
    assert k["fader2"]["a"] < k["fader5"]["a"] < k["fader8"]["a"] == pytest.approx(0.95)  # 推子按位置：2 以下灭，5 半亮，8 以上全亮
    assert k["fader5"]["a"] == pytest.approx(0.16 + 0.79 * 0.5)
    assert not k["bassHpf"]["lit"] and not k["hatLpf"]["lit"]  # Filter 拧到一半：高通吃掉 bass，低通吃掉镲
    r = out["ramp"]
    assert r[0] == r[1] == r[2] == 0.05 and r[-1] == r[-2] == pytest.approx(0.95)
    assert all(a < b for a, b in zip(r[2:6], r[3:7]))  # 两成到八成之间一路变亮


def test_the_four_knob_lines_on_the_grid_sit_side_by_side(out):
    """四条线同一个值时上下错开（HIGH、MID、LOW、推子，和左边图例的顺序一样），不会只看见最上面那条；
    错开以后最高、最低也不出格子那一块。效果的带子按怎么接进去分三种颜色。"""
    g = out["gy"]
    ys = [g["hi"], g["mid"], g["lo"], g["vol"]]
    steps = [b - a for a, b in zip(ys, ys[1:])]
    assert steps == pytest.approx([3.5, 3.5, 3.5])
    assert g["mid0"] == pytest.approx((g["mid"] + g["lo"]) / 2)
    assert g["top"] - 1.5 >= g["box"][0] and g["bottom"] + 1.5 <= g["box"][1]
    assert out["cat"] == ["send", "proc", "proc", "sample"]
