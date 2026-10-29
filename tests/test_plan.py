"""打碟谱 → set：钟点位换算、小节对齐、跳变、换速、特效、曲目匹配。

真 PDF 的解析测试需要用户的打碟谱：设置 DJSTUDIO_PLAN_PDF=<路径> 才跑（PDF 不进仓库）。
"""
import os
from pathlib import Path

import pytest

from djstudio.grid import Grid
from djstudio.plan.cdw import Knobs, Plan, PlanEffect, PlanError, PlanRow, PlanTrack, PlanTransition, clock, fader
from djstudio.plan.convert import LibTrack, align, convert, duration_ok, eq_db, file_matches, filter_val, match_tracks
from djstudio.setfile import SetFile


def test_clock_positions():
    assert clock("12点") == 0 and clock("关") == 0
    assert clock("11点半") == -0.5 and clock("10点") == -2 and clock("7点(切)") == -5
    assert clock("5点") == 5 and clock("1点") == 1
    assert clock("左9点", True) == -3 and clock("左10点半", True) == -1.5
    assert clock("右3点", True) == 3 and clock("右5点", True) == 5 and clock("右1点半", True) == 1.5
    assert fader("5") == 5 and fader("10") == 10
    with pytest.raises(PlanError):
        clock("13点")
    with pytest.raises(PlanError):
        fader("11")


def test_units_match_plan_conventions():
    assert eq_db(0) == 0
    assert eq_db(-5) == -25  # 7 点 = 全切（谱：-25 dB）
    assert eq_db(5) == 6  # 5 点 = 满（+6 dB）
    assert eq_db(-2) == pytest.approx(-6, abs=0.1)  # 10 点 =「给到一半」
    assert eq_db(-1) > eq_db(-2) > eq_db(-3) > eq_db(-4) > eq_db(-5)
    assert filter_val(-3) == -0.6 and filter_val(3) == 0.6  # 左 9 点 / 右 3 点 = 切一半


def K(lo=0, mid=0, hi=0, f=0, v=10):
    return Knobs(lo, mid, hi, f, v)


def lib_track(id, bpm, first_beat, downbeat, dur, name, sections=()):
    return LibTrack(id, f"/lib/{name}", name, "", dur, Grid(bpm, first_beat, downbeat, dur), list(sections))


def make_plan():
    """两首：A 128 BPM，第 1 小节在 0.1s；B 130 BPM，谱上第 1 小节在 16 小节的引子之后。"""
    ta = PlanTrack(1, "Alpha — X", 128, 128, False, 96, None, None, "X - Alpha.mp3", 0.1, [(33, "breakdown 16"), (49, "drop")])
    b_beat = 60 / 130
    tb = PlanTrack(2, "Beta — X", 130, 129, False, 80, 65, "longhand", "X - Beta.mp3", 0.25 + 64 * b_beat, [(17, "drop")])
    rows = [
        PlanRow(61, None, False, K(), None),
        PlanRow(65, 1, False, K(), K(lo=-5, hi=-1, f=-3, v=5), "①"),
        PlanRow(73, 9, False, K(), K(lo=-2, hi=-1, f=-3, v=5)),
        PlanRow(80, 16, False, K(lo=0), K(lo=-2, hi=-1, f=0, v=10)),
        PlanRow(81, 17, False, K(lo=-5), K(lo=0, hi=-1, f=0, v=10), "②"),  # 一小节内大变化 = 跳变
        PlanRow(96, 32, True, K(lo=-5, mid=-3, hi=-5), K(lo=0, hi=0, f=0, v=10)),
    ]
    tr = PlanTransition(1, "Alpha", "Beta", "longhand", 65, 128, 129, (40, 47), rows,
                        [PlanEffect("A", "reverb", 89, 96, 1 / 3)], [])
    return Plan("测试谱", [ta, tb], [tr])


def make_lib():
    a = lib_track("a", 128, 0.1, 0, 96 * 4 * 60 / 128 + 0.1, "01_X_-_Alpha.mp3",
                  [{"start": 1, "end": 33, "label": "groove"}, {"start": 33, "end": 49, "label": "breakdown"},
                   {"start": 49, "end": 97, "label": "drop"}])
    # B：曲库里自动估的小节相位和谱不同（差 2 拍）
    b = lib_track("b", 130, 0.25, 2, 80 * 4 * 60 / 130 + 0.25 + 64 * 60 / 130, "02_X_-_Beta.mp3")
    c = lib_track("c", 140, 0.0, 0, 300, "03_Other.mp3")
    return [c, b, a]


def test_match_by_number_name_and_duration():
    plan = make_plan()
    m, notes = match_tracks(plan, make_lib())
    assert [t.id for t in m] == ["a", "b"] and not notes
    m, notes = match_tracks(plan, make_lib()[:2])  # 少了 A
    assert m[0] is None and notes and notes[0].level == "error" and "#01" in notes[0].where


