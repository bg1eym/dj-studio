"""端到端：conductor 驱动 **DJ Studio 声音引擎**（tools/mixxx-engine：Mixxx 2.5.6 的引擎核心）。

需要先按 tools/mixxx-engine/README.md 构建，并设置 DJSTUDIO_ENGINE_BIN=<djstudio-engine 的路径>，否则跳过。
DJSTUDIO_HARNESS_RECORD=<路径.wav> 时把整套主输出录下来。测试里不出声（DJSTUDIO_AUDIO=0），按倍速跑。
"""
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from djstudio.compiler import TrackInfo, compile_set
from djstudio.engine.bundled import engine_env
from djstudio.engine.client import EngineClient
from djstudio.engine.conductor import Conductor
from djstudio.engine.link import EngineLink
from djstudio.grid import Grid
from djstudio.setfile import SetFile
from djstudio.synth import techno, write_wav

from .conftest import lanes_mismatch

BIN = os.environ.get("DJSTUDIO_ENGINE_BIN")
pytestmark = pytest.mark.skipif(not BIN or not Path(BIN).exists(), reason="没有构建 DJ Studio 声音引擎")

SPEED = 3.0
SPECS = {  # id: (bpm, first_beat, downbeat, 小节数)
    "a": (128.0, 0.10, 0, 80),
    "b": (130.0, 0.25, 2, 80),
    "c": (132.0, 0.05, 1, 64),
}


