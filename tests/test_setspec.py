"""播放规范 docs/SET_SPEC.md 和代码一致：表里的每一行都要和代码算出来的一样（改一边不改另一边就失败）。
另外测 hot cue 的格式检查，以及按 set 放 hot cue（v2：垫子没有固定用途）。"""
import math
import re
from pathlib import Path

import pytest

from djstudio.compiler import fader_gain
from djstudio.plan.convert import eq_db, filter_val
from djstudio.setfile import HotCue, SetTrack
from djstudio.setspec import CUE_KINDS, SPEC_VERSION, fmt_time
from djstudio.studio import LOUDNESS_TARGET, PEAK_CEILING_DB

SPEC = (Path(__file__).resolve().parents[1] / "docs" / "SET_SPEC.md").read_text(encoding="utf-8")


def table(header: str) -> list[list[str]]:
    """md 里以 header 开头的那张表（不含表头和分隔行）。"""
    lines = SPEC.splitlines()
    i = next(k for k, ln in enumerate(lines) if ln.startswith(header))
    rows = []
    for ln in lines[i + 2:]:
        if not ln.startswith("|"):
            break
        rows.append([c.strip() for c in ln.strip("|").split("|")])
    assert rows, header
    return rows


def hours(text: str) -> float:
    """钟点位 → 离 12 点几个钟头（左负右正）：「11 点半」= -0.5，「9 点」= -3，「2 点」= 2。"""
    m = re.match(r"^(\d+) 点(半)?$", text.strip())
    assert m, text
    c = int(m.group(1))
    return (c - 12 if c >= 7 else c) + (0.5 if m.group(2) else 0)


def test_version():
    assert SPEC.startswith(f"# DJ Studio Set 播放规范 v{SPEC_VERSION}")


def test_eq_table_matches_code():
    rows = table("| 钟点位 | dB |")
    assert len(rows) == 16
    for name, db in rows:
        assert eq_db(hours(name)) == pytest.approx(float(db), abs=0.051), name


def test_filter_table_matches_code():
    for name, v in table("| 谱 | set 值 |"):
        h = 0.0 if name == "关" else hours(name[1:])
        assert filter_val(h) == pytest.approx(float(v)), name


def test_fader_table_matches_code():
    rows = table("| 推子 | 位置 | Mixxx volume | 实际增益 dB |")
    assert [r[0] for r in rows] == [str(n) for n in range(11)]
    for n, pos, vol, db in rows:
        g = fader_gain(float(pos))
        assert float(pos) == int(n) / 10
        assert g == pytest.approx(float(vol), abs=0.0006), n
        assert (db == "-∞") if g <= 0 else 20 * math.log10(g) == pytest.approx(float(db), abs=0.051), n


def test_hot_cue_kind_table_matches_code():
    rows = table("| 名字 | 颜色 | 什么时候放 |")
    assert [(n, c, w) for n, c, w in rows] == [(k.names, f"{k.color_name} {k.color}", k.when) for k in CUE_KINDS]
    assert "没有固定用途" in SPEC


def test_loudness_and_crossfader_rules():
    assert f"**{LOUDNESS_TARGET:g} LUFS**" in SPEC and f"**{PEAK_CEILING_DB:g} dBFS**" in SPEC


def test_hot_cue_format_rules():
    with pytest.raises(ValueError):
        HotCue(pad=9, name="X", bar=1)
    with pytest.raises(ValueError):
        HotCue(pad=1, name="进点", bar=1)  # 名字只能用英文（Prime 屏幕上显示），中文写 note
    with pytest.raises(ValueError):
        HotCue(pad=1, name="A" * 17, bar=1)
    with pytest.raises(ValueError):
        SetTrack(id="x", hot_cues=[{"pad": 2, "name": "A", "bar": 1}, {"pad": 2, "name": "B", "bar": 5}])
    assert HotCue(pad=1, name="START", bar=1, color="#a45cff").color == "#A45CFF"
    assert fmt_time(97.4164) == "1:37.416" and fmt_time(5.0) == "0:05.000"
