"""读音频：有 DJ Studio 声音引擎（djstudio-engine）就用它解码——和播放用的是同一个 Mixxx 解码器
（Mac 上是 CoreAudio），分析出的拍子位置和实际播放一致；没有引擎时（开发 / 测试）用 ffmpeg / ffprobe。"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

AUDIO_EXTS = {".mp3", ".wav", ".aif", ".aiff", ".flac", ".m4a", ".aac", ".ogg", ".opus"}
ANALYSIS_SR = 22050


class AudioError(RuntimeError):
    pass


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def have_decoder() -> bool:
    from ..engine.bundled import engine_binary

    return engine_binary() is not None or have_ffmpeg()


# 声音引擎解码结果的版本：/2 = 输出按播放时的帧号对齐（解码器起点不是 0 时前面补静音；Mac 的 CoreAudio 可能不是 0，
# CI 上实测 mp3 / aac / wav 都是 0）。改了就会让旧的分析按新解码器重做。
ENGINE_DECODER = "mixxx-engine/2"


def decoder_name() -> str:
    """现在用哪个解码器（写进分析结果：换了解码器要重新分析，mp3 开头的编码延迟两边处理可能不同）。"""
    return ENGINE_DECODER if _engine() else "ffmpeg"


def _engine() -> Path | None:
    from ..engine.bundled import engine_binary

    return engine_binary()


def _run_engine(binary: Path, args: list[str], timeout: float) -> subprocess.CompletedProcess[bytes]:
    from ..engine.bundled import engine_env

    try:
        return subprocess.run([str(binary), *args], capture_output=True, timeout=timeout, env=engine_env(binary))
    except (OSError, subprocess.TimeoutExpired) as e:
        raise AudioError(f"声音引擎没能运行：{e}") from e


@dataclass
class Probe:
    duration: float
    samplerate: int
    channels: int
    codec: str
    bitrate: int
    title: str
    artist: str
    album: str


def probe(path: Path) -> Probe:
    eng = _engine()
    if eng:
        r = _run_engine(eng, ["--probe", str(path)], 60)
        line = next((ln for ln in r.stdout.decode(errors="replace").splitlines() if ln.startswith("{")), "{}")
        d = json.loads(line)
        if r.returncode != 0 or "error" in d:
            raise AudioError(f"读不了 {path.name}：{d.get('error') or r.stderr.decode(errors='replace').strip()[-200:]}")
        return Probe(duration=float(d["duration"]), samplerate=int(d["samplerate"]), channels=int(d["channels"]),
                     codec=d.get("codec", ""), bitrate=int(d.get("bitrate") or 0), title=d.get("title", ""),
                     artist=d.get("artist", ""), album=d.get("album", ""))
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "format=duration,bit_rate:format_tags=title,artist,album:stream=codec_name,sample_rate,channels",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60)
    except FileNotFoundError as e:
        raise AudioError("没有 ffprobe（brew install ffmpeg）") from e
    if r.returncode != 0:
        raise AudioError(f"ffprobe 读不了 {path.name}：{r.stderr.strip()[:200]}")
    d = json.loads(r.stdout or "{}")
    fmt = d.get("format", {})
    st = (d.get("streams") or [{}])[0]
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    return Probe(
        duration=float(fmt.get("duration") or 0),
        samplerate=int(st.get("sample_rate") or 0),
        channels=int(st.get("channels") or 0),
        codec=st.get("codec_name") or "",
        bitrate=int(fmt.get("bit_rate") or 0),
        title=tags.get("title", ""),
        artist=tags.get("artist", ""),
        album=tags.get("album", ""),
    )


def decode_mono(path: Path, sr: int = ANALYSIS_SR) -> np.ndarray:
    return decode_mono_stats(path, sr)[0]


def decode_mono_stats(path: Path, sr: int = ANALYSIS_SR) -> tuple[np.ndarray, dict[str, float]]:
    """解码成单声道 + 响度（LUFS）和采样峰值。声音引擎按立体声算（BS.1770）；退回 ffmpeg 时按单声道近似。"""
    eng = _engine()
    if eng:
        with tempfile.TemporaryDirectory(prefix="djstudio-decode-") as tmp:
            out = Path(tmp) / "mono.f32"
            r = _run_engine(eng, ["--decode", str(path), str(out), str(sr)], 300)
            if r.returncode != 0 or not out.exists():
                raise AudioError(f"解码失败 {path.name}：{r.stderr.decode(errors='replace').strip()[-200:]}")
            x = np.fromfile(out, dtype=np.float32)
            stats: dict[str, float] = {}
            for line in r.stdout.decode(errors="replace").splitlines():
                if line.startswith("{"):
                    try:
                        stats = {k: float(v) for k, v in json.loads(line).items()}
                    except (ValueError, TypeError):
                        stats = {}
            if "loudness" not in stats:  # 旧版引擎没有：按单声道估
                stats = {"loudness": loudness_mono(x, sr), "peak": float(np.max(np.abs(x))) if len(x) else 0.0}
            return x, stats
    x = _ffmpeg_mono(path, sr)
    return x, {"loudness": loudness_mono(x, sr), "peak": float(np.max(np.abs(x))) if len(x) else 0.0}


def loudness_mono(x: np.ndarray, sr: int) -> float:
    """单声道近似的积分响度（LUFS）：K 计权（频域乘幅度响应）、400 ms 块、-70 / -10 LU 门限。
    单声道 = 0.707 (L+R)：两声道相同时功率和立体声算法一致，完全不相关时低 3 dB。"""
    n = len(x)
    if n < sr // 2:
        return -70.0
    nfft = 1 << (n - 1).bit_length()
    f = np.fft.rfftfreq(nfft, 1.0 / sr)
    w = 2 * np.pi * f / sr
    z = np.exp(1j * w)

    def biquad(b: list[float], a: list[float]) -> np.ndarray:
        return np.abs((b[0] + b[1] / z + b[2] / z**2) / (1 + a[0] / z + a[1] / z**2))

    K = np.tan(np.pi * 1681.974450955533 / sr)
    vh = 10 ** (3.999843853973347 / 20)
    vb = vh ** 0.4996667741545416
    q = 0.7071752369554196
    a0 = 1 + K / q + K * K
    h1 = biquad([(vh + vb * K / q + K * K) / a0, 2 * (K * K - vh) / a0, (vh - vb * K / q + K * K) / a0],
                [2 * (K * K - 1) / a0, (1 - K / q + K * K) / a0])
    K = np.tan(np.pi * 38.13547087602444 / sr)
    q = 0.5003270373238773
    a0 = 1 + K / q + K * K
    h2 = biquad([1.0, -2.0, 1.0], [2 * (K * K - 1) / a0, (1 - K / q + K * K) / a0])
    y = np.fft.irfft(np.fft.rfft(x.astype(np.float64), nfft) * h1 * h2, nfft)[:n]
    sub = int(round(sr * 0.1))
    m = (y[: n // sub * sub] ** 2).reshape(-1, sub).mean(axis=1)
    if len(m) < 4:
        return -70.0
    blocks = (m[:-3] + m[1:-2] + m[2:-1] + m[3:]) / 4
    lv = -0.691 + 10 * np.log10(np.maximum(blocks, 1e-12))
    g = blocks[lv > -70]
    if not len(g):
        return -70.0
    rel = -0.691 + 10 * np.log10(g.mean()) - 10
    g = blocks[(lv > -70) & (lv > rel)]
    return round(float(-0.691 + 10 * np.log10(g.mean())), 2) if len(g) else -70.0


def _ffmpeg_mono(path: Path, sr: int) -> np.ndarray:
    try:
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
            capture_output=True, timeout=300)
    except FileNotFoundError as e:
        raise AudioError("没有 ffmpeg（brew install ffmpeg）") from e
    if r.returncode != 0:
        raise AudioError(f"ffmpeg 解码失败 {path.name}：{r.stderr.decode(errors='replace').strip()[:200]}")
    return np.frombuffer(r.stdout, dtype=np.float32).copy()
