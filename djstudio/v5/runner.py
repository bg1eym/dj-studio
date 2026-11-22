"""v5 演出时的指挥：把编译好的计划交给引擎里的调度器，之后只做「慢」的事——看回执、记日志、处理停止 / 暂停 /
跳转 / 手动接管。逐拍的执行全在调度器里（音频时钟），这里卡一下也不影响声音。

和 v4 的 conductor（engine/conductor.py）对网页是同一个样子：有 running、progress（同样的字段），所以首页的
「分析 → 可以演出 → 演出画面」那几屏不用知道放的是哪一代的谱。

- 开始前核对能力签名：编译时按的能力表和引擎自己报的不一样（换了引擎）→ 不放，重新分析；
- 回执（rc）：片段装载 / 起播 / 停止、loop、跳段各一条，带计划的拍和实际的拍；晚到（late）、跳过（skipped）、
  失效（failed）的记进日志并留给界面标出来——界面上「做完了没有」只看回执和引擎报的状态，不看浏览器的计时；
- 一台 Deck 装载失败：别的 Deck 照常放，失效的动作标出来，不临时编一个循环顶上；
- 引擎进程没了：停在那里报错，不自动重启重放。
"""
from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from fractions import Fraction
from typing import Any, Callable

from .caps import Caps
from .compile import Compiled5, actions, scene_of
from .link import Engine5Error, Engine5Link
from .pos import fmt_pos

LOST_AFTER = 1.5  # 秒没有状态 = 失联
PARAMS = ("vol", "lo", "mid", "hi", "filter")


@dataclass
class Progress5:
    """和 engine.conductor.Progress 同样的字段（网页按它换屏、显示日志）。"""

    set_id: str
    n_tracks: int
    stage: str = "idle"  # idle starting preparing running paused done stopping stopped error
    index: int = 0  # 现在在第几个场景（0 起；没有场景 = 0）
    program: str | None = None
    message: str = ""
    engine_ok: bool = True
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    log: list[str] = field(default_factory=list)
    v5: bool = True

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["log"] = self.log[-300:]
        return d


