"""旧谱适配器（A07）：v1–v4 的 set 转成全场时间线以后，和旧编译器的结果逐点等价；转出来的计划在调度器里放得完。"""
import shutil
from pathlib import Path

import pytest

from djstudio.compiler import TrackInfo, compile_set
from djstudio.grid import Grid
from djstudio.plan import mdplan
from djstudio.plan.convert import convert
from djstudio.setfile import SetFile
from djstudio.v5 import legacy
from djstudio.v5.compile import compile_project
from djstudio.v5.link import SIM_DIR
from djstudio.v5.score import parse_text
from djstudio.v5.writer import write

from .test_score import TEMPLATE, TEMPLATE_LIB, lib
from .v5util import Run

HAVE_CXX = (SIM_DIR / "sim5.cpp").is_file() and bool(shutil.which("c++") or shutil.which("g++") or shutil.which("clang++"))


def both(s: SetFile, infos: dict[str, TrackInfo]):
    """旧编译 + 转换 + 新编译。"""
    old = compile_set(s, lambda st: infos.get(st.id))
    assert old.ok, [str(i) for i in old.issues]
    p = legacy.from_set(s, old)
    table = legacy.asset_infos(old)
    new = compile_project(p, lambda a: table.get(a.id))
    return old, p, new


def template():
    tracks = lib()
    plan = mdplan.parse(TEMPLATE)
    conv = convert(plan, tracks, set_id="tpl")
    assert conv.ok, conv.notes
    return conv.set, {t.id: TrackInfo(t.id, t.title, t.grid) for t in tracks}


def test_score_template_is_equivalent_after_conversion():
    """规范里的 v4 模板谱（3 首、2 段过渡、换速、效果、淡出）：转换前后逐点一样。"""
    s, infos = template()
    old, p, new = both(s, infos)
    assert new.ok, [str(i) for i in new.issues]
    assert legacy.check(old, new) == []
    # 结构：一首一个片段，Deck 1 / 2 交替；一段过渡一个场景；A / B 的读法留着
    assert [(k.id, k.deck) for k in p.clips] == [("C1", 1), ("C2", 2), ("C3", 1)]
    assert [sc.no for sc in p.scenes] == [1, 2] and p.scenes[0].roles == {"D1": "出曲", "D2": "进曲"}
    assert [t["a"] for t in p.legacy["transitions"]] == ["C1", "C2"]
    assert new.duration == pytest.approx(old.duration, rel=0.01)


def infos3():
    return {"a": TrackInfo("a", "a", Grid(128.0, 0.10, 0, 360.0)), "b": TrackInfo("b", "b", Grid(130.0, 0.25, 0, 400.0)),
            "c": TrackInfo("c", "c", Grid(132.0, 0.05, 1, 300.0))}


def set3(**over) -> SetFile:
    """三种手法各一段：长交接（模板展开的曲线、reverb 尾巴、换速）、瞬时切换（真空、echo、一拍换速）、结尾淡出。"""
    d = {"schema": "djset/2", "id": "three",
         "tracks": [{"id": "a", "title": "A", "start": 9}, {"id": "b", "title": "B", "play_bpm": 131}, {"id": "c", "title": "C"}],
         "transitions": [{"technique": "longhand", "start": 97, "b_in": 5, "bars": 48},
                         {"technique": "instant", "start": 93, "b_in": 33}],
         "ending": {"mode": "fade", "bar": 81, "bars": 8}}
    d.update(over)
    return SetFile.model_validate(d)


def test_longhand_instant_and_fade_are_equivalent():
    old, p, new = both(set3(), infos3())
    assert new.ok, [str(i) for i in new.issues]
    assert legacy.check(old, new) == []
    # 长交接里 A 的 reverb 尾巴、瞬时切换前的 echo 都成了各自的效果实例
    assert sorted(f["kind"] for f in new.fx) == ["echo", "reverb"]
    # 结尾：旧引擎是增益直线降到 0——新曲线每一拍都在那条直线上
    c3 = new.plan["clips"][2]
    L = 32
    pts = dict(c3["curves"]["vol"])
    for k in (0, 8, 16, 24, 32):
        assert pts[c3["stop"] - L + k] == pytest.approx(1 - k / L, abs=1e-4)


