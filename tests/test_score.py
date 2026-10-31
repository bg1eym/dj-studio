"""《打碟谱规范》（docs/SCORE_SPEC.md）：Markdown 打碟谱的解析、换算、编译。

- 模板 docs/score_template.md 必须能读、能换成 set、能编译通过（给用户的例子不能是坏的）；
- 规范里的 EQ / 推子 / 打击垫 / 颜色表和代码逐行一致（改一边不改另一边就失败）；
- 写错时报第几行、错在哪。
"""
import math
import re
from pathlib import Path

import pytest

from djstudio.compiler import TrackInfo, compile_set, fader_gain
from djstudio.grid import Grid
from djstudio.plan import mdplan
from djstudio.plan.cdw import PlanError
from djstudio.plan.convert import LibTrack, convert, eq_db
from djstudio.setspec import COLOR_NAMES, CUE_KINDS, fill_hot_cues

DOCS = Path(__file__).resolve().parents[1] / "docs"
SPEC = (DOCS / "SCORE_SPEC.md").read_text(encoding="utf-8")
TEMPLATE = DOCS / "score_template.md"

# 模板里的三首：(id, 文件名, BPM, 第 1 小节秒数, 小节数)
TEMPLATE_LIB = [("a", "Artist A - Track One.mp3", 130, 0.25, 160), ("b", "Artist B - Track Two.mp3", 132, 0.18, 128),
                ("c", "Artist C - Track Three.mp3", 132, 0.12, 192)]


def lib(specs=TEMPLATE_LIB):
    out = []
    for i, name, bpm, b1, bars in specs:
        dur = b1 + bars * 4 * 60 / bpm
        out.append(LibTrack(i, f"/lib/{name}", name, "", dur, Grid(bpm, b1, 0, dur), []))
    return out


def compile_with(s, tracks):
    infos = {t.id: TrackInfo(t.id, t.title, t.grid) for t in tracks}
    return compile_set(s, lambda st: infos.get(st.id))


def table(header: str) -> list[list[str]]:
    lines = SPEC.splitlines()
    i = next(k for k, ln in enumerate(lines) if ln.startswith(header))
    rows = []
    for ln in lines[i + 2:]:
        if not ln.startswith("|"):
            break
        rows.append([c.strip() for c in ln.strip("|").split("|")])
    assert rows, header
    return rows


# ---------------------------------------------------------------- 模板

def test_template_parses():
    plan = mdplan.parse(TEMPLATE)
    assert plan.title == "我的 set 打碟谱" and not plan.warnings
    assert plan.start_bar == 1 and plan.ending == (177, 16.0)
    assert [t.no for t in plan.tracks] == [1, 2, 3] and plan.tracks[0].bar1_s == 0.25
    assert plan.tracks[0].file_name == "Artist A - Track One.mp3" and not plan.tracks[0].keylock
    t1, t2 = plan.transitions
    assert (t1.technique, t1.b_in, t1.b1_at_a, t1.tempo_from, t1.tempo_to, t1.ramp_b) == ("longhand", 1, 129, 130, 132, (17, 24))
    assert (t2.technique, t2.b_in, t2.b1_at_a, t2.ramp_b) == ("instant", 33, 97, None)
    assert t2.rows[-1].stop and t2.rows[-1].end and not t1.rows[-1].stop and t1.rows[-1].end
    # 空格 = 和上一行一样
    assert t2.rows[1].a.mid == 0 and t2.rows[1].a.vol == 10 and t2.rows[2].a.lo == -2 and t2.rows[2].a.filter == 3
    # 自己加的 hot cue：颜色名、时间写法、DROP 也当段落标记
    assert not plan.tracks[0].hot_cues and not plan.tracks[1].hot_cues
    c3 = {c.pad: c for c in plan.tracks[2].hot_cues}
    assert c3[7].time == pytest.approx(72.12) and c3[7].bar is None and c3[7].color == "#FF5FA2"
    assert c3[8].bar == 113 and c3[8].color == "#FF8C1A" and (113.0, "drop 2") in plan.tracks[2].cues
    # 动作写在开始动手的那一行
    assert [r.marks for r in t1.rows][1].startswith("① B 起播")


