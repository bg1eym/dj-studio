"""djset/3：四 Deck 编排的项目模型（JSON，人读单位）。

和 djset/2（一串「第 k 首 → 第 k+1 首」的过渡）不同，这里没有「第几首」：
- 素材（Asset）= 原音频文件 + 拍网格；
- 片段（Clip）= 这份素材某一次在某台 Deck 上的演出：从原曲哪里读起、全场第几拍起播、第几拍停。
  同一份素材可以用很多次、装进不同的 Deck，每个片段有自己的 ID；自动化和效果都认片段 ID，
  所以换曲以后旧动作不会落到新曲上；
- 场景（Scene）= 整场里的一段，写清它的音乐目的（作用、保留什么、谁接管前景、LOW 交接条件、MID/HIGH 为什么动或不动）；
- 全场速度（tempo）= 唯一的一张速度图，所有 Deck 都跟它；
- 参数（Lane）、Loop、跳段（Jump）、效果（Fx）都按「全场位置」写，任何时候都能写——独奏的时候一样能动每个旋钮。

单位和 djset/2 的曲线一样（编译器 compiler.to_native 同一套换算）：推子 = 位置 0..1，EQ = dB，Filter = -1..1
（负 = 低通，正 = 高通）。位置 / 时值在 JSON 里是字符串（「129.1」「16.4+1/2」「8拍」），见 pos.py。
"""
from __future__ import annotations

import json
import re
from fractions import Fraction
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..setfile import HotCue
from .pos import Dur, Pos

SCHEMA = "djset/3"
SCORE_VERSION = "5"
MAX_DECKS = 4
PARAMS = ("vol", "lo", "mid", "hi", "filter")
# 参数的人读范围（同 setfile.LANE_RANGES）
PARAM_RANGES: dict[str, tuple[float, float]] = {
    "vol": (0.0, 1.0), "lo": (-60.0, 12.0), "mid": (-60.0, 12.0), "hi": (-60.0, 12.0), "filter": (-1.0, 1.0),
}
PARAM_NEUTRAL = {"vol": 1.0, "lo": 0.0, "mid": 0.0, "hi": 0.0, "filter": 0.0}

# 效果：发送类（DJ 常用的 Delay / Echo / Reverb，P0）和现场效果（P1）。路由见 docs/SCORE_SPEC_V5.md 第 8 节
FX_SEND = ("delay", "echo", "reverb")
FX_INSERT = ("trans", "roll", "filter_echo", "spiral", "noise", "flanger", "phaser", "filter")
FX_OTHER = ("sample", "release")
FX_KINDS = FX_SEND + FX_INSERT + FX_OTHER
ROUTES = ("send", "insert", "group-insert", "master-insert")
ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]{0,23}$")


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


def _check_id(v: str, what: str) -> str:
    if not ID_RE.match(v):
        raise ValueError(f"{what} 的 ID {v!r}：字母开头，只用英文、数字、下划线（比如 A1、C12、F3）")
    return v


class Asset(_M):
    """素材：一个原音频文件和它的拍网格。file 是相对路径（相对于谱所在的文件夹），不改原文件。"""

    id: str
    title: str = ""
    artist: str = ""
    file: str = ""
    bpm: float = Field(gt=40, lt=250)  # 原 BPM（谱上写的；网格以曲库里核对过的为准）
    bar1_s: float | None = None  # 第 1 小节第 1 拍离文件开头的秒数
    bars: int | None = Field(None, ge=1)
    key: str = ""
    track_id: str = ""  # 曲库里对上的那首（导入时填）
    sha256: str = ""  # 内容哈希（第一次确认后记下，换了内容要重新确认）
    hot_cues: list[HotCue] = Field(default_factory=list)
    line: int = 0  # 谱里的行号（报错、图谱追溯用）

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        return _check_id(v, "素材")


class TempoPoint(_M):
    """全场速度图上的一个点。ramp = True：从上一个点匀速变到这里（对全场拍线性）；False：在这一拍一下换过去。"""

    at: Pos
    bpm: float = Field(gt=40, lt=250)
    ramp: bool = False
    line: int = 0


