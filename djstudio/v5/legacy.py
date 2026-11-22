"""旧谱适配器：v1–v4 的 set（djset/2，一串「第 k 首 → 第 k+1 首」的过渡）→ 项目（djset/3，全场时间线）。

旧规则只在这里保留——v4 里「时间线从过渡表第一行开始」「第一行之前旋钮就是第一行的位置」「B 起播前先摆好旋钮」
「效果开到 A 结束就不关」这些隐含的约定，在这里都换成显式的全场位置、片段和曲线。转出来的项目和手写的 v5 谱走同一个
编译器、同一个调度器。原来的谱 / set 不动；「另存为 v5」= 把转出来的项目写成一个新的 .md（writer.py）。

保留的东西：曲序、每首从哪起播到哪停、EQ / Filter / 推子的每个点（含跳变）、主速（含换速）、固定的 Echo / Reverb 参数、
Hot Cue、过渡里的 loop、独奏时的 loop 和跳段、结尾淡出。Deck 还是 1、2 交替（和旧引擎一样），所以图上「A 出曲、B 进曲」
对应的物理 Deck 不变。

check() 把转出来的计划和旧编译器的结果逐点比（过渡里每一拍每条曲线的原生值、B 起播 / A 停的拍、loop、跳段、主速），
`djstudio v5check <谱或 set>` 用它核对自己的旧谱。
"""
from __future__ import annotations

import math
from fractions import Fraction
from typing import Any

from ..compiler import Compiled, LoopSpan, TrackInfo, deck_for, own_offset, play_bpm
from ..grid import BEATS_PER_BAR
from ..setfile import SetFile
from ..setspec import lane_value
from .compile import AssetInfo, Compiled5
from .model import PARAM_NEUTRAL, PARAMS, Project

FX_OFF = 0.001  # 旧引擎：效果湿度 ≤ 这个数就把效果从这个通道上关掉
TAIL_SECONDS = 3.0  # 旧引擎：过渡结束 3 秒后出场 deck 归位（效果尾巴留这么久）


def _F(x: float) -> Fraction:
    """小节 / 拍（浮点，实际都是 1/4、1/8 这样的数）→ 有理数。"""
    return Fraction(x).limit_denominator(960)


def _beats(bars: float) -> Fraction:
    return _F(bars) * BEATS_PER_BAR


def fader_pos_for_gain(g: float) -> float:
    """推子增益 → 推子位置（compiler.fader_gain 的反函数）。旧的结尾淡出是增益直线下降，新曲线按推子位置写。"""
    if g <= 0:
        return 0.0
    if g >= 1:
        return 1.0
    off = 10 ** (-20 / 20.0)
    return (20.0 * math.log10(g * (1 - off) + off) + 20.0) / 20.0


def asset_infos(c: Compiled, paths: dict[str, str] | None = None, gains: dict[str, float] | None = None
                ) -> dict[str, AssetInfo]:
    """旧编译结果里每首的网格 → 新编译器要的素材信息（素材 ID = 曲库里的 track id）。"""
    out: dict[str, AssetInfo] = {}
    for t, info in zip(c.set.tracks, c.tracks):
        if info is not None and _aid(t.id) not in out:
            out[_aid(t.id)] = AssetInfo(_aid(t.id), info.grid, path=(paths or {}).get(t.id), gain=(gains or {}).get(t.id),
                                        title=t.title or info.title, track_id=t.id)
    return out


def _aid(track_id: str) -> str:
    """曲库的 track id → 素材 ID（字母开头、只有字母数字下划线横线）。"""
    s = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in track_id)[:22]
    return s if s[:1].isalpha() else "T" + s


