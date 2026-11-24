"""曲谱 v5 的 hot cue（djstudio/v5/hotcue.py）：每份素材在这套谱里要用到的点，放进打击垫 1–8。
名字、颜色、放不下时先留谁，用的是 docs/SET_SPEC.md 第 7 节那一套（setspec）。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from djstudio.setfile import HotCue
from djstudio.setspec import KIND
from djstudio.v5 import hotcue
from djstudio.v5.compile import chart, compile_project
from djstudio.v5.hotcue import FX_NAME, Point, asset_cues, clip_points, cue_sheet_md
from djstudio.v5.score import parse

from .test_v5_score import EXAMPLE, resolve

DOCS = Path(__file__).resolve().parents[1] / "docs"


@pytest.fixture(scope="module")
def c():
    return compile_project(parse(EXAMPLE).project, resolve)


def pads(cues):
    return [(h.pad, h.name, h.bar) for h in cues]


def test_example_pads_follow_what_each_asset_does_in_this_score(c):
    """示例谱：每份素材的垫子 = 它在这套谱里要用到的点，按在原曲上的先后从 1 号垫排；颜色按种类。"""
    cues = asset_cues(c)
    # A1 主曲：起播、Filter 扫上去 / 回中、人声那几小节让 MID、交接开始（OUT）、换低频、最后的 loop（在圈里停，不另放 STOP）
    assert pads(cues["A1"]) == [(1, "IN", 1.0), (2, "HPF", 25.0), (3, "FILTER OFF", 33.0), (4, "MID DOWN", 48.0), (5, "MID UP", 53.0),
                                (6, "OUT", 81.0), (7, "BASS OUT", 97.0), (8, "LOOP 32", 113.0)]  # 九个点留八个：让掉的是一个旋钮点（HIGH UP）
    assert "在圈里停" in cues["A1"][7].note and "MID DOWN" in cues["A1"][7].note  # 同一拍开始拧的旋钮写进圈头那个垫的注释
    # A2 进曲：全场 81.1 进来的是它自己的第 1 小节；换低频在它的第 17 小节
    a2 = {h.name: h for h in cues["A2"]}
    assert a2["IN"].bar == 1.0 and a2["BASS IN"].bar == 17.0 and a2["STOP"].bar == 145.0 and a2["TRANS"].bar == 121.0
    assert "LOW 7点" in a2["IN"].note and "全场 81.1" in a2["IN"].note
    # A3 是 loop 用的素材：起播和圈头在同一个地方，只占一个垫子（两个片段都用它）
    assert pads(cues["A3"]) == [(1, "IN", 1.0)] and "LOOP 16" in cues["A3"][0].note and "C5" in cues["A3"][0].note
    # A4 一句人声放两次（C4 从第 5 小节、C7 从第 9 小节）：两个片段的点合在一起；C4 停的地方就是 C7 起播的地方，留要按的那个
    assert pads(cues["A4"]) == [(1, "IN", 5.0), (2, "ECHO", 8.0), (3, "IN", 9.0), (4, "DELAY", 12.0), (5, "STOP", 13.0)]
    assert "STOP" in cues["A4"][2].note and "C4" in cues["A4"][2].note
    for hs in cues.values():
        assert [h.pad for h in hs] == list(range(1, len(hs) + 1)) and all(h.auto for h in hs)
        assert [h.bar for h in hs] == sorted(h.bar for h in hs)
        assert all(h.name.isascii() and len(h.name) <= 16 and h.bar == round(h.bar * 4) / 4 for h in hs)  # 英文、16 个字符以内、落在整拍上
    col = {h.name: h.color for hs in cues.values() for h in hs}
    assert col["IN"] == KIND["in"].color and col["STOP"] == KIND["stop"].color and col["LOOP 32"] == KIND["loop"].color
    assert col["OUT"] == KIND["out"].color
    assert col["BASS OUT"] == KIND["move"].color and col["ECHO"] == KIND["fx"].color == col["TRANS"]


def test_more_points_than_pads_keeps_the_ones_you_press(c, monkeypatch):
    """点比垫子多：先留 IN / 跳段落点 / 圈头 / STOP，再留效果和换低频，别的旋钮点按先后；留下的还是按位置排。"""
    def many(_c, cc):
        if cc.clip.id != "C1":
            return []
        moves = [Point(float(b), "move", f"MID UP {b}", "", "C1", b * 4.0) for b in (5, 9, 13, 17, 21, 25, 29)]
        return ([Point(1.0, "in", "IN", "", "C1", 0.0), Point(41.0, "jump", "JUMP 41", "", "C1", 100.0), Point(33.0, "loop", "LOOP 8", "", "C1", 90.0),
                 Point(61.0, "stop", "STOP", "", "C1", 400.0), Point(45.0, "fx", "ECHO", "", "C1", 200.0),
                 Point(37.0, "move", "BASS OUT", "", "C1", 150.0)] + moves)

    monkeypatch.setattr(hotcue, "clip_points", many)
    got = [h.name for h in asset_cues(c)["A1"]]
    assert got == ["IN", "MID UP 5", "MID UP 9", "LOOP 8", "BASS OUT", "JUMP 41", "ECHO", "STOP"]


def test_pads_already_on_the_asset_are_left_alone(c):
    """素材上本来就有的垫子（旧 set 带过来的 / 手写的）不动；它们标过的位置、同名的点不再放，自动的只进空着的垫子。"""
    p = c.project.model_copy(deep=True)
    a1 = p.asset("A1")
    a1.hot_cues = [HotCue(pad=1, name="DROP", bar=33.0, color="#FF8C1A", note="手写的"), HotCue(pad=4, name="IN", bar=3.0, note="自己定的进点")]
    cues = asset_cues(compile_project(p, resolve))["A1"]
    by_pad = {h.pad: h for h in cues}
    assert (by_pad[1].name, by_pad[1].bar, by_pad[1].auto) == ("DROP", 33.0, False) and (by_pad[4].name, by_pad[4].bar) == ("IN", 3.0)
    auto = [h for h in cues if h.auto]
    assert all(h.pad not in (1, 4) for h in auto) and len(cues) == 8
    assert "IN" not in [h.name for h in auto]  # 手写了 IN：自动的 IN 让给它
    assert 33.0 not in [h.bar for h in auto]  # 第 33 小节已经有垫子了（不管叫什么）


def test_points_of_one_clip(c):
    """一个片段的点：起播时不在 12 点的旋钮写在 IN 的注释里；发送效果关掉时另有「… OFF」。"""
    pts = {p.name: p for p in clip_points(c, c.clip("C4"))}
    assert set(pts) == {"IN", "ECHO", "STOP"} and pts["IN"].bar == 5.0 and pts["STOP"].bar == 9.0
    assert "推子 8" in pts["IN"].note and "LOW 7点" in pts["IN"].note and pts["ECHO"].kind == "fx"
    c2 = [p.name for p in clip_points(c, c.clip("C2"))]
    assert c2[0] == "IN" and c2[-1] == "STOP" and "REVERB" in c2 and "TRANS" in c2


def test_cue_sheet_and_chart(c):
    """Hot Cue 表（给 Engine DJ / Prime 4+ 照着设）：每份素材一张表，位置 = 原曲的小节.拍和离文件开头的时间；图谱里同一份数据。"""
    md = cue_sheet_md(c)
    assert md.startswith("# 四 Deck 场景示例 — Hot Cue 表") and "## A4 Vocal Phrase" in md
    row = next(ln for ln in md.splitlines() if ln.startswith("| 1 | IN |") and "C4" in ln)
    t = c.clip("C4").info.beat_time(16.0)  # A4 的第 5 小节 = 它的第 16 拍
    assert f"| 5.1 | {int(t // 60)}:{t % 60:06.3f} |" in row and "绿 #22D45A" in row
    ch = chart(c)
    a4 = next(a for a in ch["assets"] if a["id"] == "A4")
    assert [(h["pad"], h["name"], h["bar"]) for h in a4["hotcues"]] == [(1, "IN", 5.0), (2, "ECHO", 8.0), (3, "IN", 9.0), (4, "DELAY", 12.0), (5, "STOP", 13.0)]
    assert a4["bars"] > 16 and a4["dur"] > 0 and a4["track_id"] == c.clip("C4").info.track_id
    assert a4["t0"] == pytest.approx(c.clip("C4").info.beat_time(0.0), abs=1e-3)


def test_spec_lists_the_effect_names():
    """曲谱规范 v5 里「Hot cue」那一节的效果名字表和代码一致。"""
    text = (DOCS / "SCORE_SPEC_V5.md").read_text(encoding="utf-8")
    sec = text[text.index("## 12 Hot cue"):]
    rows = dict(re.findall(r"^\| `(\w+)` \| ([A-Z ]+?) \|", sec, re.M))
    assert rows == FX_NAME
