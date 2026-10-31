"""Markdown 打碟谱（《打碟谱规范》docs/SCORE_SPEC.md）的解析。确定性规则，不用 AI。

谱是一个 .md 文本文件，结构：

    # 标题
    - 规范：v1 / 开场：… / 结尾：…          （可选）
    ## 曲目          一张表：# / 曲名 / 文件名 / 原 BPM / 播放 BPM / Key Lock / 小节数 / 第 1 小节（秒） / 调性
    ## Hot Cue       每首一个「### #N 曲名」，下面一张表：垫 / 名字 / 颜色 / 位置 / 注释
    ## 过渡 N：A → B  几条「- 手法：…」「- 进场：…」「- 主速：…」「- 效果：…」，再一张逐小节旋钮表

读出来和 PDF 模板（cdw.py）是同一套 Plan 结构，后面的换算（convert.py）完全一样。出错时报谱文件里的行号。
表格的列按表头的名字认（顺序随意，空格和全角 / 半角括号不计较）；表外的文字、「>」引用、<!-- 注释 --> 都当说明，不读。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..setspec import COLOR_NAMES, NAME_MAX, default_color
from .cdw import TECH, CueRef, Knobs, Plan, PlanCue, PlanEffect, PlanError, PlanRow, PlanTrack, PlanTransition, check, clock, fader

SCORE_SPEC_VERSION = "3"
# v1 / v2 的谱写法一样：hot cue 按 v2 放（垫子没有固定用途）；v3 起过渡 / 开场 / 结尾可以引用谱上的 hot cue
ACCEPTED_VERSIONS = ("1", "2", "3")
SUFFIXES = (".md", ".markdown")
DASHES = ("—", "-", "–", "－", "--")


@dataclass
class _Line:
    no: int  # 谱文件里的行号（从 1 起）
    text: str


@dataclass
class _Table:
    line: int
    cols: dict[str, int]  # 规范化后的列名 → 第几格
    rows: list[tuple[int, dict[str, str]]]  # (行号, 列名 → 格子文字)


@dataclass
class _Section:
    title: str
    line: int
    body: list[_Line] = field(default_factory=list)


def _err(line: int, msg: str) -> PlanError:
    return PlanError(f"第 {line} 行：{msg}" if line else msg)


# ---------------------------------------------------------------- 文本 → 行 / 段 / 表

def _key(s: str) -> str:
    """列名、设置项名字规范化：去空格、去 Markdown 加粗、全角括号冒号换半角、小写。"""
    s = s.replace("**", "").replace("`", "").replace("（", "(").replace("）", ")").replace("：", ":")
    return re.sub(r"\s+", "", s).lower()


def _clean(s: str) -> str:
    return s.replace("**", "").replace("`", "").strip()


def _lines(text: str) -> list[_Line]:
    """去掉 <!-- 注释 --> 和 ``` 代码块（保留行号）。"""
    text = re.sub(r"<!--.*?-->", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
    out, fence = [], False
    for i, ln in enumerate(text.splitlines(), 1):
        if ln.lstrip().startswith("```"):
            fence = not fence
            continue
        if not fence:
            out.append(_Line(i, ln.rstrip()))
    return out


def _cells(text: str) -> list[str]:
    t = text.strip()
    if t.startswith("|"):
        t = t[1:]
    if t.endswith("|"):
        t = t[:-1]
    return [_clean(c) for c in t.split("|")]


def _tables(body: list[_Line], aliases: dict[str, str]) -> list[_Table]:
    """一段里所有的表。aliases：规范化的列名 → 规范列名（表头里不认识的列忽略）。"""
    out: list[_Table] = []
    i = 0
    while i < len(body):
        if not body[i].text.lstrip().startswith("|"):
            i += 1
            continue
        block = []
        while i < len(body) and body[i].text.lstrip().startswith("|"):
            block.append(body[i])
            i += 1
        head = block[0]
        if len(block) < 2 or not re.fullmatch(r"[\s|:\-]*-[\s|:\-]*", block[1].text):
            raise _err(head.no + 1, "表格第二行要是分隔行「|---|---|…」")
        names = _cells(head.text)
        cols: dict[str, int] = {}
        for k, name in enumerate(names):
            canon = aliases.get(_key(name))
            if canon and canon not in cols:
                cols[canon] = k
        rows = []
        for ln in block[2:]:
            cells = _cells(ln.text)
            if all(not c for c in cells):
                continue
            if len(cells) != len(names):
                raise _err(ln.no, f"这一行有 {len(cells)} 格，表头有 {len(names)} 列（少了或多了「|」）")
            rows.append((ln.no, {c: cells[k] for c, k in cols.items()}))
        out.append(_Table(head.no, cols, rows))
    return out


