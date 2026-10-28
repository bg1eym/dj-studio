"""set（小节、人读单位）→ 引擎程序（拍、Mixxx 原生值），并做整套校验。

引擎程序（program）的格式就是 Python ⇄ DJStudio.js 的契约，见 docs/ENGINE.md。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

from .grid import BEATS_PER_BAR, Grid
from .setfile import SetFile, SetTrack, Transition
from .techniques import Expanded, TechniqueError, expand

KILL_DB = -40.0  # ≤ 这个 dB 视为 kill（增益 0）
EQ_MAX_GAIN = 4.0  # Mixxx EQ 旋钮上限（+12 dB）
DB_RESAMPLE_BEATS = 1.0  # dB 曲线按拍重采样，引擎侧只做线性插值

# 各通道在时间线开始前 / 未指定时的中性值（人读单位）
NEUTRAL = {"vol": 1.0, "lo": 0.0, "mid": 0.0, "hi": 0.0, "filter": 0.0, "echo": 0.0, "reverb": 0.0}


@dataclass
class TrackInfo:
    """编译需要的一首歌的事实（来自 Mixxx 库 + 我们的分析）。"""

    id: str
    title: str
    grid: Grid  # 含 downbeat 与 duration

    @property
    def bpm(self) -> float:
        return self.grid.bpm


@dataclass
class Issue:
    level: str  # error | warn
    where: str
    msg: str

    def __str__(self) -> str:
        return f"[{self.level}] {self.where}: {self.msg}"


@dataclass
class Segment:
    """时间表里的一段（用于界面时间线与超时估算）。"""

    kind: str  # solo | transition | ending
    index: int
    t0: float  # set 内秒
    t1: float
    label: str = ""


@dataclass
class Compiled:
    set: SetFile
    tracks: list[TrackInfo | None]
    programs: list[dict[str, Any]] = field(default_factory=list)
    ending: dict[str, Any] | None = None
    opening: dict[str, Any] | None = None
    issues: list[Issue] = field(default_factory=list)
    schedule: list[Segment] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(i.level == "error" for i in self.issues)

    @property
    def duration(self) -> float:
        return self.schedule[-1].t1 if self.schedule else 0.0


def deck_for(index: int) -> int:
    """第 index 首（0 起）放在哪个 deck：1、2 交替。"""
    return 1 if index % 2 == 0 else 2


# ---------------------------------------------------------------- 单位换算

def to_native(param: str, v: float) -> float:
    if param in ("lo", "mid", "hi"):
        if v <= KILL_DB:
            return 0.0
        return round(min(EQ_MAX_GAIN, 10 ** (v / 20.0)), 5)
    if param == "filter":
        return round(0.5 + 0.5 * max(-1.0, min(1.0, v)), 5)
    return round(max(0.0, min(1.0, v)), 5)


def _lane_native(name: str, pts: list[list[Any]], play_at: float) -> list[list[float]]:
    """小节/人读 → 拍/原生。step 点展开成同一拍上的两个点（先旧值后新值）。"""
    deck, _, param = name.partition(".")
    before = 0.0 if name == "B.vol" else NEUTRAL[param]
    out: list[list[float]] = []
    prev_bar: float | None = None
    prev_val = before
    is_db = param in ("lo", "mid", "hi")
    for p in pts:
        bar, val = float(p[0]), float(p[1])
        step = len(p) == 3 and p[2] == "step"
        x = bar * BEATS_PER_BAR
        if step:
            out.append([x, to_native(param, prev_val)])
        elif prev_bar is not None and is_db and val != prev_val:
            # dB 线性插值 → 逐拍重采样
            x0 = prev_bar * BEATS_PER_BAR
            n = int((x - x0) / DB_RESAMPLE_BEATS)
            for k in range(1, n):
                xi = x0 + k * DB_RESAMPLE_BEATS
                vi = prev_val + (val - prev_val) * (xi - x0) / (x - x0)
                out.append([xi, to_native(param, vi)])
        out.append([x, to_native(param, val)])
        prev_bar, prev_val = bar, val
    return [[round(a, 4), b] for a, b in out]


def _tempo_native(pts: list[list[Any]]) -> list[list[float]]:
    out: list[list[float]] = []
    prev = float(pts[0][1]) if pts else 0.0
    for p in pts:
        x = float(p[0]) * BEATS_PER_BAR
        if len(p) == 3 and p[2] == "step":
            out.append([x, round(prev, 3)])
        out.append([x, round(float(p[1]), 3)])
        prev = float(p[1])
    return out


def _grid_js(g: Grid) -> list[float]:
    return [round(g.first_beat * 1000.0, 3), g.bpm]


# ---------------------------------------------------------------- 编译

def play_bpm(t: SetTrack, info: TrackInfo) -> float:
    return t.play_bpm or info.bpm


def compile_transition(
    i: int, tr: Transition, a: TrackInfo, b: TrackInfo, a_bpm: float, b_bpm: float
) -> tuple[dict[str, Any], Expanded]:
    a_left = a.grid.last_bar - tr.start if a.grid.duration else None
    ex = expand(tr, a_bpm, b_bpm, a_left)
    lanes = {name: _lane_native(name, pts, ex.play_at) for name, pts in sorted(ex.lanes.items())}
    events = sorted(([round(float(e[0]) * BEATS_PER_BAR, 4), str(e[1])] for e in ex.events), key=lambda e: e[0])
    prog = {
        "t": "program",
        "id": f"tr{i + 1}",
        "idx": i,
        "a": {"deck": deck_for(i), "start": round(a.grid.bar_beat(tr.start), 4), "grid": _grid_js(a.grid),
              "dur": round(a.grid.duration * 1000)},
        "b": {"deck": deck_for(i + 1), "in": round(b.grid.bar_beat(tr.b_in), 4), "grid": _grid_js(b.grid),
              "dur": round(b.grid.duration * 1000)},
        "play": round(ex.play_at * BEATS_PER_BAR, 4),
        "len": round(ex.bars * BEATS_PER_BAR, 4),
        "playout": ex.a_playout,
        "lanes": lanes,
        "bpm": _tempo_native(ex.tempo),
        "events": events,
    }
    return prog, ex


def compile_set(s: SetFile, resolve: Callable[[SetTrack], TrackInfo | None]) -> Compiled:
    infos = [resolve(t) for t in s.tracks]
    c = Compiled(set=s, tracks=infos)
    err = lambda where, msg: c.issues.append(Issue("error", where, msg))  # noqa: E731
    warn = lambda where, msg: c.issues.append(Issue("warn", where, msg))  # noqa: E731

    n = len(s.tracks)
    if n == 0:
        err("set", "没有曲目")
        return c
    if len(s.transitions) != n - 1:
        err("set", f"{n} 首歌需要 {n - 1} 段过渡，现在有 {len(s.transitions)} 段")
        return c

    for k, (t, info) in enumerate(zip(s.tracks, infos)):
        w = f"#{k + 1} {t.title or t.id}"
        if info is None:
            err(w, "在曲库里找不到这首歌")
            continue
        if info.bpm <= 0 or info.grid.duration <= 0:
            err(w, "Mixxx 还没分析（没有 BPM / 时长）")
            continue
        if t.downbeat is not None:
            info.grid = Grid(info.grid.bpm, info.grid.first_beat, t.downbeat, info.grid.duration)
        pb = play_bpm(t, info)
        dev = abs(pb / info.bpm - 1)
        if dev > 0.16:
            err(w, f"主控 BPM {pb:g} 偏离原生 {info.bpm:g} 达 {dev:.0%}（Mixxx 变速范围上限 ±16%）")
        elif dev > 0.06:
            warn(w, f"主控 BPM {pb:g} 偏离原生 {info.bpm:g} 达 {dev:.0%}，听感可能明显")
    if not c.ok:
        return c

    if s.keylock:
        warn("set", "开了 keylock：变速的 deck 在 Mixxx 里会比另一边晚约 20–30ms，两个 kick 叠在一起时会有双击感")
    tinfo: list[TrackInfo] = infos  # type: ignore[assignment]
    first = s.tracks[0]
    c.opening = {
        "t": "opening",
        "keylock": s.keylock,
        "deck": deck_for(0),
        "beat": round(tinfo[0].grid.bar_beat(first.start), 4),
        "bpm": play_bpm(first, tinfo[0]),
    }

    # established[k]：第 k 首在上一段过渡结束时所处的小节（开场曲 = start）
    established = first.start
    t_now = 0.0  # set 内秒（按主控 BPM 估算）
    for i, tr in enumerate(s.transitions):
        a_t, b_t = s.tracks[i], s.tracks[i + 1]
        a, b = tinfo[i], tinfo[i + 1]
        where = f"过渡 {i + 1}（#{i + 1}→#{i + 2}）"
        a_bpm, b_bpm = play_bpm(a_t, a), play_bpm(b_t, b)
        try:
            prog, ex = compile_transition(i, tr, a, b, a_bpm, b_bpm)
        except TechniqueError as e:
            err(where, str(e))
            continue
        c.programs.append(prog)

        # 时序校验
        if tr.start < established - 1e-6:
            err(where, f"A 在第 {tr.start:g} 小节开始过渡，但上一段要到第 {established:g} 小节才结束")
        a_end_bar = tr.start + ex.bars
        if not ex.a_playout and a_end_bar > a.grid.last_bar + 0.25:
            err(where, f"过渡要用到 A 的第 {a_end_bar:g} 小节，但 A 只有 {a.grid.last_bar:.1f} 小节")
        if b.grid.bar_time(tr.b_in) < -0.01:
            err(where, f"B 的起播小节 {tr.b_in:g} 在曲首之前")
        b_est = tr.b_in + (ex.bars - ex.play_at)
        if b_est > b.grid.last_bar:
            err(where, f"过渡结束时 B 已经播到第 {b_est:g} 小节，超过曲末 {b.grid.last_bar:.1f}")
        for name in ("echo", "reverb"):
            if f"A.{name}" in ex.lanes and f"B.{name}" in ex.lanes:
                warn(where, f"A、B 同时用 {name}：两边共用同一个效果单元")
        if tr.technique == "longhand" and ex.bars - ex.play_at < 24:
            warn(where, f"长交接只有 {ex.bars - ex.play_at:g} 小节（职业实测 25–57）")

        # 时间表
        solo_bars = tr.start - established
        t_solo = solo_bars * BEATS_PER_BAR * 60.0 / a_bpm
        c.schedule.append(Segment("solo", i, t_now, t_now + t_solo, a_t.title or a_t.id))
        t_now += t_solo
        t_tr = ex.bars * BEATS_PER_BAR * 60.0 / ((a_bpm + b_bpm) / 2)
        c.schedule.append(Segment("transition", i, t_now, t_now + t_tr, tr.technique))
        t_now += t_tr
        established = b_est

    # 终曲
    last_i = n - 1
    last, last_t = tinfo[last_i], s.tracks[last_i]
    lb = play_bpm(last_t, last)
    end = s.ending
    if end.mode == "fade":
        start = end.bar if end.bar is not None else math.floor(last.grid.last_bar - end.bars)
        if start < established - 1e-6:
            err("终曲", f"淡出从第 {start:g} 小节开始，早于上一段过渡结束（第 {established:g} 小节）")
        if start + end.bars > last.grid.last_bar + 0.25:
            err("终曲", f"淡出要用到第 {start + end.bars:g} 小节，曲子只有 {last.grid.last_bar:.1f} 小节")
        L = end.bars * BEATS_PER_BAR
        c.ending = {
            "t": "program",
            "id": "end",
            "idx": n - 1,
            "a": {"deck": deck_for(last_i), "start": round(last.grid.bar_beat(start), 4),
                  "grid": _grid_js(last.grid), "dur": round(last.grid.duration * 1000)},
            "b": None,
            "play": None,
            "len": L,
            "playout": False,
            "lanes": {"A.vol": [[0.0, 1.0], [L, 0.0]]},
            "bpm": [[0.0, lb]],
            "events": [[L, "A.stop"]],
        }
        end_bar = start + end.bars
    else:
        end_bar = last.grid.last_bar
    t_solo = (end_bar - established) * BEATS_PER_BAR * 60.0 / lb
    c.schedule.append(Segment("solo" if end.mode == "playout" else "ending", last_i, t_now, t_now + max(0.0, t_solo),
                              last_t.title or last_t.id))
    return c
