"""针对 Mixxx 2.5.6 源码核对出来的行为的回归测试（每条都对应 docs/MIXXX_NOTES.md 里的一项）。"""
import shutil
import sqlite3
import struct
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from djstudio.config import Config
from djstudio.engine.client import EngineClient
from djstudio.engine.link import SimLink
from djstudio.mixxx import fakedb, process
from djstudio.mixxx.beats import decode, encode_grid_v2, is_metadata_grid
from djstudio.mixxx.db import MixxxDB
from djstudio.studio import MAPPING_DIR, Job, Studio

node = pytest.mark.skipif(shutil.which("node") is None, reason="需要 node")


# ---------------------------------------------------------------- 映射 XML

def test_xml_has_sysex_script_binding():
    """midicontroller.cpp:577：没有 status=0xF0 的 script-binding，SysEx 根本到不了 incomingData。"""
    root = ET.parse(MAPPING_DIR / "DJStudio.midi.xml").getroot()
    sysex = [c for c in root.iter("control") if c.findtext("status", "").lower() == "0xf0"]
    assert sysex, "缺少 0xF0 输入映射"
    assert sysex[0].find("options/script-binding") is not None


# ---------------------------------------------------------------- 网格解码（beats.cpp）

def test_grid_positions_are_frames():
    blob = encode_grid_v2(128.0, 0.5, 44100)  # 0.5 秒 = 22050 帧
    g = decode(blob, "BeatGrid-2.0", 44100)
    assert g and g.first_beat == pytest.approx(0.5) and g.bpm == 128.0


def test_grid_negative_and_missing_first_beat():
    # first_beat 字段缺省 → FramePos(0)
    bpm_only = b"\x0a\x09\x09" + struct.pack("<d", 140.0)
    g = decode(bpm_only, "BeatGrid-2.0", 48000)
    assert g and g.first_beat == 0.0
    neg = encode_grid_v2(128.0, -0.25, 44100)
    assert decode(neg, "BeatGrid-2.0", 44100).first_beat == pytest.approx(-0.25)


def test_legacy_grid_struct():
    # BeatGrid-1.0：protobuf 解析失败且正好 16 字节 → {double bpm; double firstBeat(帧)}
    blob = struct.pack("<dd", 125.0, 44100.0)
    g = decode(blob, "BeatGrid-1.0", 44100)
    assert g and g.bpm == 125.0 and g.first_beat == pytest.approx(1.0)


def test_beatmap_and_metadata_grid():
    def beat(frames):
        v = bytearray()
        n = frames
        while True:
            c = n & 0x7F
            n >>= 7
            v.append(c | (0x80 if n else 0))
            if not n:
                break
        body = b"\x08" + bytes(v)
        return b"\x0a" + bytes([len(body)]) + body

    sr = 44100
    frames = [int(sr * (0.1 + k * 60 / 130)) for k in range(64)]
    g = decode(b"".join(beat(f) for f in frames), "BeatMap-1.0", sr)
    assert g and g.bpm == pytest.approx(130, abs=0.05) and not g.variable
    meta = decode(encode_grid_v2(128.0, 0.0, sr), "BeatGrid-2.0", sr)
    assert is_metadata_grid(meta, "")
    assert not is_metadata_grid(meta, "plugin_id=x")


# ---------------------------------------------------------------- 曲库结构（schema.xml v39）

def test_real_schema_and_autodj_queue(tmp_path):
    db = tmp_path / "mixxxdb.sqlite"
    fakedb.create(db, ["/m"])
    m = MixxxDB(db)
    assert m.schema_problems() == []
    a = fakedb.add_track(db, "/m/a.mp3", 300, 128, 0.3)
    b = fakedb.add_track(db, "/m/b.mp3", 300, 126, 0.0, sub_version="")  # BPM 标签建的网格
    c = fakedb.add_track(db, "/m/c.mp3", 300, None)  # 扫描进库、没分析
    t = m.tracks_by_location(["/m/a.mp3", "/m/b.mp3", "/m/c.mp3"])
    assert t["/m/a.mp3"].analyzed and not t["/m/b.mp3"].analyzed and not t["/m/c.mp3"].analyzed
    m.write_autodj_queue([c, a, b])
    assert m.autodj_queue() == [c, a, b]
    rows = sqlite3.connect(db).execute("SELECT position, pl_datetime_added FROM PlaylistTracks ORDER BY position").fetchall()
    assert [r[0] for r in rows] == [1, 2, 3] and all(r[1] for r in rows)


# ---------------------------------------------------------------- mixxx.cfg 控制器映射