def _bullets(body: list[_Line]) -> list[tuple[int, str, str]]:
    """「- 键：值」→ (行号, 规范化的键, 值)；没有冒号的条目键为空。"""
    out = []
    for ln in body:
        m = re.match(r"^\s*[-*+]\s+(.*)$", ln.text)
        if not m:
            continue
        item = _clean(m.group(1))
        kv = re.match(r"^([^：:|]{1,12})[：:]\s*(.*)$", item)
        out.append((ln.no, _key(kv.group(1)), kv.group(2).strip()) if kv else (ln.no, "", item))
    return out


def _alias(table: dict[str, list[str]]) -> dict[str, str]:
    return {_key(a): canon for canon, names in table.items() for a in [canon, *names]}


TRACK_COLS = _alias({
    "#": ["编号", "no"], "曲名": ["曲目", "title"], "文件名": ["文件", "file"], "原BPM": ["原速"],
    "播放BPM": ["播放", "播放速度"], "KeyLock": ["keylock"], "小节数": ["小节", "bars"],
    "第1小节": ["第1小节(秒)", "第1小节第1拍", "第1小节第1拍(秒)", "第1小节(s)"], "调性": ["key"],
})
CUE_COLS = _alias({"垫": ["打击垫", "垫子", "pad"], "名字": ["名称", "name"], "颜色": ["color"],
                   "位置": ["小节.拍", "position", "时间"], "注释": ["说明", "备注", "note"]})
ROW_COLS = _alias({
    "A小节": [], "B小节": [], "动作": ["说明", "备注"],
    **{f"{d}{p}": [f"{d}{p.lower()}"] for d in "AB" for p in ("LOW", "MID", "HIGH", "FILTER")},
    "A推子": ["a音量"], "B推子": ["b音量"],
})


# ---------------------------------------------------------------- 取值

def _number(text: str, line: int, what: str) -> float:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*", text)
    if not m:
        raise _err(line, f"{what}「{text}」应该是一个数")
    return float(m.group(1))


def _seconds(text: str, line: int, what: str) -> float:
    """「12.345」「12.345s」「12.345 秒」或「0:12.345」。"""
    t = text.strip().rstrip("sS秒").strip()
    m = re.fullmatch(r"(\d+):(\d{1,2}(?:\.\d+)?)", t)
    if m:
        return int(m.group(1)) * 60 + float(m.group(2))
    return _number(t, line, what)


def _position(text: str, line: int) -> tuple[float | None, float | None]:
    """hot cue 位置：「33」「33.1」–「33.4」= 谱上第 33 小节第 1–4 拍；「1:37.416」= 离文件开头的时间。"""
    t = text.strip()
    m = re.fullmatch(r"(\d+)(?:\.([1-4]))?", t)
    if m:
        bar = int(m.group(1))
        if bar < 1:
            raise _err(line, f"位置「{t}」：小节从 1 数起")
        return bar + (int(m.group(2) or 1) - 1) / 4, None
    m = re.fullmatch(r"(\d+):(\d{1,2}(?:\.\d+)?)", t)
    if m:
        return None, int(m.group(1)) * 60 + float(m.group(2))
    raise _err(line, f"看不懂的位置「{t}」：写「小节.拍」（33.1 = 第 33 小节第 1 拍，拍只能是 1–4）或时间「分:秒.毫秒」（1:37.416）")


def _wet(text: str, line: int) -> float:
    t = text.strip()
    m = re.fullmatch(r"(\d+)\s*/\s*(\d+)", t)
    if m and int(m.group(2)):
        v = int(m.group(1)) / int(m.group(2))
    elif t.endswith("%"):
        v = _number(t[:-1], line, "湿度") / 100
    else:
        v = _number(t, line, "湿度")
    if not 0 < v <= 1:
        raise _err(line, f"湿度「{t}」要在 0 和 1 之间（1/3、0.5、50% 都行）")
    return v


