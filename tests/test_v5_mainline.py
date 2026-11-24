"""主线和工具、主要的交接（djstudio/v5/mainline.py）：四台 Deck 同时放时，界面和 hot cue 表要分清哪些片段是「这一首歌」，
哪些是加在上面的工具（循环、一句人声），主线上一首接一首的地方才是主要的交接。只是分类，不影响声音。"""

from __future__ import annotations

import pytest

from djstudio.v5.compile import chart, compile_project
from djstudio.v5.hotcue import asset_cues
from djstudio.v5.mainline import handovers, is_main, roles_of
from djstudio.v5.score import parse

from .test_v5_score import EXAMPLE, resolve


@pytest.fixture(scope="module")
def c():
    return compile_project(parse(EXAMPLE).project, resolve)


def test_main_line_follows_the_roles_in_the_score(c):
    """角色是 主曲 / 进曲 / 出曲 的片段是主线；节奏层、声线是工具。场景里给这台 Deck 写的角色也算。"""
    kind = {cc.clip.id: is_main(c, cc) for cc in c.clips}
    assert kind == {"C1": True, "C2": True, "C6": True, "C3": False, "C4": False, "C5": False, "C7": False}
    assert set(roles_of(c, c.clip("C1"))) == {"主曲", "出曲"}  # 片段表里是主曲，场景 3 里它是出曲；「待命」不算
    assert roles_of(c, c.clip("C3")) == ["节奏层"]


def test_handovers_are_where_one_main_track_takes_over_from_the_last(c):
    """交接 = 下一首主线进来到上一首交完；换低频的那一拍 = 下一首的 LOW 从切着放进来的那一拍。"""
    hs = handovers(c)
    assert [(h["n"], h["from"], h["to"], h["from_deck"], h["to_deck"]) for h in hs] == [(1, "C1", "C2", 1, 2), (2, "C2", "C6", 2, 1)]
    assert [(h["start"], h["end"], h["swap"]) for h in hs] == [(320.0, 512.0, 384.0), (704.0, 896.0, 768.0)]  # 81.1–129.1 换在 97.1；177.1–225.1 换在 193.1
    ch = chart(c)
    assert ch["handovers"] == hs and {k["id"]: k["main"] for k in ch["clips"]}["C3"] is False


def test_without_roles_the_shape_of_the_clip_decides(c):
    """谱上哪里都没写角色：直着放 16 小节以上、LOW 不是一直切着的算主线；只转 loop 的、很短的、LOW 从头切到尾的算工具。"""
    p = c.project.model_copy(deep=True)
    for k in p.clips:
        k.role = ""
    for sc in p.scenes:
        sc.roles = {}
    c2 = compile_project(p, resolve)
    assert {cc.clip.id: is_main(c2, cc) for cc in c2.clips} == {"C1": True, "C2": True, "C6": True, "C3": False, "C4": False, "C5": False, "C7": False}
    assert [(h["from"], h["to"]) for h in handovers(c2)] == [("C1", "C2"), ("C2", "C6")]


def test_out_pad_sits_where_the_handover_begins(c):
    """出曲的 OUT 垫（红）= 交接开始时它读到的地方；进曲的 IN 注释里写接的是谁、哪一拍换低频。工具的片段没有 OUT。"""
    cues = asset_cues(c)
    a1 = {h.name: h for h in cues["A1"]}
    assert a1["OUT"].bar == 81.0 and a1["OUT"].color == "#FF3B3B" and "交接 1" in a1["OUT"].note and "97.1 换低频" in a1["OUT"].note
    a2 = {h.name: h for h in cues["A2"]}
    assert a2["OUT"].bar == 97.0 and "交接 1：接 C1" in a2["IN"].note and "交接 2" in a2["OUT"].note
    assert all(h.name != "OUT" for a in ("A3", "A4") for h in cues[a])