def test_find_files_by_plan_names():
    """只放打碟谱时，按谱上的文件名在电脑里找曲子：编号、下划线、艺人写法不同都认；多一个 Edit / Remix 的词不认。"""
    pts = [PlanTrack(n, t, 138, 138, False, None, None, None, f) for n, t, f in [
        (1, "Overdrive (Edit) — Charlotte de Witte", "Charlotte de Witte - Overdrive (Edit).mp3"),
        (4, "High Street — Charlotte de Witte", "Charlotte de Witte - High Street.mp3"),
        (8, "Overdrive — Charlotte de Witte", "Charlotte de Witte - Overdrive.mp3"),
        (14, "Universal Nation (CdW Rework) — Push", "Push,Charlotte de Witte - Universal Nation (Charlotte de Witte Rework).mp3"),
    ]]
    plan = Plan("谱", pts, [])
    files = ["/m/RW/08_Charlotte_de_Witte_-_Overdrive.mp3", "/m/RW/01_Charlotte_de_Witte_-_Overdrive_Edit.mp3",
             "/m/RW/05_Charlotte_de_Witte_-_High_Street_Astrix_Remix.mp3",
             "/m/RW/14_Push_-_Universal_Nation_Charlotte_de_Witte_Rework.mp3", "/m/other/Totally Different.mp3"]
    got = file_matches(plan, files, [0, 1, 2, 3])
    assert got == {0: files[1], 2: files[0], 3: files[3]}  # High Street 只有 Remix 版：不认


def test_duration_check_for_found_files():
    pt = PlanTrack(1, "A", 128, 128, False, 96, None, None, "a.mp3", 0.5)
    est = 0.5 + 96 * 4 * 60 / 128
    assert duration_ok(pt, est + 10) and not duration_ok(pt, est + 30)
    assert duration_ok(PlanTrack(1, "A", 128, 128, False, None, None, None), 1.0)  # 谱上没写小节数：不核对


def test_align_phase_and_offset():
    plan = make_plan()
    lib = {t.id: t for t in make_lib()}
    al, notes = align(plan.tracks[0], lib["a"])
    assert al.k == 0 and al.downbeat == 0 and not al.rephase and abs(al.residual_ms) < 1
    al, notes = align(plan.tracks[1], lib["b"])
    # 谱的第 1 小节 = 第 64 拍（相位 0，和曲库的相位 2 不同）；本应用第 1 小节 = 第 0 拍 → 谱 b = 本应用 b + 16
    assert al.downbeat == 0 and al.rephase and al.k == 16
    assert al.bar1_s == pytest.approx(0.25)


def test_transition_lanes_steps_tempo_and_fx():
    plan = make_plan()
    conv = convert(plan, make_lib(), source="测试谱.pdf")
    assert conv.ok, [str(n) for n in conv.notes]
    s = conv.set
    assert isinstance(s, SetFile) and s.ending.mode == "playout"
    assert [t.id for t in s.tracks] == ["a", "b"]
    assert s.tracks[0].start == 1 and s.tracks[1].downbeat == 0 and s.tracks[1].play_bpm == 129
    tr = s.transitions[0]
    assert tr.technique == "custom" and tr.params == {"a_end": "playout"}
    assert tr.start == 61 and tr.play_at == 4 and tr.b_in == 17  # B 谱第 1 小节 = 本应用第 17 小节
    L = tr.lanes
    assert L["B.vol"][0] == [4.0, 0.5, "step"]  # 推子 5 进
    assert L["B.lo"][0] == [4.0, -25.0]  # 起播前就按表设好（时间线 0 起曲线就生效）
    assert [20.0, -25.0, "step"] in L["A.lo"] and [19.0, 0.0] in L["A.lo"]  # 80→81 一小节内拧到底 = 跳变
    assert L["B.filter"][0] == [4.0, -0.6]
    assert L["A.reverb"] == [[28.0, 0.0], [28.0, 0.333, "step"]]
    # 换速在 B 的第 40–47 小节：时间线 4+39=43 → 4+47=51；时间线要覆盖到换完
    assert tr.tempo == [[0, 128], [43, 128], [51, 129]] and tr.bars == 51
    assert "A.vol" not in L  # 推子一直 10：不写（出曲推子不拉）


def test_keylock_request_is_reported_not_silently_applied():
    plan = make_plan()
    plan.tracks[1].keylock = True
    conv = convert(plan, make_lib())
    assert conv.ok and not conv.set.keylock
    assert any("Key Lock" in n.msg and n.level == "warn" for n in conv.notes)


PDF = os.environ.get("DJSTUDIO_PLAN_PDF")


@pytest.mark.skipif(not PDF or not Path(PDF).exists(), reason="需要用户的打碟谱 PDF（DJSTUDIO_PLAN_PDF）")
def test_real_plan_pdf_parses():
    from djstudio.plan.cdw import parse

    plan = parse(Path(PDF))
    assert len(plan.transitions) == len(plan.tracks) - 1 and not plan.warnings
    for tr in plan.transitions:
        assert tr.rows[0].b is None and tr.rows[-1].end
        assert tr.technique in ("longhand", "instant")
        if tr.tempo_to != tr.tempo_from:
            assert tr.ramp_b
    assert all(t.bar1_s is not None and t.file_name for t in plan.tracks)
