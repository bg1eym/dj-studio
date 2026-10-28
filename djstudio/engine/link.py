"""MIDI 传输：真机走 IAC 总线（python-rtmidi），测试走子进程里的假 Mixxx（sim/fake_mixxx.mjs）。

两者接口一致：send_raw(msg) 发一条原始 MIDI 消息；on_raw 回调收到的原始消息。
"""
from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

RawCallback = Callable[[list[int]], None]


class LinkError(RuntimeError):
    pass


class Link:
    on_raw: RawCallback | None = None

    def send_raw(self, msg: list[int]) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass

    @property
    def name(self) -> str:
        return type(self).__name__


def list_ports() -> dict[str, list[str]]:
    import rtmidi  # type: ignore

    return {"in": rtmidi.MidiIn().get_ports(), "out": rtmidi.MidiOut().get_ports()}


def fix_port_name(name: str) -> str:
    """rtmidi 在 macOS 上把中文口名按 MacRoman 解码成乱码（如「IAC驱动程序 总线1」）。仅用于显示。"""
    try:
        return name.encode("mac_roman").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


class RtMidiLink(Link):
    def __init__(self, port_match: str = "IAC") -> None:
        try:
            import rtmidi  # type: ignore
        except ImportError as e:  # pragma: no cover - 仅 Mac 上有
            raise LinkError("没有安装 python-rtmidi") from e
        self._out = rtmidi.MidiOut()
        self._in = rtmidi.MidiIn()
        self._port_name = ""
        out_idx = self._find(self._out.get_ports(), port_match)
        in_idx = self._find(self._in.get_ports(), port_match)
        if out_idx is None or in_idx is None:
            ports = [fix_port_name(p) for p in self._out.get_ports()]
            raise LinkError(f"找不到包含 {port_match!r} 的 MIDI 口；现有：{ports}。请在「音频 MIDI 设置」里启用 IAC 驱动程序。")
        self._port_name = fix_port_name(self._out.get_ports()[out_idx])
        self._out.open_port(out_idx)
        # SysEx 默认会被 rtmidi 丢弃，必须打开
        self._in.ignore_types(sysex=False, timing=True, active_sense=True)
        self._in.set_callback(self._cb)
        self._in.open_port(in_idx)

    @staticmethod
    def _find(ports: list[str], match: str) -> int | None:
        m = match.lower()
        for i, p in enumerate(ports):
            if m in p.lower() or m in fix_port_name(p).lower():
                return i
        return None

    def _cb(self, event: tuple[list[int], float], data: Any = None) -> None:
        if self.on_raw:
            self.on_raw(list(event[0]))

    def send_raw(self, msg: list[int]) -> None:
        self._out.send_message(msg)

    def close(self) -> None:
        self._in.cancel_callback()
        self._in.close_port()
        self._out.close_port()

    @property
    def name(self) -> str:
        return f"IAC:{self._port_name}"


SIM_SCRIPT = Path(__file__).resolve().parents[2] / "sim" / "fake_mixxx.mjs"


class SimLink(Link):
    """在 Node 子进程里运行假 Mixxx + 真正的 DJStudio.js。

    stdin/stdout 一行一条：MIDI 消息用十六进制；以 '#' 开头的行是模拟器控制命令 / 回复（JSON）。
    """

    def __init__(self, library: list[dict[str, Any]], speed: float = 1.0, mapping: Path | None = None,
                 extra_args: list[str] | None = None) -> None:
        mapping = mapping or Path(__file__).parent / "mapping" / "DJStudio.js"
        args = ["node", str(SIM_SCRIPT), "--mapping", str(mapping), "--speed", str(speed)] + (extra_args or [])
        self._p = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, bufsize=1)
        self._lock = threading.Lock()
        self._sim_replies: list[dict[str, Any]] = []
        self._sim_cv = threading.Condition()
        self._sim_id = 0
        self.stderr_lines: list[str] = []
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()
        self.sim({"cmd": "library", "tracks": library})

    def _read(self) -> None:
        assert self._p.stdout
        for line in self._p.stdout:
            line = line.strip()
            if not line:
                continue
            if line.startswith("#"):
                with self._sim_cv:
                    self._sim_replies.append(json.loads(line[1:]))
                    self._sim_cv.notify_all()
                continue
            if self.on_raw:
                self.on_raw(list(bytes.fromhex(line)))

    def _read_err(self) -> None:
        assert self._p.stderr
        for line in self._p.stderr:
            self.stderr_lines.append(line.rstrip())

    def _write(self, line: str) -> None:
        with self._lock:
            assert self._p.stdin
            self._p.stdin.write(line + "\n")
            self._p.stdin.flush()

    def send_raw(self, msg: list[int]) -> None:
        self._write(bytes(msg).hex())

    def sim(self, cmd: dict[str, Any], timeout: float = 5.0) -> dict[str, Any]:
        """给模拟器发控制命令并等回复（故障注入、查询内部状态、设置 AutoDJ 队列……）。"""
        with self._sim_cv:
            self._sim_id += 1
            sid = self._sim_id
            self._write("#" + json.dumps({**cmd, "_id": sid}))

            def find() -> dict[str, Any] | None:
                return next((r for r in self._sim_replies if r.get("_id") == sid), None)

            if not self._sim_cv.wait_for(lambda: find() is not None, timeout):
                raise LinkError(f"模拟器无响应：{cmd.get('cmd')}；stderr: {self.stderr_lines[-5:]}")
            r = find()
            self._sim_replies.remove(r)  # type: ignore[arg-type]
            return r  # type: ignore[return-value]

    def close(self) -> None:
        try:
            self._p.terminate()
            self._p.wait(timeout=3)
        except Exception:  # noqa: BLE001
            self._p.kill()

    @property
    def name(self) -> str:
        return "sim"
