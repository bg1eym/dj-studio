"""曲谱 v5 的 hot cue：每份素材在这套谱里要用到的点（Denon Prime 4+ 的打击垫 1–8）。

规矩和 docs/SET_SPEC.md 第 7 节是同一套（名字、颜色、放不下时先留谁都用 setspec 里的那一份）：**打击垫没有固定用途**，
放的是这份素材在这套谱里要用到的点，按它们在原曲上的先后从 1 号垫排。v5 没有「过渡」，点从片段来：

- IN：片段从原曲这里起播；
- 旋钮 / 推子开始拧（或一下到位）的地方、效果开 / 关的地方；
- OUT：主线上的交接从这里开始（下一首进来的那一拍，这一首读到的地方；主线 / 交接见 mainline.py）；
- LOOP n：圈头（n = 圈长的拍数）；JUMP n：跳到的那一小节；
- STOP：片段在原曲这里停（在开着的 loop 里停的不另放，写在 LOOP 的注释里）。

位置 = 原曲上的小节（素材自己的 1.1 起），落在整拍上。同一份素材被几个片段用到（一句人声放两次）：几个片段的点合在一起。
素材上已经有的垫子（旧 set 带过来的、以后谱上手写的）一律不动，自动的点只放进空着的垫子。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from fractions import Fraction
from typing import Any

from ..compiler import NEUTRAL
from ..setfile import HotCue
from ..setspec import COLOR_NAMES, CUE_KINDS, KIND, NAME_MAX, PAD_COUNT, PARAM_TEXT, _prio, fmt_time, knob_text, lane_moves, move_name
from .model import PARAMS
from .pos import BEATS_PER_BAR, fmt_dur, fmt_pos

# 效果在打击垫上的名字（英文，Prime 4+ 屏幕上显示）。发送效果关掉时再放一个「… OFF」；现场效果只标开始
FX_NAME = {"delay": "DELAY", "echo": "ECHO", "reverb": "REVERB", "trans": "TRANS", "roll": "ROLL", "filter": "FILTER FX",
           "filter_echo": "FLT ECHO", "spiral": "SPIRAL", "noise": "NOISE", "flanger": "FLANGER", "phaser": "PHASER",
           "release": "RELEASE"}
SAME_BAR = 1 / 8  # 差不到 1/8 小节（半拍）算同一个位置：只占一个垫子
# 同一个位置上有好几个点、要紧的程度又一样时留谁：要按的垫子（从这里起播、跳到这里、圈头）排在只是记个位置的（停）前面
_ORDER = {"in": 0, "jump": 1, "loop": 2, "out": 3, "stop": 4, "fade": 5, "fx": 6, "move": 7}


@dataclass(frozen=True)
class Point:
    bar: float  # 原曲的小节（素材自己的 1.1 = 1.0；整拍）
    kind: str  # setspec.CUE_KINDS 的 key
    name: str
    note: str
    clip: str
    at: float  # 全场第几拍（第一次用到）


def _pos(b: float) -> str:
    return "开演前" if b < 0 else fmt_pos(Fraction(b).limit_denominator(96))


def clip_points(c: Any, cc: Any) -> list[Point]:
    """一个片段在它那份素材上要用到的点（c = Compiled5，cc = ClipC）。"""
    k = cc.clip

    def bar_at(g: float) -> float:  # 全场第 g 拍时读到原曲第几小节（落在整拍上）
        return max(1.0, round(cc.src_beat_at(g)) / BEATS_PER_BAR + 1)

    who = f"{k.id}（D{k.deck}{' ' + k.role if k.role else ''}）"
    init = "、".join(f"{PARAM_TEXT[p]} {knob_text(p, cc.value_at(p, cc.start))}" for p in ("vol", "lo", "mid", "hi", "filter")
                    if p == "vol" or abs(cc.value_at(p, cc.start) - NEUTRAL[p]) > 1e-6)
    pts = [Point(bar_at(cc.start), "in", "IN", f"{who} 在全场 {_pos(cc.start)} 从这里起播；起播时 {init}", k.id, cc.start)]

    # 旋钮 / 推子开始动的地方、效果开关：同一拍的合成一个点
    ev: dict[float, dict[str, list[Any]]] = {}

    def at(g: float) -> dict[str, list[Any]]:
        return ev.setdefault(round(g, 4), {"fx": [], "moves": [], "notes": []})

    lanes = {f"A.{p}": [[x / BEATS_PER_BAR, v, "step"] if rest else [x / BEATS_PER_BAR, v] for x, v, *rest in cc.lanes.get(p, [])]
             for p in PARAMS}
    for t, ms in lane_moves(lanes, "A").items():
        g = t * BEATS_PER_BAR
        if cc.start + 1e-6 < g < cc.stop - 1e-6:  # 起播那一拍的值是「起播时摆成这样」，写在 IN 里
            at(g)["moves"] += ms
    for f in c.fx:
        if f["clip"] != k.id or f["kind"] not in FX_NAME:
            continue
        name = FX_NAME[f["kind"]]
        if cc.start - 1e-6 <= f["start"] < cc.stop:
            e = at(max(cc.start, f["start"]))
            e["fx"].append(name)
            e["notes"].append(f"开 {f['id']} {name.title()}（发送）")
        if cc.start < f["end"] < cc.stop - 1e-6:
            e = at(f["end"])
            e["fx"].append(f"{name} OFF")
            e["notes"].append(f"关 {f['id']} {name.title()} 的送出，尾音留着")
    for lv in c.live:
        if k.id not in lv.get("clips", []) or lv["kind"] not in FX_NAME:
            continue
        if cc.start - 1e-6 <= lv["start"] < cc.stop:
            e = at(max(cc.start, lv["start"]))
            e["fx"].append(FX_NAME[lv["kind"]])
            e["notes"].append(f"{lv['id']} {lv['name']}：{lv.get('text', '')}".rstrip("："))
    for g, e in sorted(ev.items()):
        tokens = list(dict.fromkeys(e["fx"]))
        kind = "fx" if tokens else "move"
        if e["moves"]:
            mname, mkind = move_name(e["moves"])
            tokens.append(mname)
            kind = kind if kind == "fx" else mkind
        name = ""
        for t in tokens:
            cand = f"{name} {t}".strip()
            if len(cand) <= NAME_MAX:
                name = cand
        if g <= cc.start + 1e-6:  # 起播那一拍就开着的效果：不另占垫子，写进 IN 的注释
            pts[0] = replace(pts[0], note=pts[0].note + "；" + "；".join(e["notes"]))
            continue
        note = f"{k.id} 全场 {_pos(g)}：" + "；".join(e["notes"] + [m.text() for m in e["moves"]])
        pts.append(Point(bar_at(g), kind, name or "MIX", note, k.id, g))

    # 主要的交接：这一首交出去的那一段从哪开始（OUT）；接别人进来的，写在 IN 的注释里
    from .mainline import handovers

    for h in handovers(c):
        if h["to"] == k.id:
            pts[0] = replace(pts[0], note=pts[0].note + f"；交接 {h['n']}：接 {h['from']}"
                             + (f"，全场 {_pos(h['swap'])} 换低频" if h["swap"] is not None else ""))
        if h["from"] == k.id and cc.start + 1e-6 < h["start"] < cc.stop - 1e-6:
            note = (f"交接 {h['n']}：{h['to']}（D{h['to_deck']}）在全场 {_pos(h['start'])} 进来，{k.id} 从这里开始交出去"
                    + (f"；全场 {_pos(h['swap'])} 换低频" if h["swap"] is not None else "") + f"；全场 {_pos(h['end'])} 交完")
            pts.append(Point(bar_at(h["start"]), "out", "OUT", note, k.id, h["start"]))

    # Loop、跳段、停
    segs = cc.segs
    for i, s in enumerate(segs):
        g0 = float(s.g)
        g1 = float(segs[i + 1].g) if i + 1 < len(segs) else cc.stop
        if s.kind == "loop" and s.loop:
            laps = (g1 - g0) / float(s.loop)
            note = (f"{k.id} 全场 {_pos(g0)} 打 {fmt_dur(s.loop)} 的 loop（{s.id}），转 {laps:g} 圈到 {_pos(g1)}"
                    + ("，在圈里停" if g1 >= cc.stop - 1e-6 else "，然后出圈接着放"))
            pts.append(Point(max(1.0, round(float(s.lsrc)) / BEATS_PER_BAR + 1), "loop", f"LOOP {float(s.loop):g}", note, k.id, g0))
        elif s.kind == "jump" and g0 > cc.start + 1e-6:
            bar = max(1.0, round(float(s.src)) / BEATS_PER_BAR + 1)
            was = _pos(cc.src_beat_at(g0 - 1e-6))
            pts.append(Point(bar, "jump", f"JUMP {int(bar)}", f"{k.id} 全场 {_pos(g0)} 跳到这里；放到原曲 {was} 时按这个垫", k.id, g0))
    playing = [s for s in segs if float(s.g) < cc.stop - 1e-9]
    last = playing[-1] if playing else segs[0]
    if not last.loop:
        end = float(last.src) + (cc.stop - float(last.g))
        pts.append(Point(max(1.0, round(end) / BEATS_PER_BAR + 1), "stop", "STOP", f"{k.id} 在全场 {_pos(cc.stop)} 放到这里停", k.id, cc.stop))
    return pts


def asset_cues(c: Any) -> dict[str, list[HotCue]]:
    """每份素材的打击垫（素材 id → 按垫号排好的 hot cue）。"""
    by_asset: dict[str, list[Point]] = {}
    for cc in c.clips:
        by_asset.setdefault(cc.clip.asset, []).extend(clip_points(c, cc))
    out: dict[str, list[HotCue]] = {}
    for a in c.project.assets:
        given = list(a.hot_cues)  # 旧 set 带过来的 / 手写的：不动
        taken = {h.pad for h in given}
        free = [p for p in range(1, PAD_COUNT + 1) if p not in taken]
        # 不浪费垫子：已经有垫子标了这个位置（不管叫什么）、或者有同名的点（差不到 1 小节；IN 不论在哪），自动的就不放
        pts = [p for p in by_asset.get(a.id, [])
               if not any(abs(h.bar - p.bar) < SAME_BAR or (h.name.upper() == p.name and (abs(h.bar - p.bar) <= 1 or p.kind == "in"))
                          for h in given)]
        # 同一个位置只占一个垫子：先留要紧的（IN / STOP / 跳段落点 / 圈头 > 效果、换低频 > 别的旋钮），同级留先用到的
        merged: list[Point] = []
        for p in sorted(pts, key=lambda q: (_prio(q), _ORDER[q.kind], q.at, q.bar)):
            same = next((m for m in merged if abs(m.bar - p.bar) < SAME_BAR), None)
            if same is None:
                merged.append(p)
            elif p.name != same.name or p.clip != same.clip:
                merged[merged.index(same)] = replace(same, note=f"{same.note}；也是 {p.name}：{p.note}")
        pts = sorted(merged, key=lambda q: (q.bar, q.at))
        if len(pts) > len(free):
            keep = sorted(range(len(pts)), key=lambda i: (_prio(pts[i]), pts[i].bar))[:len(free)]
            pts = [pts[i] for i in sorted(keep)]
        auto = [HotCue(pad=pad, name=p.name, bar=round(p.bar, 4), color=KIND[p.kind].color, note=p.note, auto=True)
                for pad, p in zip(free, pts)]
        out[a.id] = sorted(given + auto, key=lambda h: h.pad)
    return out


def cue_sheet_md(c: Any) -> str:
    """每份素材一张表：打击垫、名字、颜色、位置（原曲的小节.拍、离文件开头的时间）、注释。照着在 Engine DJ 或 Prime 4+ 上设。"""
    p = c.project
    cues = asset_cues(c)
    info = {cc.clip.asset: cc.info for cc in c.clips}
    lines = [f"# {p.name or p.id} — Hot Cue 表（Denon Prime 4+）", "",
             "四 Deck 的谱（曲谱 v5）。打击垫没有固定用途：每份素材放的是它在这套谱里要用到的点，按在原曲上的先后从 1 号垫排"
             "（起播 → 旋钮 / 效果开始动的地方、loop、跳段 → 停）；素材上本来就有的垫子照原样。规矩见 docs/SET_SPEC.md 第 7 节。"
             "位置写两种：原曲的小节.拍（素材自己的 1.1 起）、离文件开头的时间（分:秒.毫秒）。"
             "在 Engine DJ 里把播放头放到这个时间，按对应的打击垫存 cue，再改名字和颜色。", "",
             "| 名字 | 颜色 | 什么时候放 |", "|---|---|---|"]
    v5_when = {"in": "片段从原曲这里起播。主 Cue 也在这里", "out": "主线上的交接从这里开始（下一首进来的那一拍）",
               "move": "这份素材的旋钮 / 推子开始拧，或一下拧到位",
               "fx": f"开 / 关效果（{' / '.join(sorted(set(FX_NAME.values())))}，发送效果关掉时是「… OFF」）",
               "jump": "跳段：跳到的那一小节", "loop": "打 loop：圈头（数字 = 圈长的拍数）", "stop": "片段在原曲这里停"}
    for kd in CUE_KINDS:
        if kd.key in v5_when:
            lines.append(f"| {kd.names} | {kd.color_name} {kd.color} | {v5_when[kd.key]} |")
    for a in p.assets:
        inf = info.get(a.id)
        used = [cc.clip.id for cc in c.clips if cc.clip.asset == a.id]
        lines += ["", f"## {a.id} {a.title or a.file or a.id}", "",
                  (f"原 BPM {inf.grid.bpm:.2f}" if inf else "原 BPM ?") + (f"；文件 {a.file}" if a.file else "")
                  + (f"；片段 {'、'.join(used)}" if used else "；这套谱没有用到"), ""]
        hs = cues.get(a.id, [])
        main = next((h for h in hs if h.name.upper() == "IN"), None)
        if main and inf:
            lines += [f"主 Cue（CUE 键）：原曲第 {_bar_txt(main.bar)} 小节，{fmt_time(inf.beat_time((main.bar - 1) * BEATS_PER_BAR))}", ""]
        if not hs:
            lines += ["（没有要放的点）"]
            continue
        lines += ["| 打击垫 | 名字 | 颜色 | 小节.拍 | 时间 | 注释 |", "|---|---|---|---|---|---|"]
        for h in hs:
            t = fmt_time(inf.beat_time((h.bar - 1) * BEATS_PER_BAR)) if inf else "?"
            cname = next((n for n, v in COLOR_NAMES.items() if v == h.color), "")
            lines.append(f"| {h.pad} | {h.name} | {cname} {h.color} | {_bar_txt(h.bar)} | {t} | {h.note} |")
    return "\n".join(lines) + "\n"


def _bar_txt(bar: float) -> str:
    return fmt_pos(Fraction((bar - 1) * BEATS_PER_BAR).limit_denominator(96))