def test_configure_controller_replaces_agentmcp(tmp_path):
    mixxx = tmp_path / "mixxx"
    (mixxx / "controllers").mkdir(parents=True)
    fakedb.create(mixxx / "mixxxdb.sqlite")
    (mixxx / "mixxx.cfg").write_text(
        "[Config]\nVersion 2.5.6\n\n[Controller]\nIAC驱动程序_总线1 1\n\n[ControllerPreset]\n"
        f"IAC驱动程序_总线1 {mixxx}/controllers/AgentMCP.midi.xml\n\n[Library]\nRescanOnStartup 0\n", encoding="utf-8")
    st = Studio(Config(home=tmp_path / "home", library=tmp_path / "lib", mixxx_dir=mixxx, browse_roots=[tmp_path], engine="mixxx"))
    job = Job("t")
    st._configure_controller(job)
    cfg = process.read_cfg(mixxx / "mixxx.cfg")
    assert cfg["[ControllerPreset]"]["IAC驱动程序_总线1"].endswith("controllers/DJStudio.midi.xml")
    assert cfg["[Controller]"]["IAC驱动程序_总线1"] == "1"
    assert cfg["[Config]"]["Version"] == "2.5.6"
    ok, _ = process.mapping_status(cfg)
    assert ok


# ---------------------------------------------------------------- 引擎在假 Mixxx 里的行为

LIB = [{"id": "x", "dur": 200.0, "bpm": 128.0, "first_beat": 0.1},
       {"id": "y", "dur": 210.0, "bpm": 130.0, "first_beat": 0.2},
       {"id": "z", "dur": 220.0, "bpm": 132.0, "first_beat": 0.3}]


@pytest.fixture
def rig():
    link = SimLink(LIB, speed=20)
    c = EngineClient(link)
    assert c.wait_until(c.online, 5)
    yield link, c
    link.close()


@node
def test_eject_empty_deck_does_not_reload(rig):
    """basetrackplayer.cpp:396：空 deck 上 eject 会装回上一首弹出的歌——引擎必须先判断是否已装载。"""
    link, c = rig
    link.sim({"cmd": "load", "deck": 1, "id": "x"})
    assert c.wait_until(lambda: c.deck(1).loaded, 3)
    c.send({"t": "eject", "deck": 1})
    assert c.wait_until(lambda: not c.deck(1).loaded, 3)
    time.sleep(0.6)  # 超过 500ms 双击窗口
    c.send({"t": "eject", "deck": 1})  # 空 deck 再弹一次：不能把 x 装回来
    time.sleep(0.5)
    assert not c.deck(1).loaded


@node
def test_engine_sets_additive_crossfader_and_orientation(rig):
    link, c = rig
    st = link.sim({"cmd": "state"})
    assert st["xFaderMode"] == 0
    assert c.send({"t": "get", "keys": [["[Channel1]", "orientation"], ["[Channel2]", "orientation"]]})["vals"] == [0, 2]


@node
def test_fresh_load_must_be_deck1(rig):
    _, c = rig
    from djstudio.engine.client import EngineError
    with pytest.raises(EngineError, match="deck1"):
        c.send({"t": "load", "deck": 2})


@node
def test_autodj_refusal_fails_fast(rig):
    link, c = rig
    link.sim({"cmd": "queue", "ids": []})
    cur = [c.mark()]
    c.send({"t": "load", "deck": 1})
    t0 = time.monotonic()
    ev = None
    while ev is None and time.monotonic() - t0 < 5:
        ev = next((e for e in c.drain(cur) if e.get("e") in ("loaded", "load_failed")), None)
        time.sleep(0.05)
    assert ev and ev["e"] == "load_failed" and ev["st"] == "autodj_refused"


@node
def test_queue_semantics_across_loads(rig):
    """AutoDJ 装载时不移除队首；下次开启时如果在播 deck 的歌在队首才移除（autodjprocessor.cpp:410）。"""
    link, c = rig
    link.sim({"cmd": "queue", "ids": ["x", "y", "z"]})

    def load(d):
        cur = [c.mark()]
        c.send({"t": "load", "deck": d})
        t0 = time.monotonic()
        while time.monotonic() - t0 < 5:
            for e in c.drain(cur):
                if e.get("e") in ("loaded", "load_failed"):
                    return e
            time.sleep(0.05)

    e1 = load(1)
    assert e1["e"] == "loaded" and e1["dur"] == 200000
    c.send({"t": "play", "deck": 1, "on": 1})  # 开场
    time.sleep(0.3)
    ds2 = c.deck(2)
    if not (ds2.loaded and ds2.dur_ms == 210000):  # 没被 P1LOADED 的预载抢先装上
        e2 = load(2)
        assert e2["e"] == "loaded" and e2["dur"] == 210000
    # 换边：deck2 播、deck1 停，再往 deck1 装 → 应该是 z
    c.send({"t": "play", "deck": 2, "on": 1})
    c.send({"t": "play", "deck": 1, "on": 0})
    time.sleep(0.2)
    e3 = load(1)
    assert e3["e"] == "loaded" and e3["dur"] == 220000