def test_template_converts_and_compiles():
    tracks = lib()
    conv = convert(mdplan.parse(TEMPLATE), tracks, source="score_template.md")
    assert conv.ok, [str(n) for n in conv.notes]
    assert not [n for n in conv.notes if n.level != "info"], [str(n) for n in conv.notes]
    s = conv.set
    assert s.spec == "2" and s.ending.mode == "fade" and s.ending.bar == 177 and s.ending.bars == 16
    tr1, tr2 = s.transitions
    # 过渡 1：长交接，A 自己播完；144→145 一小节内换低频 = 跳变；换速在 B 第 17–24 小节；Reverb 开到 A 结束不关
    assert (tr1.start, tr1.b_in, tr1.play_at, tr1.params) == (121, 1, 8, {"a_end": "playout"})
    assert [24, -25.0, "step"] in tr1.lanes["A.lo"] and [24, 0.0, "step"] in tr1.lanes["B.lo"]
    assert tr1.lanes["B.vol"][0] == [8, 0.5, "step"] and tr1.lanes["B.lo"][0] == [8, -25.0]
    assert tr1.tempo == [[0, 130], [24, 130], [32, 132]]
    assert tr1.lanes["A.reverb"] == [[32, 0.0], [32, 0.333, "step"]]
    # 过渡 2：drop 对 drop，B 从第 33 小节进，A 在第 97 小节第 1 拍停；Echo 在最后一小节，尾巴留着
    assert (tr2.start, tr2.b_in, tr2.play_at, tr2.bars, tr2.params) == (93, 33, 4, 4, {"a_end": "stop"})
    assert tr2.events == [[4.0, "A.stop"]] and tr2.lanes["B.vol"] == [[4, 1.0, "step"]]
    assert tr2.lanes["A.filter"][-2:] == [[3, 0.6], [4, 0.6]] and tr2.lanes["A.echo"] == [[3, 0.0], [3, 0.5, "step"]]
    c = compile_with(s, tracks)
    assert c.ok, [vars(i) for i in c.issues]
    assert tr1.marks[0] == [0.0, "过渡开始"] and [24.0, "③ 换低频：同一拍 A LOW 切、B LOW 回 12 点"] in tr1.marks
    # hot cue：每首按这套 set 放 进 → 进来时要处理的点 → 出 → 出去时要处理的点 → 停 / 淡出，按时间从 1 号垫排
    s = fill_hot_cues(s, c)
    assert s.spec == "2" and compile_with(s, tracks).ok
    got = [[(h.pad, h.name, h.bar) for h in t.hot_cues] for t in s.tracks]
    assert got[0] == [(1, "IN", 1), (2, "OUT", 121), (3, "HIGH DOWN", 129), (4, "BASS OUT", 145), (5, "REVERB LPF", 153)]
    assert got[1] == [(1, "IN", 1), (2, "FADER UP", 9), (3, "BASS IN", 17), (4, "OUT", 93), (5, "ECHO", 96), (6, "STOP", 97)]
    assert got[2] == [(1, "IN", 33), (2, "FADE", 177), (7, "VOCAL", 40.5), (8, "DROP 2", 113)]  # 7、8 是谱上自己加的
    h = {x.name: x for x in s.tracks[1].hot_cues}
    assert h["IN"].color == "#22D45A" and h["OUT"].color == "#FF3B3B" and h["STOP"].color == "#A45CFF"
    assert h["BASS IN"].color == "#FFD60A" and h["ECHO"].color == "#00C8FF"
    assert "③ 换低频" in h["BASS IN"].note and "LOW 7点 → 12点（一拍到位）" in h["BASS IN"].note
    assert "推子 5、LOW 7点、HIGH 11点" in h["IN"].note and "推子 5 → 10（7 小节拧到）" in h["FADER UP"].note
    assert all(x.auto for x in s.tracks[1].hot_cues) and not any(x.auto for x in s.tracks[2].hot_cues if x.pad >= 7)
    assert [t.main_cue for t in s.tracks] == [1, 1, 33]  # 主 Cue = IN
    # 再填一次结果一样（自动的重算，手写的不动）
    assert fill_hot_cues(s, compile_with(s, tracks)).tracks == s.tracks


