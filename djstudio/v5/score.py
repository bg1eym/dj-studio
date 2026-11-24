"""曲谱 v5（Markdown）→ 项目（djset/3）。确定性规则，不用 AI。规范见 docs/SCORE_SPEC_V5.md。

    # 标题
    - 规范：v5 / 主速：132
    ## 素材        ID / 曲名 / 文件名 / 原 BPM / 第 1 小节（秒）/ 小节数 / 调性
    ## 速度        全场位置 / BPM / 变化（保持、渐变、跳变）                      （可以不写）
    ## 片段        ID / 素材 / Deck / 原曲起点 / 全场起播 / 全场停止 / 角色 / 推子 / LOW / MID / HIGH / Filter / Key Lock / 准备 / 备注
    ## 场景 N：名字（129.1 – 161.1）
        - 作用 / 保留 / 前景 / LOW / MID/HIGH / 角色 / 状态 / 有意 / 说明
        ### C1 参数      位置 / LOW / MID / HIGH / Filter / 推子 / 说明
        ### Loop 与跳段   片段 / 位置 / 做什么 / 说明
        ### 效果         ID / 类型 / 路由 / 目标 / 开始 / 结束 / 参数 / 说明
    （「参数」「Loop 与跳段」「效果」也可以直接写在 ## 一级，不属于哪个场景——独奏的时候一样能动每个旋钮）

和 v4 的解析（plan/mdplan.py）不同的地方：不在第一个错误就停，能读的都读完，一次把所有错误列出来（带行号、场景、目标、
怎么改）；错误和提示分开；旋钮表里曲线的类型是写明的（「保持」/ 直接写值 = 匀速拧到 / 「跳 7点」= 一下到位），不按变化大小猜。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..plan.cdw import PlanError, clock, fader
from ..plan.convert import eq_db, filter_val
from ..plan.mdplan import _alias, _bullets, _clean, _dash, _key, _Line, _lines, _number, _seconds, _tables
from . import elements as el
from .compile import FX_PARAMS, Issue5
from .livefx import LIVE_KINDS
from .model import MAX_DECKS, SCORE_VERSION, Project
from .pos import PosError, fmt_pos, parse_dur, parse_pos

ASSET_COLS = _alias({
    "ID": ["id", "编号"], "曲名": ["曲目", "title", "名字"], "文件名": ["文件", "file"], "原BPM": ["原速", "bpm"],
    "小节数": ["小节", "bars"], "第1小节": ["第1小节(秒)", "第1小节第1拍", "第1小节第1拍(秒)", "第1小节(s)"],
    "调性": ["key"],
})
TEMPO_COLS = _alias({"全场位置": ["位置", "全场"], "BPM": ["主速", "速度"], "变化": ["怎么变", "类型"]})
ELEMENT_COLS = _alias({"素材": ["素材ID", "曲"], "元素": ["声部", "乐器"], "原曲小节": ["小节", "范围", "在哪几小节"], "说明": ["备注"]})
CLIP_COLS = _alias({
    "ID": ["id", "片段"], "素材": ["asset"], "Deck": ["deck", "播放器"], "原曲起点": ["原曲位置", "原曲", "源起点"],
    "全场起播": ["起播", "全场起点"], "全场停止": ["停止", "停"], "角色": ["role"], "推子": ["音量"],
    "LOW": ["low"], "MID": ["mid"], "HIGH": ["high"], "Filter": ["filter", "FILTER"], "KeyLock": ["keylock"],
    "准备": ["准备时间", "prep"], "备注": ["说明", "注释"],
})
PARAM_COLS = _alias({"位置": ["全场位置"], "LOW": ["low"], "MID": ["mid"], "HIGH": ["high"], "Filter": ["filter", "FILTER"],
                     "推子": ["音量"], "说明": ["动作", "备注", "注释"]})
MOVE_COLS = _alias({"片段": ["ID", "id"], "位置": ["全场位置"], "做什么": ["动作"], "说明": ["备注", "注释"]})
FX_COLS = _alias({"ID": ["id", "编号"], "类型": ["效果", "kind"], "路由": ["route"], "目标": ["片段", "target"],
                  "开始": ["开", "起"], "结束": ["关", "止"], "参数": ["params"], "说明": ["备注", "注释"]})
PARAM_OF = {"LOW": "lo", "MID": "mid", "HIGH": "hi", "Filter": "filter", "推子": "vol"}
FX_KIND = {"delay": "delay", "延迟": "delay", "echo": "echo", "回声": "echo", "reverb": "reverb", "混响": "reverb",
           "trans": "trans", "roll": "roll", "filterecho": "filter_echo", "spiral": "spiral", "noise": "noise",
           "噪声": "noise", "flanger": "flanger", "phaser": "phaser", "filter": "filter", "采样": "sample",
           "sample": "sample", "release": "release"}
FX_ROUTE = {"发送": "send", "send": "send", "辅助发送": "send", "插入": "insert", "insert": "insert",
            "组插入": "group-insert", "group-insert": "group-insert", "组": "group-insert",
            "总输出": "master-insert", "master-insert": "master-insert", "总输出插入": "master-insert",
            "采样": "master-insert", "主输出": "master-insert"}
# 效果参数的中文名 → 计划里的名字
FX_KEYS = {"拍长": "beats", "反馈": "feedback", "乒乓": "pingpong", "衰减": "decay", "带宽": "bandwidth", "阻尼": "damping"}
STRATEGY = {"作用": "purpose", "保留": "keep", "前景": "lead", "low": "low", "mid/high": "midhigh"}
INTENT = {"多低频": "multi_low", "两条声线": "multi_vocal", "中频叠加": "multi_mid"}
SCENE_RE = re.compile(r"^场景\s*0*(\d+)\s*(?:[：:]\s*(.*?))?\s*$")
RANGE_RE = re.compile(r"[（(]\s*([0-9.+/\s]+?)\s*(?:[–\-—~～]|到|至)\s*([0-9.+/\s]+?)\s*[)）]\s*$")
ELEMENT_TABLE_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_\-]*)\s*(?:的)?\s*(?:元素|声部)(?:表)?$")  # 「### A2 元素」
CLIP_TABLE_RE = re.compile(r"^(?:参数\s*)?([A-Za-z][A-Za-z0-9_\-]*)\s*(?:的)?\s*(?:参数|旋钮)?$")


@dataclass
class ScoreResult:
    project: Project | None
    issues: list[Issue5] = field(default_factory=list)
    title: str = ""

    @property
    def ok(self) -> bool:
        return self.project is not None and not any(i.level == "error" for i in self.issues)

    @property
    def errors(self) -> list[Issue5]:
        return [i for i in self.issues if i.level == "error"]


def score_version(text: str) -> str:
    """谱开头写的规范版本（「- 规范：v5」）；没写 = ""。读谱前用它决定走 v4 的解析还是 v5 的。"""
    for ln in _lines(text):
        if re.match(r"^##\s", ln.text):
            break
        m = re.match(r"^\s*[-*+]\s*规范\s*[：:]\s*v?\s*(\d+)", ln.text.replace("**", ""))
        if m:
            return m.group(1)
    return ""


def slug(name: str) -> str:
    """谱文件名 → 项目 id。名字里有中文的，留下英文数字的部分再加一小段哈希：
    「四 Deck 场景示例」和「四 Deck 测试」不会撞成同一个项目。"""
    out = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower()
    if re.fullmatch(r"[A-Za-z0-9 _.\-]*", name) and out:
        return out
    return (out or "score") + "-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:6]


def parse(path: Path) -> ScoreResult:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        return ScoreResult(None, [Issue5("error", "encoding", path.name, "谱文件不是 UTF-8 文本（用「纯文本」存，或者用 VS Code / Typora）")])
    return parse_text(text, path.stem, path.name)


# ---------------------------------------------------------------- 取值

def _amount(text: str) -> float:
    """量（0..1）：「1/3」「0.5」「50%」。"""
    t = text.strip()
    m = re.fullmatch(r"(\d+)\s*/\s*(\d+)", t)
    if m and int(m.group(2)):
        v = int(m.group(1)) / int(m.group(2))
    elif t.endswith("%"):
        v = float(t[:-1]) / 100
    else:
        v = float(t)
    if not 0 <= v <= 1:
        raise ValueError(f"「{text}」要在 0 和 1 之间（1/3、0.5、50% 都行）")
    return round(v, 4)


def knob_value(param: str, text: str) -> float:
    """旋钮位（Prime 4+ 面板上的说法）→ 人读单位：EQ 钟点位 / dB、Filter 左右钟点、推子 0–10。"""
    t = text.strip().replace(" ", "")
    if param == "vol":
        return round(fader(t) / 10.0, 6)
    if param == "filter":
        m = re.fullmatch(r"(左|右)(\d+(?:\.\d+)?)%", t)  # 对不上钟点位的：左 / 右多少（100% = 拧到底）
        if m:
            v = float(m.group(2)) / 100
            if v > 1:
                raise PlanError(f"「{text}」超过了 100%")
            return round(-v if m.group(1) == "左" else v, 4)
        return filter_val(clock(t, True))
    m = re.fullmatch(r"([+\-−]?\d+(?:\.\d+)?)d[bB]", t)
    if m:
        v = float(m.group(1).replace("−", "-"))
        if not -60 <= v <= 12:
            raise PlanError(f"「{text}」超出 -60 – +12 dB")
        return v
    if t in ("切", "切掉", "kill"):
        return eq_db(-5.0)
    return eq_db(clock(t))


class _Parser:
    def __init__(self, text: str, name: str, file: str) -> None:
        self.lines = _lines(text)
        self.name = name
        self.file = file
        self.issues: list[Issue5] = []
        self.assets: list[dict[str, Any]] = []
        self.clips: list[dict[str, Any]] = []
        self.clip_ids: dict[str, int] = {}  # 片段 ID → 行号
        self.tempo: list[dict[str, Any]] = []
        self.lanes: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.spans: dict[tuple[str, str], list[tuple[Any, Any, int]]] = {}  # 每个旋钮已经被哪几张表管了哪一段
        self.loops: list[dict[str, Any]] = []
        self.jumps: list[dict[str, Any]] = []
        self.fx: list[dict[str, Any]] = []
        self.scenes: list[dict[str, Any]] = []
        self.marks: list[dict[str, Any]] = []
        self.elements: dict[str, dict[str, tuple[int, list[tuple[int, int, float]]]]] = {}  # 素材 → 声部 → (行号, 哪几小节)
        self.scene: dict[str, Any] | None = None  # 正在读的场景

    # ------------------------------------------------------------ 报告

    def where(self, what: str = "") -> str:
        head = f"场景 {self.scene['no']}" + (f"「{self.scene['name']}」" if self.scene and self.scene["name"] else "") \
            if self.scene else self.file
        return f"{head} {what}".strip()

    def err(self, line: int, code: str, msg: str, what: str = "", fix: str = "") -> None:
        self.issues.append(Issue5("error", code, self.where(what), msg, line, fix))

    def hint(self, line: int, code: str, msg: str, what: str = "", fix: str = "") -> None:
        self.issues.append(Issue5("hint", code, self.where(what), msg, line, fix))

    def pos(self, text: str, line: int, what: str) -> Fraction | None:
        try:
            return parse_pos(text)
        except PosError as e:
            self.err(line, "pos", str(e), what)
            return None

    def tables(self, body: list[_Line], aliases: dict[str, str], line: int) -> list[Any]:
        try:
            return _tables(body, aliases)
        except PlanError as e:
            m = re.match(r"第 (\d+) 行：(.*)", str(e))
            self.err(int(m.group(1)) if m else line, "table", m.group(2) if m else str(e))
            return []

    # ------------------------------------------------------------ 总流程

    def run(self) -> ScoreResult:
        title_line = next((ln for ln in self.lines if re.match(r"^#\s+\S", ln.text)), None)
        if title_line is None:
            return ScoreResult(None, [Issue5("error", "title", self.file, "这不是曲谱：开头要有一行标题「# …」", 1)])
        title = _clean(title_line.text[1:]) or self.name
        head: list[_Line] = []
        sections: list[tuple[int, str, int, list[_Line]]] = []  # (级别, 标题, 行号, 内容)
        for ln in self.lines:
            if ln.no <= title_line.no:
                continue
            m = re.match(r"^(##|###)\s+(.*)$", ln.text)
            if m:
                sections.append((len(m.group(1)), _clean(m.group(2)), ln.no, []))
            elif sections:
                sections[-1][3].append(ln)
            else:
                head.append(ln)
        base_bpm = self._head(head)
        found = {"素材": False, "片段": False}
        for level, name, line, body in sections:
            key = _key(name)
            ms = SCENE_RE.match(name) if level == 2 else None
            if level == 2 and not ms:
                self.scene = None
            if level == 2 and key in ("素材", "素材表", "曲目", "曲目表"):
                found["素材"] = True
                self._assets(body, line)
            elif level == 2 and key in ("速度", "主速", "全场速度", "tempo"):
                self._tempo(body, line)
            elif level == 2 and key in ("片段", "片段表"):
                found["片段"] = True
                self._clips(body, line)
            elif ms:
                self._scene(int(ms.group(1)), ms.group(2) or "", body, line)
            elif key in ("loop与跳段", "跳段与loop", "loop和跳段", "跳段和loop", "loop", "跳段"):
                self._moves(body, line)
            elif key in ("效果", "效果器", "fx"):
                self._effects(body, line)
            elif level == 2 and key in ("元素", "元素表", "声部", "声部表"):
                self._elements(body, line, None)
            elif level == 3 and ELEMENT_TABLE_RE.match(name):
                self._elements(body, line, ELEMENT_TABLE_RE.match(name).group(1))
            elif key == "hotcue" or key == "hotcues":
                self.hint(line, "hotcue", "v5 的谱里 Hot Cue 表现在还不读（不影响演出）")
            else:
                mc = CLIP_TABLE_RE.match(name)
                if mc and ("参数" in name or "旋钮" in name):
                    self._params(mc.group(1), body, line)
                # 别的段落（## 思路 之类）是写给自己看的，不读
        if not found["素材"]:
            self.err(title_line.no, "structure", "没有「## 素材」这一段", fix="加一张素材表：ID / 曲名 / 文件名 / 原 BPM / 第 1 小节（秒）")
        if not found["片段"]:
            self.err(title_line.no, "structure", "没有「## 片段」这一段", fix="加一张片段表：ID / 素材 / Deck / 原曲起点 / 全场起播 / 全场停止")
        self.scene = None
        if not self.tempo:
            if base_bpm is None:
                self.err(title_line.no, "tempo", "没写全场速度", fix="开头写「- 主速：132」，或者加一张「## 速度」表")
            else:
                self.tempo = [{"at": Fraction(0), "bpm": base_bpm, "ramp": False, "line": 0}]
        elif self.tempo[0]["at"] != 0:
            if base_bpm is None:
                self.err(self.tempo[0]["line"], "tempo", "速度表要从 1.1 写起（或者开头写「- 主速：…」）")
            else:
                self.tempo.insert(0, {"at": Fraction(0), "bpm": base_bpm, "ramp": False, "line": 0})
        self._attach_elements(title_line.no)
        project = None
        if not any(i.level == "error" for i in self.issues):
            data = {
                "schema": "djset/3", "spec": SCORE_VERSION, "id": slug(self.name), "name": title, "source": self.file,
                "tempo": self.tempo, "assets": self.assets, "clips": self.clips,
                "lanes": [{"clip": c, "param": p, "points": sorted(pts, key=lambda x: x["at"])}
                          for (c, p), pts in self.lanes.items()],
                "loops": self.loops, "jumps": self.jumps, "fx": self.fx, "scenes": self.scenes, "marks": self.marks,
            }
            try:
                project = Project.model_validate(data)
            except ValidationError as e:
                for er in e.errors():
                    self.issues.append(Issue5("error", "model", self.file, str(er.get("msg", "")).removeprefix("Value error, ")))
        self.issues.sort(key=lambda i: (i.line or 1 << 30, i.level != "error"))
        return ScoreResult(project, self.issues, title)

    # ------------------------------------------------------------ 开头、素材、速度、片段

    def _head(self, head: list[_Line]) -> float | None:
        bpm = None
        for no, k, v in _bullets(head):
            if k == "规范":
                m = re.search(r"(\d+)", v)
                if not m or m.group(1) != SCORE_VERSION:
                    self.err(no, "version", f"这份谱写的是规范「{v}」，这里读的是 v{SCORE_VERSION}")
            elif k in ("主速", "速度", "bpm"):
                try:
                    bpm = _number(v, no, "主速")
                except PlanError as e:
                    self.err(no, "tempo", str(e).split("：", 1)[-1])
        return bpm

    def _assets(self, body: list[_Line], line: int) -> None:
        tables = self.tables(body, ASSET_COLS, line)
        if not tables:
            self.err(line, "structure", "「## 素材」下面要有一张表")
            return
        t = tables[0]
        for need in ("ID", "原BPM"):
            if need not in t.cols:
                self.err(t.line, "structure", f"素材表少了「{need}」这一列")
                return
        seen: set[str] = set()
        for no, r in t.rows:
            aid = r["ID"]
            if not aid:
                self.err(no, "asset", "素材的 ID 不能空着（A1、A2 …）")
                continue
            if aid in seen:
                self.err(no, "asset", f"素材 {aid} 写了两遍")
                continue
            seen.add(aid)
            try:
                bpm = _number(r["原BPM"], no, "原 BPM")
                bar1 = _seconds(r["第1小节"], no, "第 1 小节（秒）") if r.get("第1小节") and not _dash(r["第1小节"]) else None
                bars = int(_number(r["小节数"], no, "小节数")) if r.get("小节数") and not _dash(r["小节数"]) else None
            except PlanError as e:
                self.err(no, "asset", str(e).split("：", 1)[-1], f"素材 {aid}")
                continue
            title = r.get("曲名", "")
            artist = ""
            if " — " in title:
                title, artist = (x.strip() for x in title.split(" — ", 1))
            key = r.get("调性", "")
            self.assets.append({"id": aid, "title": title, "artist": artist, "file": r.get("文件名", ""), "bpm": bpm,
                                "bar1_s": bar1, "bars": bars, "key": "" if _dash(key) else key, "line": no})

    def _elements(self, body: list[_Line], line: int, asset: str | None) -> None:
        """元素表：「## 元素」下面一张表（素材 / 元素 / 原曲小节），或者「### A2 元素」下面一张表（元素 / 原曲小节）。"""
        tables = self.tables(body, ELEMENT_COLS, line)
        if not tables:
            if asset:
                self.err(line, "structure", f"「{asset} 元素」下面要有一张表：元素 / 原曲小节")
            return  # 「## 元素」下面可以只有几张「### A2 元素」
        for t in tables:
            need = ("元素", "原曲小节") if asset else ("素材", "元素", "原曲小节")
            missing = [n for n in need if n not in t.cols]
            if missing:
                self.err(t.line, "structure", f"元素表少了「{'、'.join(missing)}」这一列", fix="列：素材 / 元素 / 原曲小节（比如 A2｜kick｜1–64 满，81–192）")
                continue
            for no, r in t.rows:
                aid = asset or r.get("素材", "")
                what = f"素材 {aid} 的元素"
                name = r.get("元素", "")
                if not aid or not name:
                    if any(r.values()):
                        self.err(no, "element", "这一行要写是哪份素材的哪个元素", what)
                    continue
                cid = el.canon(name)
                if cid is None:
                    self.err(no, "element", f"不认识的元素「{name}」", what, fix=f"认得的：{el.known_names()}")
                    continue
                text = r.get("原曲小节", "")
                if not text or _dash(text):
                    continue  # 这个声部整首都没有：不用写，写了空着也行
                try:
                    spans = el.parse_spans(text)
                except ValueError as e:
                    self.err(no, "element", str(e), f"{what} {el.ZH[cid]}")
                    continue
                rows = self.elements.setdefault(aid, {})
                if cid in rows:
                    self.err(no, "element", f"{el.ZH[cid]} 写了两行（第 {rows[cid][0]} 行已经写过）：合成一行", what)
                    continue
                rows[cid] = (no, spans)

    def _attach_elements(self, line: int) -> None:
        """读完全篇以后把元素表挂到素材上；谱上要放的素材没写元素的，提示一下（画面上那一台就画不出格子）。"""
        ids = {a["id"]: a for a in self.assets}
        for aid, rows in self.elements.items():
            a = ids.get(aid)
            if a is None:
                self.err(min(no for no, _ in rows.values()), "element", f"元素表里的素材「{aid}」不在素材表里")
                continue
            for cid, (no, spans) in rows.items():
                if a["bars"] and spans and spans[-1][1] > a["bars"]:
                    self.hint(no, "element_range", f"{el.ZH[cid]} 写到了第 {spans[-1][1]} 小节，素材表上这首只有 {a['bars']} 小节", f"素材 {aid} 的元素")
            a["elements"] = [{"name": cid, "spans": rows[cid][1]} for cid in el.ORDER if cid in rows]
        used = list(dict.fromkeys(c["asset"] for c in self.clips if c["asset"] in ids))
        lacking = [aid for aid in used if aid not in self.elements]
        if used and lacking:
            which = "这份谱" if len(lacking) == len(used) else f"素材 {'、'.join(lacking)} "
            self.hint(line, "elements", f"{which}没写元素表：演出画面的 EQ 曲线底下画不出各个声部（kick、bass、镲、人声…）在哪几小节有，旋钮为什么这么拧就看不出来",
                      fix="加一段「## 元素」：素材 / 元素 / 原曲小节（写法见曲谱规范 v5 第 14 节）。不写也能演出")

    def _tempo(self, body: list[_Line], line: int) -> None:
        tables = self.tables(body, TEMPO_COLS, line)
        if not tables:
            self.err(line, "structure", "「## 速度」下面要有一张表：全场位置 / BPM / 变化")
            return
        t = tables[0]
        for need in ("全场位置", "BPM"):
            if need not in t.cols:
                self.err(t.line, "structure", f"速度表少了「{need}」这一列")
                return
        for no, r in t.rows:
            at = self.pos(r["全场位置"], no, "速度")
            if at is None:
                continue
            try:
                bpm = _number(r["BPM"], no, "BPM")
            except PlanError as e:
                self.err(no, "tempo", str(e).split("：", 1)[-1])
                continue
            how = _key(r.get("变化", ""))
            if how in ("", "保持", "跳变", "跳", "一下换", "step"):
                ramp = False
            elif how in ("渐变", "匀速", "匀速变", "ramp"):
                ramp = True
            else:
                self.err(no, "tempo", f"变化「{r['变化']}」：写「渐变」（从上一行匀速变到这里）或「跳变」（这一拍一下换过去）")
                continue
            if self.tempo and at < self.tempo[-1]["at"]:
                self.err(no, "tempo", f"{fmt_pos(at)} 在上一行之前：速度表按位置从前往后写")
                continue
            if ramp and not self.tempo:
                self.err(no, "tempo", "第一行不能是渐变（没有从哪开始变）")
                continue
            self.tempo.append({"at": at, "bpm": bpm, "ramp": ramp, "line": no})

    def _clips(self, body: list[_Line], line: int) -> None:
        tables = self.tables(body, CLIP_COLS, line)
        if not tables:
            self.err(line, "structure", "「## 片段」下面要有一张表")
            return
        t = tables[0]
        for need in ("ID", "素材", "Deck", "全场起播"):
            if need not in t.cols:
                self.err(t.line, "structure", f"片段表少了「{need}」这一列")
                return
        assets = {a["id"] for a in self.assets}
        for no, r in t.rows:
            cid = r["ID"]
            what = f"片段 {cid}"
            if not cid:
                self.err(no, "clip", "片段的 ID 不能空着（C1、C2 …）")
                continue
            if cid in self.clip_ids:
                self.err(no, "clip", f"片段 {cid} 写了两遍（第 {self.clip_ids[cid]} 行）：同一份素材再用一次要起一个新的 ID")
                continue
            self.clip_ids[cid] = no
            if r["素材"] not in assets:
                self.err(no, "clip", f"素材「{r['素材']}」不在素材表里", what)
                continue
            md = re.fullmatch(r"[Dd]?\s*([1-9])", r["Deck"].strip())
            if not md or int(md.group(1)) > MAX_DECKS:
                self.err(no, "clip", f"Deck「{r['Deck']}」：写 D1–D{MAX_DECKS}", what)
                continue
            start = self.pos(r["全场起播"], no, what)
            src_t = r.get("原曲起点", "")
            src = self.pos(src_t, no, what) if src_t and not _dash(src_t) else Fraction(0)
            stop_t = r.get("全场停止", "").strip()
            stop: Fraction | None = None
            if stop_t and not _dash(stop_t) and stop_t not in ("播完", "播到曲尾", "结束"):
                stop = self.pos(stop_t, no, what)
                if stop is None:
                    continue
            if start is None or src is None:
                continue
            init: dict[str, float] = {}
            bad = False
            for col, param in PARAM_OF.items():
                txt = r.get(col, "")
                if not txt or _dash(txt):
                    continue
                try:
                    init[param] = knob_value(param, txt)
                except (PlanError, ValueError) as e:
                    self.err(no, "knob", f"{col}：{e}", what)
                    bad = True
            kl = _key(r.get("KeyLock", ""))
            if kl not in ("", "关", "off", "否", "no", "开", "on", "是", "yes", "—", "-"):
                self.err(no, "clip", f"Key Lock「{r['KeyLock']}」写「开」或「关」", what)
                bad = True
            prep = None
            if r.get("准备") and not _dash(r["准备"]):
                try:
                    prep = parse_dur(r["准备"])
                except PosError as e:
                    self.err(no, "clip", f"准备：{e}", what)
                    bad = True
            if bad:
                continue
            self.clips.append({"id": cid, "asset": r["素材"], "deck": int(md.group(1)), "src": src, "start": start,
                               "stop": stop, "prep": prep, "role": "" if _dash(r.get("角色", "")) else r.get("角色", ""),
                               "init": init, "keylock": kl in ("开", "on", "是", "yes"), "note": r.get("备注", ""),
                               "line": no})

    # ------------------------------------------------------------ 场景

    def _scene(self, no: int, rest: str, body: list[_Line], line: int) -> None:
        m = RANGE_RE.search(rest)
        name = rest[:m.start()].strip() if m else rest.strip()
        self.scene = sc = {"no": no, "name": name, "line": line}
        if any(s["no"] == no for s in self.scenes):
            self.err(line, "scene", f"场景 {no} 写了两遍")
        if not m:
            self.err(line, "scene", "场景标题里要写范围", fix="比如「## 场景 3：连续高潮（129.1 – 161.1）」")
            return
        a, b = self.pos(m.group(1), line, "开始"), self.pos(m.group(2), line, "结束")
        if a is None or b is None:
            return
        sc.update(start=a, end=b, roles={}, intent=[], draft=False, note="")
        notes = []
        for ln, k, v in _bullets(body):
            if k in STRATEGY:
                sc[STRATEGY[k]] = v
            elif k == "角色":
                for part in re.split(r"[，,；;、]\s*", v):
                    mr = re.match(r"^[Dd]\s*([1-9])\s*[:：]?\s*(.+)$", part.strip())
                    if mr:
                        sc["roles"][f"D{mr.group(1)}"] = mr.group(2).strip()
                    elif part.strip():
                        self.err(ln, "scene", f"角色「{part.strip()}」：写「D1 主曲，D3 节奏层」这样")
            elif k == "状态":
                if _key(v) in ("草稿", "draft"):
                    sc["draft"] = True
                elif _key(v) not in ("正式", "定稿"):
                    self.err(ln, "scene", f"状态「{v}」：写「草稿」或「正式」")
            elif k == "有意":
                for part in re.split(r"[，,；;、/]\s*", v):
                    if part.strip() in INTENT:
                        sc["intent"].append(INTENT[part.strip()])
                    elif part.strip():
                        self.err(ln, "scene", f"有意「{part.strip()}」：认得的是 {' / '.join(INTENT)}")
            elif k in ("说明", "备注", ""):
                notes.append(v)
            else:
                self.hint(ln, "scene_key", f"没认出「{k}」这一项，没读（认得的：作用 / 保留 / 前景 / LOW / MID/HIGH / 角色 / 状态 / 有意 / 说明）")
        sc["note"] = "；".join(n for n in notes if n)
        self.scenes.append(sc)

    def _in_scene(self, at: Fraction, line: int, what: str) -> None:
        sc = self.scene
        if sc and "start" in sc and not sc["start"] <= at <= sc["end"]:
            self.hint(line, "scene_range", f"{fmt_pos(at)} 不在这个场景的范围里（{fmt_pos(sc['start'])} – {fmt_pos(sc['end'])}）", what,
                      "照样会执行；写到它所在的场景里更好找")

    # ------------------------------------------------------------ 参数表

    def _params(self, cid: str, body: list[_Line], line: int) -> None:
        what = f"{cid} 参数"
        if cid not in self.clip_ids:
            self.err(line, "param", f"片段表里没有 {cid}", what, "参数表的标题写片段的 ID：「### C1 参数」")
            return
        for t in self.tables(body, PARAM_COLS, line):
            if "位置" not in t.cols:
                self.err(t.line, "structure", "参数表少了「位置」这一列", what)
                continue
            anchors: dict[str, int] = {}  # 这张表里每个旋钮上一次写了东西的行
            span: dict[str, list[Any]] = {}  # 这张表里每个旋钮管的那一段：[第一行的位置, 最后一行的位置, 第一行的行号]
            last_at = None
            for no, r in t.rows:
                at = self.pos(r["位置"], no, what)
                if at is None:
                    continue
                if last_at is not None and at < last_at:
                    self.err(no, "param_order", f"{fmt_pos(at)} 排在 {fmt_pos(last_at)} 后面：参数表按位置从前往后写", what)
                    continue
                last_at = at
                self._in_scene(at, no, what)
                wrote = False
                for col, param in PARAM_OF.items():
                    txt = r.get(col, "").strip()
                    if not txt or _dash(txt):
                        continue
                    pts = self.lanes.setdefault((cid, param), [])
                    try:
                        if txt in ("保持", "=", "不动", "hold"):
                            # 锚点：值 = 上一个点的值；前面没有点 = 片段起播时的位置。这一拍已经有点了（上一张表正好
                            # 拧到这里）就不用再加
                            prev = [p for p in pts if p["at"] <= at]
                            if any(p["at"] == at for p in pts):
                                pass
                            elif prev:
                                pts.append({"at": at, "value": max(prev, key=lambda p: p["at"])["value"], "step": False,
                                            "line": no})
                            else:
                                pts.append({"at": at, "value": None, "step": False, "line": no})
                        else:
                            m = re.match(r"^(跳到?|切到|一下到?|直接)\s*(.+)$", txt)
                            if m:
                                pts.append({"at": at, "value": knob_value(param, m.group(2)), "step": True, "line": no})
                            else:
                                if param not in anchors:
                                    self.err(no, "ramp_start", f"{col} 写了「{txt}」= 匀速拧到这个位置，但这张表前面没有它从哪一拍开始拧",
                                             what, "在开始动手的那一拍加一行，这一格写「保持」；要一下到位就写「跳 " + txt + "」")
                                    continue
                                pts.append({"at": at, "value": knob_value(param, txt), "step": False, "line": no})
                        anchors[param] = no
                        span.setdefault(param, [at, at, no])[1] = at
                        wrote = True
                    except (PlanError, ValueError) as e:
                        self.err(no, "knob", f"{col}：{e}", what)
                note = r.get("说明", "")
                if note:
                    self.marks.append({"at": at, "clip": cid, "text": note, "line": no})
                elif not wrote:
                    self.hint(no, "empty_row", "这一行什么也没写", what)
            # 同一个旋钮写在几张表里：各管各的一段，两段不能叠着（首尾相接可以）
            zh = {v: k for k, v in PARAM_OF.items()}
            for param, (a, b, no) in span.items():
                for a0, b0, no0 in self.spans.setdefault((cid, param), []):
                    if a < b0 and a0 < b:
                        self.err(no, "lane_overlap", f"{zh[param]} 在 {fmt_pos(a)} – {fmt_pos(b)} 这一段，和第 {no0} 行那张表里的"
                                 f"（{fmt_pos(a0)} – {fmt_pos(b0)}）叠在一起了", what, "同一个旋钮同一段时间只写在一张表里")
                        break
                else:
                    self.spans[(cid, param)].append((a, b, no))
        # 「保持」写在第一个点上：值 = 片段的初始值
        clip = next((c for c in self.clips if c["id"] == cid), None)
        for (c, param), pts in self.lanes.items():
            if c != cid:
                continue
            pts.sort(key=lambda p: p["at"])
            cur = (clip or {}).get("init", {}).get(param, {"vol": 1.0}.get(param, 0.0))
            for p in pts:
                if p["value"] is None:
                    p["value"] = cur
                cur = p["value"]

    # ------------------------------------------------------------ Loop 与跳段

    def _moves(self, body: list[_Line], line: int) -> None:
        for t in self.tables(body, MOVE_COLS, line):
            for need in ("片段", "位置", "做什么"):
                if need not in t.cols:
                    self.err(t.line, "structure", f"Loop 与跳段的表少了「{need}」这一列")
                    break
            else:
                for no, r in t.rows:
                    self._move(no, r)

    def _move(self, no: int, r: dict[str, str]) -> None:
        cid = r["片段"]
        what = f"{cid} Loop / 跳段"
        if cid not in self.clip_ids:
            self.err(no, "move", f"片段表里没有 {cid}", what)
            return
        at = self.pos(r["位置"], no, what)
        if at is None:
            return
        self._in_scene(at, no, what)
        text = re.sub(r"\s+", " ", r["做什么"].strip())
        note = r.get("说明", "")
        n = len(self.loops) + len(self.jumps) + 1
        m = re.fullmatch(r"跳(?:到|回)?\s*(?:原曲|源)?\s*(?:第\s*)?([0-9.+/]+)\s*(?:小节)?", text)
        if m:
            to = self.pos(m.group(1), no, what)
            if to is not None:
                self.jumps.append({"id": f"J{n}", "clip": cid, "at": at, "to": to, "note": note, "line": no})
            return
        m = re.match(r"^(slip\s*)?loop\s*([0-9./]+\s*(?:拍|小节))\s*(.*)$", text, re.I)
        if not m:
            self.err(no, "move", f"做什么「{text}」看不懂", what,
                     "loop 写「loop 2小节，到 161.1 出」或「loop 8拍 × 4」；跳段写「跳到 97.1」（原曲的小节.拍）")
            return
        try:
            length = parse_dur(m.group(2))
        except PosError as e:
            self.err(no, "move", str(e), what)
            return
        rest = m.group(3)
        until: Fraction | None = None
        src: Fraction | None = None
        same = False
        mu = re.search(r"到\s*([0-9.+/]+)\s*(?:出|为止)?", rest)
        mt = re.search(r"[×xX*]\s*(\d+)\s*遍?|转\s*(\d+)\s*遍", rest)
        if mu:
            until = self.pos(mu.group(1), no, what)
            if until is None:
                return
        elif mt:
            times = int(mt.group(1) or mt.group(2))
            if times < 1:
                self.err(no, "move", "loop 至少转 1 遍", what)
                return
            until = at + length * times
        else:
            self.err(no, "move", f"「{text}」没写 loop 到哪出", what, "写「，到 161.1 出」或者「× 4」（转 4 遍）")
            return
        ms = re.search(r"从\s*(?:原曲|源)\s*([0-9.+/]+)", rest)
        if ms:
            src = self.pos(ms.group(1), no, what)
            if src is None:
                return
        if "同起点" in rest or "接上一个" in rest:
            same = True
        self.loops.append({"id": f"L{n}", "clip": cid, "at": at, "length": length, "until": until, "src": src,
                           "same_start": same, "slip": bool(m.group(1)), "note": note, "line": no})

    # ------------------------------------------------------------ 效果

    def _effects(self, body: list[_Line], line: int) -> None:
        for t in self.tables(body, FX_COLS, line):
            for need in ("ID", "类型", "目标", "开始", "结束"):
                if need not in t.cols:
                    self.err(t.line, "structure", f"效果表少了「{need}」这一列")
                    break
            else:
                for no, r in t.rows:
                    self._effect(no, r)

    def _effect(self, no: int, r: dict[str, str]) -> None:
        fid = r["ID"]
        what = f"效果 {fid}"
        kind = FX_KIND.get(_key(r["类型"]))
        if not kind:
            self.err(no, "fx", f"类型「{r['类型']}」不认识", what, "Delay / Echo / Reverb（发送）；Trans / Roll / Filter Echo / Spiral / Noise / "
                     "Flanger / Phaser / 采样 / Release（现场效果）")
            return
        if any(f["id"] == fid for f in self.fx):
            self.err(no, "fx", f"效果 {fid} 写了两遍", what)
            return
        route_t = _key(r.get("路由", ""))
        route = FX_ROUTE.get(route_t) if route_t else "send"
        if not route:
            self.err(no, "fx", f"路由「{r['路由']}」：写 发送 / 插入 / 组插入 / 总输出", what)
            return
        targets = [] if _key(r["目标"]) in ("总输出", "master", "") else [x.strip() for x in re.split(r"[+＋,，、]\s*", r["目标"]) if x.strip()]
        if kind == "sample":
            route = "master-insert"  # 采样直接进主输出；「目标」写的是音源（内置的名字或文件名），不是片段
        for tg in targets if kind != "sample" else []:
            if tg not in self.clip_ids:
                self.err(no, "fx", f"目标「{tg}」不在片段表里", what, "目标写片段的 ID（C3），几个片段写「C1+C3」")
                return
        start, end = self.pos(r["开始"], no, what), self.pos(r["结束"], no, what)
        if start is None or end is None:
            return
        self._in_scene(start, no, what)
        params: dict[str, Any] = {}
        send: list[dict[str, Any]] = []
        wet: list[dict[str, Any]] = []
        tail = None
        known = FX_PARAMS.get(kind, {})
        for part in re.split(r"[；;]\s*", r.get("参数", "")):
            part = part.strip()
            if not part or _dash(part):
                continue
            if part in ("回原声", "退场"):  # Release 的收尾可以只写这两个字
                part = "收尾 " + part
            mp = re.match(r"^(\S+?)\s*[:：]?\s+(.+)$", part) or re.match(r"^([^\d\s]+)\s*(.+)$", part)
            if not mp:
                self.err(no, "fx", f"参数「{part}」看不懂", what, "写「拍长 1/2拍；送出 1/3；反馈 0.7」，用分号隔开")
                return
            name, val = mp.group(1).strip(), mp.group(2).strip()
            try:
                if kind in LIVE_KINDS and name != "尾音":
                    # 现场效果：参数原样留着（中文名 + 写法），编译时按那种效果的表检查、换算（livefx.py）
                    params[name] = val
                    continue
                if name in ("送出", "返回"):
                    # 「送出 1/2」一直是它；「送出 0 → 1/2」在开着的这一段里匀速变过去；中间要停、要拐弯的，在值后面写位置：
                    # 「返回 0 → 1/2@144.4」= 到 144.4 走到 1/2，之后保持；「跳 0@146.1」= 那一拍一下到
                    pts = send if name == "送出" else wet
                    stages = [x for x in re.split(r"\s*(?:→|->)\s*", val) if x]
                    prev_at = start
                    for i, st in enumerate(stages):
                        ms = re.fullmatch(r"(跳\s*)?(.+?)(?:\s*[@＠]\s*([0-9.+/\s]+))?", st.strip())
                        if not ms:
                            raise ValueError(f"「{st}」看不懂")
                        if ms.group(3):
                            at_i = parse_pos(ms.group(3).strip())
                        elif i == 0:
                            at_i = start
                        elif i == len(stages) - 1:
                            at_i = end
                        else:
                            raise ValueError(f"中间的「{st}」要写到哪一拍为止（{st}@位置）")
                        if at_i < prev_at or at_i > end or (i == 0 and at_i != start):
                            raise ValueError(f"「{st}」的位置要在效果开着的这一段里、从前往后写")
                        prev_at = at_i
                        pts.append({"at": at_i, "value": _amount(ms.group(2)), "step": bool(ms.group(1)) or i == 0, "line": no})
                elif name == "尾音":
                    tail = parse_dur(val)
                elif name == "拍长":
                    params["beats"] = float(parse_dur(val))
                elif name == "反馈" and re.fullmatch(r"[+\-−]?\d+(\.\d+)?\s*d[bB]", val):
                    params["feedback"] = round(10 ** (float(re.sub(r"\s*d[bB]", "", val).replace("−", "-")) / 20), 4)
                elif name in FX_KEYS:
                    params[FX_KEYS[name]] = _amount(val) if FX_KEYS[name] != "beats" else float(parse_dur(val))
                elif known:
                    self.err(no, "fx", f"「{name}」不是这个效果的参数", what,
                             "有：" + " / ".join(["送出", "返回", "尾音"] + [zh for zh, en in FX_KEYS.items() if en in known]))
                    return
                else:
                    params[name] = val  # 现场效果（P1）的参数先原样留着，编译时按能力表报
            except (ValueError, PosError) as e:
                self.err(no, "fx", f"{name}：{e}", what)
                return
        self.fx.append({"id": fid, "kind": kind, "route": route, "targets": targets, "start": start, "end": end,
                        "params": params, "send": send, "wet": wet, "tail": tail, "note": r.get("说明", ""), "line": no})


def parse_text(text: str, name: str = "", file: str = "") -> ScoreResult:
    return _Parser(text, name or "score", file or (name + ".md" if name else "曲谱")).run()