def from_set(s: SetFile, c: Compiled) -> Project:
    """c = 旧编译器对 s 的编译结果（要没有错误）。"""
    if not c.ok:
        raise ValueError("set 校验没通过，转不了：" + "；".join(str(i) for i in c.issues if i.level == "error"))
    n = len(s.tracks)
    infos: list[TrackInfo] = c.tracks  # type: ignore[assignment]
    assets: dict[str, dict[str, Any]] = {}
    for t, info in zip(s.tracks, infos):
        a = assets.setdefault(_aid(t.id), {
            "id": _aid(t.id), "title": t.title or info.title, "artist": t.artist, "file": t.file or "",
            "bpm": round(info.grid.bpm, 4), "bar1_s": round(info.grid.bar_time(1), 4),
            "bars": max(1, int(info.grid.last_bar - 1)) if info.grid.duration else None, "track_id": t.id, "hot_cues": []})
        if t.hot_cues and not a["hot_cues"]:
            a["hot_cues"] = [h.model_dump() for h in t.hot_cues]

    clips: list[dict[str, Any]] = []
    lanes: dict[tuple[str, str], list[dict[str, Any]]] = {}
    cur: dict[tuple[str, str], float] = {}  # 每个旋钮现在的值（接着上一段往下写）
    loops: list[dict[str, Any]] = []
    jumps: list[dict[str, Any]] = []
    fx: list[dict[str, Any]] = []
    scenes: list[dict[str, Any]] = []
    marks: list[dict[str, Any]] = []
    trans: list[dict[str, Any]] = []
    tempo: list[dict[str, Any]] = [{"at": Fraction(0), "bpm": play_bpm(s.tracks[0], infos[0]), "ramp": False}]

    def cid(k: int) -> str:
        return f"C{k + 1}"

    def put(clip: str, param: str, at: Fraction, value: float, step: bool) -> None:
        pts = lanes.setdefault((clip, param), [])
        value = round(float(value), 6)
        if step and abs(cur.get((clip, param), PARAM_NEUTRAL[param]) - value) < 1e-9:
            step = False  # 跳到现在的位置 = 没动：留一个锚点（后面的斜线从这里开始，不是从更早的点）
        if pts and pts[-1]["at"] == at and not step and abs(pts[-1]["value"] - value) < 1e-9:
            return
        pts.append({"at": at, "value": value, "step": step})
        cur[(clip, param)] = value

    def set_tempo(at: Fraction, bpm: float, ramp: bool) -> None:
        last = tempo[-1]
        if abs(last["bpm"] - bpm) < 1e-9:
            if at > last["at"]:  # 速度没变：留一个锚点（后面的渐变从这里开始，不是从更早的点）
                tempo.append({"at": at, "bpm": last["bpm"], "ramp": False})
            return
        if ramp and at <= last["at"]:
            ramp = False
        tempo.append({"at": at, "bpm": round(bpm, 4), "ramp": ramp})

    def solo(k: int, g0: Fraction, own0: float, own1: float) -> Fraction:
        """第 k 首从自己的第 own0 小节独奏到第 own1 小节（全场从 g0 拍起）：把跳段 / loop 换成全场位置，返回独奏结束的全场拍。"""
        g, own = g0, own0
        for m in sorted(s.tracks[k].moves, key=lambda m: m.bar):
            if m.bar < own - 1e-6 or m.bar > own1 - 1e-6:
                continue  # 起播以前的（截短的 set）不做，和旧引擎一样
            at = g + _beats(m.bar - own)
            if m.to is not None:
                jumps.append({"id": f"{cid(k)}.J{len(jumps) + 1}", "clip": cid(k), "at": at, "to": _beats(m.to - 1), "note": m.note})
                g, own = at, m.to
            else:
                until = at + _beats(m.loop) * m.times
                loops.append({"id": f"{cid(k)}.L{len(loops) + 1}", "clip": cid(k), "at": at, "length": _beats(m.loop),
                              "until": until, "note": m.note})
                g, own = until, m.bar + m.loop
        return g + _beats(own1 - own)

    first = s.tracks[0]
    clips.append({"id": cid(0), "asset": _aid(first.id), "deck": deck_for(0), "src": _beats(first.start - 1),
                  "start": Fraction(0), "stop": None, "role": "主曲", "init": {}, "keylock": s.keylock})
    g_solo, own = Fraction(0), first.start  # 这一首独奏从全场哪一拍、自己的第几小节开始

    for i, tr in enumerate(s.transitions):
        ex = c.expanded[i]
        spans: dict[str, list[LoopSpan]] = c.loops[i]
        a, b = cid(i), cid(i + 1)
        G = solo(i, g_solo, own, tr.start)  # 时间线第 0 小节 = 全场第 G 拍

        def at(t: float, G: Fraction = G) -> Fraction:
            return G + _beats(t)

        # ---- B：起播、起播时的旋钮（旧引擎装好 B 就先摆到起播那一刻的位置）
        init: dict[str, float] = {}
        for prm in PARAMS:
            pts = ex.lanes.get(f"B.{prm}")
            before = 0.0 if prm == "vol" else PARAM_NEUTRAL[prm]
            init[prm] = round(lane_value(pts, ex.play_at, before), 6) if pts else PARAM_NEUTRAL[prm]
            cur[(b, prm)] = init[prm]
        clips.append({"id": b, "asset": _aid(s.tracks[i + 1].id), "deck": deck_for(i + 1), "src": _beats(tr.b_in - 1),
                      "start": at(ex.play_at), "stop": None, "role": "进曲", "init": init, "keylock": s.keylock})
        # ---- 曲线。A：过渡一开始旋钮就是曲线在第 0 小节的值；之后每个点照搬（斜线还是斜线，跳变还是跳变）
        for name, pts in ex.lanes.items():
            side, prm = name.split(".")
            if prm not in PARAMS:
                continue
            clip = a if side == "A" else b
            t0 = 0.0 if side == "A" else ex.play_at
            before = 0.0 if name == "B.vol" else PARAM_NEUTRAL[prm]
            if side == "A":
                put(clip, prm, at(0.0), lane_value(pts, 0.0, before), True)
            for p in pts:
                t = float(p[0])
                if t < t0 - 1e-9 or (t <= t0 + 1e-9 and side == "B"):
                    continue  # B 起播以前的点已经算进起播时的位置里了
                step = len(p) == 3 and p[2] == "step"
                put(clip, prm, at(t), float(p[1]), step)
        # ---- 效果：湿度 > 0 的每一段是一个实例；开到过渡结束还没关的 = 尾巴留 3 秒（旧引擎 3 秒后把出场 deck 归位）
        a_stop_t = next((float(e[0]) for e in ex.events if str(e[1]) == "A.stop"), None)
        if a_stop_t is None and not ex.a_playout:
            a_stop_t = ex.bars
        for name, pts in ex.lanes.items():
            side, kind = name.split(".")
            if kind not in ("echo", "reverb"):
                continue
            clip = a if side == "A" else b
            fx += _fx_windows(f"F{i + 1}{side}{kind[0].upper()}", kind, clip, pts, ex.bars, at,
                              keep_to=None if side == "B" else a_stop_t, bpm=float(ex.tempo[-1][1]) if ex.tempo else 120.0)
        # ---- loop（过渡里）
        for side, clip in (("A", a), ("B", b)):
            for sp in spans.get(side, []):
                end_t = sp.t_off if sp.t_off is not None else (a_stop_t if a_stop_t is not None else ex.bars)
                loops.append({"id": f"{clip}.L{len(loops) + 1}", "clip": clip, "at": at(sp.t_on), "length": _beats(sp.bars),
                              "until": at(end_t), "note": ""})
        # ---- A 停：写了「停」/ 时间线到头就停；「结束」= 自己播完（不写停止，编译器按曲尾算）
        if a_stop_t is not None:
            clips[i]["stop"] = at(a_stop_t)
        # ---- 主速：过渡一开始就是曲线在第 0 小节的值
        tp = ex.tempo or [[0, play_bpm(s.tracks[i], infos[i])]]
        set_tempo(at(0.0), float(tp[0][1]), False)
        for p in tp:
            set_tempo(at(float(p[0])), float(p[1]), not (len(p) == 3 and p[2] == "step"))
        end = at(ex.bars)
        scenes.append({"no": i + 1, "name": tr.label or {"longhand": "长交接", "instant": "瞬时切换"}.get(tr.technique, "过渡"),
                       "start": at(0.0), "end": end, "purpose": tr.note, "draft": False,
                       "roles": {f"D{deck_for(i)}": "出曲", f"D{deck_for(i + 1)}": "进曲"}})
        for t, text in tr.marks:
            marks.append({"at": at(float(t)), "clip": "", "text": str(text)})
        trans.append({"no": i + 1, "a": a, "b": b, "start": float(at(0.0)), "play": float(at(ex.play_at)),
                      "end": float(end), "a_bar": tr.start, "b_bar": tr.b_in, "label": scenes[-1]["name"]})
        # B 接着独奏：过渡结束时它在自己的第几小节
        g_solo, own = end, tr.b_in + own_offset(spans.get("B", []), ex.bars) - ex.play_at

    # ---- 最后一首：淡出（旧引擎是增益直线降到 0）或者播完
    last = n - 1
    lb = play_bpm(s.tracks[last], infos[last])
    if s.ending.mode == "fade":
        e = s.ending
        start_bar = e.bar if e.bar is not None else math.floor(infos[last].grid.last_bar - e.bars)
        g0 = solo(last, g_solo, own, start_bar)
        L = int(round(e.bars * BEATS_PER_BAR))
        put(cid(last), "vol", g0, 1.0, True)  # 旧的结尾程序从满推子开始
        for k in range(1, L + 1):
            put(cid(last), "vol", g0 + k, fader_pos_for_gain(1.0 - k / L), False)
        clips[last]["stop"] = g0 + L
        set_tempo(g0, lb, False)
    else:
        solo(last, g_solo, own, infos[last].grid.last_bar)

    data = {
        "schema": "djset/3", "spec": "5", "id": s.id, "name": s.name or s.id, "source": s.source, "note": s.note,
        "tempo": tempo, "assets": list(assets.values()), "clips": clips,
        "lanes": [{"clip": cl, "param": prm, "points": pts} for (cl, prm), pts in lanes.items() if pts],
        "loops": loops, "jumps": jumps, "fx": fx, "scenes": scenes, "marks": marks,
        "legacy": {"schema": s.schema_, "set": s.id, "spec": s.spec, "transitions": trans,
                   "tracks": [{"clip": cid(k), "track": t.id, "bar_offset": t.plan_bar_offset} for k, t in enumerate(s.tracks)]},
    }
    return Project.model_validate(data)


