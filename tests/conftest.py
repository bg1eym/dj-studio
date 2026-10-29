import pytest

from djstudio.compiler import TrackInfo
from djstudio.grid import Grid


def make_info(id: str, bpm: float = 128.0, dur: float = 360.0, first_beat: float = 0.1, downbeat: int = 0) -> TrackInfo:
    return TrackInfo(id=id, title=id, grid=Grid(bpm=bpm, first_beat=first_beat, downbeat=downbeat, duration=dur))


@pytest.fixture(autouse=True)
def _no_side_effects(monkeypatch):
    """测试里不自动 git pull，也不去真的「下载 / 文稿」等文件夹里找曲子（要找的测试自己设 cfg.find_dirs）。"""
    monkeypatch.setenv("DJSTUDIO_NO_UPDATE", "1")
    monkeypatch.setenv("DJSTUDIO_FIND_DIRS", "")


@pytest.fixture
def lib():
    return {
        "a": make_info("a", 128.0, 360.0),
        "b": make_info("b", 130.0, 400.0, 0.25),
        "c": make_info("c", 132.0, 300.0, 0.05, 1),
    }


@pytest.fixture
def resolver(lib):
    return lambda t: lib.get(t.id)


def lane_value(pts, x):
    """与 DJStudio.interp 相同：分段线性，同一拍两点 = 阶跃。"""
    if x < pts[0][0]:
        return pts[0][1]
    i = len(pts) - 1
    while i > 0 and pts[i][0] > x:
        i -= 1
    if i == len(pts) - 1:
        return pts[i][1]
    (a, va), (b, vb) = pts[i][:2], pts[i + 1][:2]
    return va + (vb - va) * (x - a) / (b - a)


MIXER_INDEX = {"hi": 0, "mid": 1, "lo": 2, "filter": 3, "echo": 4, "reverb": 5}


def lanes_mismatch(statuses, prog, tol=0.01):
    """程序运行中每个状态帧：读回的旋钮值 vs 曲线在该帧时间线位置的值。返回 (核对数, 超差数)。"""
    checked = bad = 0
    for st in statuses:
        pg = st.get("pg")
        if not pg or pg["id"] != prog["id"] or pg["st"] != "running" or not 0 <= pg["tl"] <= prog["len"]:
            continue
        for name, pts in prog["lanes"].items():
            side, param = name.split(".")
            if param not in MIXER_INDEX:
                continue
            d = prog["a"]["deck"] if side == "A" else prog["b"]["deck"]
            checked += 1
            bad += abs(st["m"][d - 1][MIXER_INDEX[param]] - lane_value(pts, pg["tl"])) > tol
    return checked, bad
