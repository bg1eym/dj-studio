"""拍网格与小节换算。

约定（全项目统一）：
- 网格来自 Mixxx（它的 sync / quantize 都基于自己的网格，我们只读不改）：
  ``first_beat``（秒）是网格第 0 拍的位置，``bpm`` 是网格 BPM（恒定网格）。
- ``downbeat``（0..3）：小节相位——网格拍号 k 满足 k ≡ downbeat (mod 4) 的拍是小节第 1 拍。
  Mixxx 不知道小节线，由我们的分析给出初值，用户在网页上纠正。
- 小节号从 1 开始，与编排方案 / 打碟谱一致：第 1 小节 = 曲中最早的那个小节起点
  （网格向前延伸，起点时间 ≥ -50ms 的第一个小节线）。Mixxx 的「第 0 拍」若不在曲首也不影响编号。
  小节号可以是小数（64.5 = 第 64 小节的后半）。
- 拍号（beat）是网格上的绝对拍序号（第 0 拍 = Mixxx 的 first beat），可为负、可为小数。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

BEATS_PER_BAR = 4
BAR1_TOL = 0.05  # 秒


@dataclass(frozen=True)
class Grid:
    bpm: float
    first_beat: float = 0.0  # 秒
    downbeat: int = 0
    duration: float = 0.0  # 秒；0 = 未知

    @property
    def beat_len(self) -> float:
        return 60.0 / self.bpm

    def beat_time(self, beat: float) -> float:
        return self.first_beat + beat * self.beat_len

    def time_beat(self, t: float) -> float:
        return (t - self.first_beat) / self.beat_len

    @property
    def bar1_beat(self) -> int:
        """第 1 小节起点的网格拍号。"""
        k = math.ceil(self.time_beat(-BAR1_TOL))
        k += (self.downbeat - k) % BEATS_PER_BAR
        return k

    def bar_beat(self, bar: float) -> float:
        return (bar - 1) * BEATS_PER_BAR + self.bar1_beat

    def beat_bar(self, beat: float) -> float:
        return (beat - self.bar1_beat) / BEATS_PER_BAR + 1

    def bar_time(self, bar: float) -> float:
        return self.beat_time(self.bar_beat(bar))

    def time_bar(self, t: float) -> float:
        return self.beat_bar(self.time_beat(t))

    @property
    def last_bar(self) -> float:
        """曲末对应的小节号（小数）。"""
        return self.time_bar(self.duration) if self.duration else 0.0


def fmt_time(t: float) -> str:
    sign = "-" if t < 0 else ""
    t = abs(t)
    return f"{sign}{int(t // 60)}:{t % 60:05.2f}"
