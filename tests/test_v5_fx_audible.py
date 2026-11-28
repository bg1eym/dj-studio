"""规范第 8.7 节的套路在声音上对不对（要声音引擎）：把效果套路示例里每一行的参数搬到台架上——一首满的 groove、
一处效果——带 / 不带各录一遍，量四样：

- 听得出来：效果开着的那一段，和不带效果的差在 −12 dB 以上（0.4.9 的套路有一半在 −13 到 −25 dB，等于没有）；
- 不盖过音乐：差不超过 +4 dB，整体电平的变化不超过 ±3 dB（Roll / Release 这种「断」的另算）；
- 没有削波：录下来的 16 位里没有顶到满刻度的采样（主输出的限幅器）；
- 接进接出是干净的：效果开之前、收完以后，和不带效果的逐采样相同。

Release 另量「不是一个洞」：这一段的电平不比音乐低 9 dB 以上（0.4.9 的 Release Echo 低 18 dB，后半段是数字静音）。
素材是合成的（tests/fxbench.py），不是真曲子：这里钉住的是量，音色好不好听要人听（DEVICE_TEST.md）。
"""
from pathlib import Path

import numpy as np
import pytest

from . import fxbench
from .v5util import HAVE_ENGINE

pytestmark = pytest.mark.skipif(not HAVE_ENGINE, reason="要声音引擎（量的是它录出来的声音）")

EXAMPLE = Path(__file__).resolve().parents[1] / "docs" / "score_v5_fx_example.md"
# 套路 → [(示例谱里的哪一行, 台架上从哪到哪)]；效果量的范围（dB，相对不带效果的）；是不是「断」（Roll / Release：这一段原来的音乐不响）
RECIPES: dict[str, tuple[list[tuple[str, str, str]], tuple[float, float], bool]] = {
    "句尾回声": ([("F1", "8.1", "9.1")], (-12, 4), False),
    "句尾的门": ([("F2", "7.1", "9.1")], (-12, 4), False),
    "Roll 接 Release Echo": ([("F3", "8.1", "8.3"), ("F4", "8.3", "9.1")], (-6, 4), True),
    "噪声上行": ([("F5", "5.1", "9.1")], (-12, 4), False),
    "军鼓 fill": ([("F6", "8.1", "9.1")], (-12, 4), False),
    "HPF Echo": ([("F7", "5.1", "9.1")], (-12, 4), False),
    "Flanger": ([("F8", "5.1", "9.1")], (-12, 4), False),
    "Spiral": ([("F9", "7.1", "9.1")], (-12, 4), False),
    "混响": ([("F10", "5.1", "9.1")], (-12, 4), False),
    "Roll 接刹车": ([("F11", "8.1", "8.3"), ("F12", "8.3", "9.1")], (-6, 4), True),
}


def example_rows() -> dict[str, list[str]]:
    rows = {}
    for ln in EXAMPLE.read_text(encoding="utf-8").splitlines():
        if ln.startswith("| F"):
            cells = [c.strip() for c in ln.strip().strip("|").split("|")]
            rows[cells[0]] = cells
    return rows


def bench_md(parts: list[tuple[str, str, str]]) -> str:
    rows = example_rows()
    out = []
    for n, (fid, start, end) in enumerate(parts, 1):
        _, kind, route, target, _, _, params, _ = rows[fid]
        target = target if route == "采样" else "C1"
        out.append(f"| F{n} | {kind} | {route} | {target} | {start} | {end} | {params} | |")
    return fxbench.HEAD + fxbench.FXHEAD + "\n".join(out) + "\n"


@pytest.fixture(scope="module")
def bench(tmp_path_factory):
    d = tmp_path_factory.mktemp("fxbench")
    asset = d / "bench.wav"
    fxbench.write_wav(asset, fxbench.techno(132.0, 0.25, 64))
    dry, fd, bad = fxbench.render(fxbench.HEAD, asset, d / "dry.wav")
    assert not bad, bad
    return d, asset, dry, fd


def pos(p: str) -> float:
    bar, beat = p.split(".")
    return (int(bar) - 1) * 4.0 + int(beat) - 1.0


def test_the_bench_material_looks_like_a_mastered_track(bench):
    """台架的素材：整体约 −14 dBFS，三个频段都有东西（分频段的效果在它上面量得出来），引擎放出来和文件是同一段。"""
    _, asset, dry, fd = bench
    seg = dry[int(fd[16.0]):int(fd[48.0])].astype(np.float64)
    spec = np.abs(np.fft.rfft(seg)) ** 2
    f = np.fft.rfftfreq(len(seg), 1 / fxbench.SR)
    share = [10 * np.log10(spec[(f >= lo) & (f < hi)].sum() / spec.sum()) for lo, hi in ((0, 250), (250, 2500), (2500, 22050))]
    assert -15.5 < fxbench.db(seg) < -12.5 and share[0] > -3 and -13 < share[1] < -5 and -18 < share[2] < -10, (fxbench.db(seg), share)
    assert 0.6 < np.abs(dry).max() < 0.985  # 单放一首：没到限幅器的上限


@pytest.mark.parametrize("name", list(RECIPES))
def test_recipe_is_audible_clean_and_seamless(bench, name):
    d, asset, dry, fd = bench
    parts, (lo, hi), is_break = RECIPES[name]
    wet, fw, bad = fxbench.render(bench_md(parts), asset, d / f"{name}.wav")
    exits = "退场" in example_rows()[parts[-1][0]][6]
    assert not bad, bad
    s, e = pos(parts[0][1]), pos(parts[-1][2])

    def seg(b0: float, b1: float) -> tuple[np.ndarray, np.ndarray]:
        a, a2 = int(round(fw[b0])), int(round(fd[b0]))
        m = min(int(round(fw[b1])) - a, len(wet) - a, len(dry) - a2)
        return wet[a:a + m], dry[a2:a2 + m]

    w0, d0 = seg(4.0, s - 0.5)
    assert fxbench.db(w0 - d0) < -100, "效果开之前就和干声不一样"
    w1, d1 = seg(s, e)
    diff, level = fxbench.db(w1 - d1) - fxbench.db(d1), fxbench.db(w1) - fxbench.db(d1)
    assert lo <= diff <= hi, f"{name}：和不带效果的差 {diff:+.1f} dB，不在 {lo}…{hi} 里"
    assert int((np.abs(wet) >= 0.999).sum()) == 0 and np.abs(wet).max() <= 0.986, "削波了"
    if is_break:
        assert level > -9, f"{name}：这一段比音乐低 {-level:.1f} dB，是个洞"
    else:
        assert -3 <= level <= 3, f"{name}：电平变了 {level:+.1f} dB"
    if not exits:  # 收完以后回到干声（最后两小节：差在 −85 dB 以下——长混响的尾巴还在衰减，别的是逐采样相同）；退场的是保持静音
        w2, d2 = seg(52.0, 60.0)
        assert fxbench.db(w2 - d2) - fxbench.db(d2) < -85, "收完以后没有回到干声"
    else:
        w2, _ = seg(e + 1.0, 60.0)
        assert fxbench.db(w2) < -90, "退场以后还有声音"