@pytest.fixture(scope="module")
def tracks(tmp_path_factory):
    d = tmp_path_factory.mktemp("harness_tracks")
    out = {}
    for k, (bpm, fb, db, bars) in SPECS.items():
        p = d / f"{k}.wav"
        layout = (("intro", 8), ("groove", bars // 2 - 8), ("breakdown", 8), ("drop", bars // 2 - 16), ("outro", 8))
        write_wav(p, techno(bpm, fb, db, layout, seed=ord(k)))
        out[k] = p
    return out


def run_set(tracks, record: str | None = None):
    link = EngineLink(Path(BIN), [], speed=SPEED)
    client = EngineClient(link)
    assert client.wait_until(client.online, 30)
    lib = link.sim({"cmd": "library", "tracks": [
        {"id": k, "path": str(p), "bpm": SPECS[k][0], "first_beat": SPECS[k][1]} for k, p in tracks.items()]},
        timeout=60)["tracks"]
    infos = {t["id"]: TrackInfo(t["id"], t["id"], Grid(t["bpm"], SPECS[t["id"]][1], 0, t["duration"])) for t in lib}
    s = SetFile.model_validate({
        "schema": "djset/2", "id": "harness",
        "tracks": [{"id": "a", "title": "A"}, {"id": "b", "title": "B", "downbeat": 2},
                   {"id": "c", "title": "C", "downbeat": 1}],
        "transitions": [
            {"technique": "longhand", "start": 41, "b_in": 1, "bars": 24},
            {"technique": "instant", "start": 49, "b_in": 33},
        ],
        "ending": {"mode": "fade", "bar": 48, "bars": 8},
    })
    compiled = compile_set(s, lambda t: infos.get(t.id))
    assert compiled.ok, [str(i) for i in compiled.issues]
    link.sim({"cmd": "queue", "ids": ["a", "b", "c"]})
    if record:
        link.sim({"cmd": "record", "path": record})
    cond = Conductor(client, compiled, stall_timeout=8 * SPEED)
    samples: list[float] = []
    statuses: list[dict] = []
    client.add_listener(lambda m: statuses.append(m) if m.get("t") == "st" else None)

    def sampler():
        p1 = compiled.programs[0]
        while cond.running or not cond.progress.finished_at:
            if cond.progress.stage == "transition" and cond.progress.program == "tr1":
                st = link.sim({"cmd": "state"})
                d1, d2 = st["decks"]
                if d1["playing"] and d2["playing"]:
                    samples.append((d1["beat"] - p1["a"]["start"]) - (d2["beat"] - p1["b"]["in"]))
            time.sleep(0.1)

    cond.start()
    th = threading.Thread(target=sampler, daemon=True)
    th.start()
    cond.join(600)
    if record:
        link.sim({"cmd": "record_stop"}, timeout=30)
    state = link.sim({"cmd": "state"})
    link.sim({"cmd": "quit"})
    time.sleep(0.5)
    link.close()
    return cond, samples, state, link, statuses, compiled


def kick_flams(wav: Path, t0: float, t1: float) -> tuple[int, int]:
    """录音里 [t0, t1) 秒的 kick 起音数与「双击」数（两次起音相隔 < 80ms = 两个 kick 没对齐）。"""
    import numpy as np

    from djstudio.library import analysis as A
    from djstudio.library.audio import decode_mono

    x = decode_mono(wav, 44100)
    fr = A.stft_bands(x, 44100)
    k = A._norm(fr.low_flux)
    t = A.frame_time(np.arange(len(k)), 44100)
    pk = [i for i in range(2, len(k) - 2) if k[i] > 0.25 and k[i] == k[i - 2:i + 3].max()]
    on = t[pk]
    on = on[(on >= t0) & (on < t1)]
    return len(on), int((np.diff(on) < 0.08).sum())


def test_full_set_on_real_mixxx(tracks, tmp_path):
    record = os.environ.get("DJSTUDIO_HARNESS_RECORD") or str(tmp_path / "set.wav")
    cond, samples, state, link, statuses, compiled = run_set(tracks, record)
    log = "\n".join(cond.progress.log)
    assert cond.progress.stage == "done", log + f"\n{cond.progress.error}\n" + "\n".join(link.stderr_lines[-20:])
    assert "装错" not in log and "失联" not in log and "小节错位" not in log, log
    assert samples, "没采到过渡中两边都在播的样本"
    # 引擎报告的位置：B 起播靠 quantize 相位同步，两边时间线拍位应几乎一致
    assert max(abs(x) for x in samples) < 0.05, samples
    assert not any(d["playing"] for d in state["decks"])
    assert state["xFaderMode"] == 0
    # 网页混音台的读数：真 Mixxx 控件读回的 EQ / 滤波跟着曲线走；电平表是 Mixxx 按真实音频算的
    checked, bad = lanes_mismatch(statuses, compiled.programs[0])
    assert checked > 50 and bad <= checked * 0.03, (checked, bad)
    solo = [st for st in statuses if st["d"][0][1] and not st["d"][1][1]]
    idle_vu = sorted(st["m"][1][6] for st in solo)
    assert solo and max(st["m"][0][6] for st in solo) > 0.2 and idle_vu[len(idle_vu) // 2] < 0.01, idle_vu[-30:]
    assert max(st["mv"] for st in solo) > 0.2
    # 真正听到的声音：过渡 1 两个 kick 都响的那一段（A 第 41 小节起 + 6..18 小节）不能有双击
    a_bar = 60 * 4 / SPECS["a"][0]
    t0 = SPECS["a"][1] + 40 * 4 * 60 / SPECS["a"][0] + 6 * a_bar
    n, flams = kick_flams(Path(record), t0, t0 + 12 * a_bar)
    assert n > 20 and flams == 0, (n, flams)


def test_engine_decodes_like_ffmpeg_and_probes(tracks, tmp_path):
    """分析用的解码：Mixxx 的解码器 + 引擎里的重采样，对 WAV 与 ffmpeg 一致；--probe 给出时长和采样率。"""
    p = tracks["a"]
    r = subprocess.run([BIN, "--probe", str(p)], capture_output=True, env=engine_env(Path(BIN)), timeout=60)
    info = json.loads(next(ln for ln in r.stdout.decode().splitlines() if ln.startswith("{")))
    ff = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(p)],
                                   capture_output=True, check=True).stdout)
    assert info["samplerate"] == 22050 and abs(info["duration"] - float(ff["format"]["duration"])) < 0.01, info
    out = tmp_path / "a.f32"
    for sr in (22050, 11025):
        subprocess.run([BIN, "--decode", str(p), str(out), str(sr)], check=True, env=engine_env(Path(BIN)), timeout=120)
        a = np.fromfile(out, dtype=np.float32)
        b = np.frombuffer(subprocess.run(["ffmpeg", "-v", "error", "-i", str(p), "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
                                         capture_output=True, check=True).stdout, dtype=np.float32)
        n = min(len(a), len(b))
        assert abs(len(a) - len(b)) <= 2 and np.corrcoef(a[sr:n - sr], b[sr:n - sr])[0, 1] > 0.999, sr


def test_engine_reports_audio_state_without_sound(tracks):
    link = EngineLink(Path(BIN), [], speed=1.0, audio=False)
    try:
        a = link.audio()
        assert a["on"] is False and "device" in a
    finally:
        link.sim({"cmd": "quit"})
        link.close()


def test_engine_sound_follows_device_clock(tracks, tmp_path, monkeypatch):
    """出声时引擎按声卡的时钟走：用按真实时钟取数据的假声卡跑 15 秒，引擎时间 ≈ 墙钟，没有断音，取到的就是在播的歌。"""
    import wave

    out = tmp_path / "device.wav"
    monkeypatch.setenv("DJSTUDIO_AUDIO_FAKE", str(out))
    link = EngineLink(Path(BIN), [{"id": "a", "path": str(tracks["a"]), "bpm": SPECS["a"][0], "first_beat": SPECS["a"][1]}],
                      speed=1.0, audio=True)
    try:
        assert link.audio()["on"] and link.audio()["device"] == "fake"
        link.sim({"cmd": "load", "deck": 1, "id": "a"})
        time.sleep(1.0)
        t0, e0 = time.monotonic(), link.sim({"cmd": "state"})["t"]
        link.sim({"cmd": "set", "group": "[Channel1]", "key": "play", "value": 1})
        time.sleep(15)
        dt_wall = (time.monotonic() - t0) * 1000
        a, eng = link.audio(), link.sim({"cmd": "state"})["t"]
    finally:
        link.sim({"cmd": "quit"})
        time.sleep(0.5)
        link.close()
    # 引擎只比声卡多准备一个缓冲区的量（约 46ms；声卡在引擎开始处理前就已经在取静音）；声卡按真实时钟取
    assert abs(eng - a["played_ms"]) < 120, (eng, a)
    assert abs(eng - e0 - dt_wall) < 0.03 * dt_wall, (eng - e0, dt_wall)
    assert a["underruns"] == 0, a
    with wave.open(str(out)) as w:
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).reshape(-1, 2) / 32768.0
    assert abs(len(x) / 44100 - (dt_wall / 1000 + 1.0)) < 1.0  # 假声卡从启动起一直在取
    assert x[:22050].std() < 1e-4 and x[-44100 * 10:].std() > 0.005  # 起播前是静音，之后是在播的歌（前奏比较轻）