def _fx_windows(prefix: str, kind: str, clip: str, pts: list[list[Any]], bars: float, at: Any, keep_to: float | None,
                bpm: float) -> list[dict[str, Any]]:
    """一条 echo / reverb 湿度曲线 → 发送效果。湿度 > 0 的每一段一个实例；返回量 = 原来的湿度曲线，送出一直开着
    （固定的送出增益：Echo -3 dB、Reverb 满，和旧的效果单元一样）。段尾湿度回到 0 = 旧引擎把效果关了，尾巴不留。"""
    xs = sorted({0.0, float(bars)} | {float(p[0]) for p in pts})
    out: list[dict[str, Any]] = []
    k = 0
    while k < len(xs):
        # 找一段湿度 > 0 的：从第一个「之后有值」的点到回到 0 的点
        if lane_value(pts, xs[k], 0.0) <= FX_OFF and not (k + 1 < len(xs) and lane_value(pts, (xs[k] + xs[k + 1]) / 2, 0.0) > FX_OFF):
            k += 1
            continue
        t0 = xs[k]
        j = k + 1
        while j < len(xs) and (lane_value(pts, xs[j], 0.0) > FX_OFF or
                               (j + 1 < len(xs) and lane_value(pts, (xs[j] + xs[j + 1]) / 2, 0.0) > FX_OFF)):
            j += 1
        open_end = j >= len(xs)  # 开到时间线到头还没关
        t1 = xs[min(j, len(xs) - 1)]
        if open_end and keep_to is not None:
            t1 = max(t1, keep_to)
        if t1 > t0:
            wet = []
            for p in pts:
                t = float(p[0])
                if t0 - 1e-9 <= t <= t1 + 1e-9:
                    wet.append({"at": at(t), "value": round(float(p[1]), 4), "step": len(p) == 3 and p[2] == "step"})
            if not wet or wet[0]["at"] > at(t0):
                wet.insert(0, {"at": at(t0), "value": round(lane_value(pts, t0, 0.0), 4), "step": True})
            while len(wet) >= 2 and wet[0]["at"] == wet[1]["at"] and wet[1]["step"]:
                wet.pop(0)  # 开的那一拍「0 → 跳到湿度」：只留跳到的那个值
            wet[0]["step"] = True
            tail = _F(TAIL_SECONDS * bpm / 60.0).limit_denominator(4) if open_end else Fraction(1, 4)
            out.append({"id": f"{prefix}{len(out) + 1}", "kind": kind, "route": "send", "targets": [clip], "start": at(t0),
                        "end": at(t1), "params": {}, "send": [], "wet": wet, "tail": max(Fraction(1, 4), tail)})
        k = j + 1
    return out