class Init(_M):
    """片段起播时各旋钮的位置（装好以后、起播以前就摆好，像真机上先把 LOW 拧掉再推推子）。"""

    vol: float = Field(1.0, ge=0.0, le=1.0)
    lo: float = Field(0.0, ge=-60.0, le=12.0)
    mid: float = Field(0.0, ge=-60.0, le=12.0)
    hi: float = Field(0.0, ge=-60.0, le=12.0)
    filter: float = Field(0.0, ge=-1.0, le=1.0)


class Clip(_M):
    id: str
    asset: str
    deck: int = Field(ge=1, le=MAX_DECKS)
    src: Pos = Field(default_factory=lambda: Fraction(0))  # 原曲起点（这首自己的「小节.拍」）
    start: Pos  # 全场起播
    stop: Pos | None = None  # 全场停止；None = 播到曲尾
    prep: Dur | None = None  # 起播前多久开始装（不写 = 编译器按秒数给）
    role: str = ""  # 主曲 / 出曲 / 进曲 / 节奏层 / 声线 …（随场景变的在 Scene.roles 里）
    init: Init = Field(default_factory=Init)
    keylock: bool = False
    note: str = ""
    line: int = 0

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        return _check_id(v, "片段")


class Point(_M):
    """参数曲线上的一个点。step = False：从上一个点走直线到这里（斜线）；True：在这一拍一下跳到这个值（竖线）。
    两个点之间值不变 = 保持（水平线）。v5 只认显式写的类型，不按变化大小猜。"""

    at: Pos
    value: float
    step: bool = False
    line: int = 0


class Lane(_M):
    """一个片段的一个旋钮在全场时间上的变化。片段起播时的值是 Clip.init；第一个点如果是斜线，就从起播那一拍走过来。"""

    clip: str
    param: Literal["vol", "lo", "mid", "hi", "filter"]
    points: list[Point] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> "Lane":
        lo, hi = PARAM_RANGES[self.param]
        prev = None
        for p in self.points:
            if not lo <= p.value <= hi:
                raise ValueError(f"{self.clip} 的 {self.param} 值 {p.value} 超出范围 [{lo}, {hi}]")
            if prev is not None and p.at < prev:
                raise ValueError(f"{self.clip} 的 {self.param}：点没有按位置递增")
            prev = p.at
        return self


class Loop(_M):
    """普通 loop：从全场 at 起，这个片段在原曲的一段里转（全场照常往前走），到 until 出，出来以后从实际读到的地方接着放。
    src = None：就地转（at 那一刻读到哪就从哪开始的一段）；写了 src：先跳到原曲那里再转。
    slip = True（出来回到「一直往前走的虚拟播放头」）P0 不支持，带着它的谱会被拒绝。"""

    id: str = ""
    clip: str
    at: Pos
    length: Dur
    until: Pos
    src: Pos | None = None
    same_start: bool = False  # 「同起点」：还从上一个 loop 的开头转（缩短 loop）
    slip: bool = False
    note: str = ""
    line: int = 0


class Jump(_M):
    """跳段：全场 at 那一拍，这个片段跳到原曲的 to。只改这个片段读到哪，全场位置和别的 Deck 不变。"""

    id: str = ""
    clip: str
    at: Pos
    to: Pos
    note: str = ""
    line: int = 0


class Fx(_M):
    """一个效果。kind + route 决定它是什么、声音怎么走：
    - send（辅助发送）：片段的推子后信号另送一份进效果，返回里只有效果声；原通道的干声照常出一次。Delay / Echo / Reverb。
    - insert / group-insert / master-insert（插入）：目标的声音整个经过效果，干声不再另走一遍（P1）。
    send / wet = 送出量 / 返回量随时间的变化（0..1）。end 之后送出关掉，尾音留 tail 这么久。"""

    id: str
    kind: str
    route: Literal["send", "insert", "group-insert", "master-insert"] = "send"
    targets: list[str] = Field(default_factory=list)  # 片段 ID；master-insert 不写
    start: Pos
    end: Pos
    params: dict[str, Any] = Field(default_factory=dict)
    send: list[Point] = Field(default_factory=list)
    wet: list[Point] = Field(default_factory=list)
    tail: Dur | None = None
    version: int = 0  # 保存时用的效果版本（0 = 没记，按当前的）
    note: str = ""
    line: int = 0

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        return _check_id(v, "效果")

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        if v not in FX_KINDS:
            raise ValueError(f"不认识的效果 {v!r}（认得的：{' / '.join(FX_KINDS)}）")
        return v