def _dash(text: str) -> bool:
    return text.strip() in DASHES


# ---------------------------------------------------------------- 解析

def parse(path: Path) -> Plan:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as e:
        raise PlanError("谱文件不是 UTF-8 文本（用「纯文本」存，或者用 VS Code / Typora 之类的编辑器）") from e
    return parse_text(text, path.stem)


def parse_text(text: str, name: str = "") -> Plan:
    lines = _lines(text)
    title_line = next((ln for ln in lines if re.match(r"^#\s+\S", ln.text)), None)
    if title_line is None:
        raise PlanError("这不是打碟谱：开头要有一行标题「# …」（见《打碟谱规范》）")
    title = _clean(title_line.text[1:]) or name
    head: list[_Line] = []
    sections: list[_Section] = []
    for ln in lines:
        if ln.no <= title_line.no:
            continue
        m = re.match(r"^##\s+(.*)$", ln.text)
        if m:
            sections.append(_Section(_clean(m.group(1)), ln.no))
        elif sections:
            sections[-1].body.append(ln)
        else:
            head.append(ln)

    warnings: list[str] = []
    start_bar, start_line, start_ref, ending = _head(head, warnings)
    sec_tracks = [s for s in sections if _key(s.title) in ("曲目", "曲目表")]
    if not sec_tracks:
        raise PlanError("这不是打碟谱：没有「## 曲目」这一段（见《打碟谱规范》）")
    tracks = _tracks(sec_tracks[0])
    by_no = {t.no: t for t in tracks}
    for s in sections:
        if _key(s.title) in ("hotcue", "hotcues"):
            _hot_cues(s, by_no)
    refs: list[CueRef] = []
    trans = []
    for s in sections:
        m = re.match(r"^过渡\s*0*(\d+)\s*(?:[：:]\s*(.*))?$", s.title)
        if m:
            trans.append(_transition(int(m.group(1)), (m.group(2) or "").strip(), s, by_no, warnings, refs))
    if start_ref:  # 「开场：从 1 号垫起播」
        cue = ref_cue(tracks[0], start_ref, start_line)
        if cue.bar is not None:
            start_bar = cue.bar
        else:
            start_bar = 1
            refs.append(CueRef(tracks[0].no, cue, "start"))
    start_bar, ending = _opener_and_ending(tracks, start_bar, start_line, ending, refs)
    if not trans and len(tracks) > 1:
        raise PlanError("没有任何「## 过渡 N：A → B」段落")
    trans.sort(key=lambda t: t.no)
    plan = Plan(title=title, tracks=tracks, transitions=trans, warnings=warnings, start_bar=start_bar, ending=ending,
                cue_refs=refs)
    if len({t.no for t in trans}) != len(trans):
        raise PlanError("有两段过渡用了同一个编号")
    check(plan)
    # 提示按谱里的行号排（没有行号的放最后）
    plan.warnings.sort(key=lambda w: int(m.group(1)) if (m := re.match(r"第 (\d+) 行", w)) else 1 << 30)
    return plan


