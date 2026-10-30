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


# 声音引擎解码结果的版本：/2 = 输出按播放时的帧号对齐（Mac 的 CoreAudio 解码器起点不是 0，以前没补，
# 拍线和波形比实际声音早 12–48ms）。改了就会让旧的分析按新解码器重做。
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
    eng = _engine()
    if eng:
        with tempfile.TemporaryDirectory(prefix="djstudio-decode-") as tmp:
            out = Path(tmp) / "mono.f32"
            r = _run_engine(eng, ["--decode", str(path), str(out), str(sr)], 300)
            if r.returncode != 0 or not out.exists():
                raise AudioError(f"解码失败 {path.name}：{r.stderr.decode(errors='replace').strip()[-200:]}")
            return np.fromfile(out, dtype=np.float32)
    try:
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
            capture_output=True, timeout=300)
    except FileNotFoundError as e:
        raise AudioError("没有 ffmpeg（brew install ffmpeg）") from e
    if r.returncode != 0:
        raise AudioError(f"ffmpeg 解码失败 {path.name}：{r.stderr.decode(errors='replace').strip()[:200]}")
    return np.frombuffer(r.stdout, dtype=np.float32).copy()
