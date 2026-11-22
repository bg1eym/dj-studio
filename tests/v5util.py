"""v5 测试的公用件：每拍一个脉冲的素材（模拟器里是假 deck 自己出，声音引擎里是合成的 WAV）、
起一个调度器跑一份项目、在录下来的主输出里找脉冲落在第几帧。"""
from __future__ import annotations

import os
import wave
from pathlib import Path
from typing import Any

import numpy as np

from djstudio.grid import Grid
from djstudio.v5.compile import AssetInfo, Compiled5, compile_project
from djstudio.v5.link import Engine5Link, open_engine
from djstudio.v5.model import Project

SR = 44100
BUF = 512
EQ_DELAY = 59  # 通道音频比 deck 位置晚的帧数（Mixxx 默认 EQ 在 250 Hz 的群延迟；模拟器照这个数做）
# 素材：id → (网格 BPM, 第 0 拍秒数)。四首速度、相位都不一样：对得上全靠调度
SPECS = {"A1": (128.0, 0.10), "A2": (130.0, 0.25), "A3": (132.0, 0.05), "A4": (126.0, 0.31)}
ENGINE_BIN = os.environ.get("DJSTUDIO_ENGINE_BIN")
HAVE_ENGINE = bool(ENGINE_BIN and Path(ENGINE_BIN).exists())


def click_wav(path: Path, bpm: float, first: float, dur: float) -> None:
    """和模拟器的假 deck 同一种脉冲：每拍一个 4 ms 的衰减 1 kHz，小节第 1 拍幅度 0.8，其余 0.4。"""
    n = int(dur * SR)
    x = np.zeros(n, dtype=np.float32)
    m = np.arange(int(0.004 * SR))
    k = 0
    while True:
        i = int(round((first + k * 60.0 / bpm) * SR))
        if i + len(m) >= n:
            break
        x[i:i + len(m)] += ((0.8 if k % 4 == 0 else 0.4) * np.exp(-m / (0.001 * SR)) * np.cos(2 * np.pi * 1000 * m / SR))
        k += 1
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(np.repeat((np.clip(x, -1, 1) * 32767).astype("<i2"), 2).tobytes())


def infos(dur: float = 600.0, paths: dict[str, Path] | None = None) -> dict[str, AssetInfo]:
    return {k: AssetInfo(k, Grid(b, fb, 0, dur), path=str(paths[k]) if paths else None, title=k)
            for k, (b, fb) in SPECS.items()}


def project(clips: list[dict[str, Any]], tempo: list[dict[str, Any]] | None = None, **extra: Any) -> Project:
    used = sorted({c["asset"] for c in clips})
    return Project.model_validate({
        "schema": "djset/3", "id": "t", "tempo": tempo or [{"at": "1.1", "bpm": 132}],
        "assets": [{"id": a, "bpm": SPECS[a][0], "title": a} for a in used], "clips": clips, **extra})


def compiled(p: Project, dur: float = 600.0, paths: dict[str, Path] | None = None, **kw: Any) -> Compiled5:
    table = infos(dur, paths)
    return compile_project(p, lambda a: table.get(a.id), **kw)


class Run:
    """一场：起调度器、交计划、可选录音。with 里用。"""

    def __init__(self, c: Compiled5, speed: float = 40.0, sim: bool = True, record: Path | None = None,
                 decks: int | None = None) -> None:
        assert c.ok, [str(i) for i in c.errors]
        self.c = c
        self.e: Engine5Link = open_engine(c.library_rows(), speed=speed, sim=sim, decks=decks)
        self.sim = self.e.name == "sim5"
        self.record = record
        self.rec_frame0 = 0
        self.e.call({"cmd": "v5.plan", "plan": c.plan})
        if record is not None:
            if self.sim:
                self.rec_frame0 = self.e.call({"cmd": "sim.record", "path": str(record)})["frame"]
            else:
                self.rec_frame0 = self.e.call({"cmd": "record", "path": str(record)}).get("frame", 0)

    def __enter__(self) -> "Run":
        return self

    def __exit__(self, *a: Any) -> None:
        self.e.close()

    def start(self, beat: float = 0.0, prerun: float | None = None) -> None:
        cmd: dict[str, Any] = {"cmd": "v5.start", "beat": beat}
        if prerun is not None:
            cmd["prerun"] = prerun
        self.e.call(cmd)

    def state(self) -> str:
        return (self.e.status or {}).get("state", "")

    def beat(self) -> float:
        return float((self.e.status or {}).get("beat", -1e9))

    def wait_state(self, state: str, timeout: float = 60.0) -> None:
        assert self.e.wait(lambda: self.state() == state, timeout), (state, self.e.status and self.e.status["state"],
                                                                     list(self.e.stderr_lines)[-5:])

    def wait_beat(self, beat: float, timeout: float = 60.0) -> dict[str, Any]:
        assert self.e.wait(lambda: self.state() in ("running", "done") and (self.beat() >= beat or self.state() == "done"),
                           timeout), (beat, self.e.status)
        return self.e.status  # type: ignore[return-value]

    def deck(self, d: int) -> dict[str, Any]:
        return self.e.status["decks"][d - 1]  # type: ignore[index]

    def rc(self, rid: str | None = None) -> list[dict[str, Any]]:
        return [r for r in self.e.receipts() if r.get("t") == "rc" and (rid is None or r.get("id") == rid)]

    def audio(self) -> np.ndarray:
        """录下来的主输出（左声道，float）。先停录音。"""
        assert self.record is not None
        if self.sim:
            self.e.call({"cmd": "sim.record", "path": ""})
            return np.fromfile(self.record, dtype=np.float32).reshape(-1, 2)[:, 0].copy()
        self.e.call({"cmd": "record_stop"})
        with wave.open(str(self.record)) as w:
            return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").reshape(-1, 2)[:, 0].astype(np.float32) / 32767

    def beat_of_frame(self, frame: float) -> float:
        """引擎的第 frame 帧是全场第几拍（按 Python 的速度图，从「running」事件的那一对帧 / 拍算）。"""
        ev = next(r for r in self.e.receipts() if r.get("t") == "ev5" and r.get("e") in ("running", "prerun"))
        return self.c.tempo.beat_at(self.c.tempo.sec_at(ev["beat"]) + (frame - ev["frame"]) / SR)

    def frame_of(self, beat: float) -> float:
        """全场第 beat 拍的声音落在录音的第几帧（模拟器：走带起点从「running」事件里取）。"""
        ev = next(r for r in self.e.receipts() if r.get("t") == "ev5" and r.get("e") in ("running", "prerun"))
        sec = self.c.tempo.sec_at(beat) - self.c.tempo.sec_at(ev["beat"])
        return ev["frame"] + sec * SR + EQ_DELAY - self.rec_frame0


def onsets(x: np.ndarray, thr: float = 0.05, gap: int = 1500) -> np.ndarray:
    """脉冲的起点（帧号）：幅度第一次超过 thr 的地方，之后 gap 帧里的不算。"""
    idx = np.flatnonzero(np.abs(x) > thr)
    out: list[int] = []
    for i in idx:
        if not out or i - out[-1] > gap:
            out.append(int(i))
    return np.array(out, dtype=np.int64)


def peak_at(x: np.ndarray, frame: float, width: int = 220) -> float:
    i = int(round(frame))
    if i < 0 or i >= len(x):
        return 0.0
    return float(np.abs(x[max(0, i - 3):i + width]).max())