def _head(head: list[_Line], warnings: list[str]) -> tuple[float | None, int, str, tuple[float | None, float | None] | None]:
    """开头设置 → (开场小节 | None, 开场那一行, 开场引用的 hot cue（「从 1 号垫起播」）, 结尾)。"""
    start_bar: float | None = None
    start_ref = ""
    ending: tuple[float | None, float | None] | None = None
    start_line = 0
    for no, k, v in _bullets(head):
        if k == "规范":
            m = re.search(r"(\d+)", v)
            if not m or m.group(1) not in ACCEPTED_VERSIONS:
                raise _err(no, f"这份谱写的是规范「{v}」，本应用认的是 v{SCORE_SPEC_VERSION}")
        elif k == "开场":
            m = re.search(r"第\s*(\d+)\s*小节", v) or re.fullmatch(r"\s*(\d+)\s*", v)
            if m and int(m.group(1)) >= 1:
                start_bar, start_line = int(m.group(1)), no
            else:  # 从某个 hot cue 起播：「从 1 号垫起播」「从 IN 起播」
                ref = re.sub(r"^\s*从\s*|\s*(开始)?起播\s*$|\s*开始\s*$", "", v).strip()
                if not ref:
                    raise _err(no, f"开场「{v}」：写「从第 N 小节起播」或「从 1 号垫起播」")
                start_bar, start_line, start_ref = None, no, ref  # 曲目表和 Hot Cue 读完再找
        elif k == "结尾":
            if re.fullmatch(r"(最后一首)?(自己)?播完", v.replace(" ", "")):
                ending = None
                continue
            m = re.search(r"第\s*(\d+)\s*小节.*?淡出\D*?(\d+)\s*小节", v)
            m2 = re.search(r"淡出\D*?(\d+)\s*小节", v)
            if m:
                ending = (int(m.group(1)), float(m.group(2)))
            elif m2:
                ending = (None, float(m2.group(1)))
            else:
                raise _err(no, f"结尾「{v}」：写「播完」或「从第 N 小节淡出，淡出 M 小节」")
            if ending[1] <= 0:
                raise _err(no, "淡出的小节数要大于 0")
    return start_bar, start_line, start_ref, ending


# ---------------------------------------------------------------- 引用谱上的 hot cue

AUTH = {"IN": ("IN", "MIX IN"), "FADE": ("FADE",)}  # 没写从哪进 / 从哪淡出时，按这首叫 IN / FADE 的垫子


def _pad(track: PlanTrack, key: str) -> PlanCue | None:
    hits = [c for c in track.hot_cues if c.name.strip().upper() in AUTH[key]]
    return hits[0] if hits else None


def ref_cue(track: PlanTrack, ref: str, line: int) -> PlanCue:
    """「3 号垫」「垫 3」「hot cue 3」「DROP」「IN」「1 号垫（DROP）」→ 这首谱上写的那个 hot cue。"""
    t = re.sub(r"[（(][^）)]*[）)]\s*$", "", ref).strip().strip("「」\"'“”").strip()
    t = re.sub(r"^(hot\s*cue|打击垫|垫子?|pad)\s*", "", t, flags=re.I).strip()
    m = re.fullmatch(r"([1-8])\s*(号)?\s*(垫子?|pad)?", t, flags=re.I)
    hit = None
    if m:
        hit = next((c for c in track.hot_cues if c.pad == int(m.group(1))), None)
    else:
        hit = next((c for c in track.hot_cues if c.name.strip().upper() == t.upper()), None)
    if hit is None:
        have = "、".join(f"{c.pad} 号垫 {c.name}" for c in track.hot_cues) or "还没写 Hot Cue"
        raise _err(line, f"#{track.no} 的 Hot Cue 里没有「{ref}」（这首有：{have}）")
    return hit


def bar_beat(bar: float) -> str:
    whole = int(bar + 1e-9)
    return f"{whole}.{round((bar - whole) * 4) + 1}"


def _opener_and_ending(tracks: list[PlanTrack], start_bar: float | None, start_line: int,
                       ending: tuple[float | None, float | None] | None,
                       refs: list[CueRef]) -> tuple[float, tuple[float | None, float | None] | None]:
    """开场：写了小节就按小节；写了「从某个 hot cue 起播」就按那个垫子；都没写、#1 有叫 IN 的垫子就从 IN，不然第 1 小节。
    结尾：最后一首有叫 FADE 的垫子、结尾没写从第几小节淡出 → 从 FADE 淡出（没写淡出几小节 = 一直淡到曲末）。"""
    first, last = tracks[0], tracks[-1]
    cue: PlanCue | None = None
    if start_bar is None:
        cue = _pad(first, "IN")
    if cue is not None:
        if cue.bar is not None:
            start_bar = cue.bar
        else:
            refs.append(CueRef(first.no, cue, "start"))
    fade = _pad(last, "FADE")
    if fade is not None and (ending is None or ending[0] is None):
        n = ending[1] if ending else None
        if fade.bar is not None:
            ending = (fade.bar, n)
        else:
            ending = (None, n)
            refs.append(CueRef(last.no, fade, "fade"))
    return (start_bar if start_bar is not None else 1), ending


