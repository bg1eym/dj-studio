"""位置和时值。

位置是字符串「小节.拍」，细分拍写「16.4+1/2」（第 16 小节第 4 拍再过半拍）——不是小数，不按十进制解析。
全场第 1 小节第 1 拍 = 「1.1」= 内部的第 0 拍；内部一律是从 0 起的有理数拍（Fraction），换成秒只在最后一步做。
首期只有 4/4。时值必须带单位：「8拍」「2小节」「1/2拍」（半拍就是半拍，不会被当成半小节）。
"""
from __future__ import annotations

import re
from fractions import Fraction
from typing import Annotated, Any

from pydantic import BeforeValidator, PlainSerializer

BEATS_PER_BAR = 4


class PosError(ValueError):
    pass


_POS = re.compile(r"^(\d+)(?:\.(\d+))?(?:\+(\d+)/(\d+))?$")
_DUR = re.compile(r"^(\d+/\d+|\d+(?:\.\d+)?)(拍|小节|beats?|bars?)$", re.I)


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", str(text)).replace("＋", "+").replace("／", "/").replace("．", ".")


def parse_pos(text: Any) -> Fraction:
    """「129」「129.1」「16.4+1/2」→ 从 0 起的拍。只写小节 = 第 1 拍。"""
    if isinstance(text, Fraction):
        return text
    if isinstance(text, bool) or isinstance(text, (int, float)):
        raise PosError(f"位置要写成「小节.拍」的字符串（比如 \"129.1\"），不是数 {text!r}")
    t = _squash(text)
    m = _POS.match(t)
    if not m:
        raise PosError(f"看不懂的位置「{text}」：写「小节.拍」（129.1 = 第 129 小节第 1 拍），细分拍写「16.4+1/2」")
    bar, beat = int(m.group(1)), int(m.group(2) or 1)
    if bar < 1:
        raise PosError(f"位置「{text}」：小节从 1 数起")
    if not 1 <= beat <= BEATS_PER_BAR:
        raise PosError(f"位置「{text}」：拍只能是 1–{BEATS_PER_BAR}（4/4）")
    frac = Fraction(0)
    if m.group(3):
        if int(m.group(4)) == 0:
            raise PosError(f"位置「{text}」：分母不能是 0")
        frac = Fraction(int(m.group(3)), int(m.group(4)))
        if not 0 < frac < 1:
            raise PosError(f"位置「{text}」：「+」后面是不满一拍的部分（1/2、1/4、3/4 …）")
    return Fraction((bar - 1) * BEATS_PER_BAR + beat - 1) + frac


def fmt_pos(beats: Fraction | int | float) -> str:
    """拍 → 「小节.拍」（有零头时「小节.拍+a/b」）。"""
    b = beats if isinstance(beats, Fraction) else Fraction(beats).limit_denominator(960)
    bar, rest = divmod(b, BEATS_PER_BAR)
    beat, frac = divmod(rest, 1)
    s = f"{int(bar) + 1}.{int(beat) + 1}"
    if frac:
        s += f"+{frac.numerator}/{frac.denominator}"
    return s


def parse_dur(text: Any) -> Fraction:
    """「8拍」「2小节」「1/2拍」「1.5小节」→ 拍数。不带单位的不认。"""
    if isinstance(text, Fraction):
        return text
    if isinstance(text, bool) or isinstance(text, (int, float)):
        raise PosError(f"时值要带单位（「8拍」「2小节」「1/2拍」），不是数 {text!r}")
    t = _squash(text)
    m = _DUR.match(t)
    if not m:
        raise PosError(f"看不懂的时值「{text}」：要带单位，写「8拍」「2小节」「1/2拍」")
    num, unit = m.group(1), m.group(2).lower()
    try:
        v = Fraction(num)
    except (ValueError, ZeroDivisionError) as e:
        raise PosError(f"看不懂的时值「{text}」") from e
    if unit in ("小节", "bar", "bars"):
        v *= BEATS_PER_BAR
    if v <= 0:
        raise PosError(f"时值「{text}」要大于 0")
    return v


def fmt_dur(beats: Fraction | int | float) -> str:
    b = beats if isinstance(beats, Fraction) else Fraction(beats).limit_denominator(960)
    if b.denominator == 1 and b % BEATS_PER_BAR == 0:
        return f"{int(b // BEATS_PER_BAR)}小节"
    if b.denominator == 1:
        return f"{int(b)}拍"
    return f"{b.numerator}/{b.denominator}拍"


def bar_of(beats: Fraction | float) -> float:
    """拍 → 小节号（小数；画图、日志用）。"""
    return float(beats) / BEATS_PER_BAR + 1


# pydantic 里的位置 / 时值：JSON 里是字符串，Python 里是 Fraction
Pos = Annotated[Fraction, BeforeValidator(parse_pos), PlainSerializer(fmt_pos, return_type=str)]
Dur = Annotated[Fraction, BeforeValidator(parse_dur), PlainSerializer(fmt_dur, return_type=str)]
