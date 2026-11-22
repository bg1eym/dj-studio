"""项目（djset/3）→ 曲谱 v5 的 Markdown。「另存为 v5」用：v1–v4 的旧谱转成新写法以后写成一个新文件（不覆盖原来的谱）。

写出来的谱再读回去是同一个项目（tests/test_v5_score.py 的往返测试）。场景里放策略说明、Loop 与跳段、效果；
旋钮曲线按片段写在场景后面的「## C1 参数」里（一条曲线可能跨好几个场景，放在一张表里起点最清楚）。
值尽量写成 Prime 4+ 面板上的说法（钟点位、推子 0–10）；对不上钟点位的写 dB / 百分比。
"""
from __future__ import annotations

from fractions import Fraction

from ..plan.convert import eq_db
from .model import PARAM_NEUTRAL, Fx, Project
from .pos import fmt_dur, fmt_pos

ZH_KIND = {"delay": "Delay", "echo": "Echo", "reverb": "Reverb", "trans": "Trans", "roll": "Roll",
           "filter_echo": "Filter Echo", "spiral": "Spiral", "noise": "Noise", "flanger": "Flanger", "phaser": "Phaser",
           "filter": "Filter", "sample": "采样", "release": "Release"}
ZH_ROUTE = {"send": "发送", "insert": "插入", "group-insert": "组插入", "master-insert": "总输出"}
ZH_FX_KEY = {"beats": "拍长", "feedback": "反馈", "pingpong": "乒乓", "decay": "衰减", "bandwidth": "带宽", "damping": "阻尼"}
ZH_INTENT = {"multi_low": "多低频", "multi_vocal": "两条声线", "multi_mid": "中频叠加"}
COLS = (("lo", "LOW"), ("mid", "MID"), ("hi", "HIGH"), ("filter", "Filter"), ("vol", "推子"))


class NotExpressible(ValueError):
    """项目里有 v5 的表格写不出来的东西（比如任意形状的送出曲线）。"""


def _clock(h: float) -> str:
    whole = int(h)
    return f"{whole}点半" if h - whole else f"{whole}点"


def knob_write(param: str, v: float) -> str:
    """值 → 谱上的写法，读回去（score.knob_value）还是同一个值。"""
    if param == "vol":
        return f"{round(v * 10, 4):g}"
    if param == "filter":
        h = v * 5
        if abs(h * 2 - round(h * 2)) < 1e-6:
            h = round(h * 2) / 2
            return "关" if h == 0 else (f"左{_clock(12 + h)}" if h < 0 else f"右{_clock(h)}")
        return f"{'左' if v < 0 else '右'}{round(abs(v) * 100, 2):g}%"
    for half in range(-10, 11):
        h = half / 2
        if abs(eq_db(h) - v) < 1e-6:
            return "12点" if h == 0 else _clock(12 + h if h < 0 else h)
    return f"{v:+g}dB"


def _amount(v: float) -> str:
    f = Fraction(v).limit_denominator(12)
    if abs(float(f) - v) < 1e-4 and f.denominator > 1:
        return f"{f.numerator}/{f.denominator}"
    return f"{round(v, 4):g}"


def _beats(v: float) -> str:
    return fmt_dur(Fraction(v).limit_denominator(96))


def _table(head: list[str], rows: list[list[str]]) -> list[str]:
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return out + [""]


def _fx_params(f: Fx) -> str:
    parts = []
    for k, v in f.params.items():
        if k in ("legacy", "send_gain"):
            continue
        parts.append(f"{ZH_FX_KEY.get(k, k)} {_beats(float(v)) if k == 'beats' else (_amount(float(v)) if isinstance(v, (int, float)) else v)}")
    for name, raw in (("送出", f.send), ("返回", f.wet)):
        if not raw:
            continue
        # 值没变、后面也不再变的点不用写（保持到底）；中间的「停一段再变」要留着
        pts = [x for i, x in enumerate(raw) if i == 0 or abs(x.value - raw[i - 1].value) > 1e-9
               or (i + 1 < len(raw) and abs(raw[i + 1].value - x.value) > 1e-9)]
        if pts[0].at != f.start:
            raise NotExpressible(f"效果 {f.id} 的{name}曲线不是从效果开始那一拍写起的")
        cells = []
        for i, x in enumerate(pts):
            at = "" if i == 0 or (i == len(pts) - 1 and x.at == f.end) else f"@{fmt_pos(x.at)}"
            cells.append(("跳 " if x.step and i else "") + _amount(x.value) + at)
        parts.append(f"{name} {' → '.join(cells)}")
    if f.tail is not None:
        parts.append(f"尾音 {fmt_dur(f.tail)}")
    return "；".join(parts)