def _tracks(sec: _Section) -> list[PlanTrack]:
    tables = _tables(sec.body, TRACK_COLS)
    if not tables:
        raise _err(sec.line, "「## 曲目」下面要有一张表")
    t = tables[0]
    for need in ("#", "曲名", "原BPM"):
        if need not in t.cols:
            raise _err(t.line, f"曲目表少了「{need}」这一列")
    out = []
    for no, r in t.rows:
        m = re.fullmatch(r"#?\s*0*(\d+)", r["#"])
        if not m:
            raise _err(no, f"曲目编号「{r['#']}」应该是 1、2、3 …")
        title = r["曲名"]
        if not title:
            raise _err(no, "曲名不能空着")
        bpm = _number(r["原BPM"], no, "原 BPM")
        play = _number(r["播放BPM"], no, "播放 BPM") if r.get("播放BPM") else bpm
        kl = _key(r.get("KeyLock", ""))
        if kl in ("", "关", "off", "否", "no"):
            keylock = False
        elif kl in ("开", "on", "是", "yes"):
            keylock = True
        else:
            raise _err(no, f"Key Lock「{r['KeyLock']}」写「开」或「关」")
        bars = int(_number(r["小节数"], no, "小节数")) if r.get("小节数") else None
        bar1 = _seconds(r["第1小节"], no, "第 1 小节（秒）") if r.get("第1小节") else None
        out.append(PlanTrack(no=int(m.group(1)), title=title, bpm_orig=bpm, bpm_play=play, keylock=keylock, bars=bars,
                             entry_in_prev=None, technique=None, file_name=r.get("文件名", ""), bar1_s=bar1,
                             key=r.get("调性", "")))
    if not out:
        raise _err(t.line, "曲目表是空的")
    return out


def _hot_cues(sec: _Section, by_no: dict[int, PlanTrack]) -> None:
    groups: list[tuple[_Line, list[_Line]]] = []
    for ln in sec.body:
        if re.match(r"^###\s+", ln.text):
            groups.append((ln, []))
        elif groups:
            groups[-1][1].append(ln)
    seen: set[int] = set()
    for head, body in groups:
        m = re.match(r"^###\s+#?\s*0*(\d+)\b", head.text)
        if not m:
            raise _err(head.no, "每首的 hot cue 用「### #编号 曲名」开头（编号和曲目表一致）")
        no = int(m.group(1))
        if no not in by_no:
            raise _err(head.no, f"曲目表里没有 #{no}")
        if no in seen:
            raise _err(head.no, f"#{no} 的 hot cue 写了两遍")
        seen.add(no)
        tables = _tables(body, CUE_COLS)
        if not tables:
            continue
        t = tables[0]
        for need in ("垫", "位置"):
            if need not in t.cols:
                raise _err(t.line, f"hot cue 表少了「{need}」这一列")
        pads: set[int] = set()
        for line, r in t.rows:
            pm = re.fullmatch(r"0*([1-8])", r["垫"])
            if not pm:
                raise _err(line, f"打击垫「{r['垫']}」只能是 1–8")
            pad = int(pm.group(1))
            if pad in pads:
                raise _err(line, f"打击垫 {pad} 写了两遍")
            pads.add(pad)
            name = r.get("名字", "")
            if not name:
                raise _err(line, "名字要写（英文 / 数字，≤ 16 个字符，比如 IN、OUT、BASS SWAP、VOCAL）")
            if not name.isascii():
                raise _err(line, f"名字「{name}」只能用英文 / 数字（Prime 4+ 屏幕上显示），中文写在注释里")
            if len(name) > NAME_MAX:
                raise _err(line, f"名字「{name}」超过 {NAME_MAX} 个字符")
            color = _color(r.get("颜色", ""), default_color(name), line)
            bar, time = _position(r["位置"], line)
            by_no[no].hot_cues.append(PlanCue(pad, name, color, bar, time, r.get("注释", ""), line))
            # BREAK / DROP 也是段落标记：小节对齐时拿来核对，自动补别的垫子时也按它们定
            if bar is not None and re.search(r"break|drop", name, re.I):
                by_no[no].cues.append((bar, name.lower()))


