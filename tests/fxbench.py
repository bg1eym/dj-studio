"""效果的台架：一段像真曲子的合成 techno（频谱、响度都接近母带），和「一首歌 + 一处效果」的小谱。

为什么要它：效果对不对不能只看回执。0.4.9 的套路回执全是 ok，实际上句尾回声、噪声、混响比音乐低十几 dB（听不出来），
Release Echo 抓到的是空的（一小节的洞），回声类的效果把主输出顶过满刻度（硬削波）。这些只有把声音录下来量才看得出来。
仓库里演示用的合成器（djstudio/synth.py）几乎只有 kick 和低音，分频段的效果在它上面量不出来，所以这里另做一份。
"""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

SR = 44100
LAYOUT = (("groove", 64),)

HEAD = """# 效果台架

- 规范：v5
- 主速：132

## 素材

| ID | 曲名 | 文件名 | 原 BPM | 第 1 小节（秒） | 小节数 | 调性 |
|---|---|---|---|---|---|---|
| A1 | Bench — Track | bench.wav | 132 | 0.250 | 64 | 8A |

## 片段

| ID | 素材 | Deck | 原曲起点 | 全场起播 | 全场停止 | 角色 | 推子 | LOW | MID | HIGH | Filter | 备注 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| C1 | A1 | D1 | 9.1 | 1.1 | 17.1 | 主曲 | 10 | 12点 | 12点 | 12点 | 关 | groove |

## 场景 1：台架（1.1 – 17.1）

- 状态：草稿
"""
FXHEAD = "\n### 效果\n\n| ID | 类型 | 路由 | 目标 | 开始 | 结束 | 参数 | 说明 |\n|---|---|---|---|---|---|---|---|\n"


def _env(n: int, a: float, d: float) -> np.ndarray:
    t = np.arange(n) / SR
    return np.minimum(1.0, t / max(a, 1e-4)) * np.exp(-t / d)


def _bp(x: np.ndarray, lo: float, hi: float) -> np.ndarray:
    spec = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / SR)
    spec[(f < lo) | (f > hi)] = 0
    return np.fft.irfft(spec, len(x))


def techno(bpm: float, first: float, bars: int, seed: int = 21) -> np.ndarray:
    """整段都是满的 groove：kick（带点击）、反拍的低音、16 分的闭镲、反拍的开镲、2 / 4 拍的拍手、中频的合成器动机。
    各频段占总能量大约 LOW −1 / MID −9 / HIGH −14 dB，整体 −14 dBFS RMS，峰值软削波在 1 以内。同样的参数结果逐位相同。"""
    rng = np.random.default_rng(seed)
    beat = 60.0 / bpm
    n = int((first + bars * 4 * beat + 2.0) * SR)
    x = np.zeros(n)

    def put(sig: np.ndarray, t: float, g: float) -> None:
        i = int(round(t * SR))
        j = min(n, i + len(sig))
        if 0 <= i < n:
            x[i:j] += g * sig[:j - i]

    t = np.arange(int(0.35 * SR)) / SR
    kick = np.sin(2 * np.pi * (48 * t + 110 * 0.03 * (1 - np.exp(-t / 0.03)))) * np.exp(-t / 0.12)
    kick[:300] += 0.5 * _bp(rng.standard_normal(300), 1500, 6000) * np.exp(-np.arange(300) / 60)
    tb = np.arange(int(beat / 2 * SR)) / SR

    def bass(f: float) -> np.ndarray:
        return (np.sin(2 * np.pi * f * tb) + 0.4 * np.sin(4 * np.pi * f * tb)) * _env(len(tb), 0.005, 0.12)

    chh = _bp(rng.standard_normal(int(0.05 * SR)), 7000, 14000) * _env(int(0.05 * SR), 0.001, 0.012)
    ohh = _bp(rng.standard_normal(int(0.25 * SR)), 6000, 13000) * _env(int(0.25 * SR), 0.002, 0.07)
    clap = _bp(rng.standard_normal(int(0.2 * SR)), 900, 4500) * _env(int(0.2 * SR), 0.001, 0.05)

    def stab(f: float) -> np.ndarray:
        tt = np.arange(int(0.22 * SR)) / SR
        saw = sum(np.sin(2 * np.pi * f * k * tt) / k for k in range(1, 9))
        return _bp(saw, 250, 3500) * _env(len(tt), 0.004, 0.09)

    riff = [220.0, 0, 261.6, 0, 220.0, 329.6, 0, 261.6, 220.0, 0, 293.7, 0, 220.0, 329.6, 261.6, 0]
    stabs = {f: stab(f) for f in set(riff) if f}
    for bar in range(bars):
        t0 = first + bar * 4 * beat
        for b in range(4):
            tb0 = t0 + b * beat
            put(kick, tb0, 0.85)
            put(bass(55.0 if (bar % 4) < 2 else 49.0), tb0 + beat / 2, 0.42)
            put(ohh, tb0 + beat / 2, 0.30)
            for s in range(4):
                put(chh, tb0 + s * beat / 4, 0.22 if s % 2 == 0 else 0.13)
            if b in (1, 3):
                put(clap, tb0, 0.42)
        for s, f in enumerate(riff):
            if f and s % 2 == 0:
                put(stabs[f], t0 + s * beat / 4, 0.36)
    a, b = int(first * SR), int((first + bars * 4 * beat) * SR)
    rms = float(np.sqrt(np.mean(x[a:b] ** 2))) or 1.0
    return (np.tanh(x * (10 ** (-13 / 20) / rms) * 1.15) / 1.15).astype(np.float32)


def write_wav(path: Path, x: np.ndarray) -> None:
    """44.1 kHz、立体声、16 位。"""
    pcm = (np.clip(x, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(np.repeat(pcm, 2).tobytes())


def db(v: np.ndarray) -> float:
    return 20.0 * np.log10(max(1e-9, float(np.sqrt(np.mean(np.square(v, dtype=np.float64))))))


def render(md: str, asset: Path, out: Path, speed: float = 20.0):
    """用声音引擎放一遍、录下来。返回（左声道, 第几拍 → 录音里的第几帧, 不是 ok 的回执）。"""
    from djstudio.grid import Grid
    from djstudio.v5.compile import AssetInfo, compile_project
    from djstudio.v5.score import parse_text

    from .v5util import Run

    r = parse_text(md, "bench")
    assert r.ok, [str(i) for i in r.issues]

    def resolve(a):
        return AssetInfo(a.id, Grid(a.bpm, a.bar1_s or 0.0, 0, (a.bars or 64) * 240 / a.bpm + (a.bar1_s or 0) + 2), path=str(asset), title=a.title)

    c = compile_project(r.project, resolve)
    assert c.ok, [str(i) for i in c.issues if i.level == "error"]
    with Run(c, speed=speed, sim=False, record=out) as run:
        run.start()
        run.wait_state("done", 300)
        bad = [(m["id"], m["st"], m.get("msg")) for m in run.rc() if m["st"] != "ok"]
        x = run.audio()
        frames = {float(b): run.frame_of(float(b)) for b in np.arange(0, c.length + 1, 0.5)}
    return x, frames, bad
