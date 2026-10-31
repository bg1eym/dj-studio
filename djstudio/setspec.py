"""Set 播放规范（docs/SET_SPEC.md）在代码里的那一份。

docs/SET_SPEC.md 是给人看的规范，以后生成的 set 都按它来；这里的常量和它的表一一对应
（tests/test_setspec.py 逐项比对 md 里的表，改一边不改另一边测试会失败）。

Prime 4+（Engine DJ）的 hot cue（v2）：打击垫**没有固定用途**。hot cue 跟着这一套 set 走：每首放的是它在这套 set 里
要用到的点——什么时候进（IN）、进来时要处理的点、什么时候出（OUT）、出去时要处理的点、停（STOP）/ 淡出（FADE）——
按时间先后从 1 号垫往后排（1–4 上排最常用）。点从过渡的曲线里找：旋钮 / 推子开始拧或一下拧到位、开关效果、loop、停。
颜色表示点的种类（绿 = 进，红 = 出，黄 = 动旋钮 / 推子，青 = 效果，紫 = 停）。

- ``set_points``：每首歌在这套 set 里的点；
- ``fill_hot_cues``：把点放进空着的垫子（谱上 / 手写的垫子不动），主 Cue = IN，保存 loop；
- ``check_hot_cues``：编译时检查；
- ``cue_sheet_md``：导出给 Prime 4+ 照着设 cue 的表（Markdown）。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .setfile import HotCue, SavedLoop, SetFile

if TYPE_CHECKING:
    from .compiler import Compiled, TrackInfo

SPEC_VERSION = "2"
NAME_MAX = 16  # Prime 4+ 屏幕上 hot cue 名字的长度上限（按 16 个 ASCII 字符算）
PAD_COUNT = 8

# 打碟谱 / set 里可以直接写的颜色名（Engine DJ 里选最接近的那个）
COLOR_NAMES: dict[str, str] = {
    "白": "#FFFFFF", "绿": "#22D45A", "蓝": "#2F7BFF", "橙": "#FF8C1A", "红": "#FF3B3B", "紫": "#A45CFF",
    "黄": "#FFD60A", "青": "#00C8FF", "粉": "#FF5FA2",
}
LOOP_COLOR = COLOR_NAMES["青"]


@dataclass(frozen=True)
class CueKind:
    key: str
    names: str  # 显示在 Prime 4+ 屏幕上的名字（英文）
    color_name: str
    when: str  # 什么时候放

    @property
    def color(self) -> str:
        return COLOR_NAMES[self.color_name]


# 自动放的点的种类：名字、颜色、什么时候放。顺序 = 一首歌在 set 里走过的顺序
CUE_KINDS: tuple[CueKind, ...] = (
    CueKind("in", "IN", "绿", "进：这首从这里起播（开场曲 = 起播小节；其它 = 过渡里进场的小节）。主 Cue 也在这里"),
    CueKind("move", "BASS IN / BASS OUT / MID UP / HIGH DOWN / EQ DOWN / HPF / LPF / FILTER OFF / FADER UP / FADER DOWN",
            "黄", "过渡里这首的旋钮 / 推子开始拧，或一下拧到位"),
    CueKind("fx", "ECHO / REVERB / ECHO OFF / REVERB OFF", "青", "过渡里开 / 关效果"),
    CueKind("loop", "LOOP 8 …", "粉", "过渡里打 loop"),
    CueKind("out", "OUT", "红", "出：这首当出曲的那段过渡从这里开始"),
    CueKind("stop", "STOP", "紫", "这首在这里停（谱上的「停」）"),
    CueKind("fade", "FADE", "红", "最后一首：从这里开始淡出"),
)
KIND = {k.key: k for k in CUE_KINDS}
_PRIO = {"in": 0, "out": 0, "stop": 0, "fade": 0, "fx": 1, "loop": 2, "move": 2}  # 点太多放不下时先留的


def default_color(name: str) -> str:
    """谱上的 hot cue 没写颜色时，按名字猜（IN 绿、OUT / FADE 红、STOP 紫、效果青、loop 粉、旋钮黄、DROP 橙、BREAK 蓝，其它白）。"""
    n = name.upper()
    words = set(re.findall(r"[A-Z]+", n))
    if "IN" in words and not words & {"BASS", "FADER"}:
        return KIND["in"].color
    if words & {"OUT", "FADE"} and not words & {"BASS", "FADER"}:
        return KIND["out"].color
    if words & {"STOP", "END"}:
        return KIND["stop"].color
    if words & {"ECHO", "REVERB", "FX"}:
        return KIND["fx"].color
    if "LOOP" in words:
        return KIND["loop"].color
    if words & {"BASS", "LOW", "MID", "HIGH", "EQ", "HPF", "LPF", "FILTER", "FADER"}:
        return KIND["move"].color
    if "DROP" in words:
        return COLOR_NAMES["橙"]
    if "BREAK" in words or "BREAKDOWN" in words:
        return COLOR_NAMES["蓝"]
    return COLOR_NAMES["白"]


# ---------------------------------------------------------------- 钟点位（注释里用 Prime 4+ 的说法）

def _clock(c: float) -> str:
    c = round(c * 2) / 2
    whole = math.floor(c)
    return f"{whole}点半" if c - whole else f"{whole}点"


def knob_text(param: str, v: float) -> str:
    """set 里的值 → Prime 4+ 面板上的说法：EQ dB → 钟点位，Filter → 关 / 左 N 点 / 右 N 点，推子 → 0–10，效果 → 湿度。"""
    from .plan.convert import EQ_CUT_DB, EQ_MAX_DB, EQ_TAPER

    if param in ("lo", "mid", "hi"):
        if v >= 0:
            return _clock(min(v, EQ_MAX_DB) / EQ_MAX_DB * 5) if v > 0.05 else "12点"
        h = 5 * (min(-v, -EQ_CUT_DB) / -EQ_CUT_DB) ** (1 / EQ_TAPER)
        return _clock(12 - h)
    if param == "filter":
        h = round(v * 5 * 2) / 2
        return "关" if h == 0 else (f"左{_clock(12 + h)}" if h < 0 else f"右{_clock(h)}")
    if param == "vol":
        return f"{round(v * 10, 1):g}"
    return "关" if v <= 0 else f"湿度 {v:.2g}"


PARAM_TEXT = {"lo": "LOW", "mid": "MID", "hi": "HIGH", "filter": "Filter", "vol": "推子", "echo": "Echo", "reverb": "Reverb"}


# ---------------------------------------------------------------- 一首歌在 set 里的点

@dataclass
class Move:
    param: str
    frm: float
    to: float
    t0: float  # 时间线小节：开始
    t1: float  # 到位
    jump: bool  # 一拍到位

    def text(self) -> str:
        how = "一拍到位" if self.jump else f"{self.t1 - self.t0:g} 小节拧到"
        if self.param in ("echo", "reverb"):
            return f"{PARAM_TEXT[self.param]} {'开 ' + knob_text(self.param, self.to) if self.to > 0 else '关'}"
        return f"{PARAM_TEXT[self.param]} {knob_text(self.param, self.frm)} → {knob_text(self.param, self.to)}（{how}）"


def lane_moves(lanes: dict[str, list[list[Any]]], deck: str) -> dict[float, list[Move]]:
    """一个 deck 的曲线里，旋钮 / 推子 / 效果在哪些时间点开始动（时间线小节 → 这一刻开始的动作）。
    一下拧到位（step）= 那一拍；渐变 = 渐变开始的那一拍；同方向接着拧的几段算一次。"""
    from .compiler import NEUTRAL

    out: dict[float, list[Move]] = {}
    for name, pts in sorted(lanes.items()):
        d, _, param = name.partition(".")
        if d != deck or not pts or param not in NEUTRAL:
            continue
        prev_t: float | None = None
        prev_v = 0.0 if name == "B.vol" else NEUTRAL[param]
        last: Move | None = None
        for p in pts:
            t, v = float(p[0]), float(p[1])
            step = len(p) == 3 and p[2] == "step"
            if step:
                if abs(v - prev_v) > 1e-9:
                    last = Move(param, prev_v, v, t, t, True)
                    out.setdefault(round(t, 4), []).append(last)
            elif prev_t is not None and abs(v - prev_v) > 1e-9:
                if last and not last.jump and abs(last.t1 - prev_t) < 1e-9 and (v - prev_v) * (last.to - last.frm) > 0:
                    last.t1, last.to = t, v  # 同方向接着拧
                else:
                    last = Move(param, prev_v, v, prev_t, t, False)
                    out.setdefault(round(prev_t, 4), []).append(last)
            prev_t, prev_v = t, v
    return out


def lane_value(pts: list[list[Any]], t: float, before: float) -> float:
    """曲线在时间线 t（这一拍上的跳变已经发生）的值。"""
    if not pts:
        return before
    if float(pts[0][0]) > t + 1e-9:
        return before if len(pts[0]) == 3 and pts[0][2] == "step" else float(pts[0][1])
    v = float(pts[0][1])
    for k, p in enumerate(pts):
        x = float(p[0])
        if x <= t + 1e-9:
            v = float(p[1])
            continue
        x0, v0 = float(pts[k - 1][0]), float(pts[k - 1][1])
        if not (len(p) == 3 and p[2] == "step") and x > x0:
            v = v0 + (float(p[1]) - v0) * (t - x0) / (x - x0)
        break
    return v


def move_name(moves: list[Move]) -> tuple[str, str]:
    """一组同时开始的动作 → (名字, 种类)。名字放不下 16 个字符时按 效果 > 低频 > Filter > EQ > 推子 留前面的。"""
    tokens: list[tuple[int, str, str]] = []
    for m in moves:
        if m.param in ("echo", "reverb"):
            tokens.append((0, m.param.upper() + ("" if m.to > 0 else " OFF"), "fx"))
        elif m.param == "lo":
            tokens.append((1, "BASS IN" if m.to > m.frm else "BASS OUT", "move"))
        elif m.param == "filter":
            tokens.append((2, "FILTER OFF" if abs(m.to) < 1e-9 else ("HPF" if m.to > 0 else "LPF"), "move"))
    eq = [m for m in moves if m.param in ("mid", "hi")]
    if len(eq) == 2 and (eq[0].to - eq[0].frm) * (eq[1].to - eq[1].frm) > 0:
        tokens.append((3, "EQ UP" if eq[0].to > eq[0].frm else "EQ DOWN", "move"))
    else:
        for m in eq:
            tokens.append((3, f"{'MID' if m.param == 'mid' else 'HIGH'} {'UP' if m.to > m.frm else 'DOWN'}", "move"))
    for m in moves:
        if m.param == "vol":
            tokens.append((4, "FADER UP" if m.to > m.frm else "FADER DOWN", "move"))
    tokens.sort(key=lambda x: x[0])
    name = ""
    for _, txt, _ in tokens:
        cand = f"{name} {txt}".strip()
        if len(cand) <= NAME_MAX:
            name = cand
    return (name or "MIX"), (tokens[0][2] if tokens else "move")


@dataclass
class SetPoint:
    bar: float  # 这首歌的小节（本应用）
    kind: str  # CUE_KINDS 的 key
    name: str
    note: str
    moves: list[Move] = field(default_factory=list)


def set_points(s: SetFile, c: Compiled) -> list[list[SetPoint]]:
    """每首歌在这套 set 里的点（按时间排）：IN → 进来时要处理的点 → OUT → 出去时要处理的点 → STOP / FADE。
    过渡里的动作分给动的那个 deck：A 的放在出曲上（A 的小节），B 的放在进曲上（B 的小节）。谱上「动作」列的字写进注释。"""
    from .compiler import NEUTRAL

    n = len(s.tracks)
    out: list[list[SetPoint]] = [[] for _ in range(n)]
    if n == 0:
        return out
    out[0].append(SetPoint(s.tracks[0].start, "in", "IN", f"开场：从第 {s.tracks[0].start:g} 小节起播"))
    for i, tr in enumerate(s.transitions):
        ex = c.expanded[i] if i < len(c.expanded) else None
        if ex is None or i + 1 >= n:
            continue
        where = f"第 {i + 1} 段过渡（{tr.label or {'longhand': '长交接', 'instant': '瞬时切换', 'custom': '自定义'}[tr.technique]}）"
        marks = {round(float(m[0]), 4): str(m[1]) for m in tr.marks}
        nxt = s.tracks[i + 1]
        play = float(ex.play_at)

        def note(head: str, t: float, moves: list[Move]) -> str:
            parts = [head]
            if marks.get(round(t, 4)):
                parts.append(f"谱上的动作：{marks[round(t, 4)]}")
            if moves:
                parts.append("；".join(m.text() for m in moves))
            return "。".join(parts)

        # A：出曲
        a_moves = lane_moves(ex.lanes, "A")
        stop = next((float(e[0]) for e in ex.events if str(e[1]) == "A.stop"), None)
        loops = {round(float(e[0]), 4): str(e[1]).split(":")[1] for e in ex.events if str(e[1]).startswith("A.loop:")}
        times = sorted(set(a_moves) | {0.0} | ({round(stop, 4)} if stop is not None else set()) | set(loops))
        for t in times:
            mv = a_moves.get(t, [])
            if t == 0:
                name, kind, head = "OUT", "out", f"{where}从这里开始，交给 #{i + 2} {nxt.title or nxt.id}"
            elif stop is not None and abs(t - stop) < 1e-9:
                name, kind, head = "STOP", "stop", f"{where}：这首在这里停"
            elif t in loops:
                name, kind, head = f"LOOP {loops[t]}", "loop", f"{where}：在这里打 {loops[t]} 拍 loop"
            else:
                name, kind = move_name(mv)
                head = f"{where}，时间线第 {t:g} 小节"
            out[i].append(SetPoint(tr.start + t, kind, name, note(head, t, mv), mv))
        # B：进曲
        b_moves = lane_moves(ex.lanes, "B")
        state = []
        for param in ("vol", "lo", "mid", "hi", "filter", "echo", "reverb"):
            pts = ex.lanes.get(f"B.{param}")
            if pts is None:
                continue
            before = 0.0 if param == "vol" else NEUTRAL[param]
            v = lane_value(pts, play, before)
            if param == "vol" or abs(v - NEUTRAL[param]) > 1e-6:
                state.append(f"{PARAM_TEXT[param]} {knob_text(param, v)}")
        head = f"{where}：从这里起播" + (f"（{'、'.join(state)}）" if state else "")
        at_play = [m for m in b_moves.get(round(play, 4), []) if not (m.param == "vol" and m.jump)]
        out[i + 1].append(SetPoint(tr.b_in, "in", "IN", note(head, play, at_play), at_play))
        b_loops = {round(float(e[0]), 4): str(e[1]).split(":")[1] for e in ex.events if str(e[1]).startswith("B.loop:")}
        for t in sorted(set(b_moves) | set(b_loops)):
            if t <= play + 1e-9:
                continue
            mv = b_moves.get(t, [])
            if t in b_loops:
                name, kind, head = f"LOOP {b_loops[t]}", "loop", f"{where}：在这里打 {b_loops[t]} 拍 loop"
            else:
                name, kind = move_name(mv)
                head = f"{where}，时间线第 {t:g} 小节"
            out[i + 1].append(SetPoint(tr.b_in + t - play, kind, name, note(head, t, mv), mv))
    last = c.tracks[n - 1] if len(c.tracks) == n else None
    if c.ending and last is not None:
        bar = last.grid.beat_bar(c.ending["a"]["start"])
        out[n - 1].append(SetPoint(bar, "fade", "FADE", f"最后一首：从这里开始淡出，{c.ending['len'] / 4:g} 小节淡完"))
    for pts in out:
        pts.sort(key=lambda p: p.bar)
    return out


# ---------------------------------------------------------------- 放进打击垫

# v1 规范里固定用途的垫子（自动放的）：v1 的 set 重新填时换掉
_V1_PADS = {(1, "START"), (2, "MIX IN"), (3, "BREAK 1"), (4, "DROP 1"), (5, "BREAK 2"), (6, "DROP 2"), (7, "MIX OUT"), (8, "END")}


def fill_hot_cues(s: SetFile, c: Compiled) -> SetFile:
    """把每首歌在这套 set 里的点按时间先后放进空着的打击垫（从 1 号垫起）。谱上 / 手写的垫子不动（auto = False）；
    以前自动放的（auto = True）重新算。点比空垫子多时先留 IN / OUT / STOP / FADE，再留效果，其它按时间。主 Cue = IN。"""
    points = set_points(s, c)
    tracks = []
    for i, st in enumerate(s.tracks):
        manual = [h for h in st.hot_cues if not h.auto and not (s.spec == "1" and (h.pad, h.name) in _V1_PADS)]
        taken = {h.pad for h in manual}
        free = [p for p in range(1, PAD_COUNT + 1) if p not in taken]
        # 手写了同名、位置差不到 1 小节的点不再放；手写了叫 IN 的垫子，自动的 IN 让给它
        pts = [p for p in points[i] if not any(h.name.upper() == p.name and (abs(h.bar - p.bar) <= 1 or p.kind == "in")
                                               for h in manual)]
        if len(pts) > len(free):
            keep = sorted(range(len(pts)), key=lambda k: (_PRIO[pts[k].kind], pts[k].bar))[:len(free)]
            pts = [pts[k] for k in sorted(keep)]
        auto = [HotCue(pad=pad, name=p.name, bar=round(max(1.0, p.bar), 4), color=KIND[p.kind].color, note=p.note, auto=True)
                for pad, p in zip(free, pts)]
        manual_in = next((h for h in manual if h.name.upper() == "IN"), None)
        auto_in = next((p for p in points[i] if p.kind == "in"), None)
        main = manual_in.bar if manual_in else (auto_in.bar if auto_in else st.main_cue)
        tracks.append(st.model_copy(update={"hot_cues": sorted(manual + auto, key=lambda h: h.pad), "main_cue": main}))
    # 保存 loop：过渡事件里的 loop
    loops: list[list[SavedLoop]] = [[lp for lp in t.loops if not lp.auto] for t in tracks]
    for i, tr in enumerate(s.transitions):
        ex = c.expanded[i] if i < len(c.expanded) else None
        play = float(ex.play_at) if ex else float(tr.play_at or 0)
        for bar, ev in (ex.events if ex else tr.events):
            m = re.match(r"^(A|B)\.loop:(\d+(?:\.\d+)?)$", str(ev))
            if not m:
                continue
            k = i if m.group(1) == "A" else i + 1
            at = tr.start + float(bar) if m.group(1) == "A" else tr.b_in + float(bar) - play
            if len(loops[k]) < PAD_COUNT:
                beats = float(m.group(2))
                loops[k].append(SavedLoop(pad=len(loops[k]) + 1, name=f"LOOP {beats:g}", bar=round(max(1.0, at), 4),
                                          beats=beats, note=f"第 {i + 1} 段过渡里的 {beats:g} 拍 loop", auto=True))
    tracks = [t.model_copy(update={"loops": lp}) for t, lp in zip(tracks, loops)]
    return s.model_copy(update={"tracks": tracks, "spec": SPEC_VERSION})


# ---------------------------------------------------------------- 检查

def check_hot_cues(s: SetFile, infos: list[TrackInfo | None]) -> list[tuple[str, str, str]]:
    """(级别, 位置, 说明)。位置超出曲子 = error。"""
    out: list[tuple[str, str, str]] = []
    for i, (st, info) in enumerate(zip(s.tracks, infos)):
        where = f"#{i + 1} {st.title or st.id} 的 hot cue"
        last = info.grid.last_bar if info and info.grid.duration else None
        for h in [*st.hot_cues, *st.loops]:
            if last is not None and h.bar > last + 0.01:
                out.append(("error", where, f"打击垫 {h.pad}「{h.name}」在第 {h.bar:g} 小节，曲子只有 {last:.1f} 小节"))
        if st.main_cue is not None and last is not None and st.main_cue > last + 0.01:
            out.append(("error", where, f"主 Cue 在第 {st.main_cue:g} 小节，曲子只有 {last:.1f} 小节"))
    return out


# ---------------------------------------------------------------- Prime 4+ 的 hot cue 表

def fmt_time(t: float) -> str:
    t = max(0.0, t)
    m = int(t // 60)
    return f"{m}:{t - 60 * m:06.3f}"


def _bar_txt(bar: float, offset: int | None) -> tuple[str, str]:
    whole = math.floor(bar + 1e-6)
    beat = round((bar - whole) * 4) + 1
    ours = f"{whole}.{beat}"
    plan = f"{whole - offset}.{beat}" if offset is not None and whole - offset >= 1 else "—"
    return ours, plan


def cue_sheet_md(s: SetFile, c: Compiled) -> str:
    """每首一张表：打击垫、名字、颜色、位置（本应用小节.拍 / 谱上小节.拍 / 时间）、注释。照着在 Engine DJ 或 Prime 4+ 上设。"""
    lines = [f"# {s.name or s.id} — Hot Cue 表（Denon Prime 4+）", "",
             f"按 DJ Studio 播放规范 v{s.spec or SPEC_VERSION}（docs/SET_SPEC.md）。打击垫没有固定用途：每首放的是它在这套 set 里"
             "要用到的点，按时间先后从 1 号垫排（进 → 进来时要处理的点 → 出 → 出去时要处理的点 → 停 / 淡出）；谱上自己写的垫子照写的。"
             "位置写三种：本应用的小节.拍、谱（Engine DJ）上的小节.拍、离文件开头的时间（分:秒.毫秒）。"
             "在 Engine DJ 里把播放头放到这个时间，按对应的打击垫存 cue，再改名字和颜色。", "",
             "| 名字 | 颜色 | 什么时候放 |", "|---|---|---|"]
    for k in CUE_KINDS:
        lines.append(f"| {k.names} | {k.color_name} {k.color} | {k.when} |")
    for i, (st, info) in enumerate(zip(s.tracks, c.tracks)):
        title = st.title or st.id
        bpm = f"{info.grid.bpm:.2f}" if info else "?"
        lines += ["", f"## #{i + 1} {title}", "",
                  f"原 BPM {bpm}" + (f"，set 里放 {st.play_bpm:g} BPM" if st.play_bpm else "")
                  + (f"；谱上第 b 小节 = 本应用第 b + {st.plan_bar_offset} 小节" if st.plan_bar_offset else ""), ""]
        if st.main_cue is not None:
            ours, plan = _bar_txt(st.main_cue, st.plan_bar_offset)
            t = fmt_time(info.grid.bar_time(st.main_cue)) if info else "?"
            lines += [f"主 Cue（CUE 键）：第 {ours} 小节（谱 {plan}），{t}", ""]
        lines += ["| 打击垫 | 名字 | 颜色 | 小节.拍 | 谱上 | 时间 | 注释 |", "|---|---|---|---|---|---|---|"]
        for h in st.hot_cues:
            ours, plan = _bar_txt(h.bar, st.plan_bar_offset)
            t = fmt_time(info.grid.bar_time(h.bar)) if info else "?"
            cname = next((k for k, v in COLOR_NAMES.items() if v == h.color), "")
            lines.append(f"| {h.pad} | {h.name} | {cname} {h.color} | {ours} | {plan} | {t} | {h.note} |")
        if st.loops:
            lines += ["", "| 保存 loop | 名字 | 小节.拍 | 谱上 | 时间 | 长度 | 注释 |", "|---|---|---|---|---|---|---|"]
            for lp in st.loops:
                ours, plan = _bar_txt(lp.bar, st.plan_bar_offset)
                t = fmt_time(info.grid.bar_time(lp.bar)) if info else "?"
                lines.append(f"| {lp.pad} | {lp.name} | {ours} | {plan} | {t} | {lp.beats:g} 拍 | {lp.note} |")
    return "\n".join(lines) + "\n"
