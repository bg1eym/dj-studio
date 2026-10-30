"""Conductor：按编译好的 set 逐段指挥引擎。

分工：引擎（DJStudio.js）负责实时执行（采样级时间线、曲线、起播、相位校正）；
conductor 只做「慢」的事：装载下一首并核对身份、提前下发下一段程序、监控进度、处理异常。

硬规则（来自旧系统的教训）：
- 所有等待都有上限；
- 引擎状态帧 >1.5s 没来 = 失联：立刻报警、停发一切命令；超过 lost_timeout 仍未恢复则放弃；
- 装载后用时长（±1s）+ 网格 BPM 核对身份，对不上宁可停也不错放；
- Mixxx 重载了映射（收到 boot hello）= 引擎状态丢失，停止。
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Callable

from ..compiler import Compiled, deck_for
from .client import EngineClient, EngineError

DUR_TOL_MS = 1000  # Mixxx 库里的时长可能被取整；再加 BPM 指纹一起核对
BPM_TOL = 0.1


class StopRequested(Exception):
    pass


class ConductorError(RuntimeError):
    pass


@dataclass
class Progress:
    set_id: str
    n_tracks: int
    stage: str = "idle"  # idle starting loading armed transition solo ending done stopping stopped error
    index: int = 0  # 当前主奏的曲目（0 起）
    program: str | None = None
    message: str = ""
    engine_ok: bool = True
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    log: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["log"] = self.log[-200:]
        return d


class Conductor:
    def __init__(self, client: EngineClient, compiled: Compiled,
                 on_progress: Callable[[Progress], None] | None = None,
                 on_log: Callable[[str], None] | None = None,
                 load_timeout: float = 20.0, lost_timeout: float = 60.0, stall_timeout: float = 8.0,
                 stop_fade: float = 4.0) -> None:
        if not compiled.ok:
            raise ConductorError("set 校验没通过，不能播放")
        self.c = client
        self.compiled = compiled
        self.tracks = compiled.tracks
        self.on_progress = on_progress
        self.on_log = on_log
        self.load_timeout = load_timeout
        self.lost_timeout = lost_timeout
        self.stall_timeout = stall_timeout
        self.stop_fade = stop_fade  # 手动停止时淡出几秒
        self.progress = Progress(set_id=compiled.set.id, n_tracks=len(compiled.set.tracks))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._boots0 = 0

    # ------------------------------------------------------------ 外部接口

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, name="conductor", daemon=True)
        self._thread.start()

    def request_stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ------------------------------------------------------------ 日志 / 进度

    def log(self, msg: str) -> None:
        line = f"{datetime.now():%H:%M:%S} {msg}"
        self.progress.log.append(line)
        if self.on_log:
            self.on_log(line)
        self._publish()

    def _stage(self, stage: str, message: str = "", **kw: Any) -> None:
        self.progress.stage = stage
        self.progress.message = message
        for k, v in kw.items():
            setattr(self.progress, k, v)
        self._publish()

    def _publish(self) -> None:
        if self.on_progress:
            self.on_progress(self.progress)

    def _title(self, k: int) -> str:
        t = self.compiled.set.tracks[k]
        return f"#{k + 1} {t.title or t.id}"

    # ------------------------------------------------------------ 主流程

    def run(self) -> None:
        self.progress.started_at = time.time()
        try:
            self._run()
            self._stage("done", "整套播完")
            self.log("✓ 整套播完")
        except StopRequested:
            self._fade_out_all(self.stop_fade)
            self._stage("stopped", "已手动停止")
            self.log("■ 已停止")
        except Exception as e:  # noqa: BLE001
            self.progress.error = str(e)
            self._stage("error", str(e))
            self.log(f"!! 出错停止：{e}")
            try:
                self.c.send({"t": "abort"}, timeout=1.0)
            except Exception:  # noqa: BLE001
                pass
        finally:
            self.progress.finished_at = time.time()
            self._publish()

    def _run(self) -> None:
        self._stage("starting", "连接引擎")
        if not self.c.wait_until(self.c.online, 10.0):
            raise ConductorError("引擎不在线：Mixxx 没开，或 IAC 总线没有启用「DJ Studio」映射")
        self.c.send({"t": "hello"})
        self._boots0 = self.c.boots
        self.log(f"引擎在线（DJStudio.js {self.c.version}，{self.c.link.name}）")

        for d in (1, 2):
            ds = self.c.deck(d)
            if ds and ds.playing:
                raise ConductorError(f"deck{d} 正在播放，请先在 Mixxx 里停掉")
        for d in (1, 2):
            ds = self.c.deck(d)
            if ds and ds.loaded:
                self.c.send({"t": "eject", "deck": d})
        self._wait(lambda: not any(self.c.deck(d).loaded for d in (1, 2)), 5.0, "清空 deck")  # type: ignore[union-attr]

        op = self.compiled.opening
        assert op is not None
        for d in (1, 2):
            self.c.send({"t": "set", "g": f"[Channel{d}]", "k": "keylock", "v": 1 if op.get("keylock") else 0})

        # 开场
        self._load(0)
        info = self.tracks[0]
        assert info is not None
        d0 = op["deck"]
        self.c.send({"t": "neutral", "deck": d0})
        self.c.send({"t": "cue", "deck": d0, "ms": info.grid.beat_time(op["beat"]) * 1000.0})
        self.c.send({"t": "bpm", "deck": d0, "v": op["bpm"]})
        self.c.send({"t": "vol", "deck": d0, "v": 1.0})
        self.c.send({"t": "play", "deck": d0, "on": 1})
        self._wait(lambda: self.c.deck(d0).playing, 3.0, "开场起播")  # type: ignore[union-attr]
        self.log(f"▶ 开场 {self._title(0)}（第 {self.compiled.set.tracks[0].start:g} 小节，{op['bpm']:g} BPM）")

        for i, prog in enumerate(self.compiled.programs):
            self._stage("loading", f"装载 {self._title(i + 1)}", index=i)
            self._load(i + 1)
            prog = self._arm(prog, i)
            self._run_program(prog, i)
            self.progress.index = i + 1
            self.log(f"✓ 过渡 {i + 1} 完成，{self._title(i + 1)} 主奏")

        last = len(self.tracks) - 1
        if self.compiled.ending:
            prog = self._arm(self.compiled.ending, last)
            self._run_program(prog, last, ending=True)
        else:
            d = deck_for(last)
            self._stage("solo", f"{self._title(last)} 播到曲末", program=None)
            info = self.tracks[last]
            assert info is not None
            ds = self.c.deck(d)
            left = (info.grid.duration * 1000 - (ds.pos_ms if ds else 0)) / 1000
            self._wait(lambda: not self.c.deck(d).playing, left / max(0.5, (ds.bpm / ds.file_bpm) if ds and ds.file_bpm else 1) + 30,  # type: ignore[union-attr]
                       "终曲播完")

    # ------------------------------------------------------------ 等待（带上限、失联检测、停止请求）

    def _wait(self, pred: Callable[[], bool], timeout: float, what: str) -> None:
        deadline = time.monotonic() + timeout
        lost_since: float | None = None
        while True:
            if self._stop.is_set():
                raise StopRequested()
            if self.c.boots > self._boots0:
                raise ConductorError("Mixxx 重新加载了映射（重启或脚本被重载），引擎状态已丢失")
            if not self.c.online():
                if lost_since is None:
                    lost_since = time.monotonic()
                    self.progress.engine_ok = False
                    self.log(f"!! 引擎失联（{self.c.status_age():.1f}s 没有状态）—— 停发命令，等待恢复")
                elif time.monotonic() - lost_since > self.lost_timeout:
                    raise ConductorError(f"引擎失联超过 {self.lost_timeout:g}s，放弃")
                deadline += 0.25  # 失联期间不计入超时
            else:
                if lost_since is not None:
                    self.log(f"引擎恢复（失联 {time.monotonic() - lost_since:.1f}s）")
                    lost_since = None
                    self.progress.engine_ok = True
                    self._publish()
                if pred():
                    return
            if time.monotonic() > deadline:
                raise ConductorError(f"等待超时：{what}（{timeout:.0f}s）")
            self.c.wait_until(lambda: self._stop.is_set(), 0.25)

    # ------------------------------------------------------------ 装载

    def _load(self, k: int) -> None:
        d = deck_for(k)
        info = self.tracks[k]
        assert info is not None
        want = info.grid.duration * 1000
        ds = self.c.deck(d)
        if (ds and ds.loaded and not ds.playing and abs(ds.dur_ms - want) <= DUR_TOL_MS
                and (ds.file_bpm <= 0 or abs(ds.file_bpm - info.bpm) <= BPM_TOL)):
            self.log(f"deck{d} 已装好 {self._title(k)}")
            return
        if ds and ds.playing:
            # 程序刚把这边停下时，「done」事件可能比反映停止的那次状态（每 100ms 一次）先到：等新状态再判断
            t0, deadline = self.c.last_status_at, time.monotonic() + 2.0

            def stopped() -> bool:
                s = self.c.deck(d)
                return (self.c.last_status_at > t0 and not (s and s.playing)) or time.monotonic() > deadline

            self._wait(stopped, 5.0, f"deck{d} 停下")
            ds = self.c.deck(d)
            if ds and ds.playing:
                raise ConductorError(f"要往 deck{d} 装 {self._title(k)}，但它还在播")
        cursor = [self.c.mark()]
        self.c.send({"t": "load", "deck": d})
        got: dict[str, Any] = {}

        def done() -> bool:
            for ev in self.c.drain(cursor):
                if ev.get("t") == "ev" and ev.get("e") in ("loaded", "load_failed") and ev.get("deck") == d:
                    got.update(ev)
                    return True
            return False

        self._wait(done, self.load_timeout, f"装载 {self._title(k)}")
        if got["e"] == "load_failed":
            raise ConductorError(f"装载 {self._title(k)} 失败（AutoDJ 队列空了？阶段 {got.get('st')}）")
        if abs(got["dur"] - want) > DUR_TOL_MS:
            raise ConductorError(
                f"装错曲：deck{d} 期望 {self._title(k)}（{want / 1000:.1f}s），实际装上 {got['dur'] / 1000:.1f}s"
                "——AutoDJ 队列和 set 不一致，请重新准备")
        fbpm = float(got.get("fbpm") or 0)
        if fbpm > 0 and abs(fbpm - info.bpm) > BPM_TOL:
            raise ConductorError(
                f"装错曲或网格变了：deck{d} 期望 {self._title(k)}（{info.bpm:g} BPM），实际 {fbpm:g} BPM")
        self.log(f"载入 {self._title(k)} → deck{d}（{got['dur'] / 1000:.1f}s，{got.get('fbpm')} BPM）✓")

    # ------------------------------------------------------------ 程序

    def _arm(self, prog: dict[str, Any], i: int) -> dict[str, Any]:
        """下发程序；来不及（late）就换成应急切换。返回实际下发的程序。"""
        try:
            self.c.send(prog, timeout=3.0)
        except EngineError as e:
            if "late" not in str(e):
                raise
            self.log(f"!! {prog['id']} 来不及按谱执行（{e}）→ 改为应急切换")
            prog = self._emergency(prog)
            self.c.send(prog, timeout=3.0)
        self._stage("armed", f"{prog['id']} 已就位", program=prog["id"])
        a = prog["a"]
        info = self.tracks[i]
        assert info is not None
        start_bar = info.grid.beat_bar(a["start"])
        self.log(f"武装 {prog['id']}：A=deck{a['deck']} 第 {start_bar:g} 小节开始，长 {prog['len'] / 4:g} 小节")
        return prog

    def _emergency(self, prog: dict[str, Any]) -> dict[str, Any]:
        a = prog["a"]
        ds = self.c.deck(a["deck"])
        assert ds is not None
        beat_now = (ds.pos_ms - a["grid"][0]) * a["grid"][1] / 60000.0
        start = math.ceil((beat_now + 2) / 4) * 4
        bpm_pts = prog.get("bpm") or []
        b_bpm = bpm_pts[-1][1] if bpm_pts else ds.bpm
        p = dict(prog)
        p.update({
            "id": prog["id"] + "!",
            "a": {**a, "start": start},
            "play": 4,
            "len": 8,
            "playout": False,
            "lanes": {"A.vol": [[0, 1.0], [3.5, 1.0], [3.5, 0.0]], "A.filter": [[0, 0.5], [3.5, 0.8]],
                      "B.vol": [[4, 0.0], [4, 1.0]]},
            "bpm": [[0, ds.bpm], [4, ds.bpm], [4, b_bpm]],
            "events": [[4, "A.stop"]],
        })
        if prog.get("b") is None:
            p["lanes"] = {"A.vol": [[0, 1.0], [8, 0.0]]}
            p["play"] = None
            p["events"] = [[8, "A.stop"]]
        return p

    def _run_program(self, prog: dict[str, Any], i: int, ending: bool = False) -> None:
        pid = prog["id"]
        st: dict[str, Any] = {"tl": None, "since": time.monotonic(), "started": False}
        cursor = [self.c.mark()]
        bpm = prog["bpm"][0][1] if prog.get("bpm") else 120.0
        a_dur = prog["a"]["dur"] / 1000.0
        # 硬上限：A 整首时长 + 程序长度 + 余量（防止任何情况下无限等）
        cap = a_dur * 1.2 + prog["len"] * 60.0 / bpm + 60.0

        def check() -> bool:
            for ev in self.c.drain(cursor):
                if ev.get("id") != pid:
                    continue
                e = ev.get("e")
                if e == "started":
                    st["started"] = True
                    self._stage("ending" if ending else "transition", f"{pid} 进行中")
                    self.log(f"▶ {pid} 开始（{'终曲淡出' if ending else '过渡'}，tl={ev.get('tl')}）")
                elif e == "b_play":
                    self.log(f"  B 起播（触发晚了 {ev.get('over')} 拍；相位由 Mixxx 起播时对齐）")
                elif e == "fired":
                    self.log(f"  事件 {ev.get('ev')} @ {ev.get('tl')}")
                elif e == "phase_fix":
                    self.log(f"  相位校正 {ev.get('diff')} 拍")
                elif e == "bar_slip":
                    self.log(f"!! 小节错位 {ev.get('diff')} 拍")
                elif e == "done":
                    return True
            pg = self.c.program()
            if pg and pg.get("id") == pid:
                if pg.get("st") == "aborted":
                    raise ConductorError(f"{pid} 被中止")
                tl = pg.get("tl")
                if tl != st["tl"]:
                    st["tl"], st["since"] = tl, time.monotonic()
                elif time.monotonic() - st["since"] > self.stall_timeout:
                    a = self.c.deck(prog["a"]["deck"])
                    raise ConductorError(
                        f"{pid} 的时间线停了 {self.stall_timeout:g}s（A 在播：{a.playing if a else '?'}）")
                if not st["started"] and tl is not None and tl < 0:
                    self.progress.message = f"{pid} 还有 {-float(tl) / 4:.1f} 小节开始"
            return False

        self._wait(check, cap, f"{pid} 完成")

    def _fade_out_all(self, seconds: float = 4.0) -> None:
        """手动停止：中止程序，正在播的 deck 用 seconds 秒淡出后停下并归位。"""
        self._stage("stopping", "淡出停止")
        if not self.c.online():
            self.log("!! 引擎不在线，无法淡出")
            return
        try:
            self.c.send({"t": "abort"}, timeout=1.0)
            playing = [(d, ds.vol) for d in (1, 2) if (ds := self.c.deck(d)) and ds.playing]
            steps = 20
            for k in range(1, steps + 1):
                for d, v0 in playing:
                    self.c.send({"t": "vol", "deck": d, "v": v0 * (1 - k / steps)}, wait=False)
                time.sleep(seconds / steps)
            for d, _ in playing:
                self.c.send({"t": "play", "deck": d, "on": 0})
                self.c.send({"t": "neutral", "deck": d})
        except EngineError as e:
            self.log(f"!! 淡出时出错：{e}")
