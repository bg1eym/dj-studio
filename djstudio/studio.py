"""Studio：把曲库、Mixxx、引擎、conductor 串起来的服务层。网页和命令行都只调用这里。"""
from __future__ import annotations

import json
import math
import os
import shutil
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .compiler import Compiled, Issue, TrackInfo, compile_set
from .config import Config, get_config
from .engine.client import EngineClient, EngineError
from .engine.conductor import Conductor, Progress
from .engine.link import Link, LinkError, RtMidiLink, SimLink
from .grid import Grid
from .library.analysis import downbeat_from_bar1
from .library.audio import AUDIO_EXTS, AudioError, have_decoder, probe
from .library.importer import Library
from .library.store import Store, Track
from .mixxx import process
from .mixxx.db import MixxxDB, MixxxDBError, MixxxTrack, copy_if_changed
from .setfile import SetFile, SetTrack, load_set, save_set

MAPPING_DIR = Path(__file__).parent / "engine" / "mapping"
MAPPING_FILES = ("DJStudio.js", "DJStudio.midi.xml")


def mapping_version() -> str:
    src = (MAPPING_DIR / "DJStudio.js").read_text(encoding="utf-8")
    for line in src.splitlines():
        if line.startswith("DJStudio.VERSION"):
            return line.split('"')[1]
    return "?"


def _age(c: EngineClient) -> float | None:
    """多久没收到引擎状态（秒）；还没收到过 = None（JSON 里不能有 Infinity，浏览器会解析失败）。"""
    a = c.status_age()
    return round(a, 2) if math.isfinite(a) else None


def _duration(path: Path) -> float:
    try:
        return probe(path).duration
    except AudioError:
        return -1.0


def _default_engine_link(rows: list[dict[str, Any]], speed: float, audio: bool = False) -> Link:
    """内置引擎：有 DJ Studio 声音引擎（Mixxx 2.5.6 的引擎核心）就用它，audio=True 时出声；
    没有就退回 Node 模拟器（同一份 DJStudio.js，没有声音）。"""
    from .engine.bundled import engine_binary
    from .engine.link import EngineLink

    binary = engine_binary()
    if binary:
        return EngineLink(binary, rows, speed=speed, audio=audio)
    return SimLink(rows, speed=speed)


def _engine_missing() -> str | None:
    """内置引擎跑不起来的原因（既没有声音引擎，也没有 Node.js）。"""
    from .engine.bundled import engine_binary

    if engine_binary() or shutil.which("node"):
        return None
    return "找不到 DJ Studio 声音引擎（应用没装完整？重新下载 DJ Studio）"


class StudioError(RuntimeError):
    pass


@dataclass
class Job:
    """后台任务（准备 / 让 Mixxx 分析 / 探测）：一次只跑一个，网页实时看日志。"""

    name: str
    state: str = "running"  # running | ok | error
    log: list[str] = field(default_factory=list)
    result: Any = None
    started: float = field(default_factory=time.time)
    finished: float | None = None

    stages: list[str] = field(default_factory=list)  # 分步任务（一键分析）的步骤名
    weights: list[float] = field(default_factory=list)
    stage: int = -1
    frac: float = 0.0  # 当前步骤内的进度 0..1
    detail: str = ""

    def say(self, msg: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')} {msg}")
        self.detail = msg.strip()

    def step(self, i: int, detail: str = "") -> None:
        self.stage, self.frac = i, 0.0
        self.say(f"【{i + 1}/{len(self.stages)} {self.stages[i]}】{detail}")

    def sub(self, frac: float, detail: str | None = None) -> None:
        self.frac = max(0.0, min(1.0, frac))
        if detail:
            self.detail = detail

    @property
    def progress(self) -> float:
        if not self.stages:
            return 1.0 if self.state != "running" else 0.0
        if self.state == "ok":
            return 1.0
        w = self.weights or [1.0] * len(self.stages)
        done = sum(w[: max(0, self.stage)]) + (w[self.stage] * self.frac if self.stage >= 0 else 0.0)
        return done / sum(w)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "state": self.state, "log": self.log[-300:], "result": self.result,
                "started": self.started, "finished": self.finished, "stages": self.stages, "stage": self.stage,
                "progress": round(self.progress, 4), "detail": self.detail}