class Runner5:
    def __init__(self, compiled: Compiled5, link: Engine5Link, beat: float = 0.0, speed: float = 1.0,
                 on_progress: Callable[[Progress5], None] | None = None, on_log: Callable[[str], None] | None = None,
                 stop_fade_ms: float = 1000.0) -> None:
        if not compiled.ok or compiled.plan is None:
            raise Engine5Error("曲谱没编过，不能演出：" + "；".join(str(i) for i in compiled.errors[:3]))
        self.c = compiled
        self.link = link
        self.speed = speed
        self.from_beat = beat
        self.on_progress = on_progress
        self.on_log = on_log
        self.stop_fade_ms = stop_fade_ms
        self.progress = Progress5(set_id=compiled.project.id, n_tracks=len(compiled.clips))
        self.receipts: dict[str, dict[str, Any]] = {}  # 回执编号 → 最近一条
        self.actions = actions(compiled)
        self._by_rc = {a["rc"]: a for a in self.actions if a["rc"]}
        self._stop = threading.Event()
        self._cmd_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._seen = 0
        self._misses, self._miss_logged = 0, 0.0  # 读盘没跟上的次数（引擎报的）、上次记日志的时刻
        self._seek: float | None = None
        self.stopped_beat: float | None = None  # 手动停下时放到全场第几拍（「继续」从这里接）

    # ------------------------------------------------------------ 外部接口

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, name="runner5", daemon=True)
        self._thread.start()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def request_stop(self) -> None:
        self._stop.set()

    def beat(self) -> float:
        return float((self.link.status or {}).get("beat", self.from_beat))

    def pause(self) -> None:
        """全场暂停：四台 Deck 和效果一起冻住（短淡出）；继续时保持原来的全场拍。"""
        self._call({"cmd": "v5.pause"})
        self.log(f"⏸ 全场暂停（{self.pos_text()}）")

    def resume(self) -> None:
        self._call({"cmd": "v5.resume"})
        self.log("▶ 继续")

    def seek(self, beat: float) -> None:
        """跳到全场第 beat 拍重新开始（同一个引擎，不重开）。跳转会结束所有手动接管，旋钮回到那一拍谱上的位置。"""
        beat = max(0.0, float(beat))
        problem = self.c.restore_problem(beat)
        if problem:  # 抓取型的效果离得太远：不能从这里开始（不装成恢复了）
            raise Engine5Error(problem)
        self._seek = beat

    def takeover(self, deck: int, param: str, value: float) -> None:
        """手动接管一个旋钮（value = Mixxx 原生值）：只有它不再跟谱，别的照常。"""
        if param not in PARAMS:
            raise Engine5Error(f"没有「{param}」这个旋钮")
        self._call({"cmd": "v5.takeover", "deck": int(deck), "param": param, "value": float(value)})

    def give_back(self, deck: int, param: str, when: str = "beat", ramp: float = 1.0) -> None:
        """回谱：下一拍 / 下一小节起，用一条短斜线走到那时的谱值（不是跳到已经过期的点）。"""
        self._call({"cmd": "v5.return", "deck": int(deck), "param": param, "when": when, "ramp": float(ramp)})

    def _call(self, cmd: dict[str, Any], timeout: float = 3.0) -> dict[str, Any]:
        with self._cmd_lock:
            return self.link.call(cmd, timeout)

    # ------------------------------------------------------------ 日志 / 进度

    def pos_text(self, beat: float | None = None) -> str:
        b = self.beat() if beat is None else beat
        return "开演前" if b < 0 else fmt_pos(Fraction(int(b * 4), 4))

    def log(self, msg: str) -> None:
        line = f"{datetime.now():%H:%M:%S} {msg}"
        self.progress.log.append(line)
        if self.on_log:
            self.on_log(line)
        self._publish()

    def _stage(self, stage: str, message: str = "", **kw: Any) -> None:
        changed = stage != self.progress.stage or message != self.progress.message
        self.progress.stage = stage
        self.progress.message = message
        for k, v in kw.items():
            setattr(self.progress, k, v)
        if changed:
            self._publish()

    def _publish(self) -> None:
        if self.on_progress:
            self.on_progress(self.progress)

    def snapshot(self) -> dict[str, Any]:
        """给网页的：引擎报的状态 + 每个动作的回执。"""
        return {"status": self.link.status, "receipts": dict(self.receipts), "from": self.from_beat,
                "stage": self.progress.stage, "speed": self.speed}

    # ------------------------------------------------------------ 主流程

    def run(self) -> None:
        self.progress.started_at = time.time()
        try:
            self._run()
        except Exception as e:  # noqa: BLE001
            self.progress.error = str(e)
            self._stage("error", str(e))
            self.log(f"!! 出错停止：{e}")
            try:
                self.link.call({"cmd": "v5.stop", "fade_ms": 150}, timeout=1.0)
            except Exception:  # noqa: BLE001
                pass
        finally:
            self.progress.finished_at = time.time()
            self._publish()

    def _begin(self, beat: float) -> None:
        pre = self.c.prerun_from(beat)
        self.from_beat = beat
        self.receipts.clear()
        # 正好落在起点这一拍上的动作（这一拍打的 loop、这一拍停的片段）：引擎是「摆成那样再开始」，不会再执行一遍、
        # 也就没有回执。先记成 restored（起点上已经就位）；引擎真执行了的（这一拍起播的片段）会用自己的回执盖掉
        for a in self.actions:
            if a["rc"] and abs(a["at"] - beat) < 1e-6:
                self.receipts[a["rc"]] = {"st": "restored", "beat": a["at"], "at": a["at"], "msg": ""}
        self._seen = self.link.n_events
        self._misses, self._miss_logged = 0, 0.0
        self._call({"cmd": "v5.start", "beat": beat, "prerun": pre})
        where = self.pos_text(beat)
        self.log(f"▶ 从 {where} 开始" + (f"（先从 {self.pos_text(pre)} 静音预运行，把效果的尾音补出来）" if pre < beat else ""))

    def _run(self) -> None:
        self._stage("starting", "连接引擎")
        if not self.link.wait(lambda: self.link.status is not None, 15.0):
            raise Engine5Error("内置引擎没有启动起来")
        got = Caps.from_engine(self._call({"cmd": "v5.caps"}), name=self.c.caps.name)
        if got.signature() != self.c.caps.signature():
            raise Engine5Error("引擎的能力和分析时不一样（换了引擎版本？）：重新点一次「开始分析」")
        self._call({"cmd": "v5.plan", "plan": self.c.plan}, timeout=10.0)
        self.log(f"引擎在线（{self.link.name}），计划 {self.c.plan['exec']}：{len(self.c.clips)} 个片段，"
                 f"{self.c.length / 4:g} 小节，约 {self.c.duration / 60:.1f} 分钟")
        self._begin(self.from_beat)
        lost_since: float | None = None
        while True:
            self.link.wait(lambda: self.link.n_events > self._seen or self._stop.is_set() or self._seek is not None, 0.2)
            self._drain()
            st = self.link.status or {}
            if self.link.closed:
                raise Engine5Error("引擎进程退出了：已停止。重新开始前不会自动重放")
            if not self.link.online():
                if lost_since is None:
                    lost_since = time.monotonic()
                    self.progress.engine_ok = False
                    self.log("!! 引擎失联（没有状态回来）—— 等它恢复")
                elif time.monotonic() - lost_since > 30:
                    raise Engine5Error("引擎失联超过 30 秒，放弃")
                continue
            if lost_since is not None:
                self.log(f"引擎恢复（失联 {time.monotonic() - lost_since:.1f}s）")
                lost_since = None
                self.progress.engine_ok = True
            self._watch_misses(st)
            if self._stop.is_set():
                self._stage("stopping", "淡出停止")
                if st.get("state") in ("running", "pausing", "paused"):
                    self.stopped_beat = float(st.get("beat", self.from_beat))
                self._call({"cmd": "v5.stop", "fade_ms": self.stop_fade_ms})
                self.link.wait(lambda: (self.link.status or {}).get("state") in ("idle", "done"), self.stop_fade_ms / 1000 + 3)
                self._drain()
                self._stage("stopped", "已手动停止")
                self.log(f"■ 已停止（{self.pos_text(self.stopped_beat if self.stopped_beat is not None else self.from_beat)}）")
                return
            if self._seek is not None:
                beat, self._seek = self._seek, None
                self._call({"cmd": "v5.stop", "fade_ms": 150})
                self.link.wait(lambda: (self.link.status or {}).get("state") in ("idle", "done"), 3)
                self._begin(beat)
                continue
            state = st.get("state")
            beat = float(st.get("beat", 0.0))
            sc = scene_of(self.c.project, beat)
            where = f"场景 {sc.no}" + (f"「{sc.name}」" if sc.name else "") if sc else ""
            if state in ("preparing", "prerun"):
                self._stage("preparing", "装载、摆旋钮" if state == "preparing" else "静音预运行")
            elif state in ("running", "pausing"):
                self._stage("running", f"{self.pos_text(beat)} {where}".strip(), index=(sc.no - 1) if sc else 0)
            elif state == "paused":
                self._stage("paused", f"暂停在 {self.pos_text(beat)}")
            elif state == "done":
                self._stage("done", "整场放完")
                self.log("✓ 整场放完")
                return

    def _watch_misses(self, st: dict[str, Any]) -> None:
        """读盘没跟上的次数（声音引擎才报；模拟器没有盘）：涨了就记一行，最多两秒一行。"""
        if st.get("state") not in ("prerun", "running", "pausing", "paused"):
            return
        n = int(st.get("misses", 0) or 0)
        if n < self._misses:
            self._misses = n  # 重新开始了：引擎从 0 重新数
        elif n > self._misses and time.monotonic() - self._miss_logged > 2.0:
            self.log(f"!! 读盘没跟上：有 Deck 断了一小下（这次开始以来 {n} 次）—— 电脑太忙，或者放音乐的盘太慢")
            self._misses, self._miss_logged = n, time.monotonic()

    def _drain(self) -> None:
        new = self.link.receipts(self._seen)
        self._seen = self.link.n_events
        for m in new:
            if m.get("exec") not in (None, "", self.c.plan["exec"]):
                continue  # 上一份计划留下的
            if m.get("t") == "rc":
                rid = str(m.get("id"))
                self.receipts[rid] = {"st": m.get("st"), "beat": m.get("beat"), "at": m.get("at"), "msg": m.get("msg", "")}
                a = self._by_rc.get(rid)
                text = a["text"] if a else rid
                st = m.get("st")
                kind = a["kind"] if a else ""
                if st == "ok":
                    at, planned = float(m.get("at") or 0), float(m.get("beat") or 0)
                    cc = self.c.clip(a["clip"]) if a and a.get("clip") else None
                    if kind == "start" and cc is not None and at - cc.start > 1e-3 and planned - cc.start > 1e-3:
                        # 从中间开始：这个片段不是从头起播，是接着放——写它这时读到原曲哪里
                        src = fmt_pos(Fraction(cc.src_beat_at(at)).limit_denominator(4))
                        text = f"D{cc.clip.deck} 接着放 {cc.clip.id}（原曲 {src}）"
                    if kind != "load":
                        self.log(f"  {self.pos_text(at)} {text}")
                elif st == "late":
                    self.log(f"!! 晚了：{text}（计划 {self.pos_text(float(m.get('beat') or 0))}，实际 "
                             f"{self.pos_text(float(m.get('at') or 0))}）{m.get('msg', '')}")
                elif st == "skipped":
                    self.log(f"!! 跳过：{text} {m.get('msg', '')}")
                else:
                    self.log(f"!! 失效：{text} {m.get('msg', '')}——别的 Deck 照常放")
            elif m.get("t") == "ev5" and m.get("e") in ("paused", "resumed"):
                self._publish()
