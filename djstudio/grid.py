"""拍网格与小节换算。

约定（全项目统一）：
- 网格来自 Mixxx（它的 sync / quantize 都基于自己的网格，我们只读不改）：
  ``first_beat``（秒）是网格第 0 拍的位置，``bpm`` 是网格 BPM（恒定网格）。
- ``downbeat``：网格上第几拍（0..3）是一小节的第 1 拍。Mixxx 不知道小节线，
  由我们的分析给出初值，用户在网页上纠正。
- 小节号从 1 开始，与编排方案 / 打碟谱一致：第 1 小节从网格第 ``downbeat`` 拍开始。
  小节号可以是小数（64.5 = 第 64 小节的后半）。
- 拍号（beat）是网格上的绝对拍序号，可为负、可为小数。
"""
from __future__ import annotations

from dataclasses import dataclass

BEATS_PER_BAR = 4


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

    def bar_beat(self, bar: float) -> float:
        return (bar - 1) * BEATS_PER_BAR + self.downbeat

    def beat_bar(self, beat: float) -> float:
        return (beat - self.downbeat) / BEATS_PER_BAR + 1

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