class Studio:
    def __init__(self, cfg: Config | None = None, link_factory: Callable[[], Link] | None = None) -> None:
        self.cfg = cfg or get_config()
        self.cfg.ensure_dirs()
        self.store = Store(self.cfg.db_path, self.cfg.cache_dir)
        self.mixxx = MixxxDB(self.cfg.mixxx_db)
        self.library = Library(self.store, self.cfg.library, self._mixxx_grid_for, self.cfg.browse_roots)
        self.link_factory = link_factory or (lambda: RtMidiLink(self.cfg.midi_port))
        self._client: EngineClient | None = None
        self._client_lock = threading.Lock()
        self.conductor: Conductor | None = None
        self._rehearsal: dict[str, Any] | None = None
        self._plan_cache: tuple[Any, Any] | None = None
        # 预演用的引擎：默认 Node 模拟器；开发时可以换成真 Mixxx 测试台（HarnessLink，同一套行协议）
        self.rehearsal_link_factory: Callable[..., Link] = _default_engine_link
        self.job: Job | None = None
        self._job_lock = threading.Lock()
        # 按打碟谱在电脑里找曲子（网页显示进度）；一键分析自动开始过的「打碟谱 + 曲目」组合
        self.finder: dict[str, Any] = {"state": "idle"}
        self._find_lock = threading.Lock()
        self._auto_lock = threading.Lock()
        self._auto_key: tuple[Any, ...] | None = None
        self.listeners: list[Callable[[str, Any], None]] = []  # (kind, payload) → 网页 SSE
        self.library.on_change = lambda tid: self.emit("track", tid)

    @property
    def builtin(self) -> bool:
        """内置引擎：不连本机的 Mixxx（不读它的曲库、不启动它、不发 MIDI），过渡在内置的模拟 Mixxx 里跑。"""
        return self.cfg.engine != "mixxx"

    def _need_mixxx(self, what: str) -> None:
        if self.builtin:
            raise StudioError(f"现在用的是内置引擎，不连 Mixxx，不能{what}（要用 Mixxx：DJSTUDIO_ENGINE=mixxx djstudio）")

    # ------------------------------------------------------------ 事件广播

    def emit(self, kind: str, payload: Any) -> None:
        for fn in list(self.listeners):
            try:
                fn(kind, payload)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------ 引擎连接

    def client(self, connect: bool = True) -> EngineClient | None:
        if connect:
            self._need_mixxx("连接 Mixxx 的引擎")
        with self._client_lock:
            if self._client is None and connect:
                link = self.link_factory()
                self._client = EngineClient(link)
                self._client.add_listener(lambda m: self.emit("engine", m) if m.get("t") != "st" else None)
            return self._client

    def shutdown(self) -> None:
        """退出前：停掉演出 / 预演，关掉引擎进程。"""
        if self.conductor and self.conductor.running:
            self.conductor.request_stop()
            self.conductor.join(3)
        r = self._rehearsal
        if r:
            r["client"].link.close()
            self._rehearsal = None
        self.disconnect()

    def disconnect(self) -> None:
        with self._client_lock:
            if self._client:
                self._client.link.close()
                self._client = None

    def engine_state(self) -> dict[str, Any]:
        r = self._rehearsal
        if r:  # 预演中：网页看的是模拟器里的引擎
            c = r["client"]
            return {"connected": True, "online": c.online(), "version": c.version, "age": _age(c),
                    "status": c.status, "link": c.link.name, "rehearsal": True, "speed": r["speed"],
                    "set": r["set"], "from": r["from"], "builtin": r.get("builtin", False), "audio": r.get("audio")}
        if self.builtin:
            return {"connected": False, "online": False, "builtin": True}
        c = self._client
        if not c:
            return {"connected": False, "online": False}
        return {"connected": True, "online": c.online(), "version": c.version, "age": _age(c),
                "status": c.status, "link": c.link.name}

    # ------------------------------------------------------------ 曲目解析

    def _mixxx_tracks(self, tracks: list[Track]) -> dict[str, MixxxTrack]:
        if self.builtin or not self.mixxx.exists():
            return {}
        try:
            by_loc = self.mixxx.tracks_by_location([t.path for t in tracks])
        except MixxxDBError:
            return {}
        return {t.id: by_loc[t.path] for t in tracks if t.path in by_loc}

    def _mixxx_grid_for(self, t: Track) -> tuple[float, float] | None:
        m = self._mixxx_tracks([t]).get(t.id)
        return (m.grid.bpm, m.grid.first_beat) if m and m.grid and m.analyzed else None

    def find_track(self, st: SetTrack) -> Track | None:
        t = self.store.get(st.id) if st.id else None
        if t is None and st.file:
            p = Path(st.file)
            t = self.store.by_path(str(p if p.is_absolute() else self.cfg.library / p))
        return t

    def track_infos(self, s: SetFile) -> tuple[list[Track | None], dict[str, MixxxTrack]]:
        tracks = [self.find_track(st) for st in s.tracks]
        mx = self._mixxx_tracks([t for t in tracks if t])
        return tracks, mx

    def compile(self, s: SetFile) -> Compiled:
        tracks, mx = self.track_infos(s)
        lookup = {id(st): t for st, t in zip(s.tracks, tracks)}

        def resolve(st: SetTrack) -> TrackInfo | None:
            t = lookup.get(id(st))
            if t is None:
                return None
            m = mx.get(t.id)
            own = (t.analysis or {}).get("own_grid")
            if self.builtin and own:  # 内置引擎：用自己分析的网格
                g = Grid(own["bpm"], own["first_beat"], 0, t.duration)
            elif m is None or m.grid is None or not m.analyzed:
                # 不在 Mixxx 库 / 没分析：返回空网格让编译器报「没分析」
                return TrackInfo(t.id, t.display, Grid(0.0 if m is None else m.bpm or 0.0, 0.0, 0, 0.0))
            else:
                g = Grid(m.grid.bpm, m.grid.first_beat, 0, m.duration or t.duration)
            db = downbeat_from_bar1(g, t.bar1) if t.bar1 is not None else 0
            return TrackInfo(t.id, t.display, Grid(g.bpm, g.first_beat, db, g.duration))

        c = compile_set(s, resolve)
        # Mixxx 网格与 kick 对不上：同步会对齐网格而不是 kick，过渡里两个 kick 会错开
        for k, (st, t) in enumerate(zip(s.tracks, tracks)):
            gc = (t.analysis or {}).get("grid_check") if t else None
            if t and t.id in mx and mx[t.id].analyzed and gc and not gc.get("ok"):
                c.issues.append(Issue("error", f"#{k + 1} {st.title or t.display}",
                                      f"网格存疑：{gc.get('note')}。在曲库页点「校正网格」"))
        # 把「不在 Mixxx 库」说清楚
        for k, (st, t) in enumerate(zip(s.tracks, tracks)):
            if t is not None and t.id not in mx:
                for iss in c.issues:
                    if iss.where.startswith(f"#{k + 1} ") and "没分析" in iss.msg:
                        iss.msg = "还不在 Mixxx 曲库里（需要让 Mixxx 扫描托管曲库）"
        return c

    # ------------------------------------------------------------ set 文件

    def set_path(self, set_id: str) -> Path:
        return self.cfg.sets_dir / f"{set_id}.json"

    def list_sets(self) -> list[dict[str, Any]]:
        out = []
        for p in sorted(self.cfg.sets_dir.glob("*.json")):
            try:
                s = load_set(p)
                out.append({"id": s.id, "name": s.name, "n": len(s.tracks), "created": s.created, "source": s.source})
            except Exception as e:  # noqa: BLE001
                out.append({"id": p.stem, "name": p.stem, "error": str(e)[:300]})
        return out

    def load(self, set_id: str) -> SetFile:
        p = self.set_path(set_id)
        if not p.exists():
            raise StudioError(f"没有 set {set_id}")
        return load_set(p)

    def save(self, s: SetFile) -> None:
        save_set(s, self.set_path(s.id))

    # ------------------------------------------------------------ 体检

    def doctor(self) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []

        def add(name: str, level: str, detail: str) -> None:
            out.append({"name": name, "level": level, "detail": detail})

        from .engine.bundled import engine_binary

        binary = engine_binary()
        add("解码", "ok" if have_decoder() else "error",
            ("声音引擎（Mixxx 的解码器）" if binary else "ffmpeg") if have_decoder() else "没有解码器：找不到声音引擎，也没有 ffmpeg")
        add("托管曲库", "ok", str(self.cfg.library))
        if self.builtin:
            if binary:
                add("引擎", "ok", f"内置声音引擎（Mixxx 2.5.6 的引擎核心）：{binary}；DJStudio.js {mapping_version()}，不连本机的 Mixxx")
            else:
                miss = _engine_missing()
                add("引擎", "error" if miss else "warn", miss or "没有声音引擎，用 Node 模拟器代替（看得到旋钮，没有声音）")
            return out
        if not self.mixxx.exists():
            add("Mixxx 曲库", "error", f"找不到 {self.cfg.mixxx_db}（先正常打开一次 Mixxx；或设置 DJSTUDIO_MIXXX_DIR）")
        else:
            probs = self.mixxx.schema_problems()
            add("Mixxx 曲库", "error" if probs else "ok", "；".join(probs) or str(self.cfg.mixxx_db))
            dirs = [Path(d) for d in self.mixxx.directories()]
            lib = self.cfg.library.resolve()
            covered = any(lib == d or d in lib.parents for d in dirs)
            add("Mixxx 曲库目录", "ok" if covered else "error",
                f"已包含 {lib}" if covered else
                f"Mixxx 的曲库目录里没有 {lib}：Mixxx → 偏好设置 → 曲库 → 添加，选这个文件夹")
            cfg = process.read_cfg(self.cfg.mixxx_dir / "mixxx.cfg")
            ok, msg = process.mapping_status(cfg)
            add("控制器映射", "ok" if ok else "warn",
                msg if ok else msg + "：Mixxx → 偏好设置 → 控制器 → IAC 总线 → 映射选「DJ Studio」并启用")
            # ReplayGain 默认开（replaygainsettings.cpp）：把响的母带统一压到参考响度，两首叠加才有余量
            rg = cfg.get("[ReplayGain]", {}).get("ReplayGainEnabled", "1").strip()
            add("ReplayGain", "ok" if rg != "0" else "warn",
                "已开启（两首叠加时主输出有余量）" if rg != "0" else
                "关着：现在的母带都很响，长交接两首满音量叠加时主输出会削波。Mixxx → 偏好设置 → Normalization（标准化）→ 勾选 ReplayGain")
        deployed = all((self.cfg.mixxx_controllers / f).exists() and
                       (self.cfg.mixxx_controllers / f).read_bytes() == (MAPPING_DIR / f).read_bytes()
                       for f in MAPPING_FILES)
        add("引擎脚本", "ok" if deployed else "warn",
            f"DJStudio.js {mapping_version()} 已部署" if deployed else "未部署或不是最新（准备 set 时会自动部署）")
        running = process.mixxx_running()
        add("Mixxx 进程", "ok" if running else "warn", "运行中" if running else "没在运行")
        try:
            from .engine.link import fix_port_name, list_ports

            ports = list_ports()
            names = [fix_port_name(p) for p in ports["out"]]
            has = any(self.cfg.midi_port.lower() in n.lower() for n in names)
            add("IAC 总线", "ok" if has else "error",
                f"找到：{names}" if has else f"没有包含 {self.cfg.midi_port!r} 的 MIDI 口（音频 MIDI 设置 → IAC 驱动程序 → 启用）")
        except ImportError:
            add("IAC 总线", "error", "没有安装 python-rtmidi")
        except Exception as e:  # noqa: BLE001
            add("IAC 总线", "error", str(e))
        if running:
            try:
                c = self.client()
                assert c
                online = c.wait_until(c.online, 3.0)
                if online:
                    c.send({"t": "hello"})
                    same = c.version == mapping_version()
                    add("引擎", "ok" if same else "warn",
                        f"在线，版本 {c.version}" + ("" if same else f"（磁盘上是 {mapping_version()}，需要重新准备）"))
                else:
                    add("引擎", "error", "收不到引擎状态：映射没启用、IAC 口不对，或 SysEx 不通（见 docs/DEVICE_TEST.md）")
            except (LinkError, EngineError) as e:
                add("引擎", "error", str(e))
        return out

    # ------------------------------------------------------------ 后台任务

    def start_job(self, name: str, fn: Callable[[Job], Any], stages: list[str] | None = None,
                  weights: list[float] | None = None) -> Job:
        with self._job_lock:
            if self.job and self.job.state == "running":
                raise StudioError(f"已经有任务在跑：{self.job.name}")
            if self.conductor and self.conductor.running:
                raise StudioError("set 正在播放，不能做这个操作")
            job = self.job = Job(name, stages=list(stages or []), weights=list(weights or []))

        def run() -> None:
            try:
                job.result = fn(job)
                job.state = "ok"
                job.say("✓ 完成")
            except Exception as e:  # noqa: BLE001
                job.state = "error"
                job.say(f"!! {e}")
                if not isinstance(e, (StudioError, MixxxDBError, EngineError, LinkError)):
                    job.say(traceback.format_exc(limit=3))
            finally:
                job.finished = time.time()
                self.emit("job", job.to_dict())

        threading.Thread(target=run, name=f"job-{name}", daemon=True).start()
        return job

    # ------------------------------------------------------------ Mixxx 重启 / 部署

    def _ensure_quit(self, job: Job) -> None:
        if not process.mixxx_running():
            return
        c = self.client()
        if c and c.online():
            for d in (1, 2):
                ds = c.deck(d)
                if ds and ds.playing:
                    raise StudioError(f"Mixxx 的 deck{d} 正在播放，先停掉再准备")
        job.say("退出 Mixxx …")
        self.disconnect()
        if not process.quit_mixxx(30):
            raise StudioError("Mixxx 没有在 30 秒内退出（可能弹了对话框），请手动退出后重试")
        job.say("Mixxx 已退出")

    def _deploy(self, job: Job) -> None:
        changed = [f for f in MAPPING_FILES if copy_if_changed(MAPPING_DIR / f, self.cfg.mixxx_controllers / f)]
        job.say(f"引擎脚本 {mapping_version()} " + (f"已更新：{', '.join(changed)}" if changed else "已是最新"))

    def _launch_and_connect(self, job: Job, settle: float) -> EngineClient:
        job.say("启动 Mixxx …")
        process.launch_mixxx()
        deadline = time.monotonic() + 120
        c: EngineClient | None = None
        while time.monotonic() < deadline:
            try:
                c = self.client()
                break
            except LinkError:
                time.sleep(1.0)
        if c is None:
            raise StudioError("连不上 IAC 总线")
        if not c.wait_until(c.online, max(1.0, deadline - time.monotonic())):
            raise StudioError("Mixxx 启动 2 分钟内引擎没有回传状态：检查控制器映射是否启用（docs/DEVICE_TEST.md）")
        c.send({"t": "hello"})
        job.say(f"引擎在线（DJStudio.js {c.version}），等待 Mixxx 安定 {settle:g}s …")
        boots = c.boots
        t_end = time.monotonic() + settle
        while time.monotonic() < t_end:
            if not c.online():
                raise StudioError("Mixxx 启动后引擎又掉线了")
            time.sleep(0.5)
        if c.boots != boots:
            job.say("（期间映射被重载过一次）")
        c.send({"t": "hello"})
        if c.version != mapping_version():
            raise StudioError(f"引擎版本 {c.version} 和磁盘上的 {mapping_version()} 不一致：映射没重载？")
        return c

    # ------------------------------------------------------------ 准备

    def prepare(self, set_id: str, settle: float | None = None) -> Job:
        settle = float(settle if settle is not None else 15.0)
        if self.builtin:
            return self.start_job(f"准备 {set_id}", lambda job: self._prepare_builtin(job, set_id))
        return self.start_job(f"准备 {set_id}", lambda job: self._prepare(job, set_id, settle))

    def _engine_link(self, rows: list[dict[str, Any]], speed: float, audio: bool = False) -> Link:
        if self.rehearsal_link_factory is _default_engine_link:
            return _default_engine_link(rows, speed, audio=audio)
        return self.rehearsal_link_factory(rows, speed)  # 测试 / 开发换上的（两个参数）

    def _sim_rows(self, s: SetFile, c: Compiled) -> list[dict[str, Any]]:
        """内置引擎（模拟器）里的曲目：时长 / BPM / 网格用编译时同一份结果，小节和过渡时刻与编译出的程序一致。"""
        rows = []
        for t, info in zip(s.tracks, c.tracks):
            tr = self.store.get(t.id)
            rows.append({"id": t.id, "dur": info.grid.duration, "bpm": info.grid.bpm, "first_beat": info.grid.first_beat,
                         "path": tr.path if tr else None})
        return rows

    def _prepare_builtin(self, job: Job, set_id: str) -> dict[str, Any]:
        """内置引擎：校验 set，试启动一次模拟器确认能跑。不碰本机的 Mixxx。"""
        s = self.load(set_id)
        c = self.compile(s)
        errs = [str(i) for i in c.issues if i.level == "error"]
        if errs:
            raise StudioError("set 校验没通过：\n" + "\n".join(errs))
        miss = _engine_missing() if self.rehearsal_link_factory is _default_engine_link else None
        if miss:
            raise StudioError(miss)
        link = self._engine_link(self._sim_rows(s, c), 1.0, audio=True)
        try:
            client = EngineClient(link)
            if not client.wait_until(client.online, 15):
                raise StudioError("内置引擎没有启动起来")
            audio = link.audio() if hasattr(link, "audio") else None
            if audio and audio.get("on"):
                job.say(f"内置引擎就绪（Mixxx 2.5.6 引擎 + DJStudio.js {client.version}），声音从「{audio.get('device')}」出")
            elif audio:
                job.say(f"!! 内置引擎就绪，但没有声音：{audio.get('error') or '打不开声音输出'}")
            else:
                job.say(f"内置引擎就绪（模拟器 + DJStudio.js {client.version}，没有声音）")
        finally:
            link.close()
        state = {"set": set_id, "at": time.time(), "engine": "builtin"}
        (self.cfg.home / "prepared.json").write_text(json.dumps(state))
        return state

    def _prepare(self, job: Job, set_id: str, settle: float) -> dict[str, Any]:
        s = self.load(set_id)
        job.say(f"set「{s.name or s.id}」：{len(s.tracks)} 首")
        tracks, mx = self.track_infos(s)
        missing = [f"#{k + 1} {st.title or st.id}" for k, (st, t) in enumerate(zip(s.tracks, tracks)) if t is None]
        if missing:
            raise StudioError("曲库里找不到：" + "、".join(missing))
        not_in_mixxx = [t for t in tracks if t and t.id not in mx]
        if not_in_mixxx:
            raise StudioError(f"{len(not_in_mixxx)} 首还不在 Mixxx 库里：先在「曲库」页点「同步到 Mixxx」")
        c = self.compile(s)
        errs = [str(i) for i in c.issues if i.level == "error"]
        if errs:
            raise StudioError("set 校验没通过：\n" + "\n".join(errs))
        for i in c.issues:
            job.say(f"提示 {i}")
        ids = [mx[t.id].id for t in tracks if t]
        self._ensure_quit(job)
        self._deploy(job)
        self._configure_controller(job)
        bk = self.mixxx.backup(self.cfg.home / "backups")
        job.say(f"已备份 Mixxx 曲库 → {bk.name}")
        self.mixxx.write_autodj_queue(ids)
        job.say(f"AutoDJ 队列已写入 {len(ids)} 首")
        self._launch_and_connect(job, settle)
        q = self.mixxx.autodj_queue()
        if q != ids:
            raise StudioError(f"Mixxx 启动后 AutoDJ 队列不对（{len(q)} 首，期望 {len(ids)}）：可能是上次脏退出，请重试")
        job.say("AutoDJ 队列核对无误")
        state = {"set": set_id, "at": time.time(), "queue": ids, "engine": mapping_version()}
        (self.cfg.home / "prepared.json").write_text(json.dumps(state))
        return state

    def prepared(self) -> dict[str, Any] | None:
        p = self.cfg.home / "prepared.json"
        return json.loads(p.read_text()) if p.exists() else None

    # ------------------------------------------------------------ 同步到 Mixxx（扫描 + 分析）

    def sync_mixxx(self, track_ids: list[str] | None = None, settle: float = 15.0) -> Job:
        self._need_mixxx("同步到 Mixxx")
        return self.start_job("同步到 Mixxx", lambda job: self._sync(job, track_ids, settle))

    def _sync(self, job: Job, track_ids: list[str] | None, settle: float) -> dict[str, Any]:
        tracks = [t for t in self.store.all() if track_ids is None or t.id in track_ids]
        mx = self._mixxx_tracks(tracks)
        need_scan = [t for t in tracks if t.id not in mx]
        if need_scan:
            job.say(f"{len(need_scan)} 首不在 Mixxx 库：开启「启动时重新扫描」并重启 Mixxx")
            self._ensure_quit(job)
            process.set_cfg_value(self.cfg.mixxx_dir / "mixxx.cfg", "[Library]", "RescanOnStartup", "1")
            self._deploy(job)
            self._configure_controller(job)
            self._launch_and_connect(job, settle)
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                mx = self._mixxx_tracks(tracks)
                if all(t.id in mx for t in need_scan):
                    break
                time.sleep(2)
            still = [t.display for t in need_scan if t.id not in mx]
            if still:
                raise StudioError("扫描 3 分钟后仍不在 Mixxx 库：" + "、".join(still[:10]) +
                                  "（托管曲库目录加进 Mixxx 了吗？见体检）")
            job.say("扫描完成")
        unanalyzed = [t for t in tracks if not mx[t.id].analyzed]
        if unanalyzed:
            self._mixxx_analyze(job, unanalyzed, mx, settle)
        for t in tracks:  # 用 Mixxx 网格重新切小节
            self.library.enqueue(t.id)
        return {"scanned": len(need_scan), "analyzed": len(unanalyzed)}

    def _mixxx_analyze(self, job: Job, tracks: list[Track], mx: dict[str, MixxxTrack], settle: float) -> None:
        """让 Mixxx 分析：每首歌只要被装进 deck，Mixxx 就会排队分析（playermanager.cpp slotAnalyzeTrack），
        分析器自己持有曲目引用，弹出后也会做完并写库。

        用 AutoDJ 逐首装载（两边音量都是 0，听不到）：开 AutoDJ → 第 1 首装进 deck1 并播放、第 2 首装进 deck2；
        之后每按一次 skip_next，deck2 换成队列里的下一首（autodjprocessor.cpp skipNext）。全部装过一遍后关掉，
        再轮询数据库直到每首都有真正的分析网格。
        """
        job.say(f"{len(tracks)} 首还没被 Mixxx 分析：逐首静音装载触发分析")
        self._ensure_quit(job)
        self._deploy(job)
        self._configure_controller(job)
        self.mixxx.write_autodj_queue([mx[t.id].id for t in tracks])
        c = self._launch_and_connect(job, settle)
        for d in (1, 2):
            ds = c.deck(d)
            if ds and ds.playing:
                raise StudioError("Mixxx 里有 deck 在播")
        for d in (1, 2):
            c.send({"t": "eject", "deck": d})
            c.send({"t": "vol", "deck": d, "v": 0.0})
        c.wait_until(lambda: not any((x := c.deck(d)) and x.loaded for d in (1, 2)), 5)

        def loads() -> int:
            return sum((x := c.deck(d)) and x.load_seq or 0 for d in (1, 2))

        def autodj_on() -> bool:
            return bool(c.status and c.status.get("ad"))

        # 一直按 skip_next，直到队列用完、AutoDJ 自己关掉（autodjprocessor.cpp loadNextTrackFromQueue）。
        # 每按一次至多两次就会让队首前进一首（skipNext / removeLoadedTrackFromTopOfQueue），所以上限 2n+4 次。
        n0 = loads()
        c.send({"t": "set", "g": "[AutoDJ]", "k": "enabled", "v": 1})
        if not c.wait_until(lambda: loads() > n0, 30):
            raise StudioError("AutoDJ 没有装载第 1 首（队列空？两个 deck 朝向不是左/右？）")
        presses = 0
        while autodj_on() and presses < 2 * len(tracks) + 4:
            before = loads()
            c.wait_until(lambda: loads() > before or not autodj_on(), 3)  # 开启后自动预载的那一首
            time.sleep(0.5)  # 给分析调度一点时间
            before = loads()
            c.send({"t": "set", "g": "[AutoDJ]", "k": "skip_next", "v": 1})
            presses += 1
            c.wait_until(lambda: loads() > before or not autodj_on(), 30)
            job.say(f"  已装载 {loads() - n0} 次")
            job.sub(0.4 * min(1.0, (loads() - n0) / max(1, len(tracks))), f"Mixxx 装载第 {loads() - n0}/{len(tracks)} 首")
        if autodj_on():
            c.send({"t": "set", "g": "[AutoDJ]", "k": "enabled", "v": 0})
        for d in (1, 2):
            c.send({"t": "play", "deck": d, "on": 0})
        c.wait_until(lambda: not any((x := c.deck(d)) and x.playing for d in (1, 2)), 5)
        for d in (1, 2):
            c.send({"t": "eject", "deck": d})
            c.send({"t": "vol", "deck": d, "v": 1.0})
        job.say("全部装载过一遍，等待 Mixxx 分析完成并写库 …")
        deadline = time.monotonic() + 60 + 30 * len(tracks)
        pending = list(tracks)
        while pending and time.monotonic() < deadline:
            time.sleep(3)
            mx2 = self._mixxx_tracks(pending)
            done = [t for t in pending if t.id in mx2 and mx2[t.id].analyzed]
            for t in done:
                job.say(f"  ✓ {t.display}：{mx2[t.id].bpm:.2f} BPM，调 {mx2[t.id].key or '?'}")
            pending = [t for t in pending if t not in done]
            job.sub(0.4 + 0.4 * (len(tracks) - len(pending)) / len(tracks),
                    f"Mixxx 分析完 {len(tracks) - len(pending)}/{len(tracks)} 首")
        if pending:
            raise StudioError("这些歌 Mixxx 还没分析完（或分析失败）：" + "、".join(t.display for t in pending))
        job.say("Mixxx 分析完成")

    def _configure_controller(self, job: Job) -> None:
        """Mixxx 退出时，把 IAC 总线的控制器映射设成 DJ Studio 并启用（mixxx.cfg：
        [ControllerPreset] <设备名> = 映射文件绝对路径，[Controller] <设备名> = 1；controllermanager.cpp）。
        设备名从已有配置里找：原来用 AgentMCP / DJ Studio 映射的那个设备，或名字里含 IAC 的设备。"""
        cfg_path = self.cfg.mixxx_dir / "mixxx.cfg"
        cfg = process.read_cfg(cfg_path)
        presets = cfg.get("[ControllerPreset]", {})
        want = str((self.cfg.mixxx_controllers / "DJStudio.midi.xml").resolve())
        key = next((k for k, v in presets.items() if v.endswith(("AgentMCP.midi.xml", "DJStudio.midi.xml"))), None)
        if key is None:
            key = next((k for k in list(presets) + list(cfg.get("[Controller]", {}))
                        if self.cfg.midi_port.lower() in k.lower()), None)
        if key is None:
            job.say("!! mixxx.cfg 里找不到 IAC 设备：第一次需要在 Mixxx 偏好设置 → 控制器里手动选一次「DJ Studio」映射")
            return
        ch = process.set_cfg_value(cfg_path, "[ControllerPreset]", key, want)
        ch |= process.set_cfg_value(cfg_path, "[Controller]", key, "1")
        job.say(f"控制器 {key}：映射 = DJ Studio" + ("（已更新 mixxx.cfg）" if ch else "（已是）"))

    # ------------------------------------------------------------ 网格校正

    def grid_fix_plan(self, track_ids: list[str] | None = None) -> list[dict[str, Any]]:
        """哪些歌的 Mixxx 网格需要换成自估网格（自估网格的线放在 kick 起音上，见 analysis.kick_phase）。"""
        tracks = [t for t in self.store.all() if track_ids is None or t.id in track_ids]
        mx = self._mixxx_tracks(tracks)
        plan = []
        for t in tracks:
            m = mx.get(t.id)
            a = t.analysis or {}
            gc = a.get("grid_check")
            if not m or not m.analyzed or not gc or gc.get("ok"):
                continue
            own = a["own_grid"]
            # BPM 一致（比值 ≈1）就保留 Mixxx 的 BPM 只换相位；Mixxx 判错倍数时整个换成自估
            bpm = m.bpm if abs(gc["ratio"] - 1) < 0.01 else own["bpm"]
            plan.append({"id": t.id, "mixxx_id": m.id, "title": t.display, "note": gc.get("note"),
                         "bpm": bpm, "first_beat": own["first_beat"]})
        return plan

    def fix_grids(self, track_ids: list[str] | None = None) -> Job:
        self._need_mixxx("校正 Mixxx 的网格")
        return self.start_job("校正网格", lambda job: self._fix_grids(job, track_ids))

    def _fix_grids(self, job: Job, track_ids: list[str] | None) -> dict[str, Any]:
        plan = self.grid_fix_plan(track_ids)
        if not plan:
            job.say("没有需要校正的网格")
            return {"fixed": 0}
        self._ensure_quit(job)
        bk = self.mixxx.backup(self.cfg.home / "backups")
        job.say(f"已备份 Mixxx 曲库 → {bk.name}")
        for p in plan:
            self.mixxx.write_grid(p["mixxx_id"], p["bpm"], p["first_beat"])
            job.say(f"  {p['title']}：{p['note']} → {p['bpm']:.3f} BPM，首拍 {p['first_beat']:.4f}s（已锁定）")
            t = self.store.get(p["id"])
            if t and t.bar1_confirmed and t.bar1 is not None:
                # 在旧网格上确认的小节线可能正好落在新网格两拍中间：取整会偏一拍，只能请用户重新确认
                off = (t.bar1 - p["first_beat"]) * p["bpm"] / 60.0
                if abs(off - round(off)) > 0.25:
                    self.store.update(p["id"], bar1_confirmed=False)
                    job.say("    ⚠ 之前确认的小节线不在新网格的拍点上，已取消确认，请在曲库页重新核对小节线")
            self.library.enqueue(p["id"])  # 按新网格重新切小节、重新核对
        self._wait_analysis(job, 0.1, 1.0, "按校正后的网格重新切小节")
        return {"fixed": len(plan)}

    # ------------------------------------------------------------ 一键：放进曲子和打碟谱 → 分析 → 可以演出

    AUTO_STAGES = ["音频分析", "读打碟谱", "Mixxx 分析", "网格核对", "生成 set", "准备 Mixxx"]
    AUTO_WEIGHTS = [0.25, 0.03, 0.40, 0.07, 0.05, 0.20]
    BUILTIN_STAGES = ["音频分析", "读打碟谱", "生成 set", "准备内置引擎"]  # 内置引擎：没有 Mixxx 那几步
    BUILTIN_WEIGHTS = [0.85, 0.03, 0.08, 0.04]

    @property
    def plans_dir(self) -> Path:
        return self.cfg.home / "plans"

    @property
    def inbox_dir(self) -> Path:
        return self.cfg.home / "inbox" / "上传"

    def current_plan(self) -> Path | None:
        """最近放进来的打碟谱。"""
        pdfs = sorted(self.plans_dir.glob("*.pdf"), key=lambda p: p.stat().st_mtime) if self.plans_dir.exists() else []
        return pdfs[-1] if pdfs else None

    def load_plan(self, path: Path) -> Any:
        """读打碟谱（按文件修改时间缓存：35 页的 PDF 读一次要一两秒）。"""
        from .plan.cdw import parse

        key = (str(path), path.stat().st_mtime)
        if self._plan_cache and self._plan_cache[0] == key:
            return self._plan_cache[1]
        plan = parse(path)
        self._plan_cache = (key, plan)
        return plan

    def _wait_analysis(self, job: Job, lo: float, hi: float, what: str) -> None:
        """等托管曲库的分析队列做完，把进度报在当前步骤的 [lo, hi] 区间里。"""
        total = None
        while True:
            q = self.library.queue_state()
            left = len(q["queued"]) + (1 if q["busy"] else 0)
            total = max(total or 0, left, 1)
            job.sub(lo + (hi - lo) * (1 - left / total), f"{what}：还有 {left} 首" if left else f"{what}：完成")
            if not left:
                return
            time.sleep(0.5)

    def lib_tracks(self, track_ids: list[str] | None = None) -> list[Any]:
        """曲库 → 打碟谱转换器用的候选曲目（有 Mixxx 网格用 Mixxx 的，否则用自估网格）。"""
        from .plan.convert import LibTrack

        tracks = [t for t in self.store.all() if track_ids is None or t.id in track_ids]
        mx = self._mixxx_tracks(tracks)
        out = []
        for t in tracks:
            a = t.analysis or {}
            m = mx.get(t.id)
            if m and m.grid and m.analyzed:
                g = Grid(m.grid.bpm, m.grid.first_beat, 0, m.duration or t.duration)
            elif a.get("own_grid"):
                g = Grid(a["own_grid"]["bpm"], a["own_grid"]["first_beat"], 0, t.duration)
            else:
                g = None
            if g is not None and t.bar1 is not None:
                g = Grid(g.bpm, g.first_beat, downbeat_from_bar1(g, t.bar1), g.duration)
            out.append(LibTrack(t.id, t.path, t.title, t.artist, t.duration, g, a.get("sections", [])))
        return out

    def plan_status(self) -> dict[str, Any] | None:
        """当前打碟谱读出来是什么、每首对上了曲库里的哪个文件（上传完就能看）。"""
        from .plan.cdw import PdfError, PlanError
        from .plan.convert import match_tracks

        path = self.current_plan()
        if not path:
            return None
        try:
            plan = self.load_plan(path)
        except (PlanError, PdfError) as e:
            return {"file": path.name, "error": str(e)}
        lib = self.lib_tracks()
        matches, _ = match_tracks(plan, lib)
        by_id = {t.id: t for t in self.store.all()}
        mx = self._mixxx_tracks(list(by_id.values()))
        rows = []
        for pt, lt in zip(plan.tracks, matches):
            t = by_id.get(lt.id) if lt else None
            rows.append({"no": pt.no, "title": pt.title, "bpm": pt.bpm_play, "file": pt.file_name,
                         "track": t.id if t else None, "display": t.display if t else None,
                         "analyzed": bool(t and t.analysis), "mixxx": bool(t and t.id in mx and mx[t.id].analyzed),
                         "grid_ok": bool(t and (t.analysis or {}).get("grid_check", {"ok": True}).get("ok", True))})
        return {"file": path.name, "title": plan.title, "n_tracks": len(plan.tracks),
                "n_transitions": len(plan.transitions), "tracks": rows,
                "techniques": [tr.technique for tr in plan.transitions], "warnings": plan.warnings}

    # ------------------------------------------------------------ 按打碟谱在电脑里找曲子

    FIND_MAX_FILES = 50000
    FIND_MAX_DEPTH = 8

    def _audio_files(self) -> list[Path]:
        """find_dirs 下的音频文件（跳过隐藏目录、托管曲库、Library、.app）。读不了的文件夹（macOS 没给权限）跳过。"""
        skip = {p.resolve() for p in (self.cfg.library, self.cfg.home, self.cfg.mixxx_dir)}
        out: list[Path] = []
        stack = [(d, 0) for d in reversed(self.cfg.find_dirs) if d.is_dir()]
        while stack and len(out) < self.FIND_MAX_FILES:
            d, depth = stack.pop()
            try:
                entries = sorted(os.scandir(d), key=lambda e: e.name)
            except OSError:
                continue
            for e in entries:
                if e.name.startswith("."):
                    continue
                try:
                    if e.is_dir(follow_symlinks=False):
                        sub = Path(e.path)
                        if depth < self.FIND_MAX_DEPTH and e.name not in ("Library", "node_modules") \
                                and sub.suffix != ".app" and sub.resolve() not in skip:
                            stack.append((sub, depth + 1))
                    elif e.is_file() and Path(e.name).suffix.lower() in AUDIO_EXTS:
                        out.append(Path(e.path))
                except OSError:
                    continue
        return out

    def find_plan_tracks(self) -> None:
        """后台：谱上还没进曲库的歌，去 find_dirs 里按文件名找（时长核对），找到就导入。完了试着自动开始一键分析。"""
        with self._find_lock:
            if self.finder.get("state") == "running":
                return
            self.finder = {"state": "running"}
        self.emit("plan", self.finder)
        threading.Thread(target=self._find, daemon=True).start()

    def _find(self) -> None:
        from .plan.convert import duration_ok, file_matches

        res: dict[str, Any] = {"state": "done", "dirs": [str(d) for d in self.cfg.find_dirs], "searched": 0, "found": []}
        try:
            path = self.current_plan()
            st = self.plan_status()
            if path and st and not st.get("error"):
                want = [i for i, r in enumerate(st["tracks"]) if not r["track"]]
                if want:
                    files = self._audio_files()
                    res["searched"] = len(files)
                    plan = self.load_plan(path)
                    picks = {i: f for i, f in file_matches(plan, [str(f) for f in files], want).items()
                             if duration_ok(plan.tracks[i], _duration(Path(f)))}
                    if picks:
                        self.library.import_paths(list(picks.values()), trusted=True)
                    res["found"] = [{"no": plan.tracks[i].no, "file": f} for i, f in sorted(picks.items())]
        except Exception as e:  # noqa: BLE001
            res = {"state": "error", "error": str(e)}
        self.finder = res
        self.emit("plan", res)
        self.maybe_auto()

    def maybe_auto(self) -> Job | None:
        """打碟谱和谱上的歌都齐了 → 自动开始一键分析（同一份谱 + 同一组歌只自动开始一次）。"""
        with self._auto_lock:
            if (self.job and self.job.state == "running") or (self.conductor and self.conductor.running):
                return None
            if self.finder.get("state") == "running":
                return None
            st = self.plan_status()
            if not st or st.get("error") or not all(r["track"] for r in st["tracks"]):
                return None
            key = (st["file"], tuple(r["track"] for r in st["tracks"]))
            if key == self._auto_key:
                return None
            try:
                job = self.auto_prepare()
            except StudioError:
                return None
            self._auto_key = key
            return job

    AUTO_SETTLE = 15.0  # Mixxx 启动后等它安定的秒数

    def auto_prepare(self, plan_path: Path | None = None, settle: float | None = None) -> Job:
        settle = self.AUTO_SETTLE if settle is None else settle
        path = plan_path or self.current_plan()
        if not path:
            raise StudioError("还没有打碟谱：把 PDF 拖进来")
        if self.builtin:
            return self.start_job("分析", lambda job: self._auto(job, path, settle), self.BUILTIN_STAGES, self.BUILTIN_WEIGHTS)
        return self.start_job("分析", lambda job: self._auto(job, path, settle), self.AUTO_STAGES, self.AUTO_WEIGHTS)

    def _auto(self, job: Job, path: Path, settle: float) -> dict[str, Any]:
        from .plan.convert import convert, match_tracks

        def step(name: str, detail: str = "") -> None:
            job.step(job.stages.index(name), detail)

        # 1 音频分析：等托管曲库里排队的分析做完
        step("音频分析")
        self._wait_analysis(job, 0.0, 1.0, "音频分析")
        bad = [t.display for t in self.store.all() if t.analysis_error]
        if bad:
            job.say("!! 这些文件分析失败（不影响别的）：" + "、".join(bad))

        # 2 读打碟谱 + 对上曲库里的文件
        step("读打碟谱", path.name)
        plan = self.load_plan(path)
        job.say(f"「{plan.title}」：{len(plan.tracks)} 首，{len(plan.transitions)} 段过渡")
        matches, notes = match_tracks(plan, self.lib_tracks())
        missing = [f"#{pt.no:02d} {pt.title}" for pt, m in zip(plan.tracks, matches) if m is None]
        if missing:
            raise StudioError("这些歌还没放进来：" + "、".join(missing))
        ids = [m.id for m in matches if m]
        if self.builtin:
            # 以前连 Mixxx 时按 Mixxx 的网格切的段落：内置引擎用自己的网格，重切一遍
            stale = [i for i in ids if ((t := self.store.get(i)) and (t.analysis or {}).get("grid_source") == "mixxx")]
            if stale:
                job.say(f"{len(stale)} 首以前按 Mixxx 的网格切的段落，按自己的网格重切 …")
                for i in stale:
                    self.library.enqueue(i)
                self._wait_analysis(job, 0.3, 1.0, "按自己的网格重切段落")

        if not self.builtin:
            # 3 Mixxx 扫描 + 分析（Mixxx 的网格是同步的依据）
            step("Mixxx 分析")
            self._sync(job, ids, settle)
            self._wait_analysis(job, 0.8, 1.0, "按 Mixxx 的网格重新切小节")

            # 4 网格核对：Mixxx 把网格放在反拍上的，换成自估网格并锁定
            step("网格核对")
            fix = self.grid_fix_plan(ids)
            if fix:
                job.say(f"{len(fix)} 首的 Mixxx 网格对不上 kick，自动校正（先备份 Mixxx 曲库）")
                self._fix_grids(job, ids)
            else:
                job.say("网格都对得上")
            job.sub(1.0)

        # 5 打碟谱 → set，并校验。谱上的第 1 小节就是你在 Engine DJ 里打 Hot Cue 1 的位置：
        #   小节相位和这里自动估的不同时，按谱写回曲库（确认过的小节线），重新切段落后再转一次
        step("生成 set")
        conv = convert(plan, self.lib_tracks(ids), source=path.name, keylock_note=not self.builtin)
        moved = []
        for pt, lt, al in zip(plan.tracks, conv.matches, conv.alignments):
            if lt and al and al.rephase:
                t = self.store.get(lt.id)
                if t and t.bar1_confirmed:
                    job.say(f"提示 #{pt.no:02d} {t.display}：曲库里确认过的小节线和谱差 {al.downbeat} 拍相位，按谱改")
                self.store.update(lt.id, bar1=al.bar1_s, bar1_confirmed=True)
                self.library.enqueue(lt.id)
                moved.append(f"#{pt.no:02d}")
        if moved:
            job.say(f"小节线按谱定：{'、'.join(moved)}；重新切段落 …")
            self._wait_analysis(job, 0.1, 0.9, "按谱的小节线重新切段落")
            conv = convert(plan, self.lib_tracks(ids), source=path.name, keylock_note=not self.builtin)
        for n in conv.notes:
            job.say(("!! " if n.level == "error" else "提示 " if n.level == "warn" else "  ") + str(n))
        if not conv.ok or conv.set is None:
            raise StudioError("打碟谱转成 set 没成功：" + "；".join(str(n) for n in conv.notes if n.level == "error"))
        s = conv.set
        self.save(s)
        c = self.compile(s)
        errs = [str(i) for i in c.issues if i.level == "error"]
        if errs:
            raise StudioError("生成的 set 校验没通过：\n" + "\n".join(errs))
        job.say(f"set「{s.name}」已生成（{s.id}），约 {c.duration / 60:.1f} 分钟")

        if self.builtin:
            step("准备内置引擎")
            self._prepare_builtin(job, s.id)
        else:
            # 6 准备 Mixxx：部署引擎、写 AutoDJ 队列、启动 Mixxx、等引擎上线
            step("准备 Mixxx")
            self._prepare(job, s.id, settle)
        return {"set": s.id, "duration": c.duration,
                "notes": [vars(n) for n in conv.notes if n.level != "info"],
                "issues": [{"level": i.level, "where": i.where, "msg": i.msg} for i in c.issues]}

    # ------------------------------------------------------------ 播放

    def play(self, set_id: str, speed: float = 1.0, from_transition: int = 0) -> Progress:
        if self.builtin:  # 内置引擎：倍速、从第 k 段过渡开始都可以
            return self.rehearse(set_id, speed, from_transition, builtin=True)
        if self.conductor and self.conductor.running:
            raise StudioError("正在播放或预演，先停下")
        self._rehearsal = None
        if self.job and self.job.state == "running":
            raise StudioError(f"任务进行中：{self.job.name}")
        prep = self.prepared()
        if not prep or prep.get("set") != set_id:
            raise StudioError("这套 set 还没准备（先点「准备」）")
        s = self.load(set_id)
        c = self.compile(s)
        if not c.ok:
            raise StudioError("set 校验没通过")
        ids = [m.id for m in self._mixxx_tracks([t for t in self.track_infos(s)[0] if t]).values()]
        q = self.mixxx.autodj_queue()
        if len(q) != len(s.tracks) or sorted(q) != sorted(ids):
            raise StudioError("Mixxx 的 AutoDJ 队列和这套 set 对不上（准备之后 Mixxx 被动过？请重新准备）")
        client = self.client()
        assert client
        if not client.wait_until(client.online, 3):
            raise StudioError("引擎不在线")
        log_path = self.cfg.logs_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{set_id}.log"

        def on_log(line: str) -> None:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            self.emit("log", line)

        self.conductor = Conductor(client, c, on_progress=lambda p: self.emit("progress", p.to_dict()), on_log=on_log)
        self.conductor.start()
        return self.conductor.progress

    def stop(self) -> None:
        if self.conductor and self.conductor.running:
            self.conductor.request_stop()

    # ------------------------------------------------------------ 预演（模拟器里跑，不碰 Mixxx）

    REHEARSAL_LEAD_BARS = 8  # 从第 k 段过渡开始预演时，提前几小节起播

    def rehearsal_set(self, s: SetFile, from_transition: int = 0) -> SetFile:
        """from_transition = k（从 1 起）：只留第 k 首起的部分，第 k 首从过渡前 REHEARSAL_LEAD_BARS 小节起播。"""
        if not from_transition:
            return s
        k = from_transition - 1
        if not 0 <= k < len(s.transitions):
            raise StudioError(f"没有第 {from_transition} 段过渡")
        tracks = [t.model_copy() for t in s.tracks[k:]]
        tracks[0] = tracks[0].model_copy(update={"start": max(1.0, s.transitions[k].start - self.REHEARSAL_LEAD_BARS)})
        return s.model_copy(update={"id": f"{s.id}-from{from_transition}", "tracks": tracks, "transitions": s.transitions[k:]})

    def rehearse(self, set_id: str, speed: float = 1.0, from_transition: int = 0, builtin: bool = False) -> Progress:
        """在 Node 模拟器里按这套 set 跑一遍（真正的 DJStudio.js，假 Mixxx，没有声音），网页演出页照常显示。
        曲目的时长 / BPM / 网格用和真机一样的解析结果，所以小节、过渡时刻和旋钮动作与真机一致。"""
        if self.conductor and self.conductor.running:
            raise StudioError("正在播放或预演，先停下")
        if self.job and self.job.state == "running":
            raise StudioError(f"任务进行中：{self.job.name}")
        miss = _engine_missing() if self.rehearsal_link_factory is _default_engine_link else None
        if miss:
            raise StudioError(miss)
        s = self.rehearsal_set(self.load(set_id), from_transition)
        c = self.compile(s)
        if not c.ok:
            raise StudioError("set 校验没通过")
        # 演出（内置引擎、原速）出声；预演和倍速不出声
        link = self._engine_link(self._sim_rows(s, c), speed, audio=builtin and speed == 1.0)
        link.sim({"cmd": "queue", "ids": [t.id for t in s.tracks]})
        client = EngineClient(link)
        if not client.wait_until(client.online, 15):
            link.close()
            raise StudioError("内置引擎没有启动起来")
        audio = link.audio() if hasattr(link, "audio") else None
        self._rehearsal = {"client": client, "speed": speed, "set": set_id, "from": from_transition, "builtin": builtin,
                           "audio": audio}
        log_path = self.cfg.logs_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{set_id}.log" if builtin else None

        def on_log(line: str) -> None:
            if log_path:
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            self.emit("log", line)

        cond = Conductor(client, c, on_progress=lambda p: self.emit("progress", p.to_dict()), on_log=on_log)
        self.conductor = cond

        def reap() -> None:  # 预演结束后留几秒最后画面，再关模拟器
            cond.join(None)
            time.sleep(3)
            if self._rehearsal and self._rehearsal["client"] is client:
                self._rehearsal = None
            link.close()

        cond.start()
        threading.Thread(target=reap, name="rehearsal-reaper", daemon=True).start()
        return cond.progress

    # ------------------------------------------------------------ 真机探测

    def probe(self) -> Job:
        from .probe import run_probe

        self._need_mixxx("真机探测")
        return self.start_job("真机探测", lambda job: run_probe(self, job))