# ---------------------------------------------------------------- 等价检查

def _interp(pts: list[list[float]], x: float) -> float:
    """和 DJStudio.interp 一样：分段线性，同一拍两个点 = 阶跃（取后一个）。"""
    if x < pts[0][0]:
        return pts[0][1]
    i = len(pts) - 1
    while i > 0 and pts[i][0] > x:
        i -= 1
    if i == len(pts) - 1:
        return pts[i][1]
    (a, va), (b, vb) = pts[i][:2], pts[i + 1][:2]
    return va + (vb - va) * (x - a) / (b - a) if b > a else vb


def check(old: Compiled, new: Compiled5, tol: float = 2e-4) -> list[str]:
    """转换前后逐点比，返回不一样的地方（空 = 等价）：
    每段过渡里 B 起播的拍和原曲位置、A 停的拍、每条旋钮曲线每 1/4 拍的原生值、主速、过渡里的 loop；
    独奏时的跳段 / loop；最后一首的淡出；整套的长度。"""
    diffs: list[str] = []
    if not new.ok or new.plan is None:
        return ["新计划没编过：" + "；".join(str(i) for i in new.errors)]
    plan = {k["id"]: k for k in new.plan["clips"]}
    trans = (new.project.legacy or {}).get("transitions", [])
    tempo = [(b, v) for b, v in new.plan["tempo"]]

    def bpm_at(g: float) -> float:
        return new.tempo.bpm_at(g)

    def curve(clip: dict[str, Any], prm: str, g: float) -> float:
        pts = clip["curves"].get(prm)
        return _interp(pts, g) if pts else clip["init"][prm]

    progs = list(old.programs) + ([old.ending] if old.ending else [])
    for i, prog in enumerate(progs):
        ending = prog.get("b") is None
        if ending:
            k = len(old.set.tracks) - 1
            a = plan[f"C{k + 1}"]
            G = a["stop"] - prog["len"]
            b = None
            where = "结尾淡出"
        else:
            a, b = plan[f"C{i + 1}"], plan[f"C{i + 2}"]
            G = trans[i]["start"]
            where = f"过渡 {i + 1}"
            if abs(b["start"] - (G + prog["play"])) > 1e-6:
                diffs.append(f"{where}：B 起播在全场第 {b['start']:g} 拍，旧的是 {G + prog['play']:g}")
            want = prog["b"]["grid"][0] / 1000.0 + prog["b"]["in"] * 60.0 / prog["b"]["grid"][1]
            if abs(b["segs"][0]["src"] - want) > 1e-3:
                diffs.append(f"{where}：B 从原曲 {b['segs'][0]['src']:.3f}s 起播，旧的是 {want:.3f}s")
        stops = [x for x, ev in prog["events"] if ev == "A.stop"]
        if stops and abs(a["stop"] - (G + stops[0])) > 1e-6:
            diffs.append(f"{where}：A 在全场第 {a['stop']:g} 拍停，旧的是 {G + stops[0]:g}")
        if not stops and not prog.get("playout") and abs(a["stop"] - (G + prog["len"])) > 1e-6:
            diffs.append(f"{where}：A 在全场第 {a['stop']:g} 拍停，旧的是 {G + prog['len']:g}（时间线到头）")
        n = int(round(prog["len"] * 4))
        a_end = stops[0] if stops else prog["len"]
        for name, pts in prog["lanes"].items():
            side, prm = name.split(".")
            if prm not in PARAMS:
                continue
            clip = a if side == "A" else b
            t0 = 0.0 if side == "A" else prog["play"]
            worst, where_t = 0.0, 0.0
            for q in range(n + 1):
                t = q / 4.0 + 0.125  # 每 1/4 拍取一个点，错开半个 1/8 拍：不落在跳变的那一拍上
                if t < t0 or t > prog["len"] or (side == "A" and t >= a_end) or (clip and G + t >= clip["stop"]):
                    continue
                d = abs(curve(clip, prm, G + t) - _interp(pts, t))
                if d > worst:
                    worst, where_t = d, t
            if worst > tol:
                diffs.append(f"{where}：{name} 在时间线第 {where_t:g} 拍差了 {worst:.5f}（原生值）")
        if not ending:
            # B 起播时的旋钮（旧引擎装好 B 就摆好）
            for prm in ("lo", "mid", "hi", "filter"):
                pts = prog["lanes"].get(f"B.{prm}")
                want = _interp(pts, prog["play"]) if pts else (0.5 if prm == "filter" else 1.0)
                if abs(b["init"][prm] - want) > tol:
                    diffs.append(f"{where}：B 起播时 {prm} = {b['init'][prm]:g}，旧的是 {want:g}")
            for q in range(n + 1):
                t = q / 4.0 + 0.125
                if t <= prog["len"] and prog.get("bpm"):
                    if abs(bpm_at(G + t) - _interp(prog["bpm"], t)) > 1e-3:
                        diffs.append(f"{where}：主速在时间线第 {t:g} 拍是 {bpm_at(G + t):g}，旧的是 {_interp(prog['bpm'], t):g}")
                        break
            # 过渡里的 loop：开的拍和长度；出的拍（旧引擎提前半遍关，新计划写的是出的那一拍）
            for x, ev in prog["events"]:
                side, _, act = ev.partition(".")
                clip = a if side == "A" else b
                if act.startswith("loop:"):
                    L = float(act[5:])
                    if not any(sg["loop"] == L and abs(sg["g"] - (G + x)) < 1e-6 for sg in clip["segs"]):
                        diffs.append(f"{where}：{side} 第 {x:g} 拍开的 {L:g} 拍 loop 没对上")
    # 独奏时的跳段 / loop：旧计划按这首自己的拍，新的按全场拍——比「跳多少拍 / loop 多长、转到哪一拍出」
    for k, acts in enumerate(old.plans):
        clip = plan.get(f"C{k + 1}")
        info = old.tracks[k]
        if clip is None or info is None:
            continue
        for act in acts:
            src_at = info.grid.beat_time(act["at"])  # 这一拍在原曲的秒数
            if "jump" in act:
                to = info.grid.beat_time(act["at"] + act["jump"])
                if not any(abs(sg["src"] - to) < 2e-3 for sg in clip["segs"][1:]):
                    diffs.append(f"第 {k + 1} 首：第 {info.grid.beat_bar(act['at']):g} 小节的跳段没对上")
            else:
                hit = [sg for sg in clip["segs"] if sg["loop"] == act["loop"] and abs(sg.get("lsrc", sg["src"]) - src_at) < 2e-3]
                if not hit:
                    diffs.append(f"第 {k + 1} 首：第 {info.grid.beat_bar(act['at']):g} 小节的 loop 没对上")
                    continue
                out = next((sg for sg in clip["segs"] if sg["g"] > hit[0]["g"]), None)
                if out is None or abs(out["g"] - hit[0]["g"] - act["loop"] * act["times"]) > 1e-6:
                    diffs.append(f"第 {k + 1} 首：第 {info.grid.beat_bar(act['at']):g} 小节的 loop 转的遍数没对上")
    # 整套的长度：旧时间表在过渡里按两首的平均 BPM 估，新的按速度图积分——差在 1% 以内
    if old.duration > 0 and abs(new.duration - old.duration) > max(2.0, 0.01 * old.duration):
        diffs.append(f"整套长度 {new.duration:.1f}s，旧的时间表是 {old.duration:.1f}s")
    del tempo
    return diffs
