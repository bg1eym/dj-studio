"""djset/2：一套 set 的唯一真源（JSON，人和 Claude 都能直接写）。

结构见 docs/SET_FORMAT.md。要点：
- 全部以「小节」为单位（1 起），单位是人读单位：音量 0..1、EQ dB、滤波 -1..1（负 LPF / 正 HPF）、
  echo/reverb 湿度 0..1。编译器（compiler.py）再换算成引擎用的拍与 Mixxx 原生值。
- ``transitions[i]`` 是 ``tracks[i] → tracks[i+1]`` 的过渡，长度必须是 len(tracks)-1。
- 一段过渡有自己的时间线（timeline），以 A 曲的 ``start`` 小节为时间线第 0 小节；
  B 在时间线 ``play_at`` 小节从自己的 ``b_in`` 小节起播。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA = "djset/2"

TECHNIQUES = ("longhand", "instant", "custom")
LANE_PARAMS = ("vol", "lo", "mid", "hi", "filter", "echo", "reverb")
EVENT_RE = re.compile(r"^(A|B)\.(stop|loop:(\d+(\.\d+)?)|loop_off)$")

# 每种参数的人读取值范围
LANE_RANGES: dict[str, tuple[float, float]] = {
    "vol": (0.0, 1.0),
    "lo": (-60.0, 12.0),
    "mid": (-60.0, 12.0),
    "hi": (-60.0, 12.0),
    "filter": (-1.0, 1.0),
    "echo": (0.0, 1.0),
    "reverb": (0.0, 1.0),
}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


class HotCue(_Model):
    """Denon Prime 4+（Engine DJ）的 hot cue：打击垫 1–8，名字显示在 Prime 屏幕上（ASCII，≤ 16 个字符），
    颜色在 Engine DJ 里设；位置用本应用的小节号（可以带拍：33.25 = 第 33 小节第 2 拍）。垫子没有固定用途，见 docs/SET_SPEC.md。"""

    pad: int = Field(ge=1, le=8)
    name: str = Field(min_length=1, max_length=16)
    bar: float = Field(ge=1)
    color: str = "#FFFFFF"
    note: str = ""  # 中文注释：这个点用来干什么
    auto: bool = False  # 按 set 自动放的（setspec.fill_hot_cues 重新填时重算）；False = 谱上 / 手写的，不动

    @field_validator("name")
    @classmethod
    def _ascii(cls, v: str) -> str:
        if not v.isascii():
            raise ValueError(f"hot cue 名字 {v!r} 只能用英文 / 数字（Prime 4+ 屏幕上显示），中文写在 note 里")
        return v

    @field_validator("color")
    @classmethod
    def _color(cls, v: str) -> str:
        if not HEX_COLOR_RE.match(v):
            raise ValueError(f"hot cue 颜色 {v!r} 应为 #RRGGBB")
        return v.upper()


class SavedLoop(_Model):
    """Prime 4+ 的保存 loop（loop 模式的打击垫 1–8）：从 bar 起 beats 拍。"""

    pad: int = Field(ge=1, le=8)
    name: str = Field(min_length=1, max_length=16)
    bar: float = Field(ge=1)
    beats: float = Field(gt=0, le=64)
    note: str = ""
    auto: bool = False  # 按过渡里的 loop 事件自动放的


class SetTrack(_Model):
    id: str = ""  # 曲库 track id（library.store 里的 id）
    title: str = ""
    artist: str = ""
    file: str | None = None  # 备选引用：托管曲库内的相对路径
    start: float = 1.0  # 作为开场曲时从第几小节起播
    downbeat: int | None = Field(None, ge=0, le=3)  # 覆盖曲库里的 downbeat
    play_bpm: float | None = Field(None, gt=40, lt=250)  # 独奏时的主控 BPM；None = 原生
    cues: dict[str, float] = Field(default_factory=dict)  # 命名小节标记（drop/break/out…），仅展示与引用
    hot_cues: list[HotCue] = Field(default_factory=list)  # Prime 4+ 的 hot cue（≤ 8 个，打击垫不重复），见 docs/SET_SPEC.md
    main_cue: float | None = None  # Prime 4+ 的主 Cue 点（CUE 键）：小节号；规范里 = 进点
    loops: list[SavedLoop] = Field(default_factory=list)  # Prime 4+ 的保存 loop（可选）
    plan_bar_offset: int | None = None  # 谱（Engine DJ）上第 b 小节 = 本应用第 b + 这个数 小节；cue 表里同时写两种编号
    note: str = ""

    @field_validator("hot_cues", "loops")
    @classmethod
    def _unique_pads(cls, v: list[Any]) -> list[Any]:
        pads = [c.pad for c in v]
        if len(pads) != len(set(pads)):
            raise ValueError(f"打击垫号重复：{pads}")
        return sorted(v, key=lambda c: c.pad)


Point = list[Any]  # [bar, value] 或 [bar, value, "step"]


class Transition(_Model):
    technique: Literal["longhand", "instant", "custom"] = "longhand"
    start: float  # A 曲小节：时间线第 0 小节
    b_in: float = 1.0  # B 起播时所在的 B 曲小节
    play_at: float | None = None  # B 在时间线第几小节起播；None = 手法默认
    bars: float | None = None  # 时间线总长（小节）；None = 手法默认
    params: dict[str, Any] = Field(default_factory=dict)
    lanes: dict[str, list[Point]] = Field(default_factory=dict)  # 覆盖/追加的自动化曲线（整条替换）
    events: list[Point] = Field(default_factory=list)  # [bar, "A.stop"] / [bar, "A.loop:8"] …
    tempo: list[Point] | None = None  # 主控 BPM 曲线 [[bar, bpm], ...]；None = 默认
    label: str = ""  # 显示用的手法名（打碟谱生成的 custom 过渡写「长交接」/「瞬时切换」）
    note: str = ""
    marks: list[Point] = Field(default_factory=list)  # 谱上的「动作」：[时间线小节, "文字"]，写进那一刻的 hot cue 注释

    @field_validator("marks")
    @classmethod
    def _check_marks(cls, v: list[Point]) -> list[Point]:
        for p in v:
            if not isinstance(p, (list, tuple)) or len(p) != 2 or not isinstance(p[1], str):
                raise ValueError(f"marks 的每一项应为 [小节, \"文字\"]：{p!r}")
            float(p[0])
        return v

    @field_validator("lanes")
    @classmethod
    def _check_lanes(cls, v: dict[str, list[Point]]) -> dict[str, list[Point]]:
        for name, pts in v.items():
            deck, _, param = name.partition(".")
            if deck not in ("A", "B") or param not in LANE_PARAMS:
                raise ValueError(f"未知的自动化通道 {name!r}（应为 A./B. + {'/'.join(LANE_PARAMS)}）")
            lo, hi = LANE_RANGES[param]
            for p in pts:
                _check_point(p, name)
                if not (lo <= float(p[1]) <= hi):
                    raise ValueError(f"{name} 的值 {p[1]} 超出范围 [{lo}, {hi}]")
        return v

    @field_validator("events")
    @classmethod
    def _check_events(cls, v: list[Point]) -> list[Point]:
        for p in v:
            if len(p) != 2 or not isinstance(p[1], str) or not EVENT_RE.match(p[1]):
                raise ValueError(f"事件格式错误：{p!r}（例：[48, \"A.stop\"]、[40, \"A.loop:8\"]）")
            float(p[0])
        return v

    @field_validator("tempo")
    @classmethod
    def _check_tempo(cls, v: list[Point] | None) -> list[Point] | None:
        for p in v or []:
            _check_point(p, "tempo")
            if not (40 < float(p[1]) < 250):
                raise ValueError(f"tempo 的 BPM {p[1]} 不合理")
        return v


def _check_point(p: Any, name: str) -> None:
    if not isinstance(p, (list, tuple)) or len(p) not in (2, 3):
        raise ValueError(f"{name} 的点格式错误：{p!r}（应为 [bar, value] 或 [bar, value, \"step\"]）")
    float(p[0])
    float(p[1])
    if len(p) == 3 and p[2] not in ("step", "lin"):
        raise ValueError(f"{name} 的插值方式只能是 step / lin：{p!r}")


class Ending(_Model):
    mode: Literal["fade", "playout"] = "fade"
    bar: float | None = None  # 终曲从第几小节开始淡出；None = 曲末前 bars 小节
    bars: float = 8.0


class SetFile(_Model):
    schema_: Literal["djset/2"] = Field(SCHEMA, alias="schema")
    spec: str = ""  # 按哪一版播放规范生成（docs/SET_SPEC.md，现在是 "1"）；手写的 set 可以空着
    id: str = Field(pattern=r"^[A-Za-z0-9_\-]+$")
    name: str = ""
    created: str = ""
    source: str = ""  # 编排方案出处
    note: str = ""
    # 变速时保持音高。默认关：Mixxx 的时间拉伸会让非原速的 deck 晚 17–29ms，叠 kick 时听得出（见 MIXXX_NOTES）
    keylock: bool = False
    tracks: list[SetTrack]
    transitions: list[Transition]
    ending: Ending = Field(default_factory=Ending)

    def to_json(self) -> str:
        return json.dumps(self.model_dump(by_alias=True, exclude_none=False), ensure_ascii=False, indent=2)


def load_set(path: str | Path) -> SetFile:
    return SetFile.model_validate_json(Path(path).read_text(encoding="utf-8"))


def save_set(s: SetFile, path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(s.to_json() + "\n", encoding="utf-8")
    tmp.replace(p)
