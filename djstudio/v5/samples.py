"""采样 fill 的音源：内置的几种鼓（代码合成，固定不变），或者谱旁边的短音频文件。

引擎不解码采样：这里把它们变成引擎采样率的 float32 立体声裸数据，存在缓存目录里，计划里写路径和帧数。
内置的声音是确定的（固定的随机种子）；改了合成方法要把 SAMPLE_VERSION 加一（缓存文件名里带着它）。
"""
from __future__ import annotations

import hashlib
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

SAMPLE_VERSION = 1
BUILTIN_ZH = {"底鼓": "kick", "军鼓": "snare", "拍手": "clap", "踩镲": "hat", "开镲": "openhat", "边击": "rim", "通鼓": "tom",
              "吊镲": "crash"}
AUDIO_EXTS = (".wav", ".mp3", ".aif", ".aiff", ".flac", ".m4a", ".aac", ".ogg", ".opus")


@dataclass(frozen=True)
class SampleInfo:
    path: str  # float32 立体声裸数据
    frames: int


def _env(n: int, sr: int, decay_ms: float) -> np.ndarray:
    return np.exp(-np.arange(n) / (decay_ms / 1000.0 * sr))


def _noise(n: int, seed: int) -> np.ndarray:
    return np.random.default_rng(seed).uniform(-1.0, 1.0, n)


def _highpass(x: np.ndarray, sr: int, hz: float) -> np.ndarray:
    """一阶高通（够把噪声做成镲的样子）。"""
    a = 1.0 / (1.0 + 2 * np.pi * hz / sr)
    y = np.zeros_like(x)
    prev_x = prev_y = 0.0
    for i, v in enumerate(x):
        prev_y = a * (prev_y + v - prev_x)
        prev_x = v
        y[i] = prev_y
    return y


def _sweep(n: int, sr: int, f0: float, f1: float, tau_ms: float) -> np.ndarray:
    t = np.arange(n) / sr
    f = f1 + (f0 - f1) * np.exp(-t / (tau_ms / 1000.0))
    return np.sin(2 * np.pi * np.cumsum(f) / sr)


def builtin(name: str, sr: int = 44100) -> np.ndarray | None:
    """内置采样（单声道 float，峰值约 0.9）。不认识的名字 = None。"""
    name = BUILTIN_ZH.get(name, name.lower())
    ms = lambda v: int(v / 1000.0 * sr)  # noqa: E731
    if name == "kick":
        n = ms(220)
        x = _sweep(n, sr, 160.0, 48.0, 28.0) * _env(n, sr, 70.0)
    elif name == "snare":
        n = ms(180)
        x = 0.6 * _highpass(_noise(n, 11), sr, 1500.0) * _env(n, sr, 45.0) + 0.5 * _sweep(n, sr, 240.0, 170.0, 20.0) * _env(n, sr, 35.0)
    elif name == "clap":
        n = ms(160)
        x = np.zeros(n)
        body = _highpass(_noise(n, 13), sr, 900.0)
        for k, at in enumerate((0, 11, 22)):
            i = ms(at)
            x[i:] += body[: n - i] * _env(n - i, sr, 9.0 if k < 2 else 50.0) * (0.7 if k < 2 else 1.0)
    elif name == "hat":
        n = ms(45)
        x = _highpass(_noise(n, 17), sr, 7000.0) * _env(n, sr, 9.0)
    elif name == "openhat":
        n = ms(280)
        x = _highpass(_noise(n, 19), sr, 6000.0) * _env(n, sr, 80.0)
    elif name == "rim":
        n = ms(35)
        x = np.sin(2 * np.pi * 1700.0 * np.arange(n) / sr) * _env(n, sr, 5.0)
    elif name == "tom":
        n = ms(260)
        x = _sweep(n, sr, 210.0, 95.0, 45.0) * _env(n, sr, 80.0)
    elif name == "crash":
        n = ms(1500)
        x = _highpass(_noise(n, 23), sr, 4000.0) * _env(n, sr, 380.0)
    else:
        return None
    fade = ms(2)
    x[-fade:] *= np.linspace(1.0, 0.0, fade)  # 结尾收干净（采样器放到头就停，不另外淡出）
    return (0.9 * x / max(1e-9, float(np.max(np.abs(x))))).astype(np.float32)


def _write(cache: Path, key: str, mono: np.ndarray) -> SampleInfo:
    cache.mkdir(parents=True, exist_ok=True)
    out = cache / f"{key}.f32"
    if not out.exists() or out.stat().st_size != mono.size * 8:
        tmp = out.with_suffix(".tmp")
        np.repeat(mono.astype("<f4"), 2).tofile(tmp)
        tmp.replace(out)
    return SampleInfo(str(out), int(mono.size))


def resolver(cache: Path | None = None, folders: list[Path] | None = None, sr: int = 44100,
             decode: Callable[[Path, int], np.ndarray] | None = None) -> Callable[[str], SampleInfo | None]:
    """名字 → 采样。先看内置的；再在 folders 里找这个文件名（要有 decode：把音频读成单声道 float）。"""
    cache = cache or Path(tempfile.gettempdir()) / "djstudio-samples"

    def find(name: str) -> SampleInfo | None:
        x = builtin(name, sr)
        if x is not None:
            return _write(cache, f"builtin-{BUILTIN_ZH.get(name, name.lower())}-v{SAMPLE_VERSION}-{sr}", x)
        if decode is None or not name.lower().endswith(AUDIO_EXTS):
            return None
        for d in folders or []:
            f = d / name
            if f.is_file():
                h = hashlib.sha256(f.read_bytes()).hexdigest()[:16]
                out = cache / f"file-{h}-{sr}.f32"
                if out.exists():
                    return SampleInfo(str(out), out.stat().st_size // 8)
                return _write(cache, f"file-{h}-{sr}", np.asarray(decode(f, sr), dtype=np.float32))
        return None

    return find
