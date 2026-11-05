"""set（小节、人读单位）→ 引擎程序（拍、Mixxx 原生值），并做整套校验。

引擎程序（program）的格式就是 Python ⇄ DJStudio.js 的契约，见 docs/ENGINE.md。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

from .grid import BAR1_TOL, BEATS_PER_BAR, Grid
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
class LoopSpan:
    """过渡里一边（A / B）打的一个 loop：时间线第 t_on 小节开，loop 长 bars 小节，第 t_off 小节出（None = 没出，A 在 loop 里停）。"""

    t_on: float
    bars: float
    t_off: float | None


def loop_spans(events: list[list[Any]], side: str) -> tuple[list[LoopSpan], list[str]]:
    """从过渡事件里取出一边的 loop（「A.loop:16」开 16 拍、「A.loop_off」出），顺带查写法（返回 [错误]）。"""
    spans: list[LoopSpan] = []
    errs: list[str] = []
    cur: LoopSpan | None = None
    for e in sorted(events, key=lambda e: float(e[0])):
        t, ev = float(e[0]), str(e[1])
        if not ev.startswith(side + "."):
            continue
        act = ev.split(".", 1)[1]
        if act.startswith("loop:"):
            if cur is not None:
                errs.append(f"{side} 在第 {t:g} 小节又开 loop：第 {cur.t_on:g} 小节开的 loop 还没出")
                continue
            cur = LoopSpan(t, float(act[5:]) / BEATS_PER_BAR, None)
            spans.append(cur)
        elif act == "loop_off":
            if cur is None:
                errs.append(f"{side} 在第 {t:g} 小节出 loop，但前面没开 loop")
                continue
            n = (t - cur.t_on) / cur.bars
            if n < 1 - 1e-6 or abs(n - round(n)) > 1e-6:
                errs.append(f"{side} 的 loop（第 {cur.t_on:g} 小节开，{cur.bars:g} 小节一遍）在第 {t:g} 小节出：要转满整遍"
                            f"（现在是 {n:.2f} 遍；可以在第 {cur.t_on + max(1, round(n)) * cur.bars:g} 小节出）")
            cur.t_off = t
            cur = None
    return spans, errs


def own_offset(spans: list[LoopSpan], t: float) -> float:
    """时间线第 t 小节时，这一边按自己的小节算走到了哪（时间线一直往前数；打 loop 的那几遍，自己的小节在 loop 里转）。
    A：自己的小节 = start + 这个值；B：= b_in + 这个值 − play_at。"""
    shift = 0.0
    for sp in sorted(spans, key=lambda x: x.t_on):
        if t < sp.t_on:
            break
        if sp.t_off is not None and t >= sp.t_off:
            shift += sp.t_off - sp.t_on - sp.bars
            continue
        return sp.t_on - shift + ((t - sp.t_on) % sp.bars)
    return t - shift


@dataclass
class Compiled:
    set: SetFile
    tracks: list[TrackInfo | None]
    programs: list[dict[str, Any]] = field(default_factory=list)
    ending: dict[str, Any] | None = None
    opening: dict[str, Any] | None = None
    issues: list[Issue] = field(default_factory=list)
    schedule: list[Segment] = field(default_factory=list)
    expanded: list[Any] = field(default_factory=list)  # 每段过渡展开后的曲线（人读单位、小节；hot cue 按它找点）
    loops: list[dict[str, list[LoopSpan]]] = field(default_factory=list)  # 每段过渡里 A / B 打的 loop
    plans: list[list[dict[str, Any]]] = field(default_factory=list)  # 每首独奏时的跳段 / loop（拍，发给引擎）

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

VOL_MIN_DB = -20.0  # [ChannelN],volume = ControlAudioTaperPot(-20, 0, 1)（enginemixer.cpp:894）


def fader_gain(pos: float) -> float:
    """推子位置 0..1 → Mixxx 的 volume 值（增益）。与 Mixxx 自己的推子同一条曲线：
    ControlAudioTaperPotBehavior::parameterToValue（control/controlbehavior.cpp:206），minDB -20、maxDB 0、neutral 1。
    位置 0.5 → 0.240（-12.4 dB），0.7 → 0.446（-7.0 dB），1 → 1（0 dB）；全表见 docs/SET_SPEC.md 第 5 节。"""
    if pos <= 0:
        return 0.0
    if pos >= 1:
        return 1.0
    db = pos * VOL_MIN_DB / -1.0 + VOL_MIN_DB
    off = 10 ** (VOL_MIN_DB / 20.0)
    return (10 ** (db / 20.0) - off) / (1 - off)


def to_native(param: str, v: float) -> float:
    if param in ("lo", "mid", "hi"):
        if v <= KILL_DB:
            return 0.0
        return round(min(EQ_MAX_GAIN, 10 ** (v / 20.0)), 5)
    if param == "filter":
        return round(0.5 + 0.5 * max(-1.0, min(1.0, v)), 5)
    if param == "vol":
        return round(fader_gain(v), 5)
    return round(max(0.0, min(1.0, v)), 5)


def _lane_native(name: str, pts: list[list[Any]], play_at: float) -> list[list[float]]:
    """小节/人读 → 拍/原生。step 点展开成同一拍上的两个点（先旧值后新值）。"""
    deck, _, param = name.partition(".")
    before = 0.0 if name == "B.vol" else NEUTRAL[param]
    out: list[list[float]] = []
    prev_bar: float | None = None
    prev_val = before
    is_db = param in ("lo", "mid", "hi", "vol")  # 按人读单位（dB / 推子位置）线性，逐拍重采样成原生值
    for p in pts:
        bar, val = float(p[0]), float(p[1])
        step = len(p) == 3 and p[2] == "step"
        x = bar * BEATS_PER_BAR
        if step:
            out.append([x, to_native(param, prev_val)])
        elif prev_bar is not None and is_db and val != prev_val:
            # dB / 推子位置线性插值 → 逐拍重采样
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
    # 出 loop：在最后一遍转到一半时关（Mixxx 关 loop 不挪位置，接着放到 loop 尾再往下）。正好在 loop 尾那一拍关的话，
    # 引擎可能已经跳回 loop 头了，会多转一遍
    events = []
    size: dict[str, float] = {}
    for e in sorted(ex.events, key=lambda e: float(e[0])):
        x, ev = float(e[0]) * BEATS_PER_BAR, str(e[1])
        side, _, act = ev.partition(".")
        if act.startswith("loop:"):
            size[side] = float(act[5:])
        elif act == "loop_off" and side in size:
            x -= size[side] / 2
        events.append([round(x, 4), ev])
    events.sort(key=lambda e: e[0])
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


def _moves(c: Compiled, k: int, t: SetTrack, info: TrackInfo, lo: float, hi: float,
           err: Callable[[str, str], None]) -> float:
    """第 k 首独奏（第 lo–hi 小节）时的跳段 / loop：查位置，记成发给引擎的 plan（拍），返回多放（+）/ 少放（-）的小节数。
    按放的顺序走一遍：每一步都要在上一步之后；往后跳跳过的那段、往回跳再放一遍的那段里不能再有别的跳段 / loop
    （往回跳以后再经过时不会再做一遍，容易弄错）。"""
    while len(c.plans) <= k:
        c.plans.append(None)  # type: ignore[arg-type]
    where = f"#{k + 1} {t.title or t.id}"
    acts: list[dict[str, Any]] = []
    pos, extra = lo, 0.0
    done: list[float] = []
    for m in sorted(t.moves, key=lambda m: m.bar):
        what = f"第 {m.bar:g} 小节" + (f"跳到第 {m.to:g} 小节" if m.to is not None else f"的 {m.loop:g} 小节 loop")
        if any(abs(m.bar - b) < 1e-6 for b in done):
            err(where, f"{what}：第 {m.bar:g} 小节已经有一个跳段 / loop 了。引擎同一拍的动作会一起发出去，往回跳那一下等于没做；"
                       "「放两遍再往后跳」写成「loop N 小节 × 2」，再在 loop 结束的那一小节跳")
            continue
        if m.bar < pos - 1e-6 or m.bar > hi - 1e-6:
            err(where, f"{what}：只能在这首独奏的部分（第 {pos:g}–{hi:g} 小节）——过渡里的 loop 写在过渡里，"
                       "上一个跳段跳过的地方也不行")
            continue
        if m.to is not None:
            if not lo - 1e-6 <= m.to <= hi + 1e-6:
                err(where, f"{what}：要跳到这首独奏的部分里（第 {lo:g}–{hi:g} 小节）")
                continue
            if m.to < m.bar and any(b >= m.to - 1e-6 for b in done):
                err(where, f"{what}：往回跳会再放一遍第 {m.to:g}–{m.bar:g} 小节，那里面的跳段 / loop 不会再做一遍，"
                           "把它们挪到别处")
                continue
            acts.append({"at": round(info.grid.bar_beat(m.bar), 4), "jump": round((m.to - m.bar) * BEATS_PER_BAR, 4)})
            pos = m.bar if m.to < m.bar else m.to
        else:
            assert m.loop is not None
            if m.bar + m.loop > hi + 1e-6:
                err(where, f"{what}：loop 到第 {m.bar + m.loop:g} 小节，超出这首独奏的部分（到第 {hi:g} 小节，下一段从那里开始）")
                continue
            acts.append({"at": round(info.grid.bar_beat(m.bar), 4), "loop": round(m.loop * BEATS_PER_BAR, 4),
                         "times": m.times})
            pos = m.bar + m.loop
        done.append(m.bar)
        extra += m.extra
    c.plans[k] = acts
    return extra


def compile_set(s: SetFile, resolve: Callable[[SetTrack], TrackInfo | None], resume: bool = False) -> Compiled:
    """resume = True：停下后「继续」跑的截短 set，第 1 首可以从第 1 段过渡的中途开始（引擎按 resume 补上已经过去的部分）。"""
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
            c.expanded.append(None)  # 和过渡的序号对齐
            continue
        c.programs.append(prog)
        c.expanded.append(ex)
        spans: dict[str, list[LoopSpan]] = {}
        for side in ("A", "B"):
            spans[side], errs = loop_spans(ex.events, side)
            for m in errs:
                err(where, m)
        c.loops.append(spans)
        a_stop = next((float(e[0]) for e in ex.events if str(e[1]) == "A.stop"), None)
        for sp in spans["A"]:
            if sp.t_off is None and (a_stop is None or a_stop < sp.t_on) and ex.a_playout:
                err(where, f"A 在第 {sp.t_on:g} 小节开的 loop 一直没出：A 不会自己播完。出 loop，或者让 A 停（最后一行写「停」）")
        for sp in spans["B"]:
            if sp.t_on < ex.play_at - 1e-6:
                err(where, f"B 在第 {sp.t_on:g} 小节开 loop，但 B 第 {ex.play_at:g} 小节才起播")
            if sp.t_off is None or sp.t_off > ex.bars + 1e-6:
                err(where, f"B 在第 {sp.t_on:g} 小节开的 loop 要在过渡结束（第 {ex.bars:g} 小节）以前出")

        # 时序校验
        if tr.start < established - 1e-6 and not (resume and i == 0):
            err(where, f"A 在第 {tr.start:g} 小节开始过渡，但上一段要到第 {established:g} 小节才结束")
        a_end_bar = tr.start + own_offset(spans["A"], ex.bars)
        if not ex.a_playout and a_end_bar > a.grid.last_bar + 0.25:
            err(where, f"过渡要用到 A 的第 {a_end_bar:g} 小节，但 A 只有 {a.grid.last_bar:.1f} 小节")
        # 第 1 小节 = 起点 ≥ -BAR1_TOL 的第一个小节线（grid.py），所以起播点可以在曲首前一点点：
        # Mixxx 定位到曲首之前不截到 0（enginebuffer.cpp:1353-1354 只截曲尾），读盘时那一段补静音（cachingreader.cpp:352-372）
        if b.grid.bar_time(tr.b_in) < -BAR1_TOL - 1e-6:
            err(where, f"B 的起播小节 {tr.b_in:g} 在曲首之前（{b.grid.bar_time(tr.b_in):.3f}s）")
        b_est = tr.b_in + own_offset(spans["B"], ex.bars) - ex.play_at
        if b_est > b.grid.last_bar:
            err(where, f"过渡结束时 B 已经播到第 {b_est:g} 小节，超过曲末 {b.grid.last_bar:.1f}")
        for name in ("echo", "reverb"):
            if f"A.{name}" in ex.lanes and f"B.{name}" in ex.lanes:
                warn(where, f"A、B 同时用 {name}：两边共用同一个效果单元")
        if tr.technique == "longhand" and ex.bars - ex.play_at < 24:
            warn(where, f"长交接只有 {ex.bars - ex.play_at:g} 小节（职业实测 25–57）")

        # 时间表（独奏时的跳段 / loop：多放 / 少放的小节）
        solo_bars = tr.start - established + _moves(c, i, a_t, a, established, tr.start, err)
        t_solo = solo_bars * BEATS_PER_BAR * 60.0 / a_bpm
        c.schedule.append(Segment("solo", i, t_now, t_now + t_solo, a_t.title or a_t.id))
        t_now += t_solo
        t_tr = ex.bars * BEATS_PER_BAR * 60.0 / ((a_bpm + b_bpm) / 2)
        c.schedule.append(Segment("transition", i, t_now, t_now + t_tr, tr.label or tr.technique))
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
    extra = _moves(c, last_i, last_t, last, established, start if end.mode == "fade" else end_bar, err)
    t_solo = (end_bar - established + extra) * BEATS_PER_BAR * 60.0 / lb
    c.schedule.append(Segment("solo" if end.mode == "playout" else "ending", last_i, t_now, t_now + max(0.0, t_solo),
                              last_t.title or last_t.id))
    c.plans = [c.plans[k] if k < len(c.plans) and c.plans[k] is not None else [] for k in range(n)]
    # hot cue / 主 Cue / 保存 loop：按播放规范（docs/SET_SPEC.md）检查
    from .setspec import check_hot_cues

    for level, where, msg in check_hot_cues(s, infos):
        (err if level == "error" else warn)(where, msg)
    return c