def test_custom_transition_with_loops_and_solo_moves_is_equivalent():
    """逐小节旋钮表的过渡（跳变、过渡里的 loop）+ 独奏时的跳段和 loop。"""
    s = set3(tracks=[{"id": "a", "title": "A", "moves": [{"bar": 33, "to": 49}, {"bar": 65, "loop": 4, "times": 3}]},
                     {"id": "b", "title": "B", "moves": [{"bar": 60, "to": 44}]}, {"id": "c", "title": "C"}],
             transitions=[
                 {"technique": "custom", "start": 97, "b_in": 1, "play_at": 8, "bars": 40,
                  "params": {"a_end": "stop"},
                  "lanes": {"A.lo": [[23, 0.0], [24, -25.0, "step"]], "A.hi": [[8, 0.0], [40, -6.0]],
                            "A.filter": [[32, 0.0], [39, 0.6]], "A.vol": [[36, 1.0], [40, 0.3]],
                            "B.vol": [[8, 0.5, "step"], [23, 0.5], [24, 1.0, "step"]],
                            "B.lo": [[8, -25.0], [23, -25.0], [24, 0.0, "step"]], "B.hi": [[8, -2.0], [24, 0.0]]},
                  "events": [[32, "A.loop:8"], [16, "B.loop:16"], [24, "B.loop_off"]],
                  "tempo": [[0, 128], [16, 128], [24, 130]]},
                 {"technique": "instant", "start": 93, "b_in": 33}])
    old, p, new = both(s, infos3())
    assert new.ok, [str(i) for i in new.issues]
    assert legacy.check(old, new) == []
    c1, c2 = new.clip("C1"), new.clip("C2")
    # A 独奏：33 → 49 跳过 16 小节，65 起 4 小节转 3 遍（多放 8 小节）；过渡从 A 的第 97 小节 = 全场第 (97-1-16+8)×4 拍
    G = (96 - 16 + 8) * 4
    assert p.legacy["transitions"][0]["start"] == G
    assert c1.src_beat_at(32 * 4) == 48 * 4 and c1.src_beat_at(G) == 96 * 4
    # 过渡里 B 第 16 小节开的 4 小节 loop 转了 2 遍：过渡结束时 B 少走 4 小节……第 24 小节出
    assert [s_.kind for s_ in c2.segs] == ["start", "loop", "exit", "jump"]
    assert c2.src_beat_at(G + 24 * 4) == (1 - 1 + 16 - 8 + 4) * 4
    # A 在过渡第 32 小节开的 loop 一直转到停
    assert c1.segs[-2].kind == "loop" and float(c1.segs[-1].g) == G + 160 == c1.stop


def test_broken_conversion_is_caught_by_the_check():
    """检查是真的在比：把新计划里的一条曲线、B 的起播改掉，都报得出来。"""
    old, p, new = both(set3(), infos3())
    k = new.plan["clips"][1]
    k["curves"]["lo"] = [[x, y * 0.5] for x, y in k["curves"]["lo"]]
    k["start"] += 4
    d = legacy.check(old, new)
    assert any("B.lo" in x for x in d) and any("B 起播" in x for x in d)


def test_save_as_v5_and_read_back():
    """另存为 v5：转出来的项目写成 .md，再读回来编出的计划一样（原来的谱不动）。"""
    s, infos = template()
    old, p, new = both(s, infos)
    md = write(p)
    assert md.startswith("# ") and "- 规范：v5" in md and "## 场景 1" in md
    r = parse_text(md, "tpl")
    assert r.project is not None and not r.errors, [str(i) for i in r.issues]
    table = legacy.asset_infos(old)
    again = compile_project(r.project.model_copy(update={"legacy": p.legacy}), lambda a: table.get(a.id))
    assert again.ok, [str(i) for i in again.issues]
    for a, b in zip(new.plan["clips"], again.plan["clips"]):
        assert a["init"] == b["init"] and a["start"] == b["start"] and a["stop"] == b["stop"]
        assert a["curves"].keys() == b["curves"].keys()
        for prm in a["curves"]:  # 写成谱上的数再读回来：推子位置到小数点后 4 位
            assert [x for x, _ in a["curves"][prm]] == [x for x, _ in b["curves"][prm]]
            assert [y for _, y in a["curves"][prm]] == pytest.approx([y for _, y in b["curves"][prm]], abs=2e-4)
        assert [(x["g"], x["src"], x["loop"]) for x in a["segs"]] == [(x["g"], x["src"], x["loop"]) for x in b["segs"]]
    assert new.plan["tempo"] == again.plan["tempo"]


@pytest.mark.skipif(not HAVE_CXX, reason="没有 C++ 编译器（模拟器 sim5 要现编）")
def test_converted_set_plays_through_on_the_scheduler():
    """转出来的旧谱在新的调度器里从头放到尾：每首都按时起播、停下，没有晚到 / 失效的动作。"""
    old, p, new = both(set3(), infos3())
    with Run(new, speed=60) as r:
        r.start()
        r.wait_state("done", 120)
        rc = r.rc()
        assert [x["st"] for x in rc if x["id"].endswith(".start")] == ["ok", "ok", "ok"]
        assert [x["id"] for x in rc if x["id"].endswith(".stop")] == ["C1.stop", "C2.stop", "C3.stop"]
        assert not [x for x in rc if x["st"] != "ok"], [x for x in rc if x["st"] != "ok"]


def test_template_lib_is_shared():
    assert len(TEMPLATE_LIB) == 3 and Path(TEMPLATE).exists()
