"""《CDW 技法实战 set 打碟谱》模板的解析（确定性规则，不用 AI）。

模板（v1，ReportLab 生成，A4 横版）：
- 第 2 页「曲目总表」：# / 曲目 / 原 BPM / 播放 / Key Lock / 小节 / 进场于上一首第…小节 / 过渡类型
- 每段过渡两页：页眉右上「过渡 N」；标题「过渡 N A → B」；一行「长交接 ｜ … ｜ B 的第 1 小节落在 A 的第 X 小节
  （B 小节 = A 小节 – K） ｜ 主速 135 → 136（…）」；动作清单（①②…，左右两栏）；「特效：A：Main FX Reverb，
  第 a–b 小节，Wet/Dry 1/3」；逐小节旋钮表（A 小节 / B 小节 / A·B 的 LOW MID HIGH Filter 推子 / 动作）
- 「Engine DJ 准备清单」：文件名、第 1 小节第 1 拍（秒）、BPM、Key Lock、其它 Hot Cue（breakdown / drop 小节）

旋钮写钟点位：12 点 = 平，7 点 = 全切，5 点 = 满；Filter「左 N 点」= 低通，「右 N 点」= 高通，「关」= 12 点；
推子 0–10。这里统一换成「离 12 点几个钟头」：左负右正，-5..+5；推子保持 0–10。单位换算在 convert.py。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .pdftext import Cell, PdfError, join, lines, read_cells, table

TITLE_MARK = "打碟谱"

TRACK_COLS = ["#", "曲目", "蓝本里的位置", "原 BPM", "播放", "pitch", "Key Lock", "小节", "进场于上一首第", "过渡类型"]
PREP_COLS = ["#", "曲目", "文件名", "第 1 小节第 1", "BPM 原→播", "Key Lock", "Hot Cue 1", "其它 Hot Cue", "备用 8 小节", "调性"]
ROW_COLS = ["A 小节", "B 小节", "A LOW", "A MID", "A HIGH", "A Filter", "A 推子",
            "B LOW", "B MID", "B HIGH", "B Filter", "B 推子", "动作"]
TECH = {"长交接": "longhand", "瞬时切换": "instant"}


class PlanError(ValueError):
    pass


@dataclass
class Knobs:
    lo: float  # 离 12 点的钟头数，左负右正（-5 = 7 点 = 全切，+5 = 5 点 = 满）
    mid: float
    hi: float
    filter: float  # 同上：负 = 低通（左），正 = 高通（右），0 = 关
    vol: float  # 推子 0–10


@dataclass
class PlanRow:
    a_bar: int
    b_bar: int | None
    end: bool  # A 在这一小节结束（「146 结束」）
    a: Knobs
    b: Knobs | None  # B 还没起播时为 None（「—」）
    marks: str = ""


@dataclass
class PlanEffect:
    deck: str  # "A" / "B"
    kind: str  # "reverb" / "echo"
    bar0: int  # 谱上该 deck 自己的小节号
    bar1: int
    wet: float  # 0..1


@dataclass
class PlanTransition:
    no: int
    a_name: str
    b_name: str
    technique: str  # longhand / instant
    b1_at_a: int  # B 的第 1 小节落在 A 的第几小节
    tempo_from: float
    tempo_to: float
    ramp_b: tuple[int, int] | None  # 换速在 B 的第 m–n 小节
    rows: list[PlanRow] = field(default_factory=list)
    effects: list[PlanEffect] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    header: str = ""


@dataclass
class PlanTrack:
    no: int
    title: str
    bpm_orig: float
    bpm_play: float
    keylock: bool
    bars: int | None
    entry_in_prev: int | None
    technique: str | None
    file_name: str = ""
    bar1_s: float | None = None  # 第 1 小节第 1 拍在文件里的秒数
    cues: list[tuple[int, str]] = field(default_factory=list)  # (小节, breakdown / break / drop …)
    key: str = ""


@dataclass
class Plan:
    title: str
    tracks: list[PlanTrack]
    transitions: list[PlanTransition]
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- 取值

_CLOCK = re.compile(r"^(左|右)?\s*(\d{1,2})\s*点(半)?(\(切\)|（切）)?$")


def clock(text: str, filt: bool = False) -> float:
    """钟点位 → 离 12 点的钟头数（左负右正）。「关」= 0。"""
    t = text.strip().replace(" ", "")
    if t in ("关", "12点"):
        return 0.0
    m = _CLOCK.match(t)
    if not m:
        raise PlanError(f"看不懂的旋钮位「{text}」")
    side, h, half = m.group(1), int(m.group(2)), m.group(3)
    v = h + (0.5 if half else 0.0)
    if not 1 <= h <= 12 or 5 < v < 7:  # 表盘上没有 13 点；6 点在旋钮行程（7 点–5 点）之外
        raise PlanError(f"旋钮位「{text}」不在 7 点–5 点之间")
    if side == "左" or (side is None and v >= 6.5 and v < 12):
        off = v - 12  # 7 点 → -5，11 点半 → -0.5
    elif side == "右" or (side is None and v <= 5.5):
        off = v % 12  # 1 点 → +1，5 点 → +5
    else:
        off = 0.0 if v == 12 else v - 12
    if not -5 <= off <= 5:
        raise PlanError(f"旋钮位「{text}」超出 7 点–5 点")
    return off


def fader(text: str) -> float:
    t = text.strip()
    try:
        v = float(t)
    except ValueError as e:
        raise PlanError(f"看不懂的推子位「{text}」") from e
    if not 0 <= v <= 10:
        raise PlanError(f"推子位「{text}」超出 0–10")
    return v


def _num(text: str) -> float | None:
    m = re.search(r"-?\d+(\.\d+)?", text or "")
    return float(m.group(0)) if m else None


# ---------------------------------------------------------------- 解析

def parse(path: Path) -> Plan:
    pages = read_cells(path)
    title = " ".join(c.text for c in pages[0] if c.size >= 20)[:80] or path.stem
    if TITLE_MARK not in "".join(c.text for c in pages[0]):
        raise PlanError("这不是「打碟谱」模板（第 1 页没有「打碟谱」字样）")
    tracks = _tracks(pages)
    _prep(pages, tracks)
    trans = _transitions(pages)
    plan = Plan(title=title, tracks=tracks, transitions=trans)
    _check(plan)
    return plan


def _tracks(pages: list[list[Cell]]) -> list[PlanTrack]:
    for cells in pages[:6]:
        t = table(cells, TRACK_COLS, "#", row_ok=lambda s: bool(re.fullmatch(r"\d{1,2}", s)))
        if t and t.rows:
            out = []
            for r in t.rows:
                kl = r.get("Key Lock", "").strip()
                out.append(PlanTrack(
                    no=int(r["#"]), title=r.get("曲目", ""),
                    bpm_orig=float(_num(r.get("原 BPM", "")) or 0), bpm_play=float(_num(r.get("播放", "")) or 0),
                    keylock=kl.startswith("开"), bars=int(_num(r.get("小节", "")) or 0) or None,
                    entry_in_prev=int(_num(r.get("进场于上一首第", "")) or 0) or None,
                    technique=TECH.get(r.get("过渡类型", "").strip())))
            return out
    raise PlanError("没找到曲目总表（# / 曲目 / 原 BPM / 播放 / … / 过渡类型）")


def _prep(pages: list[list[Cell]], tracks: list[PlanTrack]) -> None:
    by_no = {t.no: t for t in tracks}
    for cells in pages:
        t = table(cells, PREP_COLS, "#", row_ok=lambda s: bool(re.fullmatch(r"\d{1,2}", s)))
        if not t:
            continue
        for r in t.rows:
            tr = by_no.get(int(r["#"]))
            if not tr:
                continue
            tr.file_name = r.get("文件名", "").strip()
            m = re.match(r"\s*(\d+(?:\.\d+)?)\s*s", r.get("第 1 小节第 1", ""))
            tr.bar1_s = float(m.group(1)) if m else None
            tr.key = r.get("调性", "").strip()
            for part in re.split(r"[；;]", r.get("其它 Hot Cue", "")):
                m = re.search(r"第\s*(\d+)\s*小节\s*(.*)", part)
                if m:
                    tr.cues.append((int(m.group(1)), m.group(2).strip()))


def _page_label(cells: list[Cell]) -> str:
    """页眉右上角的标签（「过渡 3」「总览」「Engine DJ 清单」…）。"""
    top = [c for c in cells if c.y > max(cc.y for cc in cells) - 2 and c.x > 400]
    return top[0].text if top else ""


def _transitions(pages: list[list[Cell]]) -> list[PlanTransition]:
    groups: dict[int, list[list[Cell]]] = {}
    for cells in pages:
        m = re.fullmatch(r"过渡\s*(\d+)", _page_label(cells))
        if m:
            groups.setdefault(int(m.group(1)), []).append(cells)
    out = []
    for no in sorted(groups):
        out.append(_transition(no, groups[no]))
    if not out:
        raise PlanError("没找到任何「过渡 N」页")
    return out


_HEAD = re.compile(r"B\s*的第\s*1\s*小节落在\s*A\s*的第\s*(\d+)\s*小节（B\s*小节\s*=\s*A\s*小节\s*[–\-—]\s*(\d+)）")
_TEMPO = re.compile(r"主速\s*(\d+(?:\.\d+)?)(?:\s*→\s*(\d+(?:\.\d+)?))?")
_RAMP = re.compile(r"B\s*(?:独奏)?第\s*(\d+)\s*[–\-]\s*(\d+)\s*小节.{0,40}?主速\s*(\d+(?:\.\d+)?)\s*→\s*(\d+(?:\.\d+)?)")
_FX = re.compile(r"(A|B)：Main FX (Reverb|Echo)[^，,]*[，,]\s*第\s*(\d+)\s*[–\-]\s*(\d+)\s*小节[，,]\s*Wet/Dry\s*(\d+)\s*/\s*(\d+)")


def _transition(no: int, pages: list[list[Cell]]) -> PlanTransition:
    allcells = [c for p in pages for c in p]
    # 标题：最大字号的那一行「过渡 N A → B」
    title_c = max((c for c in allcells if c.text.startswith(f"过渡 {no} ") and "→" in c.text and "动作" not in c.text),
                  key=lambda c: c.size, default=None)
    if not title_c:
        raise PlanError(f"过渡 {no}：没找到标题「过渡 {no} A → B」")
    names = title_c.text.split(" ", 2)[2]
    a_name, _, b_name = names.partition("→")
    # 页眉段落：标题下面、正文起点 x 的连续几行
    head = ""
    page0 = next(p for p in pages if title_c in p)
    after = [c for c in page0 if c.y < title_c.y and abs(c.x - title_c.x) < 3]
    for c in after:
        if head and ("｜" not in head + c.text or len(head) > 400):
            break
        head = join(head, c.text)
        if _HEAD.search(head) and _TEMPO.search(head) and not head.rstrip().endswith("｜"):
            # 主速后面可能还有一截（「…（在 B 独奏后）｜ 真实叠放…」），拼到下一个「｜」为止已经够用
            pass
    tech_word = head.split("｜")[0].strip()
    technique = TECH.get(tech_word)
    if not technique:
        raise PlanError(f"过渡 {no}：页眉里没有「长交接」/「瞬时切换」：{head[:40]}…")
    mh = _HEAD.search(head)
    if not mh:
        raise PlanError(f"过渡 {no}：页眉里没有「B 的第 1 小节落在 A 的第 X 小节（B 小节 = A 小节 – K）」")
    b1_at_a, const = int(mh.group(1)), int(mh.group(2))
    if b1_at_a - const != 1:
        raise PlanError(f"过渡 {no}：页眉自相矛盾：B1 在 A{b1_at_a}，却写 B = A – {const}")
    mt = _TEMPO.search(head)
    if not mt:
        raise PlanError(f"过渡 {no}：页眉里没有「主速」")
    t_from = float(mt.group(1))
    t_to = float(mt.group(2) or mt.group(1))

    # 动作清单：①②… 左右两栏，按栏拼续行
    acts = _actions(pages)
    ramp = None
    for a in acts:
        m = _RAMP.search(a)
        if m:
            ramp = (int(m.group(1)), int(m.group(2)))
            if float(m.group(3)) != t_from or float(m.group(4)) != t_to:
                raise PlanError(f"过渡 {no}：动作里的换速 {m.group(3)}→{m.group(4)} 与页眉的主速 {t_from:g}→{t_to:g} 不一致")
    if t_to != t_from and ramp is None:
        raise PlanError(f"过渡 {no}：页眉写了主速 {t_from:g} → {t_to:g}，动作清单里没找到「B 第 m–n 小节把主速 …」")

    # 特效：在「为什么这么做 … 特效：…」段落里（可能折行）
    para = ""
    for p in pages:
        for ln in lines(p):
            t = " ".join(c.text for c in ln)
            if t.startswith("为什么这么做") or (para and not t.startswith("A 小节") and abs(ln[0].x - 51) < 3 and len(ln) == 1):
                para = join(para, t)
            elif para and t.startswith("A 小节"):
                break
    effects = []
    for m in _FX.finditer(para):
        effects.append(PlanEffect(m.group(1), m.group(2).lower(), int(m.group(3)), int(m.group(4)),
                                  int(m.group(5)) / int(m.group(6))))

    rows: list[PlanRow] = []
    for p in pages:
        t = table(p, ROW_COLS, "A 小节", stop=["读法"], row_ok=lambda s: bool(re.match(r"^\d+", s)))
        if not t:
            continue
        for r in t.rows:
            rows.append(_row(no, r))
    if not rows:
        raise PlanError(f"过渡 {no}：没找到逐小节旋钮表")
    return PlanTransition(no, a_name.strip(), b_name.strip(), technique, b1_at_a, t_from, t_to, ramp,
                          rows, effects, acts, head)


def _actions(pages: list[list[Cell]]) -> list[str]:
    items: list[str] = []
    for p in pages:
        cols: dict[float, list[str]] = {}
        started = False
        for c in p:
            if c.text.startswith("为什么这么做") or c.text.startswith("A 小节"):
                break
            if re.match(r"^[①-⑳]", c.text):
                started = True
            if not started:
                continue
            col = cols.setdefault(round(c.x / 50) * 50.0, [])
            if re.match(r"^[①-⑳]", c.text):
                col.append(c.text)
            elif col:
                col[-1] = join(col[-1], c.text)
        for col in cols.values():
            items.extend(col)
    items.sort(key=lambda s: s[0])
    return items


def _row(no: int, r: dict[str, str]) -> PlanRow:
    a_txt = r["A 小节"].strip()
    m = re.match(r"^(\d+)\s*(结束)?", a_txt)
    if not m:
        raise PlanError(f"过渡 {no}：看不懂的 A 小节「{a_txt}」")
    try:
        a = Knobs(clock(r["A LOW"]), clock(r["A MID"]), clock(r["A HIGH"]), clock(r["A Filter"], True), fader(r["A 推子"]))
        b_bar_t = r.get("B 小节", "").strip()
        if b_bar_t in ("—", "-", ""):
            b, b_bar = None, None
        else:
            b_bar = int(b_bar_t)
            b = Knobs(clock(r["B LOW"]), clock(r["B MID"]), clock(r["B HIGH"]), clock(r["B Filter"], True),
                      fader(r["B 推子"]))
    except KeyError as e:
        raise PlanError(f"过渡 {no} 第 {a_txt} 行缺一列：{e}") from e
    except PlanError as e:
        raise PlanError(f"过渡 {no} 第 {a_txt} 行：{e}") from e
    return PlanRow(int(m.group(1)), b_bar, bool(m.group(2)), a, b, r.get("动作", "").strip())


def _check(plan: Plan) -> None:
    n = len(plan.tracks)
    if [t.no for t in plan.tracks] != list(range(1, n + 1)):
        raise PlanError(f"曲目编号不连续：{[t.no for t in plan.tracks]}")
    if [t.no for t in plan.transitions] != list(range(1, n)):
        raise PlanError(f"{n} 首应有 {n - 1} 段过渡，谱上是 {[t.no for t in plan.transitions]}")
    for tr in plan.transitions:
        prev = None
        for r in tr.rows:
            if prev and r.a_bar <= prev.a_bar:
                raise PlanError(f"过渡 {tr.no}：A 小节没有递增（{prev.a_bar} → {r.a_bar}）")
            if r.b_bar is not None and r.b_bar != r.a_bar - (tr.b1_at_a - 1):
                raise PlanError(f"过渡 {tr.no}：第 {r.a_bar} 行 B 小节 {r.b_bar} 与页眉「B = A – {tr.b1_at_a - 1}」对不上")
            prev = r
        if not tr.rows[-1].end:
            plan.warnings.append(f"过渡 {tr.no}：最后一行没有标「结束」，按 A 在这里结束处理")
        nxt = plan.tracks[tr.no]
        if nxt.entry_in_prev and nxt.entry_in_prev != tr.b1_at_a:
            raise PlanError(f"过渡 {tr.no}：总表写 #{nxt.no} 进场于上一首第 {nxt.entry_in_prev} 小节，过渡页写 {tr.b1_at_a}")
        if nxt.technique and nxt.technique != tr.technique:
            raise PlanError(f"过渡 {tr.no}：总表的过渡类型与过渡页不一致")
        if abs(plan.tracks[tr.no - 1].bpm_play - tr.tempo_from) > 0.01:
            plan.warnings.append(f"过渡 {tr.no}：主速 {tr.tempo_from:g} 与 #{tr.no} 的播放 BPM {plan.tracks[tr.no - 1].bpm_play:g} 不同")
        if abs(nxt.bpm_play - tr.tempo_to) > 0.01:
            plan.warnings.append(f"过渡 {tr.no}：换速后 {tr.tempo_to:g} 与 #{nxt.no} 的播放 BPM {nxt.bpm_play:g} 不同")


__all__ = ["Plan", "PlanError", "PlanRow", "PlanTrack", "PlanTransition", "PlanEffect", "Knobs", "PdfError",
           "parse", "clock", "fader"]
