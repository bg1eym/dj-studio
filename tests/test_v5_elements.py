"""元素表（SCORE_SPEC_V5.md 第 14 节）：谱上写每份素材的各个声部在原曲哪几小节有、有多满；程序读进来放进图谱，画面照着画格子。
规范里的声部表和代码（elements.ELEMENTS）在这里比对：改一边要同时改另一边。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from djstudio.v5 import elements as el
from djstudio.v5 import score
from djstudio.v5.writer import write

DOCS = Path(__file__).resolve().parents[1] / "docs"

HEAD = """# 元素测试

- 规范：v5
- 主速：130

## 素材

| ID | 曲名 | 文件名 | 原 BPM | 第 1 小节（秒） | 小节数 |
|---|---|---|---|---|---|
| A1 | One | one.mp3 | 130 | 0.1 | 64 |
| A2 | Two | two.mp3 | 130 | 0.1 | 64 |

## 片段

| ID | 素材 | Deck | 原曲起点 | 全场起播 | 全场停止 |
|---|---|---|---|---|---|
| C1 | A1 | D1 | 1.1 | 1.1 | 33.1 |
| C2 | A2 | D2 | 1.1 | 17.1 | 49.1 |
"""


def parse(extra: str):
    return score.parse_text(HEAD + extra, "t", "t.md")


def test_spec_table_matches_the_code():
    """规范第 14 节的声部表 = elements.ELEMENTS：名字、别的写法、三个频段的份额、先后顺序。"""
    text = (DOCS / "SCORE_SPEC_V5.md").read_text(encoding="utf-8")
    sec = text[text.index("## 14 元素表"):]
    rows = re.findall(r"^\| ([^|]+) \| ([^|]+) \| ([0-9.]+) \| ([0-9.]+) \| ([0-9.]+) \|$", sec, re.M)
    assert [r[0] for r in rows] == [e[1] for e in el.ELEMENTS]
    for (zh, alias, lo, mid, hi), (_id, zh2, al2, w) in zip(rows, el.ELEMENTS):
        assert zh == zh2 and tuple(alias.split("、")) == al2
        assert (float(lo), float(mid), float(hi)) == w
        assert sum(w) == pytest.approx(1.0)  # 三段的份额加起来是 1：全放过去 = 原来有多满
    for _id, zh, alias, _w in el.ELEMENTS:  # 每种写法都认得，大小写、空格不管
        assert all(el.canon(n) == _id for n in (zh, _id, *alias, zh.upper(), f" {alias[0]} "))
    assert el.LEVELS == {"满": 1.0, "中": 0.6, "弱": 0.3} and "满 / 中 / 弱" in sec.replace("`", "")


def test_spans_are_source_bars_inclusive_with_levels():
    assert el.parse_spans("25–72，97-168 中、200 弱; 210~212 45%") == [(24, 72, 1.0), (96, 168, 0.6), (199, 200, 0.3), (209, 212, 0.45)]
    assert el.parse_spans(el.write_spans([(0, 64, 1.0), (80, 81, 0.3), (90, 100, 0.8)])) == [(0, 64, 1.0), (80, 81, 0.3), (90, 100, 0.8)]
    for bad in ("5-3", "0-4", "a-b", "1-8 很满", "1-8, 8-9", "3 0", "3 150%"):
        with pytest.raises(ValueError):
            el.parse_spans(bad)


def test_score_reads_one_table_or_one_table_per_asset():
    r = parse("""
## 元素

| 素材 | 元素 | 原曲小节 |
|---|---|---|
| A1 | 底鼓 | 9–64 |
| A1 | hi-hat | 1–64 中 |

### A2 元素

| 元素 | 原曲小节 | 说明 |
|---|---|---|
| 人声 | 17–24，33 弱 | 一句 |
| bass | — | 这首没有低音 |
""")
    assert [i for i in r.issues] == []
    a1, a2 = r.project.assets
    assert [(e.name, e.spans) for e in a1.elements] == [("kick", [(8, 64, 1.0)]), ("hat", [(0, 64, 0.6)])]  # 顺序按画面上从上到下
    assert [(e.name, e.spans) for e in a2.elements] == [("vocal", [(16, 24, 1.0), (32, 33, 0.3)])]
    again = score.parse_text(write(r.project), "t", "t.md")  # 另存成谱再读回来是同一份
    assert again.issues == [] and [a.elements for a in again.project.assets] == [a.elements for a in r.project.assets]


def test_missing_and_wrong_element_tables_are_reported():
    """没写 = 提示（能演出，画面上没有格子）；写错 = 错误，带行号和怎么改。"""
    none = parse("")
    assert [(i.level, i.code) for i in none.issues] == [("hint", "elements")] and "这份谱没写元素表" in none.issues[0].msg
    part = parse("\n## 元素\n\n| 素材 | 元素 | 原曲小节 |\n|---|---|---|\n| A1 | kick | 1–64 |\n")
    assert [(i.level, i.code) for i in part.issues] == [("hint", "elements")] and "素材 A2" in part.issues[0].msg

    def errors(rows: str) -> list[str]:
        r = parse("\n## 元素\n\n| 素材 | 元素 | 原曲小节 |\n|---|---|---|\n| A2 | kick | 1–64 |\n" + rows)
        return [i.msg for i in r.issues if i.level == "error"]

    assert "不认识的元素「口哨」" in errors("| A1 | 口哨 | 1–8 |\n")[0]
    assert "写了两行" in errors("| A1 | kick | 1–8 |\n| A1 | 底鼓 | 9–16 |\n")[0]
    assert "不在素材表里" in errors("| A9 | kick | 1–8 |\n")[0]
    assert "看不懂" in errors("| A1 | kick | 从头到尾 |\n")[0]
    far = parse("\n## 元素\n\n| 素材 | 元素 | 原曲小节 |\n|---|---|---|\n| A1 | kick | 1–80 |\n| A2 | kick | 1–64 |\n")
    assert [(i.level, i.code) for i in far.issues] == [("hint", "element_range")]  # 写过了曲尾：提示


def test_example_chart_carries_the_element_rows():
    """示例谱：图谱里每份素材带着它的元素表，另有一份「这套谱里出现过的声部」（画面上格子图的行，从上到下）。"""
    from djstudio.v5.compile import chart, compile_project

    from .test_v5_score import EXAMPLE, resolve

    r = score.parse(EXAMPLE)
    assert r.issues == [] and all(a.elements for a in r.project.assets)
    ch = chart(compile_project(r.project, resolve))
    a2 = next(a for a in ch["assets"] if a["id"] == "A2")
    assert a2["elements"]["kick"] == [[24, 72, 1.0], [96, 168, 1.0]] and a2["elements"]["bass"][0] == [24, 72, 0.6]
    rows = ch["elements"]
    assert [e["id"] for e in rows] == [n for n in el.ORDER if any(n in a["elements"] for a in ch["assets"])]
    assert rows[0] == {"id": "kick", "name": "kick", "w": [0.8, 0.15, 0.05]}
