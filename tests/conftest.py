import pytest

from djstudio.compiler import TrackInfo
from djstudio.grid import Grid


def make_info(id: str, bpm: float = 128.0, dur: float = 360.0, first_beat: float = 0.1, downbeat: int = 0) -> TrackInfo:
    return TrackInfo(id=id, title=id, grid=Grid(bpm=bpm, first_beat=first_beat, downbeat=downbeat, duration=dur))


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
