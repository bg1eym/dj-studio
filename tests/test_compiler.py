import math

import pytest
from pydantic import ValidationError

from djstudio.compiler import _lane_native, compile_set, fader_gain, to_native
from djstudio.grid import Grid
from djstudio.setfile import SetFile


def make_set(**kw):
    d = {
        "schema": "djset/2",
        "id": "t1",
        "tracks": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
        "transitions": [
            {"technique": "longhand", "start": 97, "b_in": 1},
            {"technique": "instant", "start": 113, "b_in": 33},
        ],
    }
    d.update(kw)
    return SetFile.model_validate(d)


def test_grid_bar_math():
    g = Grid(bpm=120, first_beat=0.5, downbeat=1, duration=200)
    assert g.bar_beat(1) == 1
    assert g.bar_time(1) == pytest.approx(1.0)
    assert g.bar_time(2) == pytest.approx(3.0)
    assert g.time_bar(g.bar_time(17.5)) == pytest.approx(17.5)


def test_units():
    assert to_native("lo", 0) == 1.0
    assert to_native("lo", -60) == 0.0
    assert to_native("hi", 12) == pytest.approx(3.98107, abs=1e-4)
    assert to_native("filter", -1) == 0.0
    assert to_native("filter", 0.6) == pytest.approx(0.8)


def test_step_points_expand_to_two():
    pts = _lane_native("B.vol", [[4, 0.7, "step"], [8, 0.7], [9, 1.0]], 4)
    g07 = fader_gain(0.7)
    assert pts[0] == [16.0, 0.0] and pts[1] == [16.0, pytest.approx(g07, abs=1e-4)]
    assert pts[-1] == [36.0, 1.0]
    # 推子按位置线性推上去（逐拍重采样成增益），不是增益直线
    mid = next(v for x, v in pts if x == 34.0)
    assert mid == pytest.approx(fader_gain(0.85), abs=1e-4)


def test_fader_position_is_mixxx_taper():
    """vol = 推子位置；换成 Mixxx 的 volume 值用它自己的 ControlAudioTaperPot(-20, 0, 1) 曲线。"""
    assert fader_gain(0) == 0 and fader_gain(1) == 1
    assert 20 * math.log10(fader_gain(0.5) * 0.9 + 0.1) == pytest.approx(-10, abs=1e-6)
    assert to_native("vol", 0.7) == pytest.approx(fader_gain(0.7), abs=1e-5)


def test_db_lane_resampled_per_beat():
    pts = _lane_native("A.lo", [[0, 0], [2, -15]], 0)
    assert len(pts) == 9  # 0..8 拍
    assert pts[0] == [0.0, 1.0]
    assert pts[-1][1] == pytest.approx(10 ** (-15 / 20), abs=1e-4)


def test_compile_ok(resolver):
    c = compile_set(make_set(), resolver)
    assert c.ok, [str(i) for i in c.issues]
    p1, p2 = c.programs
    assert p1["a"]["deck"] == 1 and p1["b"]["deck"] == 2
    assert p2["a"]["deck"] == 2 and p2["b"]["deck"] == 1
    assert p1["a"]["start"] == 96 * 4  # 第 97 小节 = 第 384 拍（downbeat 0）
    assert p1["len"] == 48 * 4 and p1["play"] == 0
    assert p2["play"] == 16 and p2["len"] == 20
    # 下一首 c 的 downbeat=1
    assert p2["b"]["in"] == 32 * 4 + 1
    # longhand 默认把主控 BPM 从 128 渐变到 130
    assert p1["bpm"][0][1] == 128 and p1["bpm"][-1][1] == 130
    assert c.ending and c.ending["a"]["deck"] == 1
    kinds = [s.kind for s in c.schedule]
    assert kinds == ["solo", "transition", "solo", "transition", "ending"]


def test_overlapping_transitions_rejected(resolver):
    # 过渡 1 让 b 在 48 小节后到第 49 小节；过渡 2 从第 40 小节开始 → 冲突
    s = make_set(transitions=[
        {"technique": "longhand", "start": 97, "b_in": 1},
        {"technique": "instant", "start": 40, "b_in": 33},
    ])
    c = compile_set(s, resolver)
    assert not c.ok
    assert any("上一段" in i.msg for i in c.issues)


def test_transition_past_track_end(resolver):
    # a 360 秒 @128 = 192 小节
    s = make_set(transitions=[
        {"technique": "longhand", "start": 170, "b_in": 1},
        {"technique": "instant", "start": 113, "b_in": 33},
    ])
    c = compile_set(s, resolver)
    assert any("A 只有" in i.msg for i in c.issues)


def test_bad_lane_name():
    with pytest.raises(ValidationError):
        make_set(transitions=[
            {"technique": "custom", "start": 97, "bars": 16, "play_at": 0, "lanes": {"A.bass": [[0, 0]]}},
            {"technique": "instant", "start": 113},
        ])


def test_custom_requires_bars(resolver):
    s = make_set(transitions=[
        {"technique": "custom", "start": 97},
        {"technique": "instant", "start": 113, "b_in": 33},
    ])
    c = compile_set(s, resolver)
    assert any("custom" in i.msg for i in c.issues)


def test_custom_lanes_override(resolver):
    s = make_set(transitions=[
        {"technique": "custom", "start": 97, "bars": 16, "play_at": 0,
         "lanes": {"B.lo": [[0, -60], [8, -60], [8, 0, "step"]], "A.lo": [[8, 0], [8, -60, "step"]]}},
        {"technique": "instant", "start": 113 - 32, "b_in": 33},
    ])
    c = compile_set(s, resolver)
    assert c.ok, [str(i) for i in c.issues]
    p = c.programs[0]
    assert p["lanes"]["B.lo"][0] == [0.0, 0.0]
    assert [32.0, 1.0] in p["lanes"]["B.lo"]
    assert p["events"] == [[64.0, "A.stop"]]
    assert p["lanes"]["B.vol"] == [[0.0, 0.0], [0.0, 1.0]]


def test_unresolved_track(resolver):
    s = make_set(tracks=[{"id": "a"}, {"id": "zzz"}, {"id": "c"}])
    c = compile_set(s, resolver)
    assert not c.ok and "找不到" in c.issues[0].msg