def _color(text: str, default: str, line: int) -> str:
    t = text.strip()
    if not t:
        return default
    m = re.search(r"#[0-9A-Fa-f]{6}\b", t)
    if m:
        return m.group(0).upper()
    for k, v in COLOR_NAMES.items():
        if t.startswith(k):
            return v
    raise _err(line, f"看不懂的颜色「{t}」：写 {' / '.join(COLOR_NAMES)}，或 #RRGGBB")


_ENTRY = re.compile(r"(?:落)?在\s*A\s*的?\s*第\s*(\d+)\s*小节")
_TEMPO = re.compile(r"^(\d+(?:\.\d+)?)\s*(?:(?:→|->|－>|到)\s*(\d+(?:\.\d+)?))?\s*"
                    r"(?:[，,；;]?\s*(?:在\s*)?B\s*的?\s*第\s*(\d+)\s*(?:[–\-—~～到至]\s*(\d+))?\s*小节\s*(?:内|里)?\s*换(?:速)?)?\s*$")
_FX = re.compile(r"^(A|B)\s*(?:的)?\s*(?:Main\s*FX\s*)?(Reverb|Echo|混响|回声)\s*[，,]?\s*"
                 r"第\s*(\d+)\s*(?:[–\-—~～到至]\s*(\d+))?\s*小节\s*[，,]?\s*(?:湿度|Wet/Dry|Wet|干湿)\s*[:：]?\s*(\S+)\s*$", re.I)
FX_KIND = {"reverb": "reverb", "混响": "reverb", "echo": "echo", "回声": "echo"}
KNOWN_KEYS = ("手法", "进场", "主速", "效果", "说明", "备注", "为什么")


