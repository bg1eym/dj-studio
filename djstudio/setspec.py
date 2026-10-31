"""Set 播放规范（docs/SET_SPEC.md）在代码里的那一份。

docs/SET_SPEC.md 是给人看的规范，以后生成的 set 都按它来；这里的常量和它的表一一对应
（tests/test_setspec.py 逐项比对 md 里的表，改一边不改另一边测试会失败）。

- Prime 4+（Engine DJ）的 hot cue：8 个打击垫，每个垫子用途固定（名字、颜色都固定），换哪首歌手感都一样；
- ``fill_hot_cues``：按规范给每首填 hot cue / 主 Cue / 保存 loop（打碟谱自动生成 set 时调用）；
- ``check_hot_cues``：按规范检查（编译时调用）；
- ``cue_sheet_md``：导出给 Prime 4+ 照着设 cue 的表（Markdown）。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .setfile import HotCue, SavedLoop, SetFile

if TYPE_CHECKING:
    from .compiler import Compiled, TrackInfo

SPEC_VERSION = "1"


@dataclass(frozen=True)
class Pad:
    pad: int
    name: str  # 显示在 Prime 4+ 屏幕上（ASCII）
    color: str  # #RRGGBB；在 Engine DJ 里选最接近的颜色
    color_name: str
    role: str  # 用途


# 8 个打击垫的用途。顺序 = 一首歌在 set 里走过的顺序：开头 → 进 → 第一轮 break / drop → 第二轮 → 出 → 交完
PADS: tuple[Pad, ...] = (
    Pad(1, "START", "#FFFFFF", "白", "第 1 小节（曲子最早的小节线），回到开头用"),
    Pad(2, "MIX IN", "#22D45A", "绿", "进点：这首在 set 里从这里进（开场曲 = 起播小节，其它 = 过渡里 B 的进点 b_in）。主 Cue 点也在这里"),
    Pad(3, "BREAK 1", "#2F7BFF", "蓝", "DROP 1 前面最近的 breakdown（进 drop 前的那段）"),
    Pad(4, "DROP 1", "#FF8C1A", "橙", "进点之后第 1 个 drop"),
    Pad(5, "BREAK 2", "#2F7BFF", "蓝", "DROP 1 和 DROP 2 之间最后的 breakdown"),
    Pad(6, "DROP 2", "#FF8C1A", "橙", "进点之后第 2 个 drop"),
    Pad(7, "MIX OUT", "#FF3B3B", "红", "出点：下一段过渡从这里开始（过渡的 start）；终曲 = 淡出开始"),
    Pad(8, "END", "#A45CFF", "紫", "交完：A 在这里停或推子到底；自己播完的 = 曲末；终曲 = 淡出结束"),
)
PAD_BY_NAME = {p.name: p for p in PADS}
# 打碟谱里可以直接写的颜色名（Engine DJ 里选最接近的那个）。前 6 个就是上面打击垫用的颜色
COLOR_NAMES: dict[str, str] = {
    "白": "#FFFFFF", "绿": "#22D45A", "蓝": "#2F7BFF", "橙": "#FF8C1A", "红": "#FF3B3B", "紫": "#A45CFF",
    "黄": "#FFD60A", "青": "#00C8FF", "粉": "#FF5FA2",
}
NAME_MAX = 16  # Prime 4+ 屏幕上 hot cue 名字的长度上限（按 16 个 ASCII 字符算）
LOOP_COLOR = "#00C8FF"


# ---------------------------------------------------------------- 结构点（breakdown / drop）

def _kind(label: str) -> str | None:
    t = label.lower()
    if "drop" in t:
        return "drop"
    if "break" in t:
        return "break"
    return None


def structure_points(cues: dict[str, float], sections: list[dict[str, Any]] | None) -> dict[str, list[tuple[float, str]]]:
    """breakdown / drop 的起点（小节，来源说明）：谱上写的优先（set 的 cues），没有就用分析切出的段落。"""
    out: dict[str, list[tuple[float, str]]] = {"break": [], "drop": []}
    for label, bar in cues.items():
        k = _kind(label)
        if k:
            name = re.sub(r" #\d+$", "", label)  # 同名的第 2、3 个带了「 #2」「 #3」
            out[k].append((float(bar), f"谱上的 {name}"))
    for k, sec_label in (("break", "breakdown"), ("drop", "drop")):
        if not out[k]:
            for s in sections or []:
                if s.get("label") == sec_label:
                    out[k].append((float(s["start"]), f"分析切出的 {sec_label}"))
    for k in out:
        out[k].sort()
    return out


# ---------------------------------------------------------------- 自动填

def _cue(pad: str, bar: float, note: str) -> HotCue:
    p = PAD_BY_NAME[pad]
    return HotCue(pad=p.pad, name=p.name, bar=round(max(1.0, bar), 4), color=p.color, note=note)


def fill_hot_cues(s: SetFile, c: Compiled, sections: dict[str, list[dict[str, Any]]] | None = None) -> SetFile:
    """按规范给每首填 hot cue、主 Cue 和保存 loop（已经写了的同号打击垫保留）。c = 这套 set 编译的结果。"""
    sections = sections or {}
    n = len(s.tracks)
    tracks = []
    for i, st in enumerate(s.tracks):
        info: TrackInfo | None = c.tracks[i] if i < len(c.tracks) else None
        last = info.grid.last_bar if info and info.grid.duration else None
        cues: list[HotCue] = [_cue("START", 1.0, "曲子第 1 小节")]
        # 进点
        if i == 0:
            mix_in = st.start
            cues.append(_cue("MIX IN", mix_in, f"开场：从第 {mix_in:g} 小节起播"))
        else:
            tr = s.transitions[i - 1]
            mix_in = tr.b_in
            prog = c.programs[i - 1] if i - 1 < len(c.programs) else None
            play = (prog["play"] or 0) / 4 if prog else (tr.play_at or 0)
            label = tr.label or {"longhand": "长交接", "instant": "瞬时切换", "custom": "自定义"}[tr.technique]
            cues.append(_cue("MIX IN", mix_in, f"第 {i} 段过渡（{label}）：时间线第 {play:g} 小节 B 从这里起播"))
        # 出点和交完
        mix_out = end = None
        out_note = end_note = ""
        if i < n - 1:
            tr = s.transitions[i]
            prog = c.programs[i] if i < len(c.programs) else None
            mix_out = tr.start
            nxt = s.tracks[i + 1]
            out_note = f"第 {i + 1} 段过渡从这里开始，交给 #{i + 2} {nxt.title or nxt.id}"
            if prog:
                stop = next((b for b, ev in prog.get("events") or [] if ev == "A.stop"), None)
                if stop is not None:
                    end, end_note = tr.start + stop / 4, "A 在这里停"
                elif prog.get("playout"):
                    end, end_note = last, "A 自己播到曲尾"
                else:
                    end, end_note = tr.start + prog["len"] / 4, "过渡结束：A 推子到底"
        elif c.ending and info:
            mix_out = info.grid.beat_bar(c.ending["a"]["start"])
            end = mix_out + c.ending["len"] / 4
            out_note, end_note = "终曲：从这里开始淡出", "终曲淡出结束"
        if mix_out is not None:
            cues.append(_cue("MIX OUT", mix_out, out_note))
        if end is not None:
            cues.append(_cue("END", math.floor(end * 4) / 4, end_note))
        # 进点到交完之间，以 drop 为准：DROP 1 = 第一个 drop，BREAK 1 = 它前面最近的 breakdown（进 drop 前的那段）；
        # DROP 2 = 下一个 drop，BREAK 2 = 两个 drop 之间最后的 breakdown。没有 drop 时 BREAK 1 / 2 = 前两个 breakdown
        pts = structure_points(st.cues, sections.get(st.id))
        hi = end if end is not None else (last or 1e9)
        # 手写过的 BREAK / DROP 垫子占了的位置不再自动放（谱上只写了 BREAK 2 / DROP 2 时，不在同一处再补 BREAK 1 / DROP 1）
        taken = {round(h.bar, 3) for h in st.hot_cues if _kind(h.name)}
        drops = [p for p in pts["drop"] if mix_in <= p[0] < hi and round(p[0], 3) not in taken]
        breaks = [p for p in pts["break"] if mix_in <= p[0] < hi and round(p[0], 3) not in taken]
        picks: dict[str, tuple[float, str]] = {}
        lo = mix_in - 1e-6
        for k, (dname, bname) in enumerate((("DROP 1", "BREAK 1"), ("DROP 2", "BREAK 2"))):
            if k < len(drops):
                d = drops[k]
                picks[dname] = d
                before = [b for b in breaks if lo < b[0] < d[0]]
                if before:
                    picks[bname] = before[-1]
                lo = d[0]
            else:
                after = [b for b in breaks if lo < b[0] and b not in picks.values()]
                if after:
                    picks[bname] = after[0]
                    lo = after[0][0]
        for name in ("BREAK 1", "DROP 1", "BREAK 2", "DROP 2"):
            if name in picks:
                b, why = picks[name]
                cues.append(_cue(name, b, f"{why}（第 {b:g} 小节）"))
        keep = {h.pad: h for h in st.hot_cues}
        merged = {h.pad: h for h in cues}
        merged.update(keep)  # 手写过的打击垫不覆盖
        tracks.append(st.model_copy(update={"hot_cues": sorted(merged.values(), key=lambda h: h.pad),
                                            "main_cue": st.main_cue if st.main_cue is not None else mix_in}))
    # 保存 loop：过渡事件里的 loop
    loops: list[list[SavedLoop]] = [list(t.loops) for t in tracks]
    for i, tr in enumerate(s.transitions):
        prog = c.programs[i] if i < len(c.programs) else None
        play = (prog["play"] or 0) / 4 if prog else (tr.play_at or 0)
        for bar, ev in tr.events:
            m = re.match(r"^(A|B)\.loop:(\d+(?:\.\d+)?)$", str(ev))
            if not m:
                continue
            k = i if m.group(1) == "A" else i + 1
            at = tr.start + float(bar) if m.group(1) == "A" else tr.b_in + float(bar) - play
            if len(loops[k]) < 8:
                beats = float(m.group(2))
                loops[k].append(SavedLoop(pad=len(loops[k]) + 1, name=f"LOOP {beats:g}", bar=round(max(1.0, at), 4),
                                          beats=beats, note=f"第 {i + 1} 段过渡里的 {beats:g} 拍 loop"))
    tracks = [t.model_copy(update={"loops": lp}) for t, lp in zip(tracks, loops)]
    return s.model_copy(update={"tracks": tracks, "spec": s.spec or SPEC_VERSION})


# ---------------------------------------------------------------- 检查

def check_hot_cues(s: SetFile, infos: list[TrackInfo | None]) -> list[tuple[str, str, str]]:
    """(级别, 位置, 说明)。位置超出曲子 = error；按规范生成的 set（spec = 1）打击垫用途不对 = warn。"""
    out: list[tuple[str, str, str]] = []
    for i, (st, info) in enumerate(zip(s.tracks, infos)):
        where = f"#{i + 1} {st.title or st.id} 的 hot cue"
        last = info.grid.last_bar if info and info.grid.duration else None
        for h in [*st.hot_cues, *st.loops]:
            if last is not None and h.bar > last + 0.01:
                out.append(("error", where, f"打击垫 {h.pad}「{h.name}」在第 {h.bar:g} 小节，曲子只有 {last:.1f} 小节"))
        if s.spec:
            for h in st.hot_cues:
                p = PADS[h.pad - 1]
                if h.name != p.name:
                    out.append(("warn", where, f"打击垫 {h.pad} 按规范是「{p.name}」（{p.role}），这里是「{h.name}」"))
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
             f"按 DJ Studio 播放规范 v{s.spec or SPEC_VERSION}（docs/SET_SPEC.md）生成。"
             "位置写三种：本应用的小节.拍、谱（Engine DJ）上的小节.拍、离文件开头的时间（分:秒.毫秒）。"
             "在 Engine DJ 里把播放头放到这个时间，按对应的打击垫存 cue，再改名字和颜色。", "",
             "| 打击垫 | 名字 | 颜色 | 用途 |", "|---|---|---|---|"]
    for p in PADS:
        lines.append(f"| {p.pad} | {p.name} | {p.color_name} {p.color} | {p.role} |")
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