def test_bar_offset_when_score_bar1_is_later():
    """谱上的第 1 小节在曲子第 17 小节（前面有 16 小节引子）：谱上的小节号整体换成本应用的 + 16。"""
    plan = mdplan.parse(TEMPLATE)
    bar = 4 * 60 / 130
    plan.tracks[0].bar1_s = 0.25 + 16 * bar
    tracks = lib()
    dur = 0.25 + 176 * bar
    tracks[0] = LibTrack("a", tracks[0].path, tracks[0].title, "", dur, Grid(130, 0.25, 0, dur), [])
    conv = convert(plan, tracks)
    assert conv.ok, [str(n) for n in conv.notes]
    a = conv.set.tracks[0]
    assert a.plan_bar_offset == 16 and conv.set.transitions[0].start == 121 + 16
    s = fill_hot_cues(conv.set, compile_with(conv.set, tracks))
    assert [(h.name, h.bar) for h in s.tracks[0].hot_cues][:2] == [("IN", 1 + 16), ("OUT", 121 + 16)]


# ---------------------------------------------------------------- 规范的表 = 代码

def hours(text: str) -> float:
    m = re.match(r"^(\d+) 点(半)?$", text.strip())
    assert m, text
    c = int(m.group(1))
    return (c - 12 if c >= 7 else c) + (0.5 if m.group(2) else 0)


def test_spec_eq_table_matches_code():
    rows = table("| 钟点位 | dB |")
    assert len(rows) == 16
    for name, db in rows:
        assert eq_db(hours(name)) == pytest.approx(float(db), abs=0.051), name
        assert eq_db(mdplan.clock(name.replace(" ", ""))) == eq_db(hours(name))  # 谱里的写法读出来一样


def test_spec_fader_table_matches_code():
    rows = table("| 推子 | 实际增益 dB |")
    assert [r[0] for r in rows] == [str(n) for n in range(11)]
    for n, db in rows:
        g = fader_gain(int(n) / 10)
        assert (db == "-∞") if g <= 0 else 20 * math.log10(g) == pytest.approx(float(db), abs=0.051), n


def test_spec_cue_kinds_and_colors_match_code():
    rows = table("| 名字 | 颜色 | 什么时候放 |")
    assert rows == [[k.names, f"{k.color_name} {k.color}", k.when] for k in CUE_KINDS]
    assert dict(table("| 颜色 | 色值 |")) == COLOR_NAMES
    assert "没有固定用途" in SPEC
    assert f"v{mdplan.SCORE_SPEC_VERSION}" in SPEC.splitlines()[0]


# ---------------------------------------------------------------- 写错了：报行号

HEAD = """# 测试谱

## 曲目

| # | 曲名 | 文件名 | 原 BPM | 第 1 小节（秒） |
|---|---|---|---|---|
| 1 | A | a.wav | 128 | 0.25 |
| 2 | B | b.wav | 128 | 0.40 |

## 过渡 1：A → B

- 手法：长交接
- 进场：B 的第 1 小节落在 A 的第 25 小节

| A 小节 | B 小节 | A LOW | B LOW | B 推子 |
|---|---|---|---|---|
"""


def score(rows: str, head: str = HEAD) -> mdplan.Plan:
    return mdplan.parse_text(head + rows)