def _transition(no: int, names: str, sec: _Section, by_no: dict[int, PlanTrack], warnings: list[str],
                refs: list[CueRef]) -> PlanTransition:
    where = f"过渡 {no}"
    a_t, b_t = by_no.get(no), by_no.get(no + 1)
    if not a_t or not b_t:
        raise _err(sec.line, f"{where} 是 #{no} → #{no + 1}，曲目表里没有这两首")
    parts = re.split(r"→|->", names, maxsplit=1) if names else []
    a_name, b_name = (parts + ["", ""])[:2]
    technique = None
    entry: tuple[str, int] | None = None  # (B 从哪进：「第 N 小节」/ hot cue / 空, A 的第几小节)
    entry_line = sec.line
    tempo: tuple[float, float, tuple[int, int] | None] | None = None
    effects: list[PlanEffect] = []
    notes: list[str] = []
    for line, k, v in _bullets(sec.body):
        if k == "手法":
            technique = TECH.get(v.split("（")[0].split("(")[0].strip())
            if not technique:
                raise _err(line, f"手法「{v}」只能是「长交接」或「瞬时切换」")
        elif k == "进场":
            m = _ENTRY.search(v)
            if not m:
                raise _err(line, f"进场「{v}」：写「B 的第 N 小节落在 A 的第 X 小节」，或「B 的 3 号垫落在 A 的第 X 小节」")
            mb = re.search(r"B\s*的?\s*(.*?)\s*$", v[:m.start()])
            entry, entry_line = ((mb.group(1) if mb else "").strip(), int(m.group(1))), line
        elif k == "主速":
            m = _TEMPO.match(v)
            if not m:
                raise _err(line, f"主速「{v}」：写「132」或「132 → 134，B 第 17–24 小节换」")
            t0 = float(m.group(1))
            t1 = float(m.group(2) or t0)
            ramp = None
            if m.group(3):
                r0 = int(m.group(3))
                r1 = int(m.group(4) or r0)
                if r1 < r0:
                    raise _err(line, f"换速的小节 {r0}–{r1} 写反了")
                ramp = (r0, r1)
            tempo = (t0, t1, ramp)
        elif k == "效果":
            m = _FX.match(v)
            if not m:
                raise _err(line, f"效果「{v}」：写「A Reverb，第 89–96 小节，湿度 1/3」（Reverb / Echo；湿度 1/3、0.5、50%）")
            b0 = int(m.group(3))
            b1 = int(m.group(4) or b0)
            if b1 < b0:
                raise _err(line, f"效果的小节 {b0}–{b1} 写反了")
            effects.append(PlanEffect(m.group(1).upper(), FX_KIND[m.group(2).lower()], b0, b1, _wet(m.group(5), line)))
        elif k in ("说明", "备注", "为什么", ""):
            notes.append(v)
        else:
            warnings.append(f"第 {line} 行：{where} 里没认出「{k}」这一项，忽略了（认得的：{' / '.join(KNOWN_KEYS)}）")
    if not technique:
        raise _err(sec.line, f"{where}：少了「- 手法：长交接」或「- 手法：瞬时切换」")
    if not entry:
        raise _err(sec.line, f"{where}：少了「- 进场：B 的第 N 小节落在 A 的第 X 小节」")
    b_ref, x = entry
    # B 从哪进：「第 N 小节」，或者 B 的某个 hot cue（3 号垫 / 名字）；没写就按 B 叫 IN 的垫子
    b_in: float | None = None
    b_cue: PlanCue | None = None
    mb = re.fullmatch(r"第\s*(\d+)\s*小节", b_ref)
    if mb:
        b_in = int(mb.group(1))
    else:
        cue = ref_cue(b_t, b_ref, entry_line) if b_ref else _pad(b_t, "IN")
        if cue is None:
            raise _err(entry_line, f"{where}：进场要写 B 从哪进——「B 的第 N 小节」，或者 B 的哪个 hot cue（「B 的 3 号垫」「B 的 DROP」）")
        if cue.bar is not None:
            b_in = cue.bar
        else:
            b_cue = cue  # 用时间写的：曲子分析完后换成拍
            refs.append(CueRef(b_t.no, cue, "entry", no))
    if tempo is None:
        tempo = (a_t.bpm_play, b_t.bpm_play, None)
        if a_t.bpm_play != b_t.bpm_play:
            raise _err(sec.line, f"{where}：两首的播放 BPM 不同（{a_t.bpm_play:g} / {b_t.bpm_play:g}），"
                                 f"写「- 主速：{a_t.bpm_play:g} → {b_t.bpm_play:g}，B 第 m–n 小节换」")
    t0, t1, ramp = tempo
    if t0 != t1 and ramp is None:
        raise _err(sec.line, f"{where}：主速 {t0:g} → {t1:g} 要写在 B 的哪几小节换：「- 主速：{t0:g} → {t1:g}，B 第 m–n 小节换」")
    if b_in is not None and ramp and ramp[0] < int(b_in):
        raise _err(sec.line, f"{where}：换速从 B 第 {ramp[0]} 小节开始，B 第 {b_in:g} 小节才进场")
    for fx in effects:
        if b_in is not None and fx.deck == "B" and fx.bar0 < int(b_in):
            raise _err(sec.line, f"{where}：B 的 {fx.kind} 从 B 第 {fx.bar0} 小节开始，B 第 {b_in:g} 小节才进场")

    tables = _tables(sec.body, ROW_COLS)
    if not tables:
        raise _err(sec.line, f"{where}：少了逐小节旋钮表（A 小节 / B 小节 / A LOW … / B 推子 / 动作）")
    t = tables[0]
    for need in ("A小节", "B推子"):
        if need not in t.cols:
            raise _err(t.line, f"{where} 的旋钮表少了「{need[0]} {need[1:]}」这一列")
    rows = _rows(where, t, x, b_in)
    if rows[0].a_bar > x:
        raise _err(t.rows[0][0], f"{where}：表的第一行是 A 第 {rows[0].a_bar} 小节，晚于 B 进场（A 第 {x} 小节）")
    if not any(r.a_bar == x for r in rows):
        raise _err(t.line, f"{where}：B 进场那一小节（A 第 {x} 小节）要有一行")
    return PlanTransition(no, a_name.strip() or a_t.title.split(" — ")[0], b_name.strip() or b_t.title.split(" — ")[0],
                          technique, x, t0, t1, ramp, rows, effects, notes, "", b_in if b_in is not None else 1, b_cue)


PARAMS = (("LOW", "lo"), ("MID", "mid"), ("HIGH", "hi"), ("FILTER", "filter"), ("推子", "vol"))


