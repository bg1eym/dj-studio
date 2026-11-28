"""曲谱 v5：解析（行号、错误和提示分开）、示例谱、往返（读 → 写 → 读是同一个项目）、编译时的检查。"""
from pathlib import Path

import pytest

from djstudio.grid import Grid
from djstudio.v5.caps import BASIC, BUILTIN, MIXXX, Caps
from djstudio.v5.compile import AssetInfo, actions, chart, compile_project
from djstudio.v5.model import Project
from djstudio.v5.pos import PosError, fmt_dur, fmt_pos, parse_dur, parse_pos
from djstudio.v5.score import knob_value, parse, parse_text, score_version
from djstudio.v5.writer import knob_write, write

DOCS = Path(__file__).resolve().parents[1] / "docs"
EXAMPLE = DOCS / "score_v5_example.md"
FX_EXAMPLE = DOCS / "score_v5_fx_example.md"


def resolve(a):
    """按素材表上写的 BPM / 小节数给一个网格（测试里没有真的音频）。"""
    dur = (a.bars or 64) * 4 * 60 / a.bpm + (a.bar1_s or 0) + 2
    return AssetInfo(a.id, Grid(a.bpm, a.bar1_s or 0.0, 0, dur), title=a.title)


def norm(p: Project) -> dict:
    """项目里和写法无关的部分：不比行号、文件名、自动起的 Loop / 跳段编号、列表的先后。"""
    def strip(o):
        if isinstance(o, dict):
            return {k: strip(v) for k, v in o.items()
                    if k not in ("line", "source") and not (k == "id" and str(v)[:1] in "LJ" and str(v)[1:].isdigit())}
        if isinstance(o, list):
            return [strip(x) for x in o]
        return o
    d = strip(p.model_dump())
    for k in ("lanes", "loops", "jumps", "fx", "marks"):
        d[k] = sorted(d[k], key=lambda x: str(sorted(x.items(), key=str)))
    return d


SMALL = """# 小谱

- 规范：v5
- 主速：132

## 素材
| ID | 曲名 | 文件名 | 原 BPM | 第 1 小节（秒） | 小节数 |
|---|---|---|---|---|---|
| A1 | One | one.mp3 | 132 | 0.250 | 96 |
| A2 | Two | two.mp3 | 134 | 0.100 | 96 |

## 片段
| ID | 素材 | Deck | 原曲起点 | 全场起播 | 全场停止 | 角色 | 推子 | LOW |
|---|---|---|---|---|---|---|---|---|
| C1 | A1 | D1 | 1.1 | 1.1 | 65.1 | 主曲 | 10 | 12点 |
| C2 | A2 | D2 | 1.1 | 33.1 | 81.1 | 进曲 | 5 | 7点 |

## 场景 1：交接（33.1 – 65.1）
- 状态：草稿

### C2 参数
| 位置 | LOW | 推子 | 说明 |
|---|---|---|---|
| 48.1 | | 保持 | |
| 49.1 | 跳 12点 | 10 | 换低频 |

### C1 参数
| 位置 | LOW | 说明 |
|---|---|---|
| 49.1 | 跳 7点 | 换低频 |
"""


def small(**edits: str) -> str:
    s = SMALL
    for old, new in edits.items():
        assert old in s, old
        s = s.replace(old, new)
    return s


# ---------------------------------------------------------------- 位置与时值

def test_positions_are_strings_not_decimals():
    assert parse_pos("1.1") == 0 and parse_pos("129") == 512 and float(parse_pos("16.4+1/2")) == 63.5
    assert fmt_pos(parse_pos("16.4+1/2")) == "16.4+1/2" and fmt_pos(512) == "129.1"
    for bad in ("16.5", "0.1", "16.4+3/2", "abc", 16.5, 16):
        with pytest.raises(PosError):
            parse_pos(bad)


def test_durations_need_a_unit():
    assert parse_dur("8拍") == 8 and parse_dur("2小节") == 8 and float(parse_dur("1/2拍")) == 0.5
    assert fmt_dur(parse_dur("1/2拍")) == "1/2拍" and fmt_dur(8) == "2小节"
    for bad in ("8", "半拍", 2, "1/2"):
        with pytest.raises(PosError):
            parse_dur(bad)