def test_minimal_score_with_columns_left_out():
    plan = score("| 21 | — | 12点 | — | — |\n| 25 | 1 | | 7点 | 5 |\n| 41 结束 | 17 | 7点 | 12点 | 10 |\n")
    tr = plan.transitions[0]
    assert [r.a_bar for r in tr.rows] == [21, 25, 41] and tr.rows[1].b.lo == -5 and tr.rows[1].b.hi == 0
    assert tr.rows[0].a.vol == 10 and tr.tempo_from == tr.tempo_to == 128


@pytest.mark.parametrize("rows,line,words", [
    ("| 21 | — | 12点 | — | — |\n| 25 | 1 | | 7点 | |\n| 41 结束 | 17 | | | 10 |\n", 18, "B 推子"),
    ("| 21 | — | 13点 | — | — |\n| 25 | 1 | | 7点 | 5 |\n| 41 结束 | 17 | | | 10 |\n", 17, "13点"),
    ("| 21 | — | 12点 | 7点 | — |\n| 25 | 1 | | 7点 | 5 |\n| 41 结束 | 17 | | | 10 |\n", 17, "B 还没进场"),
    ("| 21 | — | 12点 | — | — |\n| 25 | 1 | | 7点 | 5 |\n| 41 | 17 | | | 10 |\n", 19, "结束"),
    ("| 21 | — | 12点 | — | — |\n| 25 | 2 | | 7点 | 5 |\n| 41 结束 | 17 | | | 10 |\n", 18, "应该是 1"),
    ("| 21 | — | 12点 | — | — |\n| 33 | 9 | | 7点 | 5 |\n| 41 结束 | 17 | | | 10 |\n", 15, "B 进场那一小节"),
    ("| 21 | — | 12点 | — | — |\n| 25 | 1 | — | 7点 | 5 |\n| 41 结束 | 17 | | | 10 |\n", 18, "A 一直在播"),
    ("| 21 | — | 12点 | — | — |\n| 25 | 1 | | 7点 | 5 |\n| 25 | 1 | | 7点 | 5 |\n| 41 结束 | 17 | | | 10 |\n", 19, "一行比一行大"),
])
def test_errors_name_the_line(rows, line, words):
    with pytest.raises(PlanError) as e:
        score(rows)
    assert f"第 {line} 行" in str(e.value) and words in str(e.value), str(e.value)


def test_header_errors():
    ok_rows = "| 21 | — | 12点 | — | — |\n| 25 | 1 | | 7点 | 5 |\n| 41 结束 | 17 | | | 10 |\n"
    with pytest.raises(PlanError, match="主速"):  # 两首播放 BPM 不同，没写在哪换
        score(ok_rows, HEAD.replace("| 2 | B | b.wav | 128 |", "| 2 | B | b.wav | 130 |"))
    with pytest.raises(PlanError, match="手法"):
        score(ok_rows, HEAD.replace("- 手法：长交接\n", ""))
    with pytest.raises(PlanError, match="规范"):
        score(ok_rows, HEAD.replace("# 测试谱\n", "# 测试谱\n\n- 规范：v3\n"))
    with pytest.raises(PlanError, match="曲目"):
        mdplan.parse_text("# 随便写的笔记\n\n今天练了长交接。\n")
    plan = score(ok_rows, HEAD.replace("- 进场", "- 主速：128 → 130，B 第 9–12 小节换\n- 进场"))
    assert plan.transitions[0].ramp_b == (9, 12) and plan.warnings  # 换速后和曲目表的 128 不同：提示


@pytest.mark.parametrize("cue,words", [
    ("| 2 | 进点 | 绿 | 25.1 | |", "只能用英文"),
    ("| 9 | X | | 25.1 | |", "1–8"),
    ("| 2 | MIX IN | 绿 | 25.5 | |", "拍只能是 1–4"),
    ("| 2 | MIX IN | 金 | 25.1 | |", "颜色"),
    ("| 2 | | 绿 | 25.1 | |", "名字要写"),
])
def test_hot_cue_errors(cue, words):
    rows = "| 21 | — | 12点 | — | — |\n| 25 | 1 | | 7点 | 5 |\n| 41 结束 | 17 | | | 10 |\n"
    head = HEAD.replace("## 过渡 1", f"## Hot Cue\n\n### #1 A\n\n| 垫 | 名字 | 颜色 | 位置 | 注释 |\n|---|---|---|---|---|\n{cue}\n\n## 过渡 1")
    with pytest.raises(PlanError) as e:
        score(rows, head)
    assert "第 " in str(e.value) and words in str(e.value), str(e.value)


