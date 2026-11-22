"""引擎能力表：编译时按它检查谱要的东西引擎有没有（Deck 数、效果、路由、同时几个效果实例 …），
没有的指出具体的场景、动作和行号，不会悄悄略过。编译结果绑定能力签名，换了引擎 / 设备要重新校验。

BUILTIN = 内置声音引擎的 v5 调度器（tools/mixxx-engine/djs5/engine5.hpp 里的常数；测试里和引擎自己报的 v5.caps 比对）。
MIXXX = 连本机 Mixxx 的模式：只有两个 Deck、没有原生调度，只能放 v1–v4 的旧谱。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class Caps:
    name: str = "builtin"
    protocol: int = 1
    plan: str = "djplan/1"
    max_decks: int = 4
    fx: dict[str, int] = field(default_factory=lambda: {"delay": 1, "echo": 1, "reverb": 1})  # 效果 → 算法版本
    fx_slots: int = 8  # 同时最多几个发送效果实例（尾音也算）
    routes: tuple[str, ...] = ("send",)
    slip_loop: bool = False
    sampler: int = 0  # 采样器同时几个声音
    sample_rate: int = 44100
    buffer_frames: int = 512
    gate: str = "sample"  # 起播 / 停止落在哪：采样
    deck_control: str = "buffer"  # deck 的控件（loop 开关、跳段、EQ）落在哪：音频块边界
    keylock_compensated: bool = False
    restore_max_beats: int = 256  # 定位到中间时最多往回静音预运行多少拍
    score_versions: tuple[str, ...] = ("1", "2", "3", "4", "5")

    @classmethod
    def from_engine(cls, r: dict[str, Any], name: str = "builtin") -> "Caps":
        """引擎 / 模拟器对 v5.caps 的回答 → 能力表。"""
        timing = r.get("timing") or {}
        return cls(name=name, protocol=int(r.get("protocol", 0)), plan=str(r.get("plan", "")),
                   max_decks=int(r.get("max_decks", 2)), fx={k: int(v) for k, v in (r.get("fx") or {}).items()},
                   fx_slots=int(r.get("fx_slots", 0)), routes=tuple(r.get("routes") or ()),
                   slip_loop=bool(r.get("slip_loop")), sampler=int(r.get("sampler", 0)),
                   sample_rate=int(r.get("sample_rate", 44100)), buffer_frames=int(r.get("buffer_frames", 512)),
                   gate=str(timing.get("gate", "buffer")), deck_control=str(timing.get("deck_control", "buffer")),
                   keylock_compensated=bool(r.get("keylock_compensated")),
                   restore_max_beats=int((r.get("restore") or {}).get("max_beats", 0)))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["routes"] = list(self.routes)
        d["score_versions"] = list(self.score_versions)
        d["signature"] = self.signature()
        return d

    def signature(self) -> str:
        """能力签名：能力表里影响执行的每一项（名字不算）。"""
        d = asdict(self)
        d.pop("name")
        return hashlib.sha256(json.dumps(d, sort_keys=True, default=list).encode()).hexdigest()[:12]


BUILTIN = Caps()
MIXXX = Caps(name="mixxx", protocol=0, plan="", max_decks=2, fx={}, fx_slots=0, routes=(), gate="buffer",
             restore_max_beats=0, score_versions=("1", "2", "3", "4"))