def test_knob_values_round_trip():
    for param, texts in {"lo": ["12点", "7点", "11点半", "3点", "-6dB", "+2.5dB"], "filter": ["关", "左9点", "右3点", "左62%"],
                         "vol": ["0", "5", "7.5", "10"]}.items():
        for t in texts:
            v = knob_value(param, t)
            assert knob_value(param, knob_write(param, v)) == pytest.approx(v, abs=1e-6), (param, t)
    assert knob_write("lo", knob_value("lo", "10点")) == "10点" and knob_write("filter", knob_value("filter", "左9点")) == "左9点"
    assert knob_value("lo", "切") == knob_value("lo", "7点")


# ---------------------------------------------------------------- 示例谱

def test_example_parses_and_compiles_clean():
    """规范里的示例谱：零错误、零提示，五个场景都是正式的（五项说明齐全）。"""
    assert score_version(EXAMPLE.read_text(encoding="utf-8")) == "5"
    r = parse(EXAMPLE)
    assert r.ok and not r.issues, [str(i) for i in r.issues]
    p = r.project
    assert [a.id for a in p.assets] == ["A1", "A2", "A3", "A4", "A5"] and len(p.clips) == 7 and len(p.scenes) == 5
    assert all(not s.draft and not s.missing() for s in p.scenes)
    c = compile_project(p, resolve)
    assert c.ok and not c.issues, [str(i) for i in c.issues]
    assert c.length == 960 and c.plan["t"] == "djplan/1" and len(c.plan["exec"]) == 12
    assert compile_project(p, resolve).plan == c.plan  # 相同的输入 = 相同的计划
    # 每个动作都追得回谱里的行
    lines = EXAMPLE.read_text(encoding="utf-8").splitlines()
    acts = actions(c)
    assert all(a["line"] > 0 for a in acts)
    swap = [a for a in acts if a["at"] == 384.0 and a["kind"] == "knob" and "LOW" in a["text"]]
    assert len(swap) == 2 and all("97.1" in lines[a["line"] - 1] for a in swap)
    # 同一份素材用两次 = 两个片段；Deck 3 上一前一后
    assert [k.clip.asset for k in c.clips if k.clip.deck == 3] == ["A3", "A3"]
    assert c.clip("C5").gen > c.clip("C3").gen


def test_fx_example_compiles_clean_and_the_spec_recipes_come_from_it():
    """效果套路示例（docs/score_v5_fx_example.md）：零错误、零提示，RMX-1000 那几手各用了一处。
    《曲谱规范 v5》第 8.7 节「常用套路」里的每一行都是从它抄的（一字不差），所以规范里建议的写法保证编得过；
    第 8.6 节对照表里写到的类型解析器都认识。"""
    import re

    from djstudio.v5.score import FX_KIND, _key

    r = parse(FX_EXAMPLE)
    assert r.ok and not r.issues, [str(i) for i in r.issues]
    c = compile_project(r.project, resolve)
    assert c.ok and not c.issues, [str(i) for i in c.issues]
    assert {f.kind for f in r.project.fx} == {"echo", "reverb", "trans", "roll", "release", "noise", "sample", "filter_echo", "flanger", "spiral"}
    assert len(r.project.fx) == 12 and len(r.project.scenes) == 3 and all(not sc.draft and not sc.missing() for sc in r.project.scenes)
    ch = chart(c)
    assert [(h["from"], h["to"]) for h in ch["handovers"]] == [("C1", "C2")] and ch["handovers"][0]["swap"] == 320  # 一次交接，第 81 小节换低频
    spec = (DOCS / "SCORE_SPEC_V5.md").read_text(encoding="utf-8")
    recipes = spec[spec.index("### 8.7"):spec.index("## 9 ")]
    rows = [ln for ln in recipes.splitlines() if ln.startswith("| F")]
    have = set(FX_EXAMPLE.read_text(encoding="utf-8").splitlines())
    assert len(rows) == 12 and len(set(rows)) == 12 and all(ln in have for ln in rows), [ln for ln in rows if ln not in have]
    table = spec[spec.index("### 8.6"):spec.index("### 8.7")]
    names = set(re.findall(r"`([A-Z][A-Za-z]*(?: Echo)?|采样)`", table)) - {"LOW", "MID", "HIGH"}
    assert names == {"Trans", "Roll", "Echo", "Noise", "Spiral", "Reverb", "Flanger", "Phaser", "Filter Echo", "Filter", "采样", "Release"}, names
    assert all(FX_KIND.get(_key(n)) for n in names)


