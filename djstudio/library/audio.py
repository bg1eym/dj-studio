"""用 ffmpeg / ffprobe 读音频（Mac 上 `brew install ffmpeg`）。"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

AUDIO_EXTS = {".mp3", ".wav", ".aif", ".aiff", ".flac", ".m4a", ".aac", ".ogg", ".opus"}
ANALYSIS_SR = 22050


class AudioError(RuntimeError):
    pass


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


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
    try:
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
            capture_output=True, timeout=300)
    except FileNotFoundError as e:
        raise AudioError("没有 ffmpeg（brew install ffmpeg）") from e
    if r.returncode != 0:
        raise AudioError(f"ffmpeg 解码失败 {path.name}：{r.stderr.decode(errors='replace').strip()[:200]}")
    return np.frombuffer(r.stdout, dtype=np.float32).copy()
