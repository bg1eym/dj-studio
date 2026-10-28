"""Python ⇄ DJStudio.js 的分帧协议（与 mapping/DJStudio.js 的传输层必须同步）。

一条消息 = 一个 JSON 对象（ASCII，非 ASCII 字符转义），切成 ≤200 字节的分片：
    分片体 = [seq, part, nparts] + payload 字节（全部 0..127）
SysEx 传输：F0 7D 44 <dir> <分片体> F7，dir = 0x53 'S'（Python→引擎）/ 0x45 'E'（引擎→Python）
CC 传输（备用）：CC2(0) 开始，CC1(byte) × N，CC3(0) 结束；Python→引擎 ch16，引擎→Python ch15

IAC 总线会把每条消息回送给所有监听者（包括发送者自己），所以按方向区分帧头，各自只认对方的帧。
"""
from __future__ import annotations

import json
from typing import Any, Iterator

TO_ENGINE = (0xF0, 0x7D, 0x44, 0x53)
FROM_ENGINE = (0xF0, 0x7D, 0x44, 0x45)
SYSEX_END = 0xF7
CHUNK = 200
CC_TO_ENGINE = 0xBF  # ch16
CC_FROM_ENGINE = 0xBE  # ch15


def encode(obj: dict[str, Any], seq: int) -> list[list[int]]:
    """消息 → 分片体列表。"""
    s = json.dumps(obj, ensure_ascii=True, separators=(",", ":"))
    data = s.encode("ascii")
    n = max(1, -(-len(data) // CHUNK))
    if n > 127:
        raise ValueError(f"消息太大（{len(data)} 字节）")
    return [[seq & 0x7F, part, n, *data[part * CHUNK:(part + 1) * CHUNK]] for part in range(n)]


def to_sysex(body: list[int], head: tuple[int, ...] = TO_ENGINE) -> list[int]:
    return [*head, *body, SYSEX_END]


def to_cc(body: list[int], status: int = CC_TO_ENGINE) -> list[list[int]]:
    return [[status, 2, 0], *([status, 1, b] for b in body), [status, 3, 0]]


class Decoder:
    """把收到的原始 MIDI 消息还原成 JSON 消息。SysEx 与 CC 两种传输都认，只认 head / cc_status 方向的帧。"""

    def __init__(self, head: tuple[int, ...] = FROM_ENGINE, cc_status: int = CC_FROM_ENGINE) -> None:
        self.head = head
        self.cc_status = cc_status
        self._rx: dict[int, dict[str, Any]] = {}
        self._cc: list[int] | None = None
        self.bad = 0

    def feed(self, msg: list[int]) -> Iterator[dict[str, Any]]:
        if not msg:
            return
        if msg[0] == 0xF0:
            if len(msg) >= 8 and tuple(msg[:4]) == self.head and msg[-1] == SYSEX_END:
                yield from self._packet(list(msg[4:-1]))
            return
        if len(msg) == 3 and msg[0] == self.cc_status:
            cc, val = msg[1], msg[2]
            if cc == 2:
                self._cc = []
            elif cc == 1 and self._cc is not None:
                self._cc.append(val)
            elif cc == 3 and self._cc is not None:
                body, self._cc = self._cc, None
                if len(body) >= 3:
                    yield from self._packet(body)

    def _packet(self, p: list[int]) -> Iterator[dict[str, Any]]:
        seq, part, n = p[0], p[1], p[2]
        buf = self._rx.get(seq)
        if buf is None or buf["n"] != n or part == 0:
            buf = self._rx[seq] = {"n": n, "parts": {}}
        buf["parts"][part] = bytes(p[3:])
        if len(buf["parts"]) < n:
            return
        del self._rx[seq]
        raw = b"".join(buf["parts"][i] for i in range(n))
        try:
            yield json.loads(raw.decode("ascii"))
        except (ValueError, UnicodeDecodeError):
            self.bad += 1