def test_what_the_rmx_table_says_is_missing_is_an_error_not_a_silent_skip():
    """对照表里写着「没做」的那几种（Gate、Crush Echo…）：谱里写了是错误（类型不认识，带行号），不会悄悄跳过。
    表里说「反过来写」「换个模式」的那几种写法都编得过。"""
    base = FX_EXAMPLE.read_text(encoding="utf-8")

    def issues(old, new):
        assert base.count(old) == 1, old
        r = parse_text(base.replace(old, new), "fx")
        out = [(i.level, i.code) for i in r.issues]
        if r.ok:
            out += [(i.level, i.code) for i in compile_project(r.project, resolve).issues]
        return out

    for kind in ("Crush Echo", "Gate", "Drive"):
        r = parse_text(base.replace("| F8 | Flanger |", f"| F8 | {kind} |"), "fx")
        assert not r.ok and [(i.level, i.code) for i in r.issues] == [("error", "fx")] and r.issues[0].line and "不认识" in r.issues[0].msg
    assert issues("音高 +12", "音高 -12") == []  # Spiral Down
    assert issues("频率 400Hz → 8kHz", "频率 8kHz → 400Hz") == []  # 往下扫的噪声
    assert issues("模式 高通；截止 40Hz → 2kHz", "模式 低通；截止 16kHz → 300Hz") == []  # LPF Echo
    assert issues("方式 刹车；退场", "方式 回转；退场") == []  # Back Spin
    assert issues("| snare | 80.1 | 81.1 | 重复 1/4拍；音量 0.8 |", "| hat | 80.1 | 81.1 | 重复 1/8拍；音量 0.5 |") == []  # 打击垫连打
    assert issues("| F11 | Roll |", "| F13 | Filter | 插入 | C2 | 133.1 | 141.1 | 模式 低通；截止 16kHz → 800Hz | 往下沉 |\n| F11 | Roll |") == []  # Reverb Down 的近似


def test_example_round_trips_through_the_writer():
    p = parse(EXAMPLE).project
    again = parse_text(write(p), "score_v5_example")
    assert again.ok and not again.issues, [str(i) for i in again.issues]
    assert norm(again.project) == norm(p)
    assert compile_project(again.project, resolve).plan["clips"] == compile_project(p, resolve).plan["clips"]


def test_solo_knob_moves_are_allowed_anywhere():
    """独奏的时候一样能动每个旋钮：示例谱场景 1 只有 D1 在放，Filter 照样按谱拧。"""
    c = compile_project(parse(EXAMPLE).project, resolve)
    c1 = c.clip("C1")
    assert [k.clip.id for k in c.clips if k.start <= 110 < k.stop] == ["C1"]  # 第 28 小节：只有 C1
    assert c1.value_at("filter", 96.0) == 0 and c1.value_at("filter", 110.0) == pytest.approx(0.6 * 14 / 31)
    assert c1.value_at("filter", 127.0) == pytest.approx(0.6) and c1.value_at("filter", 128.0) == 0
    curve = c.plan["clips"][0]["curves"]["filter"]
    assert [96.0, 0.5] in curve and curve[-1] == [128.0, 0.5] and max(v for _, v in curve) == pytest.approx(0.8)


# ---------------------------------------------------------------- 解析：错误带行号，能读的都读完

def line_of(text: str, needle: str) -> int:
    return next(i for i, ln in enumerate(text.splitlines(), 1) if needle in ln)


