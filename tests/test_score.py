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
from djstudio.setspec import COLOR_NAMES, PADS, fill_hot_cues

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
    # hot cue：颜色名、时间写法、BREAK / DROP 也当段落标记
    c3 = {c.pad: c for c in plan.tracks[2].hot_cues}
    assert c3[1].time == pytest.approx(0.12) and c3[1].bar is None and c3[2].bar == 33 and c3[5].color == "#2F7BFF"
    assert (97.0, "break 2") in plan.tracks[2].cues


def test_template_converts_and_compiles():
    tracks = lib()
    conv = convert(mdplan.parse(TEMPLATE), tracks, source="score_template.md")
    assert conv.ok, [str(n) for n in conv.notes]
    assert not [n for n in conv.notes if n.level != "info"], [str(n) for n in conv.notes]
    s = conv.set
    assert s.spec == "1" and s.ending.mode == "fade" and s.ending.bar == 177 and s.ending.bars == 16
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
    # hot cue：谱上写的照写的放，没写的按规范补，主 Cue = MIX IN
    s = fill_hot_cues(s, c)
    assert compile_with(s, tracks).ok
    p1, p2, p3 = ({h.pad: h for h in t.hot_cues} for t in s.tracks)
    assert p1[3].bar == 65 and p1[3].note == "16 小节的 breakdown" and p1[2].name == "MIX IN" and p1[8].name == "END"
    assert p2[8].bar == 97 and p2[7].bar == 93 == tr2.start
    assert p3[1].bar == 1 and p3[2].bar == 33 and s.tracks[2].main_cue == 33
    assert 3 not in p3 and 4 not in p3  # 谱上只写了 BREAK 2 / DROP 2：同一处不再补 BREAK 1 / DROP 1
    assert p3[7].bar == 177 and p3[8].bar == 193  # 终曲淡出


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
    assert {h.pad: h.bar for h in a.hot_cues}[3] == 65 + 16


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


def test_spec_pads_and_colors_match_code():
    rows = table("| 打击垫 | 名字 | 颜色 | 放在哪 |")
    assert [(int(p), n, c) for p, n, c, _ in rows] == [(p.pad, p.name, f"{p.color_name} {p.color}") for p in PADS]
    assert dict(table("| 颜色 | 色值 |")) == COLOR_NAMES
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
        score(ok_rows, HEAD.replace("# 测试谱\n", "# 测试谱\n\n- 规范：v2\n"))
    with pytest.raises(PlanError, match="曲目"):
        mdplan.parse_text("# 随便写的笔记\n\n今天练了长交接。\n")
    plan = score(ok_rows, HEAD.replace("- 进场", "- 主速：128 → 130，B 第 9–12 小节换\n- 进场"))
    assert plan.transitions[0].ramp_b == (9, 12) and plan.warnings  # 换速后和曲目表的 128 不同：提示


@pytest.mark.parametrize("cue,words", [
    ("| 2 | 进点 | 绿 | 25.1 | |", "只能用英文"),
    ("| 9 | X | | 25.1 | |", "1–8"),
    ("| 2 | MIX IN | 绿 | 25.5 | |", "拍只能是 1–4"),
    ("| 2 | MIX IN | 金 | 25.1 | |", "颜色"),
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
    assert spec.status_code == 200 and spec.text.startswith("# 打碟谱规范 v1")
    tmpl = client.get("/api/score/template.md", params={"download": 1})
    assert tmpl.status_code == 200 and "attachment" in tmpl.headers["content-disposition"]
    assert mdplan.parse_text(tmpl.text).title == "我的 set 打碟谱"
    assert client.get("/api/score/other.md").status_code == 404