def test_effects_turn_off_at_end_of_range():
    """「第 a–b 小节」：a 开，b 结束时关；A 的效果开到 A 结束才不关（尾巴）。"""
    rows = "| 21 | — | 12点 | — | — |\n| 25 | 1 | | 7点 | 5 |\n| 41 结束 | 17 | | | 10 |\n"
    head = HEAD.replace("- 进场", "- 效果：A Echo，第 22–23 小节，湿度 50%\n- 效果：B Reverb，第 5 小节，湿度 0.25\n- 进场")
    plan = score(rows, head)
    assert [(f.deck, f.kind, f.bar0, f.bar1, f.wet) for f in plan.transitions[0].effects] == \
        [("A", "echo", 22, 23, 0.5), ("B", "reverb", 5, 5, 0.25)]
    tracks = [LibTrack("a", "/l/a.wav", "a.wav", "", 0.25 + 56 * 1.875, Grid(128, 0.25, 0, 0.25 + 56 * 1.875), []),
              LibTrack("b", "/l/b.wav", "b.wav", "", 0.4 + 56 * 1.875, Grid(128, 0.4, 0, 0.4 + 56 * 1.875), [])]
    conv = convert(plan, tracks)
    assert conv.ok, [str(n) for n in conv.notes]
    L = conv.set.transitions[0].lanes
    assert L["A.echo"] == [[1, 0.0], [1, 0.5, "step"], [3, 0.5], [3, 0.0, "step"]]
    assert L["B.reverb"] == [[8, 0.0], [8, 0.25, "step"], [9, 0.25], [9, 0.0, "step"]]
    # 谱上写「结束」，A 实际还有 15 小节：提示要不要写「停」
    assert any("停" in n.msg for n in conv.notes if n.level == "warn")


# ---------------------------------------------------------------- 网页

def test_upload_md_score_and_download_docs(tmp_path):
    from tests.test_web import make_client

    client = make_client(tmp_path)
    r = client.put("/api/upload", params={"name": "我的谱.md"}, content=TEMPLATE.read_bytes())
    assert r.status_code == 200 and r.json()["kind"] == "plan", r.text
    st = client.get("/api/plan").json()
    assert st["file"] == "我的谱.md" and st["n_tracks"] == 3 and st["n_transitions"] == 2
    bad = TEMPLATE.read_text(encoding="utf-8").replace("| 160 结束 |", "| 160 |")
    r = client.put("/api/upload", params={"name": "写错的谱.md"}, content=bad.encode("utf-8"))
    assert r.status_code == 422 and "行" in r.json()["detail"] and "结束" in r.json()["detail"]
    assert client.get("/api/plan").json()["file"] == "我的谱.md"  # 读不了的谱不算当前打碟谱
    spec = client.get("/api/score/spec.md")
    assert spec.status_code == 200 and spec.text.startswith("# 打碟谱规范 v2")
    tmpl = client.get("/api/score/template.md", params={"download": 1})
    assert tmpl.status_code == 200 and "attachment" in tmpl.headers["content-disposition"]
    assert mdplan.parse_text(tmpl.text).title == "我的 set 打碟谱"
    assert client.get("/api/score/other.md").status_code == 404