def test_small_score_is_fine():
    r = parse_text(small())
    assert r.ok and not r.errors, [str(i) for i in r.issues]
    c = compile_project(r.project, resolve)
    assert c.ok, [str(i) for i in c.issues]


def test_all_errors_are_reported_with_lines():
    text = small(**{"| C2 | A2 | D2 | 1.1 | 33.1 |": "| C2 | A9 | D7 | 1.1 | 33.5 |",
                    "| 49.1 | 跳 7点 | 换低频 |": "| 49.1 | 6点 | 换低频 |"})
    r = parse_text(text, "small", "small.md")
    assert r.project is None
    by_line = {i.line: i for i in r.errors}
    assert line_of(text, "| C2 | A9") in by_line and "A9" in by_line[line_of(text, "| C2 | A9")].msg
    assert line_of(text, "| 49.1 | 6点") in by_line  # 另一处错误也报了（6 点不在旋钮行程里）
    assert all(i.line > 0 for i in r.errors)


def test_ramp_needs_an_explicit_start():
    """直接写值 = 匀速拧到；这张表前面没有它从哪开始拧就是错的——不按变化大小猜是不是跳变。"""
    text = small(**{"| 48.1 | | 保持 | |\n": ""})
    r = parse_text(text)
    e = [i for i in r.errors if i.code == "ramp_start"]
    assert len(e) == 1 and e[0].line == line_of(text, "| 49.1 | 跳 12点 | 10 |") and "保持" in e[0].fix and "跳 10" in e[0].fix


@pytest.mark.parametrize("old,new,code", [
    ("- 规范：v5", "- 规范：v6", "version"),
    ("（33.1 – 65.1）", "", "scene"),
    ("| 49.1 | 跳 12点 | 10 | 换低频 |", "| 49.9 | 跳 12点 | 10 | 换低频 |", "pos"),
    ("### C1 参数", "### C9 参数", "param"),
])
def test_parse_errors(old, new, code):
    r = parse_text(small(**{old: new}))
    assert code in [i.code for i in r.errors], [str(i) for i in r.issues]


def test_v4_scores_are_not_v5():
    assert score_version((DOCS / "score_template.md").read_text(encoding="utf-8")) in ("", "4")


# ---------------------------------------------------------------- 编译：占用、范围、策略、能力

def issues(text: str, caps: Caps = BUILTIN):
    r = parse_text(text)
    assert r.project is not None, [str(i) for i in r.issues]
    return compile_project(r.project, resolve, caps)


def test_formal_scene_needs_the_five_items():
    c = issues(small(**{"- 状态：草稿": "- 作用：换曲\n- 保留：D1 的主题"}))
    e = [i for i in c.errors if i.code == "scene_strategy"]
    assert len(e) == 1 and "前景" in e[0].msg and "LOW" in e[0].msg and "MID/HIGH" in e[0].msg and "草稿" in e[0].fix


def test_deck_overlap_and_prep_are_errors():
    """A02：同一台 Deck 上播放区间不能重叠；上一首还没停不能指望它预载下一首。"""
    c = issues(small(**{"| C2 | A2 | D2 |": "| C2 | A2 | D1 |"}))
    assert [i.code for i in c.errors] == ["deck_overlap"] and "换一台 Deck" in c.errors[0].fix
    text = small(**{"| C2 | A2 | D2 | 1.1 | 33.1 | 81.1 |": "| C2 | A2 | D1 | 1.1 | 65.2 | 81.1 |"})
    c = issues(text)
    e = [i for i in c.errors if i.code == "deck_prep"]
    assert len(e) == 1 and e[0].line == line_of(text, "| C2 | A2 | D1") and "换一台" in e[0].fix


def test_clip_must_stay_inside_the_asset_and_lanes_inside_the_clip():
    c = issues(small(**{"| C1 | A1 | D1 | 1.1 | 1.1 | 65.1 |": "| C1 | A1 | D1 | 60.1 | 1.1 | 65.1 |"}))
    assert "clip_range" in [i.code for i in c.errors]
    c = issues(small(**{"| 49.1 | 跳 7点 | 换低频 |": "| 70.1 | 跳 7点 | 换低频 |"}))
    e = [i for i in c.errors if i.code == "lane_range"]
    assert len(e) == 1 and "C1" in e[0].where and "65.1" in e[0].msg


