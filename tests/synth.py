"""合成测试用的 techno 片段（已知 BPM / 首拍 / 小节相位 / 段落）。"""
import wave
from pathlib import Path

import numpy as np

SR = 22050


def _kick(sr):
    t = np.arange(int(0.25 * sr)) / sr
    f = 50 + 100 * np.exp(-t / 0.03)
    return np.sin(2 * np.pi * np.cumsum(f) / sr) * np.exp(-t / 0.12)


def _hat(sr, rng):
    n = int(0.03 * sr)
    return np.diff(rng.standard_normal(n + 1)) * np.exp(-np.arange(n) / (0.008 * sr)) * 0.15


def techno(bpm=128.0, first_beat=0.25, downbeat=2, layout=(("intro", 16), ("groove", 32), ("breakdown", 16),
                                                          ("drop", 32), ("outro", 8)), sr=SR, seed=1):
    """layout 的小节从第 1 小节开始；第 1 小节起点 = 网格第 downbeat 拍。之前的拍（pickup）只放 hat。"""
    rng = np.random.default_rng(seed)
    beat = 60.0 / bpm
    nbars = sum(n for _, n in layout)
    total_beats = downbeat + nbars * 4
    dur = first_beat + total_beats * beat + 1.0
    x = np.zeros(int(dur * sr))
    kick, hat = _kick(sr), _hat(sr, rng)

    def put(sig, t, gain=1.0):
        i = int(round(t * sr))
        j = min(len(x), i + len(sig))
        if i < len(x):
            x[i:j] += gain * sig[: j - i]

    sec_of_bar = []
    for name, n in layout:
        sec_of_bar += [name] * n
    for k in range(total_beats):
        t = first_beat + k * beat
        bar = (k - downbeat) // 4  # 0 起
        name = sec_of_bar[bar] if 0 <= bar < nbars else "intro"
        has_kick = name in ("groove", "drop")
        if has_kick:
            put(kick, t, 0.9)
            # 低音：反拍
            tt = np.arange(int(beat / 2 * sr)) / sr
            put(np.sign(np.sin(2 * np.pi * 55 * tt)) * 0.15 * np.exp(-tt / 0.1), t + beat / 2)
        if name != "breakdown":
            put(hat, t + beat / 2)
        if name in ("breakdown", "drop") and (k - downbeat) % 4 == 0:
            tt = np.arange(int(4 * beat * sr)) / sr
            pad = sum(np.sin(2 * np.pi * f * tt) for f in (220, 277, 330)) * 0.05
            put(pad * np.minimum(1, tt / 0.5), t)
        if (k - downbeat) % 16 == 0 and 0 <= bar < nbars:  # 4 小节一次的镲片，标记小节线
            put(rng.standard_normal(int(0.4 * sr)) * np.exp(-np.arange(int(0.4 * sr)) / (0.1 * sr)) * 0.1, t)
    x = x / (np.abs(x).max() or 1) * 0.8
    return x.astype(np.float32)


def write_wav(path: Path, x: np.ndarray, sr=SR):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