def _knobs(where: str, side: str, r: dict[str, str], base: dict[str, float | None], line: int) -> Knobs:
    v: dict[str, float | None] = dict(base)
    for col, attr in PARAMS:
        text = r.get(f"{side}{col}", "")
        if not text:
            continue  # 空着 = 和上一行一样
        if _dash(text):
            raise _err(line, f"{side} {col} 写了「—」：" + ("A 一直在播，不变就空着" if side == "A" else "B 已经进场了，不变就空着"))
        try:
            v[attr] = fader(text) if attr == "vol" else clock(text, attr == "filter")
        except PlanError as e:
            raise _err(line, f"{side} {col}：{e}") from e
    if v["vol"] is None:
        raise _err(line, "B 进场那一行要写 B 推子（0–10）")
    return Knobs(v["lo"] or 0.0, v["mid"] or 0.0, v["hi"] or 0.0, v["filter"] or 0.0, float(v["vol"]))  # type: ignore[arg-type]


def _rows(where: str, t: _Table, x: int, b_in: float | None) -> list[PlanRow]:
    """b_in = None：B 从用时间写的 hot cue 进，读谱时还不知道是第几小节（B 小节这一列不核对）。"""
    rows: list[PlanRow] = []
    a_prev: dict[str, float | None] = {"lo": 0.0, "mid": 0.0, "hi": 0.0, "filter": 0.0, "vol": 10.0}
    b_prev: dict[str, float | None] = {"lo": 0.0, "mid": 0.0, "hi": 0.0, "filter": 0.0, "vol": None}
    for k, (line, r) in enumerate(t.rows):
        m = re.fullmatch(r"(\d+)\s*[（(]?\s*(结束|停)?\s*[)）]?", r["A小节"])
        if not m:
            raise _err(line, f"A 小节「{r['A小节']}」：写小节号，最后一行写「160 结束」或「129 停」")
        a_bar, mark = int(m.group(1)), m.group(2)
        if rows and a_bar <= rows[-1].a_bar:
            raise _err(line, f"A 小节要一行比一行大（{rows[-1].a_bar} → {a_bar}）")
        if mark and k != len(t.rows) - 1:
            raise _err(line, f"「{mark}」只能写在最后一行")
        if not mark and k == len(t.rows) - 1:
            raise _err(line, f"{where}：最后一行的 A 小节后面要写「结束」（A 播到自己的结尾）或「停」（A 在这一小节停）")
        a = _knobs(where, "A", r, a_prev, line)
        a_prev = vars(a).copy()
        b_txt = r.get("B小节", "")
        if a_bar < x:
            filled = [c for c in ("LOW", "MID", "HIGH", "FILTER", "推子") if r.get(f"B{c}") and not _dash(r[f"B{c}"])]
            if filled or (b_txt and not _dash(b_txt)):
                raise _err(line, f"A 第 {a_bar} 小节时 B 还没进场（A 第 {x} 小节才进）：B 的格子写「—」或空着")
            b, b_bar = None, None
        else:
            b_bar = a_bar - x + b_in if b_in is not None and b_in == int(b_in) else None
            if b_bar is not None and b_txt and not _dash(b_txt) and _number(b_txt, line, "B 小节") != b_bar:
                raise _err(line, f"B 小节写的是 {b_txt}，按「B 第 {b_in:g} 小节落在 A 第 {x} 小节」应该是 {int(b_bar)}")
            b = _knobs(where, "B", r, b_prev, line)
            b_prev = vars(b).copy()
        rows.append(PlanRow(a_bar, b_bar, bool(mark), a, b, r.get("动作", ""), stop=(mark == "停")))
    if not rows:
        raise _err(t.line, f"{where} 的旋钮表是空的")
    return rows


def score_doc(name: str) -> Path | None:
    """docs/ 下给写谱的人看的文件（SCORE_SPEC.md、score_template.md）。
    源码里在仓库的 docs/；打包的 App 里在 djstudio/docs/（packaging/macos/djstudio.spec 拷进去）。"""
    pkg = Path(__file__).resolve().parents[1]
    for d in (pkg / "docs", pkg.parent / "docs"):
        if (d / name).is_file():
            return d / name
    return None


__all__ = ["parse", "parse_text", "score_doc", "SUFFIXES", "SCORE_SPEC_VERSION"]