def test_hints_are_not_errors_and_can_be_marked_intentional():
    """多低频这类只是提示（按旋钮位估计，写成「可能」），不挡演出；场景里标成有意就不再提。"""
    text = small(**{"| 49.1 | 跳 7点 | 换低频 |": "| 49.1 | 跳 11点 | 换低频 |"})
    c = issues(text)
    assert c.ok and c.plan is not None and [i.code for i in c.hints] == ["multi_low", "multi_mid"]
    assert "估计" in c.hints[0].msg and "可能" in c.hints[1].msg and all(i.level == "hint" and i.line for i in c.hints)
    c = issues(text.replace("- 状态：草稿", "- 状态：草稿\n- 有意：多低频"))
    assert c.ok and [i.code for i in c.hints] == ["multi_mid"]
    c = issues(text.replace("- 状态：草稿", "- 状态：草稿\n- 有意：多低频 / 中频叠加"))
    assert c.ok and not c.hints


FX = """
### 效果
| ID | 类型 | 路由 | 目标 | 开始 | 结束 | 参数 | 说明 |
|---|---|---|---|---|---|---|---|
| F1 | Delay | 发送 | C1 | 57.1 | 65.1 | 拍长 3/4拍；送出 1/2；反馈 0.4；尾音 2小节 | |
"""


def test_send_effects_and_their_parameters():
    c = issues(SMALL + FX)
    assert c.ok, [str(i) for i in c.issues]
    f = c.fx[0]
    assert f["kind"] == "delay" and f["p"]["beats"] == 0.75 and f["p"]["feedback"] == 0.4 and f["tail"] == 8
    assert f["send"] == [[224.0, 1.0], [224.0, 0.5]] and f["slot"] == 0
    r = parse_text(SMALL + FX.replace("反馈 0.4", "湿度 0.4"))
    assert [i.code for i in r.errors] == ["fx"] and "反馈" in r.errors[0].fix


def test_p1_effects_are_rejected_with_the_line_not_skipped():
    """A22：只有四 Deck 基础的引擎打开带 Roll / Release 的谱：指出缺的能力和行号，不会略过了照放。现在的引擎会，照常编过。"""
    text = SMALL + FX.replace("| F1 | Delay | 发送 | C1 | 57.1 | 65.1 | 拍长 3/4拍；送出 1/2；反馈 0.4；尾音 2小节 | |",
                              "| F1 | Roll | 插入 | C1 | 57.1 | 64.1 | 频段 HIGH；拍长 1/2拍 | |\n"
                              "| F2 | Release | 插入 | C1 | 64.1 | 65.1 | 方式 Echo；退场 | |")
    c = issues(text, caps=BASIC)
    e = [i for i in c.errors if i.code == "caps_fx"]
    assert not c.ok and c.plan is None and len(e) == 2
    assert e[0].line == line_of(text, "| F1 | Roll") and "Roll" in e[0].msg and "场景 1" in e[0].where
    assert e[1].line == line_of(text, "| F2 | Release")
    ok = issues(text)
    assert ok.ok and [u["kind"] for b_ in ok.plan["buses"] for u in b_["units"]] == ["roll", "rel_echo"]


def test_capability_table_limits_decks_and_score_version():
    two = Caps(name="two", max_decks=2)
    r = parse_text(small(**{"| C2 | A2 | D2 |": "| C2 | A2 | D3 |"}))
    c = compile_project(r.project, resolve, two)
    assert [i.code for i in c.errors] == ["caps_decks"] and "D3" in c.errors[0].msg
    c = compile_project(parse_text(small()).project, resolve, MIXXX)  # 连本机 Mixxx：只有旧谱
    assert [i.code for i in c.errors] == ["caps_score"]
    assert BUILTIN.signature() != two.signature()


