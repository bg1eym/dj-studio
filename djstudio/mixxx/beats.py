"""解码 Mixxx 库里 library.beats 的二进制网格（不依赖 protobuf 库）。

beats_version:
- "BeatGrid-1.0"：两个小端 double {bpm, first_beat(样本)}
- "BeatGrid-2.0"：protobuf BeatGrid { Bpm bpm = 1 { double bpm = 1 }; Beat first_beat = 2 { int32 frame_position = 1 } }
- "BeatMap-1.0"：protobuf BeatMap { repeated Beat beat = 1 { int32 frame_position = 1; bool enabled = 2 } }

位置单位：Mixxx 历来存的是「引擎样本」（立体声交错样本 = 帧 × 2），2.4 起读入时再除以 2。
⚠ 这一点按 Mixxx 源码的记忆写成，需要真机核对：`djstudio probe` 会用引擎回传的 beat_distance 验证。
如果核对结果相反，把 SAMPLES_PER_POS 改成 1。
"""
from __future__ import annotations

import statistics
import struct
from dataclasses import dataclass

SAMPLES_PER_POS = 2  # 存储位置 / 帧


@dataclass
class MixxxGrid:
    bpm: float
    first_beat: float  # 秒
    kind: str  # grid | map
    variable: bool = False  # BeatMap 且拍间隔明显不均


def _varint(b: bytes, i: int) -> tuple[int, int]:
    shift = 0
    out = 0
    while True:
        c = b[i]
        i += 1
        out |= (c & 0x7F) << shift
        if not c & 0x80:
            return out, i
        shift += 7


def _fields(b: bytes) -> list[tuple[int, int, object]]:
    """极简 protobuf 解析：返回 (field_no, wire_type, value)。"""
    out: list[tuple[int, int, object]] = []
    i = 0
    while i < len(b):
        key, i = _varint(b, i)
        fno, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(b, i)
            out.append((fno, wt, v))
        elif wt == 1:
            out.append((fno, wt, b[i:i + 8]))
            i += 8
        elif wt == 2:
            ln, i = _varint(b, i)
            out.append((fno, wt, b[i:i + ln]))
            i += ln
        elif wt == 5:
            out.append((fno, wt, b[i:i + 4]))
            i += 4
        else:
            raise ValueError(f"不支持的 protobuf wire type {wt}")
    return out


def _int32(v: int) -> int:
    return v - (1 << 64) if v >= 1 << 63 else v


def decode(blob: bytes | None, version: str | None, samplerate: int) -> MixxxGrid | None:
    if not blob or not version or samplerate <= 0:
        return None
    sr = float(samplerate)
    if version == "BeatGrid-1.0":
        bpm, first = struct.unpack("<dd", blob[:16])
        return MixxxGrid(bpm, first / SAMPLES_PER_POS / sr, "grid")
    if version.startswith("BeatGrid"):
        bpm = 0.0
        pos = 0
        for fno, wt, v in _fields(blob):
            if fno == 1 and wt == 2:
                for f2, w2, v2 in _fields(v):  # type: ignore[arg-type]
                    if f2 == 1 and w2 == 1:
                        bpm = struct.unpack("<d", v2)[0]  # type: ignore[arg-type]
            elif fno == 2 and wt == 2:
                for f2, w2, v2 in _fields(v):  # type: ignore[arg-type]
                    if f2 == 1 and w2 == 0:
                        pos = _int32(v2)  # type: ignore[arg-type]
        return MixxxGrid(bpm, pos / SAMPLES_PER_POS / sr, "grid") if bpm > 0 else None
    if version.startswith("BeatMap"):
        beats: list[int] = []
        for fno, wt, v in _fields(blob):
            if fno == 1 and wt == 2:
                pos, enabled = None, True
                for f2, w2, v2 in _fields(v):  # type: ignore[arg-type]
                    if f2 == 1 and w2 == 0:
                        pos = _int32(v2)  # type: ignore[arg-type]
                    elif f2 == 2 and w2 == 0:
                        enabled = bool(v2)
                if pos is not None and enabled:
                    beats.append(pos)
        if len(beats) < 2:
            return None
        beats.sort()
        secs = [p / SAMPLES_PER_POS / sr for p in beats]
        iv = [b - a for a, b in zip(secs, secs[1:])]
        med = statistics.median(iv)
        variable = statistics.pstdev(iv) > med * 0.02
        # 用首拍 + 中位拍长近似成恒定网格（techno 基本都是恒速）
        return MixxxGrid(60.0 / med, secs[0], "map", variable)
    return None


def encode_grid_v2(bpm: float, first_beat_s: float, samplerate: int) -> bytes:
    """生成 BeatGrid-2.0 blob（测试用，也可用于纠正网格）。"""

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
    pos = round(first_beat_s * samplerate * SAMPLES_PER_POS)
    beat_msg = b"\x08" + varint(pos)
    return b"\x0a" + varint(len(bpm_msg)) + bpm_msg + b"\x12" + varint(len(beat_msg)) + beat_msg


# ChromaticKey（Mixxx key_id）→ Camelot
_CAMELOT = {
    1: "8B", 2: "3B", 3: "10B", 4: "5B", 5: "12B", 6: "7B", 7: "2B", 8: "9B", 9: "4B", 10: "11B", 11: "6B", 12: "1B",
    13: "5A", 14: "12A", 15: "7A", 16: "2A", 17: "9A", 18: "4A", 19: "11A", 20: "6A", 21: "1A", 22: "8A", 23: "3A",
    24: "10A",
}


def camelot(key_id: int | None) -> str | None:
    return _CAMELOT.get(int(key_id)) if key_id else None