def test_warnings_carry_line_numbers_in_order():
    """提示（不挡着用，但网页首页会显示）带行号，按谱里的先后排。"""
    t = TEMPLATE.read_text(encoding="utf-8").replace("- 说明：drop 对 drop", "- 混音：drop 对 drop") \
        .replace("- 说明：B 先半推子进", "- 思路：B 先半推子进").replace("- 主速：132\n", "- 主速：131 → 132，B 第 33–36 小节换\n")
    w = mdplan.parse_text(t).warnings
    assert len(w) == 3 and "思路" in w[0] and "混音" in w[1] and "131" in w[2]  # 没有行号的放最后
    lines = [int(re.match(r"第 (\d+) 行", x).group(1)) for x in w[:2]]
    assert lines == sorted(lines)


# ---------------------------------------------------------------- hot cue 跟着 set 走（v2）

def _two_track_set(**tr):
    from djstudio.setfile import SetFile

    return SetFile.model_validate({
        "schema": "djset/2", "spec": "2", "id": "t", "tracks": [{"id": "a", "title": "A"}, {"id": "b", "title": "B"}],
        "transitions": [{"technique": "longhand", "start": 97, "b_in": 1, **tr}], "ending": {"mode": "playout"}})


def test_hot_cues_for_a_set_without_score():
    """不是谱生成的 set（longhand 手法默认曲线）也一样：按曲线找进、出和中间要处理的点。"""
    tracks = lib([("a", "a.mp3", 128, 0.0, 160), ("b", "b.mp3", 128, 0.0, 160)])
    s = _two_track_set()
    c = compile_with(s, tracks)
    assert c.ok, [vars(i) for i in c.issues]
    s = fill_hot_cues(s, c)
    a, b = ([(h.name, h.bar) for h in t.hot_cues] for t in s.tracks)
    assert a[0] == ("IN", 1) and a[1] == ("OUT", 97) and len(a) > 2
    assert b[0] == ("IN", 1) and any(n == "BASS IN" for n, _ in b) and any(n == "BASS OUT" for n, _ in a)
    assert [h.pad for h in s.tracks[0].hot_cues] == list(range(1, len(a) + 1))  # 从 1 号垫起、按时间排
    assert [x[1] for x in a] == sorted(x[1] for x in a)


def test_manual_pads_win_and_v1_pads_are_replaced():
    from djstudio.setfile import HotCue

    tracks = lib([("a", "a.mp3", 128, 0.0, 160), ("b", "b.mp3", 128, 0.0, 160)])
    s = _two_track_set()
    v1 = [HotCue(pad=1, name="START", bar=1, color="#FFFFFF"), HotCue(pad=2, name="MIX IN", bar=1, color="#22D45A"),
          HotCue(pad=3, name="IN", bar=5, color="#22D45A", note="我自己的进点"), HotCue(pad=8, name="VOCAL", bar=41)]
    s = s.model_copy(update={"spec": "1", "tracks": [s.tracks[0].model_copy(update={"hot_cues": v1}), s.tracks[1]]})
    s = fill_hot_cues(s, compile_with(s, tracks))
    pads = {h.pad: (h.name, h.bar, h.auto) for h in s.tracks[0].hot_cues}
    assert pads[3] == ("IN", 5, False) and pads[8] == ("VOCAL", 41, False)  # 手写的不动
    assert pads[1][0] == "OUT" and pads[1][2]  # v1 固定用途的 START / MIX IN 换掉；自动的 IN 让给手写的 IN
    assert s.tracks[0].main_cue == 5 and s.spec == "2"


def test_too_many_points_keep_in_out_stop_first():
    from djstudio.setfile import HotCue

    tracks = lib([("a", "a.mp3", 128, 0.0, 160), ("b", "b.mp3", 128, 0.0, 160)])
    s = _two_track_set()
    mine = [HotCue(pad=p, name=f"MY {p}", bar=1 + p) for p in range(3, 9)]  # 只剩 1、2 号垫
    s = s.model_copy(update={"tracks": [s.tracks[0].model_copy(update={"hot_cues": mine}), s.tracks[1]]})
    s = fill_hot_cues(s, compile_with(s, tracks))
    assert [(h.pad, h.name) for h in s.tracks[0].hot_cues][:2] == [(1, "IN"), (2, "OUT")]