def test_studio_builtin_show_uses_engine(tmp_path, monkeypatch):
    """应用里的内置引擎：分析用引擎解码，开始演出用引擎跑（倍速时不出声），全程不碰 Mixxx。"""
    from djstudio.config import Config
    from djstudio.mixxx import process
    from djstudio.studio import Studio

    def boom(*a, **k):
        raise AssertionError("内置引擎不该碰 Mixxx")

    for name in ("mixxx_running", "launch_mixxx", "quit_mixxx"):
        monkeypatch.setattr(process, name, boom)
    monkeypatch.setenv("DJSTUDIO_ENGINE_BIN", BIN)
    src = tmp_path / "src"
    src.mkdir()
    for k in ("a", "b"):
        bpm, fb, db, bars = SPECS[k]
        write_wav(src / f"{k}.wav", techno(bpm, fb, db, (("intro", 8), ("groove", 24), ("outro", 8)), seed=ord(k)))
    studio = Studio(Config(home=tmp_path / "home", library=tmp_path / "lib", mixxx_dir=tmp_path / "no-mixxx",
                           browse_roots=[tmp_path]))
    res = studio.library.import_paths([str(src)])
    assert studio.library.wait_idle(120) and len(res.added) == 2
    ids = [studio.store.by_path(str(studio.cfg.library / "src" / f"{k}.wav")).id for k in ("a", "b")]
    s = SetFile.model_validate({"schema": "djset/2", "id": "eng", "tracks": [{"id": i} for i in ids],
                                "transitions": [{"technique": "instant", "start": 25, "b_in": 9}]})
    studio.save(s)
    assert studio.compile(s).ok
    studio.play("eng", speed=2.0)
    es = studio.engine_state()
    assert es["link"] == "mixxx-engine" and es["builtin"] and es["audio"]["on"] is False
    studio.conductor.join(120)
    p = studio.conductor.progress
    assert p.stage == "done", "\n".join(p.log) + f"\n{p.error}"