def write(p: Project) -> str:
    out = [f"# {p.name or p.id}", "", f"- 规范：v{p.spec}", f"- 主速：{p.tempo[0].bpm:g}", ""]
    if p.note:
        out += [f"> {p.note}", ""]
    out += ["## 素材", ""]
    out += _table(["ID", "曲名", "文件名", "原 BPM", "第 1 小节（秒）", "小节数", "调性"],
                  [[a.id, a.title + (f" — {a.artist}" if a.artist else ""), a.file, f"{a.bpm:g}",
                    "" if a.bar1_s is None else f"{a.bar1_s:.3f}", "" if a.bars is None else str(a.bars), a.key or "—"]
                   for a in p.assets])
    if len(p.tempo) > 1:
        out += ["## 速度", ""]
        out += _table(["全场位置", "BPM", "变化"],
                      [[fmt_pos(t.at), f"{t.bpm:g}", "渐变" if t.ramp else ("" if i == 0 else "跳变")]
                       for i, t in enumerate(p.tempo)])
    out += ["## 片段", ""]
    rows = []
    for c in p.clips:
        rows.append([c.id, c.asset, f"D{c.deck}", fmt_pos(c.src), fmt_pos(c.start), "播完" if c.stop is None else fmt_pos(c.stop),
                     c.role or "—", knob_write("vol", c.init.vol), knob_write("lo", c.init.lo), knob_write("mid", c.init.mid),
                     knob_write("hi", c.init.hi), knob_write("filter", c.init.filter), "开" if c.keylock else "关",
                     "" if c.prep is None else fmt_dur(c.prep), c.note])
    out += _table(["ID", "素材", "Deck", "原曲起点", "全场起播", "全场停止", "角色", "推子", "LOW", "MID", "HIGH", "Filter",
                   "Key Lock", "准备", "备注"], rows)

    def moves(lo: Fraction | None, hi: Fraction | None) -> list[str]:
        items = [(x.at, x) for x in p.loops + p.jumps if (lo is None and scene_free(x.at)) or (lo is not None and lo <= x.at < hi)]
        if not items:
            return []
        rows = []
        for at, x in sorted(items, key=lambda e: (e[0], e[1].clip)):
            if hasattr(x, "to"):
                what = f"跳到 {fmt_pos(x.to)}"
            else:
                what = ("slip " if x.slip else "") + f"loop {fmt_dur(x.length)}"
                if x.src is not None:
                    what += f" 从原曲 {fmt_pos(x.src)}"
                if x.same_start:
                    what += " 同起点"
                what += f"，到 {fmt_pos(x.until)} 出"
            rows.append([x.clip, fmt_pos(at), what, x.note])
        return ["### Loop 与跳段" if lo is not None else "## Loop 与跳段", ""] + _table(["片段", "位置", "做什么", "说明"], rows)

    def effects(lo: Fraction | None, hi: Fraction | None) -> list[str]:
        items = [f for f in p.fx if (lo is None and scene_free(f.start)) or (lo is not None and lo <= f.start < hi)]
        if not items:
            return []
        rows = [[f.id, ZH_KIND.get(f.kind, f.kind), ZH_ROUTE[f.route], "+".join(f.targets) or "总输出", fmt_pos(f.start),
                 fmt_pos(f.end), _fx_params(f), f.note] for f in sorted(items, key=lambda f: (f.start, f.id))]
        return ["### 效果" if lo is not None else "## 效果", ""] + _table(
            ["ID", "类型", "路由", "目标", "开始", "结束", "参数", "说明"], rows)

    def scene_free(at: Fraction) -> bool:
        return not any(s.start <= at < s.end for s in p.scenes)

    for s in sorted(p.scenes, key=lambda s: s.start):
        out += [f"## 场景 {s.no}：{s.name}（{fmt_pos(s.start)} – {fmt_pos(s.end)}）", ""]
        for zh, v in (("作用", s.purpose), ("保留", s.keep), ("前景", s.lead), ("LOW", s.low), ("MID/HIGH", s.midhigh)):
            if v:
                out.append(f"- {zh}：{v}")
        if s.roles:
            out.append("- 角色：" + "，".join(f"{d} {r}" for d, r in sorted(s.roles.items())))
        if s.draft:
            out.append("- 状态：草稿")
        if s.intent:
            out.append("- 有意：" + " / ".join(ZH_INTENT.get(i, i) for i in s.intent))
        if s.note:
            out.append(f"- 说明：{s.note}")
        out.append("")
        out += moves(s.start, s.end) + effects(s.start, s.end)
    out += moves(None, None) + effects(None, None)

    # 旋钮曲线：按片段一张表。一行 = 这个片段在那一拍有点的旋钮；「跳 X」= 一下到位，直接写值 = 从上一行匀速拧到
    for c in p.clips:
        lanes = {ln.param: ln for ln in p.lanes if ln.clip == c.id}
        if not lanes and not any(m.clip == c.id for m in p.marks):
            continue
        cols = [(prm, zh) for prm, zh in COLS if prm in lanes]
        cells: dict[Fraction, dict[str, str]] = {}
        for prm, _ in cols:
            first = True
            for pt in lanes[prm].points:
                row = cells.setdefault(pt.at, {})
                txt = knob_write(prm, pt.value)
                init = getattr(c.init, prm, PARAM_NEUTRAL[prm])
                if pt.step:
                    txt = f"跳 {txt}"
                elif first and abs(pt.value - init) < 1e-9:
                    txt = "保持"  # 第一个点的值就是起播时的位置：只是个起点
                elif first and pt.at > c.start:
                    # 第一个点是斜线 = 从起播那一拍拧过来：起播那一拍补一个「保持」当起点
                    cells.setdefault(c.start, {}).setdefault(prm, "保持")
                elif first:
                    txt = f"跳 {txt}"
                if prm in row:  # 同一拍先拧到、再跳：写成两行
                    cells.setdefault(pt.at + Fraction(0), {})
                    row = cells.setdefault(("dup", pt.at, len(cells)), {})  # type: ignore[arg-type]
                row[prm] = txt
                first = False
        marks = {m.at: m.text for m in p.marks if m.clip == c.id}
        for at in marks:  # 只有说明、没有旋钮动作的那几拍也留一行
            cells.setdefault(at, {})
        keys = sorted(cells, key=lambda k: (k[1], 1) if isinstance(k, tuple) else (k, 0))
        rows = []
        for k in keys:
            at = k[1] if isinstance(k, tuple) else k
            rows.append([fmt_pos(at)] + [cells[k].get(prm, "") for prm, _ in cols] + [marks.pop(at, "") if not isinstance(k, tuple) else ""])
        out += [f"## {c.id} 参数", ""] + _table(["位置"] + [zh for _, zh in cols] + ["说明"], rows)
    return "\n".join(out).rstrip() + "\n"
