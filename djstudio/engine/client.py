"""引擎客户端：收发分帧消息、跟踪状态、命令确认、失联检测。线程安全。"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

from .link import Link
from .protocol import Decoder, encode, to_cc, to_sysex

Listener = Callable[[dict[str, Any]], None]


class EngineError(RuntimeError):
    pass


@dataclass
class DeckState:
    loaded: bool
    playing: bool
    pos_ms: int
    dur_ms: int
    bpm: float
    file_bpm: float
    vol: float
    load_seq: int = 0  # 这个 deck 装上新歌的累计次数

    @classmethod
    def from_list(cls, a: list[Any]) -> "DeckState":
        return cls(bool(a[0]), bool(a[1]), int(a[2]), int(a[3]), float(a[4]), float(a[5]), float(a[6]),
                   int(a[7]) if len(a) > 7 else 0)


class EngineClient:
    STATUS_TIMEOUT = 1.5  # 秒没有状态帧 → 视为失联

    def __init__(self, link: Link, transport: str = "sysex") -> None:
        self.link = link
        self.transport = transport
        self._dec = Decoder()
        self._cv = threading.Condition(threading.RLock())
        self._seq = 0
        self._cmd = 0
        self._acks: dict[int, dict[str, Any]] = {}
        self._events: deque[tuple[int, dict[str, Any]]] = deque(maxlen=2000)
        self._ev_n = 0
        self._listeners: list[Listener] = []
        self.status: dict[str, Any] | None = None
        self.last_status_at = 0.0
        self.version: str | None = None
        self.boots = 0  # 收到过几次 boot hello（= Mixxx 加载/重载了映射）
        link.on_raw = self._on_raw

    # ------------------------------------------------------------ 接收

    def _on_raw(self, raw: list[int]) -> None:
        for msg in self._dec.feed(raw):
            self._dispatch(msg)

    def _dispatch(self, msg: dict[str, Any]) -> None:
        t = msg.get("t")
        with self._cv:
            if t == "st":
                self.status = msg
                self.last_status_at = time.monotonic()
            elif t == "ack":
                self._acks[int(msg["c"])] = msg
                if "v" in msg:
                    self.version = msg["v"]
            else:
                if t == "hello":
                    self.version = msg.get("v")
                    if msg.get("boot"):
                        self.boots += 1
                self._ev_n += 1
                self._events.append((self._ev_n, msg))
            self._cv.notify_all()
        for fn in list(self._listeners):
            try:
                fn(msg)
            except Exception:  # noqa: BLE001 - 监听者出错不能拖垮接收线程
                pass

    def add_listener(self, fn: Listener) -> None:
        self._listeners.append(fn)

    def remove_listener(self, fn: Listener) -> None:
        if fn in self._listeners:
            self._listeners.remove(fn)

    # ------------------------------------------------------------ 发送

    def send(self, obj: dict[str, Any], wait: bool = True, timeout: float = 2.0) -> dict[str, Any]:
        with self._cv:
            self._cmd += 1
            c = self._cmd
            seq = self._seq
            self._seq = (self._seq + 1) & 0x7F
        bodies = encode({**obj, "c": c}, seq)
        for body in bodies:
            if self.transport == "cc":
                for m in to_cc(body):
                    self.link.send_raw(m)
            else:
                self.link.send_raw(to_sysex(body))
        if not wait:
            return {}
        with self._cv:
            if not self._cv.wait_for(lambda: c in self._acks, timeout):
                raise EngineError(f"引擎没有回应命令 {obj.get('t')}（{timeout:g}s）")
            ack = self._acks.pop(c)
        if not ack.get("ok"):
            raise EngineError(f"引擎拒绝 {obj.get('t')}：{ack.get('err')}")
        return ack

    # ------------------------------------------------------------ 查询

    def online(self) -> bool:
        return time.monotonic() - self.last_status_at < self.STATUS_TIMEOUT

    def status_age(self) -> float:
        return time.monotonic() - self.last_status_at if self.last_status_at else float("inf")

    def deck(self, d: int) -> DeckState | None:
        st = self.status
        if not st:
            return None
        return DeckState.from_list(st["d"][d - 1])

    def program(self) -> dict[str, Any] | None:
        return self.status.get("pg") if self.status else None

    def mark(self) -> int:
        """事件游标：之后用 wait_event(..., since=mark) 只看这之后的事件。"""
        with self._cv:
            return self._ev_n

    def wait_event(self, match: Callable[[dict[str, Any]], bool], timeout: float, since: int | None = None
                   ) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        since = self.mark() if since is None else since
        with self._cv:
            while True:
                for n, ev in self._events:
                    if n > since and match(ev):
                        return ev
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self._cv.wait(min(left, 0.25))

    def wait_until(self, pred: Callable[[], bool], timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._cv:
            while not pred():
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._cv.wait(min(left, 0.25))
            return True

    def events_since(self, since: int) -> list[dict[str, Any]]:
        with self._cv:
            return [ev for n, ev in self._events if n > since]

    def drain(self, cursor: list[int]) -> list[dict[str, Any]]:
        """取 cursor[0] 之后的新事件，并把游标推进到最新。"""
        with self._cv:
            out = [ev for n, ev in self._events if n > cursor[0]]
            cursor[0] = self._ev_n
            return out
