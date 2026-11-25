"""元素表：每份素材里各个声部（kick、bass、拍手、镲、合成器、人声…）在原曲的哪几小节有、有多满。

谱上写（SCORE_SPEC_V5.md 第 14 节），程序不分析声音——只管画：演出画面的 EQ 曲线底下每台 Deck 一张格子图，一行一个声部、
一格一小节，格子的高 = 原曲里有多满，亮灭 = 这个声部**过完旋钮以后**还剩多少（按它主要在哪个频段算；切掉的灭、放回来的亮）。
旋钮的线叠在格子上，所以「为什么在这里切低频 / 退高频」一眼看得出来：对面的 kick 亮了，这边的 kick 就该灭了。

只是给界面看的，不影响声音：引擎照谱执行，不看这个。频段的份额是画图用的约数（kick 不全在低频，切了 LOW 还剩两成，画面上算灭）。
"""

from __future__ import annotations

import re

# (程序里的名字, 画面上的名字, 谱上还能怎么写, (LOW, MID, HIGH) 各占多少)。先后 = 画面上从上到下的顺序。
# 这张表和 SCORE_SPEC_V5.md 第 14 节的表由 tests/test_v5_elements.py 比对：改一边要同时改另一边。
ELEMENTS: tuple[tuple[str, str, tuple[str, ...], tuple[float, float, float]], ...] = (
    ("kick", "kick", ("底鼓", "大鼓"), (0.80, 0.15, 0.05)),
    ("bass", "bass", ("贝斯", "低音", "sub"), (0.85, 0.15, 0.00)),
    ("clap", "拍手", ("clap", "军鼓", "snare"), (0.00, 0.60, 0.40)),
    ("hat", "闭镲", ("镲", "hat", "hihat", "hi-hat", "开镲", "踩镲"), (0.00, 0.10, 0.90)),
    ("ride", "ride", ("叮叮镲", "crash", "吊镲"), (0.00, 0.20, 0.80)),
    ("perc", "打击", ("perc", "打击乐", "通鼓", "tom"), (0.15, 0.55, 0.30)),
    ("synth", "合成器", ("synth", "旋律", "lead", "主音", "stab", "琶音", "arp"), (0.10, 0.65, 0.25)),
    ("pad", "铺底", ("pad", "氛围", "和弦", "chord"), (0.15, 0.65, 0.20)),
    ("vocal", "人声", ("vocal", "vox"), (0.05, 0.75, 0.20)),
    ("fx", "音效", ("fx", "噪声", "riser", "sweep"), (0.05, 0.35, 0.60)),
)
ORDER = tuple(e[0] for e in ELEMENTS)
ZH = {e[0]: e[1] for e in ELEMENTS}
WEIGHTS = {e[0]: e[3] for e in ELEMENTS}
_NAMES = {n.lower(): e[0] for e in ELEMENTS for n in (e[0], e[1], *e[2])}

# 有多满：谱上写这三个字（不写 = 满）；也可以写 0–1 的小数或百分数
LEVELS = {"满": 1.0, "中": 0.6, "弱": 0.3}
_LEVEL_WORDS = {**LEVELS, "强": 1.0, "全": 1.0, "半": 0.6, "轻": 0.3, "淡": 0.3}

_ITEM = re.compile(r"^(\d+)(?:\s*(?:[-–—~～]|到|至)\s*(\d+))?\s*(?:小节)?\s*(\S+)?$")


def canon(name: str) -> str | None:
    """谱上写的声部名 → 程序里的名字（不认识 = None）。"""
    return _NAMES.get(name.strip().lower().replace(" ", ""))


def known_names() -> str:
    return " / ".join(e[1] for e in ELEMENTS)


def level_of(word: str) -> float:
    w = word.strip()
    if w in _LEVEL_WORDS:
        return _LEVEL_WORDS[w]
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(%|％)?", w)
    if not m:
        raise ValueError(f"「{word}」不是有多满的写法（满 / 中 / 弱，或者 0–1 的小数、百分数）")
    v = float(m.group(1)) / (100.0 if m.group(2) else 1.0)
    if not 0.0 < v <= 1.0:
        raise ValueError(f"有多满要在 0 到 1 之间（写的是 {word}）；没有这个声部的小节不用写")
    return v


def parse_spans(text: str) -> list[tuple[int, int, float]]:
    """「1–64 满，81–192 中」→ [(0, 64, 1.0), (80, 192, 0.6)]：原曲的小节，从 0 数、不含末尾。两头都算（1–64 = 第 1 到第 64 小节）。
    一项里不写有多满 = 满。几项之间用逗号 / 顿号 / 分号隔开。"""
    out: list[tuple[int, int, float]] = []
    for item in re.split(r"[，,、；;]+", text):
        item = item.strip()
        if not item:
            continue
        m = _ITEM.match(item)
        if not m:
            raise ValueError(f"「{item}」看不懂：写「从第几–到第几 有多满」，比如「17–64 满」「81–96 弱」「33」")
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        if a < 1 or b < a:
            raise ValueError(f"「{item}」：小节从 1 数，后一个数不能比前一个小")
        lv = level_of(m.group(3)) if m.group(3) else 1.0
        out.append((a - 1, b, lv))
    out.sort()
    for (a0, a1, _), (b0, _b1, _l) in zip(out, out[1:]):
        if b0 < a1:
            raise ValueError(f"第 {b0 + 1} 小节写了两遍（{a0 + 1}–{a1} 和 {b0 + 1}–…）")
    return out


def write_spans(spans: list[tuple[int, int, float]]) -> str:
    """parse_spans 的反过来（存下来的项目另存成谱时用）。"""
    words = {v: k for k, v in LEVELS.items()}
    out = []
    for a, b, lv in spans:
        rng = f"{a + 1}–{b}" if b > a + 1 else f"{a + 1}"
        out.append(f"{rng} {words.get(lv) or f'{lv:g}'}")
    return "，".join(out)
