"""解码 Mixxx 库里 library.beats 的二进制网格（不依赖 protobuf 库）。

完全按 Mixxx 2.5.6 src/track/beats.cpp 的 Beats::fromByteArray 实现：
- "BeatGrid-1.0" / "BeatGrid-2.0"：先按 protobuf BeatGrid 解析（beats.proto：
  BeatGrid { Bpm bpm = 1 { double bpm = 1 }; Beat first_beat = 2 { int32 frame_position = 1 } }），
  解析失败且长度正好 16 字节时，按旧结构 {double bpm; double firstBeat} 读。
- "BeatMap-1.0"：protobuf BeatMap { repeated Beat beat = 1 }，所有 beat 都用（Mixxx 不看 enabled）。
- 位置单位是**帧**（audio::FramePos，每声道一个样本），除以 library.samplerate 得秒。
  （早先按「立体声样本」理解是错的；源码里读写都直接当 FramePos。）
- 恒定网格存的是「最后一个标记」的位置（m_lastMarkerPosition），不一定是曲首第一拍；网格向两边延伸。

元数据网格：曲目带 BPM 标签时 Mixxx 会先按标签建一个首拍在 0、sub_version 为空的网格，
装载时会重新分析（analyzerbeats.cpp shouldAnalyze）。这种网格的相位不可信，视为「没分析」。
"""
from __future__ import annotations

import statistics
import struct
from dataclasses import dataclass


@dataclass
class MixxxGrid:
    bpm: float
    first_beat: float  # 秒：网格锚点拍的位置
    kind: str  # grid | map
    variable: bool = False  # BeatMap 且拍间隔明显不均


class _ParseError(ValueError):
    pass


def _varint(b: bytes, i: int) -> tuple[int, int]:
    shift = 0
    out = 0
    while True:
        if i >= len(b):
            raise _ParseError("truncated varint")
        c = b[i]
        i += 1
        out |= (c & 0x7F) << shift
        if not c & 0x80:
            return out, i
        shift += 7
        if shift > 63:
            raise _ParseError("varint too long")


def _fields(b: bytes) -> list[tuple[int, int, object]]:
    """极简 protobuf 解析：返回 (field_no, wire_type, value)。格式不对抛 _ParseError。"""
    out: list[tuple[int, int, object]] = []
    i = 0
    while i < len(b):
        key, i = _varint(b, i)
        fno, wt = key >> 3, key & 7
        if fno == 0:
            raise _ParseError("field 0")
        if wt == 0:
            v, i = _varint(b, i)
            out.append((fno, wt, v))
        elif wt == 1:
            if i + 8 > len(b):
                raise _ParseError("truncated fixed64")
            out.append((fno, wt, b[i:i + 8]))
            i += 8
        elif wt == 2:
            ln, i = _varint(b, i)
            if i + ln > len(b):
                raise _ParseError("truncated bytes")
            out.append((fno, wt, b[i:i + ln]))
            i += ln
        elif wt == 5:
            if i + 4 > len(b):
                raise _ParseError("truncated fixed32")
            out.append((fno, wt, b[i:i + 4]))
            i += 4
        else:
            raise _ParseError(f"wire type {wt}")
    return out


def _int32(v: int) -> int:
    v &= (1 << 64) - 1
    return v - (1 << 64) if v >= 1 << 63 else v


def _beat_pos(msg: bytes) -> int | None:
    pos = None
    for f2, w2, v2 in _fields(msg):
        if f2 == 1 and w2 == 0:
            pos = _int32(v2)  # type: ignore[arg-type]
    return pos


def _parse_grid_proto(blob: bytes) -> tuple[float, int | None]:
    bpm = 0.0
    pos: int | None = None
    for fno, wt, v in _fields(blob):
        if fno == 1 and wt == 2:
            for f2, w2, v2 in _fields(v):  # type: ignore[arg-type]
                if f2 == 1 and w2 == 1:
                    bpm = struct.unpack("<d", v2)[0]  # type: ignore[arg-type]
        elif fno == 2 and wt == 2:
            pos = _beat_pos(v)  # type: ignore[arg-type]
    return bpm, pos


def decode(blob: bytes | None, version: str | None, samplerate: int) -> MixxxGrid | None:
    if not blob or not version or samplerate <= 0:
        return None
    sr = float(samplerate)
    if version in ("BeatGrid-1.0", "BeatGrid-2.0"):
        try:
            bpm, pos = _parse_grid_proto(blob)
            # protobuf 的可选字段缺省为 0；Mixxx 用 FramePos(0) 照样当有效
            pos = pos if pos is not None else 0
        except _ParseError:
            if len(blob) != 16:
                return None
            bpm, first = struct.unpack("<dd", blob)
            pos = int(first)
        if bpm <= 0:
            return None
        return MixxxGrid(bpm, pos / sr, "grid")
    if version == "BeatMap-1.0":
        try:
            frames = [p for fno, wt, v in _fields(blob) if fno == 1 and wt == 2
                      for p in [_beat_pos(v)] if p is not None]  # type: ignore[arg-type]
        except _ParseError:
            return None
        if len(frames) < 2:
            return None
        frames.sort()
        secs = [p / sr for p in frames]
        iv = [b - a for a, b in zip(secs, secs[1:])]
        med = statistics.median(iv)
        if med <= 0:
            return None
        variable = statistics.pstdev(iv) > med * 0.02
        # 近似成恒定网格（techno 基本恒速）；variable=True 时界面会提示
        return MixxxGrid(60.0 / med, secs[0], "map", variable)
    return None


def is_metadata_grid(grid: MixxxGrid | None, sub_version: str | None) -> bool:
    """按 BPM 标签建的临时网格（首拍在 0、sub_version 为空）：Mixxx 装载时会重新分析。"""
    return grid is not None and not (sub_version or "") and grid.first_beat <= 0


def encode_grid_v2(bpm: float, first_beat_s: float, samplerate: int) -> bytes:
    """生成 BeatGrid-2.0 blob（与 Beats::toBeatGridByteArray 一致；测试和演示用）。"""

    def varint(n: int) -> bytes:
        n &= (1 << 64) - 1
        out = bytearray()
        while True:
            c = n & 0x7F
            n >>= 7
            if n:
                out.append(c | 0x80)
            else:
                out.append(c)
                return bytes(out)

    bpm_msg = b"\x09" + struct.pack("<d", bpm)
    pos = int(first_beat_s * samplerate)  # toLowerFrameBoundary
    beat_msg = b"\x08" + varint(pos)
    return b"\x0a" + varint(len(bpm_msg)) + bpm_msg + b"\x12" + varint(len(beat_msg)) + beat_msg


# ChromaticKey（Mixxx key_id，src/proto/keys.proto）→ Camelot
_CAMELOT = {
    1: "8B", 2: "3B", 3: "10B", 4: "5B", 5: "12B", 6: "7B", 7: "2B", 8: "9B", 9: "4B", 10: "11B", 11: "6B", 12: "1B",
    13: "5A", 14: "12A", 15: "7A", 16: "2A", 17: "9A", 18: "4A", 19: "11A", 20: "6A", 21: "1A", 22: "8A", 23: "3A",
    24: "10A",
}


def camelot(key_id: int | None) -> str | None:
    return _CAMELOT.get(int(key_id)) if key_id else None
