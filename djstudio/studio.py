"""Studio：把曲库、Mixxx、引擎、conductor 串起来的服务层。网页和命令行都只调用这里。"""
from __future__ import annotations

import json
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .compiler import Compiled, TrackInfo, compile_set
from .config import Config, get_config
from .engine.client import EngineClient, EngineError
from .engine.conductor import Conductor, Progress
from .engine.link import Link, LinkError, RtMidiLink
from .grid import Grid
from .library.analysis import downbeat_from_bar1
from .library.audio import have_ffmpeg
from .library.importer import Library
from .library.store import Store, Track
from .mixxx import process
from .mixxx.db import MixxxDB, MixxxDBError, MixxxTrack, copy_if_changed
from .setfile import SetFile, SetTrack, load_set, save_set

MAPPING_DIR = Path(__file__).parent / "engine" / "mapping"
MAPPING_FILES = ("DJStudio.js", "DJStudio.midi.xml")
ANALYZE_EXTRA = 8.0  # file_bpm 出来后再等几秒，让节拍网格 / 调性分析跑完


def mapping_version() -> str:
    src = (MAPPING_DIR / "DJStudio.js").read_text(encoding="utf-8")
    for line in src.splitlines():
        if line.startswith("DJStudio.VERSION"):
            return line.split('"')[1]
    return "?"


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

    def say(self, msg: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')} {msg}")

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "state": self.state, "log": self.log[-300:], "result": self.result,
                "started": self.started, "finished": self.finished}


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
        self.job: Job | None = None
        self._job_lock = threading.Lock()
        self.listeners: list[Callable[[str, Any], None]] = []  # (kind, payload) → 网页 SSE
        self.library.on_change = lambda tid: self.emit("track", tid)

    # ------------------------------------------------------------ 事件广播

    def emit(self, kind: str, payload: Any) -> None:
        for fn in list(self.listeners):
            try:
                fn(kind, payload)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------ 引擎连接

    def client(self, connect: bool = True) -> EngineClient | None:
        with self._client_lock:
            if self._client is None and connect:
                link = self.link_factory()
                self._client = EngineClient(link)
                self._client.add_listener(lambda m: self.emit("engine", m) if m.get("t") != "st" else None)
            return self._client

    def disconnect(self) -> None:
        with self._client_lock:
            if self._client:
                self._client.link.close()
                self._client = None

    def engine_state(self) -> dict[str, Any]:
        c = self._client
        if not c:
            return {"connected": False, "online": False}
        return {"connected": True, "online": c.online(), "version": c.version, "age": round(c.status_age(), 2),
                "status": c.status, "link": c.link.name}

    # ------------------------------------------------------------ 曲目解析

    def _mixxx_tracks(self, tracks: list[Track]) -> dict[str, MixxxTrack]:
        if not self.mixxx.exists():
            return {}
        try:
            by_loc = self.mixxx.tracks_by_location([t.path for t in tracks])
        except MixxxDBError:
            return {}
        return {t.id: by_loc[t.path] for t in tracks if t.path in by_loc}

    def _mixxx_grid_for(self, t: Track) -> tuple[float, float] | None:
        m = self._mixxx_tracks([t]).get(t.id)
        return (m.grid.bpm, m.grid.first_beat) if m and m.grid else None

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
            if m is None or m.grid is None:
                # 不在 Mixxx 库 / 没分析：返回空网格让编译器报「没分析」
                return TrackInfo(t.id, t.display, Grid(0.0 if m is None else m.bpm or 0.0, 0.0, 0, 0.0))
            g = Grid(m.grid.bpm, m.grid.first_beat, 0, m.duration or t.duration)
            db = downbeat_from_bar1(g, t.bar1) if t.bar1 is not None else 0
            return TrackInfo(t.id, t.display, Grid(g.bpm, g.first_beat, db, g.duration))

        c = compile_set(s, resolve)
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

        add("ffmpeg", "ok" if have_ffmpeg() else "error",
            "已安装" if have_ffmpeg() else "没有 ffmpeg / ffprobe：brew install ffmpeg")
        add("托管曲库", "ok", str(self.cfg.library))
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

    def start_job(self, name: str, fn: Callable[[Job], Any]) -> Job:
        with self._job_lock:
            if self.job and self.job.state == "running":
                raise StudioError(f"已经有任务在跑：{self.job.name}")
            if self.conductor and self.conductor.running:
                raise StudioError("set 正在播放，不能做这个操作")
            job = self.job = Job(name)

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
        job.say(f"引擎在线（{c.version}），等待 Mixxx 安定 {settle:g}s …")
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
        return self.start_job(f"准备 {set_id}", lambda job: self._prepare(job, set_id, settle))

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
        """让 Mixxx 分析：把这些歌排进 AutoDJ 队列，逐首装进 deck1（静音）触发分析。

        Mixxx 不通过控件公开分析进度：等到 file_bpm > 0 后再多等 ANALYZE_EXTRA 秒，最后以数据库为准核对。
        """
        job.say(f"{len(tracks)} 首还没被 Mixxx 分析：逐首装进 deck1 触发分析（静音）")
        self._ensure_quit(job)
        self._deploy(job)
        self.mixxx.write_autodj_queue([mx[t.id].id for t in tracks])
        c = self._launch_and_connect(job, settle)
        for d in (1, 2):
            ds = c.deck(d)
            if ds and ds.playing:
                raise StudioError("Mixxx 里有 deck 在播")
        for t in tracks:
            cur = [c.mark()]
            c.send({"t": "load", "deck": 1})
            ev = None
            deadline = time.monotonic() + 30
            while ev is None and time.monotonic() < deadline:
                ev = next((e for e in c.drain(cur) if e.get("e") in ("loaded", "load_failed")), None)
                time.sleep(0.2)
            if not ev or ev["e"] == "load_failed":
                raise StudioError(f"装载 {t.display} 失败")
            ok = c.wait_until(lambda: (ds := c.deck(1)) is not None and ds.file_bpm > 0, 90)
            time.sleep(ANALYZE_EXTRA)
            ds = c.deck(1)
            job.say(f"  {t.display}：{ds.file_bpm if ok and ds else '分析超时'} BPM")
        c.send({"t": "eject", "deck": 1})
        # Mixxx 在曲目卸载时把分析结果写进库；退出确保落盘
        self._ensure_quit(job)
        mx2 = self._mixxx_tracks(tracks)
        bad = [t.display for t in tracks if t.id not in mx2 or not mx2[t.id].analyzed]
        if bad:
            raise StudioError("这些歌 Mixxx 没分析出网格：" + "、".join(bad))
        job.say("Mixxx 分析完成")

    # ------------------------------------------------------------ 播放

    def play(self, set_id: str) -> Progress:
        if self.conductor and self.conductor.running:
            raise StudioError("已经在播了")
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

    # ------------------------------------------------------------ 真机探测

    def probe(self) -> Job:
        from .probe import run_probe

        return self.start_job("真机探测", lambda job: run_probe(self, job))
