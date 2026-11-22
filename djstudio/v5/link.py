"""和 v5 调度器说话：声音引擎（DJSTUDIO_V5=1）或者独立模拟器（sim5），一行一条。

「#」+ JSON = 命令（带 _id）/ 回复；「@」+ JSON = 调度器主动发的：回执（rc）、事件（ev5）、状态（st5，约 50 ms 一条）。
协议见 docs/ENGINE5.md。这里只管收发，不做决策（指挥在 runner.py）。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

from ..engine.bundled import engine_binary, engine_env

SIM_DIR = Path(__file__).resolve().parents[2] / "tools" / "mixxx-engine" / "djs5"
Listener = Callable[[dict[str, Any]], None]


class Engine5Error(RuntimeError):
    pass


def sim5_binary() -> Path | None:
    """独立模拟器（假 Deck，没有声音）：从 tools/mixxx-engine/djs5/sim5.cpp 现编一个，按源码的哈希缓存。
    只在从源码运行、没有声音引擎时用（开发 / 测试）；打包的应用里总有声音引擎。没有编译器 / 没有源码 = None。"""
    src = SIM_DIR / "sim5.cpp"
    if not src.is_file():
        return None
    cxx = os.environ.get("CXX") or shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
    if not cxx:
        return None
    h = hashlib.sha256()
    for f in sorted(SIM_DIR.glob("*.[ch]pp")):
        if f.name in ("sim5.cpp", "core.hpp", "engine5.hpp", "fx.hpp", "json.hpp"):
            h.update(f.read_bytes())
    out = Path(tempfile.gettempdir()) / f"djstudio-sim5-{h.hexdigest()[:16]}"
    if out.is_file() and os.access(out, os.X_OK):
        return out
    tmp = out.with_suffix(f".{os.getpid()}.tmp")
    r = subprocess.run([cxx, "-O2", "-std=c++17", str(src), "-o", str(tmp), "-lpthread"], capture_output=True, text=True)
    if r.returncode != 0:
        raise Engine5Error(f"模拟器编不出来：{r.stderr[-800:]}")
    tmp.replace(out)
    return out


class Engine5Link:
    """一个调度器进程。library：每份素材一行（id、path、bpm、first_beat、dur、gain），见 Compiled5.library_rows()。"""

    def __init__(self, cmd: list[str], env: dict[str, str], library: list[dict[str, Any]], name: str) -> None:
        self.name = name
        self._p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   bufsize=1, env=env)
        self._lock = threading.Lock()
        self._cv = threading.Condition()
        self._id = 0
        self._replies: dict[int, dict[str, Any]] = {}
        self.status: dict[str, Any] | None = None
        self.status_at = 0.0  # monotonic
        self.status_wall = 0.0  # Unix 秒
        self.events: deque[dict[str, Any]] = deque(maxlen=5000)  # rc / ev5
        self.n_events = 0
        self.listeners: list[Listener] = []
        self.stderr_lines: deque[str] = deque(maxlen=200)
        self.closed = False
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()
        self.library = self.call({"cmd": "library", "tracks": library}, timeout=60).get("tracks", [])

    # ------------------------------------------------------------ 收发

    def _read(self) -> None:
        assert self._p.stdout
        for line in self._p.stdout:
            line = line.strip()
            if len(line) < 2 or line[0] not in "#@":
                continue
            try:
                m = json.loads(line[1:])
            except ValueError:
                self.stderr_lines.append(line[:200])
                continue
            with self._cv:
                if line[0] == "#":
                    self._replies[m.get("_id")] = m
                elif m.get("t") == "st5":
                    self.status = m
                    self.status_at = time.monotonic()
                    self.status_wall = time.time()
                else:
                    self.n_events += 1
                    self.events.append(m)
                self._cv.notify_all()
            if line[0] == "@":
                for fn in list(self.listeners):
                    try:
                        fn(m)
                    except Exception:  # noqa: BLE001 - 监听者出错不能拖垮接收线程
                        pass
        with self._cv:
            self.closed = True
            self._cv.notify_all()

    def _read_err(self) -> None:
        assert self._p.stderr
        for line in self._p.stderr:
            self.stderr_lines.append(line.rstrip())

    def call(self, cmd: dict[str, Any], timeout: float = 5.0) -> dict[str, Any]:
        with self._lock:
            self._id += 1
            n = self._id
            try:
                assert self._p.stdin
                self._p.stdin.write("#" + json.dumps({**cmd, "_id": n}, ensure_ascii=True) + "\n")
                self._p.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as e:
                raise Engine5Error(f"引擎已经关了（{cmd.get('cmd')}）") from e
        with self._cv:
            if not self._cv.wait_for(lambda: n in self._replies or self.closed, timeout):
                raise Engine5Error(f"引擎没有回应 {cmd.get('cmd')}（{timeout:g}s）；stderr：{list(self.stderr_lines)[-3:]}")
            if n not in self._replies:
                raise Engine5Error(f"引擎退出了（{cmd.get('cmd')}）；stderr：{list(self.stderr_lines)[-3:]}")
            r = self._replies.pop(n)
        if r.get("ok") is False:
            raise Engine5Error(f"引擎拒绝 {cmd.get('cmd')}：{r.get('err')}")
        r.pop("_id", None)
        return r

    def wait(self, pred: Callable[[], bool], timeout: float) -> bool:
        """等到 pred 成立（每来一条消息看一次）。"""
        deadline = time.monotonic() + timeout
        with self._cv:
            while not pred():
                left = deadline - time.monotonic()
                if left <= 0 or self.closed:
                    return pred()
                self._cv.wait(min(left, 0.25))
            return True

    def online(self) -> bool:
        return not self.closed and time.monotonic() - self.status_at < 1.5

    def receipts(self, since: int = 0) -> list[dict[str, Any]]:
        with self._cv:
            skip = max(0, len(self.events) - (self.n_events - since))
            return list(self.events)[skip:]

    def audio(self, timeout: float = 5.0) -> dict[str, Any] | None:
        """声音输出的情况（声音引擎才有）。"""
        if self.name != "mixxx-engine":
            return None
        return self.call({"cmd": "audio"}, timeout)

    def close(self) -> None:
        self.closed = True
        try:
            self._p.terminate()
            self._p.wait(timeout=3)
        except Exception:  # noqa: BLE001
            self._p.kill()


def open_engine(library: list[dict[str, Any]], speed: float = 1.0, audio: bool = False, sim: bool = False,
                decks: int | None = None) -> Engine5Link:
    """起一个 v5 调度器：有声音引擎就用它（audio=True 且原速时出声），没有（或 sim=True）就用独立模拟器。"""
    binary = None if sim else engine_binary()
    if binary:
        env = {**engine_env(binary), "DJSTUDIO_V5": "1", "DJSTUDIO_SPEED": str(speed), "DJSTUDIO_AUDIO": "1" if audio else "0"}
        return Engine5Link([str(binary)], env, library, "mixxx-engine")
    simbin = sim5_binary()
    if not simbin:
        raise Engine5Error("找不到 DJ Studio 声音引擎（应用没装完整？重新下载 DJ Studio）")
    env = {**os.environ, "DJSTUDIO_SPEED": str(speed)}
    if decks:
        env["DJSTUDIO_DECKS"] = str(decks)
    return Engine5Link([str(simbin)], env, library, "sim5")