def test_chart_has_everything_the_page_draws():
    c = compile_project(parse(EXAMPLE).project, resolve)
    ch = chart(c)
    assert ch["decks"] == 4 and ch["bars"] == 240 and len(ch["scenes"]) == 5 and ch["exec"] == c.plan["exec"]
    c3 = next(k for k in ch["clips"] if k["id"] == "C3")
    assert [s["kind"] for s in c3["segs"]] == ["loop", "exit"] and c3["segs"][0]["loop"] == 16
    # 画波形用的：原曲第 s 拍在文件的第 t0 + s × 60 / bpm 秒（和编译器自己算的是同一个数）
    info = c.clip("C3").info
    assert c3["dur"] > 0 and c3["track_id"] == info.track_id
    assert c3["t0"] + 16 * 60 / c3["bpm"] == pytest.approx(info.beat_time(16.0), abs=1e-3)
    assert ch["scenes"][2]["strategy"]["LOW"].startswith("第 97 小节") and ch["scenes"][2]["roles"] == {"D1": "出曲", "D2": "进曲"}
    st = c.state_at(450.0)  # 第 113 小节半：D1 在旧主题的 loop 里，D2 已经接管
    assert st["decks"][0]["state"] == "looping" and st["decks"][0]["loop"]["lap"] == 1 and st["decks"][1]["clip"] == "C2"


def test_the_same_knob_in_two_tables_must_not_overlap():
    """同一个旋钮同一段时间写在两张表里 = 错误（指出两处的行号）；首尾相接可以。"""
    def more(rows: str, clip: str = "C2", col: str = "推子") -> str:
        return small() + f"\n### {clip} 参数\n\n| 位置 | {col} | 说明 |\n|---|---|---|\n{rows}"

    two = more("| 47.1 | 保持 | |\n| 50.1 | 8 | |\n")
    r = parse_text(two)
    assert [i.code for i in r.errors] == ["lane_overlap"], [str(i) for i in r.issues]
    assert r.errors[0].line == line_of(two, "| 47.1 | 保持") and f"第 {line_of(two, '| 48.1 | | 保持')} 行" in r.errors[0].msg
    touch = more("| 49.1 | 保持 | |\n| 57.1 | 6 | |\n")
    assert not parse_text(touch).errors, [str(i) for i in parse_text(touch).errors]
    back = more("| 60.1 | 保持 | |\n| 58.1 | 9点 | |\n", col="MID")
    assert [i.code for i in parse_text(back).errors] == ["param_order"]


def test_the_spec_lists_exactly_the_effects_and_parameters_the_code_knows():
    """《曲谱规范 v5》第 8 节的两张表和代码对得上：效果的种类、每种的参数名。改一边要同时改另一边。"""
    import re

    from djstudio.v5 import livefx
    from djstudio.v5.compile import FX_PARAMS
    from djstudio.v5.score import FX_KEYS, FX_KIND, _key

    spec = (Path(__file__).resolve().parents[1] / "docs" / "SCORE_SPEC_V5.md").read_text(encoding="utf-8")
    part = spec[spec.index("### 8.2"):spec.index("### 8.4")]  # 8.2 发送效果、8.3 现场效果 这两张表
    rows = re.findall(r"^\| `([^`]+)` \| (.+?) \|", part, flags=re.M)
    seen: dict[str, list[str]] = {}
    for name, cell in rows:
        kind = FX_KIND.get(_key(name))
        assert kind, f"规范里的「{name}」解析器不认识"
        names = [re.sub(r"[（(].*", "", part).strip() for part in re.split(r"、(?![^（]*）)", cell)]
        seen[kind] = [n for n in names if n]
    assert set(seen) == set(FX_PARAMS) | set(livefx.LIVE_KINDS), seen.keys()
    zh = {en: z for z, en in FX_KEYS.items()}
    for kind, spec_names in seen.items():
        if kind in FX_PARAMS:
            want = {zh[k] for k in FX_PARAMS[kind] if k in zh} | {"送出", "返回", "尾音"}
        else:
            want = set(livefx.PARAMS[kind]) | ({"尾音"} if kind in ("filter_echo", "spiral") else set())
        assert set(spec_names) == want, (kind, spec_names, sorted(want))