class Scene(_M):
    """场景：整场里的一段。正式场景要写五项说明；草稿（draft）可以空着，界面上显示「待补充」。"""

    no: int = Field(ge=1)
    name: str = ""
    start: Pos
    end: Pos
    purpose: str = ""  # 作用：在整场里干什么
    keep: str = ""  # 保留：哪些声部留着
    lead: str = ""  # 前景：谁接管前景
    low: str = ""  # LOW：低频怎么交接 / 不交接
    midhigh: str = ""  # MID/HIGH：动哪些、为什么（或者为什么不动）
    roles: dict[str, str] = Field(default_factory=dict)  # "D1" → 角色
    intent: list[str] = Field(default_factory=list)  # 标成「有意安排」的提示（multi_low / multi_vocal / multi_mid）
    draft: bool = False
    note: str = ""
    line: int = 0

    def missing(self) -> list[str]:
        names = {"purpose": "作用", "keep": "保留", "lead": "前景", "low": "LOW", "midhigh": "MID/HIGH"}
        return [zh for k, zh in names.items() if not getattr(self, k).strip()]


class Mark(_M):
    """动作提示：谱上「说明」列写的字（演出页的「接下来」、PDF 里的标注都用它）。"""

    at: Pos
    clip: str = ""
    text: str
    line: int = 0


class Project(_M):
    schema_: Literal["djset/3"] = Field(SCHEMA, alias="schema")
    spec: str = SCORE_VERSION
    id: str = Field(pattern=r"^[A-Za-z0-9_\-]+$")
    name: str = ""
    source: str = ""  # 谱的文件名
    note: str = ""
    tempo: list[TempoPoint] = Field(min_length=1)
    assets: list[Asset]
    clips: list[Clip]
    lanes: list[Lane] = Field(default_factory=list)
    loops: list[Loop] = Field(default_factory=list)
    jumps: list[Jump] = Field(default_factory=list)
    fx: list[Fx] = Field(default_factory=list)
    scenes: list[Scene] = Field(default_factory=list)
    marks: list[Mark] = Field(default_factory=list)
    # 从 v1–v4 转来的：记下原来的 set 和每一段过渡在全场的位置（A / B 的读法、图上的出曲蓝 / 进曲橙靠它）
    legacy: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _refs(self) -> "Project":
        def unique(items: list[Any], what: str) -> set[str]:
            ids = [x.id for x in items if x.id]
            dup = sorted({i for i in ids if ids.count(i) > 1})
            if dup:
                raise ValueError(f"{what} 的 ID 重复：{', '.join(dup)}")
            return set(ids)

        assets = unique(self.assets, "素材")
        clips = unique(self.clips, "片段")
        unique(self.fx, "效果")
        for c in self.clips:
            if c.asset not in assets:
                raise ValueError(f"片段 {c.id} 用的素材 {c.asset} 不在素材表里")
        for kind, items in (("参数", self.lanes), ("Loop", self.loops), ("跳段", self.jumps)):
            for x in items:
                if x.clip not in clips:
                    raise ValueError(f"{kind}写的片段 {x.clip} 不在片段表里")
        for f in self.fx:
            for t in f.targets:
                if t not in clips:
                    raise ValueError(f"效果 {f.id} 的目标 {t} 不在片段表里")
        seen: set[tuple[str, str]] = set()
        for ln in self.lanes:
            if (ln.clip, ln.param) in seen:
                raise ValueError(f"{ln.clip} 的 {ln.param} 有两条曲线（同一个旋钮只能有一条）")
            seen.add((ln.clip, ln.param))
        if self.tempo[0].at != 0:
            raise ValueError("速度图的第一个点要在 1.1")
        for a, b in zip(self.tempo, self.tempo[1:]):
            if b.at < a.at:
                raise ValueError("速度图的点没有按位置递增")
        return self

    def asset(self, aid: str) -> Asset:
        return next(a for a in self.assets if a.id == aid)

    def clip(self, cid: str) -> Clip:
        return next(c for c in self.clips if c.id == cid)

    def to_json(self) -> str:
        return json.dumps(self.model_dump(by_alias=True), ensure_ascii=False, indent=2)


def load_project(path: str | Path) -> Project:
    return Project.model_validate_json(Path(path).read_text(encoding="utf-8"))


def save_project(p: Project, path: str | Path) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(p.to_json() + "\n", encoding="utf-8")
    tmp.replace(out)
