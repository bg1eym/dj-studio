"""v5 在应用里的那一层：读谱、把素材对上曲库里的文件、一键分析、存项目、演出（开始 / 停 / 跳 / 暂停 / 接管）、整场录成 MP3、演出包。

Studio（studio.py）是网页和命令行唯一的入口；它在遇到 v5 的谱 / 项目时转到这里。这里尽量用 Studio 现成的东西：
托管曲库和分析队列、按文件名找曲子（plan/convert.match_tracks）、按谱上的 BPM / 第 1 小节对网格（convert.align）。

和 v4 不一样的地方：
- 不生成 djset/2 的 set；谱直接读成项目（djset/3），存在 ~/DJStudio/projects/<id>.json，原来的 .md 不动；
- 网格的修正（按谱的第 1 小节挪网格、小节相位）只用在这一次编译里，不改写曲库里别的谱也在用的网格；
- 演出走引擎里的原生调度器（DJSTUDIO_V5=1），不用 DJStudio.js 和 AutoDJ。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Callable, TYPE_CHECKING, Any

from ..grid import Grid
from ..plan.cdw import Plan, PlanTrack
from ..plan.convert import LibTrack, align, match_tracks
from . import samples
from .caps import BUILTIN, Caps
from .compile import AssetInfo, Compiled5, Issue5, chart, compile_project, controls_human
from .link import Engine5Error, open_engine
from .model import Project, load_project, save_project
from .runner import Progress5, Runner5
from .score import ScoreResult, parse, score_version
from .writer import write

if TYPE_CHECKING:
    from ..studio import Job, Studio

MANIFEST = "djstudio-package.json"
AUDIO_EXTS = (".mp3", ".wav", ".aif", ".aiff", ".flac", ".m4a", ".aac", ".ogg", ".opus")


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Service5:
    def __init__(self, studio: "Studio") -> None:
        self.s = studio
        self.run: dict[str, Any] | None = None  # 正在放的那一场（runner、link、项目、声音输出）
        self._score_cache: dict[tuple[str, float], ScoreResult] = {}
        self._compiled: dict[str, tuple[float, Compiled5]] = {}
        self.last: dict[str, Any] | None = None  # 上一场停在哪（「继续」用）

    # ------------------------------------------------------------ 谱

    @property
    def projects_dir(self) -> Path:
        return self.s.cfg.home / "projects"

    def is_score(self, path: Path) -> bool:
        """是不是 v5 的谱（.md，开头写着「- 规范：v5」）。"""
        if path.suffix.lower() not in (".md", ".markdown") or not path.is_file():
            return False
        try:
            return score_version(path.read_text(encoding="utf-8-sig")) == "5"
        except (UnicodeDecodeError, OSError):
            return False

    def read(self, path: Path) -> ScoreResult:
        key = (str(path), path.stat().st_mtime)
        if key not in self._score_cache:
            self._score_cache = {k: v for k, v in self._score_cache.items() if k[0] != key[0]}
            self._score_cache[key] = parse(path)
        return self._score_cache[key]

    @staticmethod
    def as_plan(p: Project) -> Plan:
        """素材表 → 按文件名找曲子用的「曲目表」（plan/convert 的那套匹配只看曲名、文件名、BPM、小节数）。"""
        tracks = [PlanTrack(no=i + 1, title=a.title + (f" — {a.artist}" if a.artist else ""), bpm_orig=a.bpm, bpm_play=a.bpm,
                            keylock=False, bars=a.bars, entry_in_prev=None, technique=None, file_name=Path(a.file).name,
                            bar1_s=a.bar1_s, key=a.key) for i, a in enumerate(p.assets)]
        return Plan(title=p.name, tracks=tracks, transitions=[])

    def pins(self, pid: str) -> dict[str, str]:
        """同名不同内容的文件：用户选定的那一个（素材 ID → 曲库 track id）。"""
        f = self.projects_dir / f"{pid}.pins.json"
        try:
            return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        except ValueError:
            return {}

    def pin(self, pid: str, asset: str, track: str) -> None:
        d = self.pins(pid)
        d[asset] = track
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        (self.projects_dir / f"{pid}.pins.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        self._compiled.pop(pid, None)

    def match(self, p: Project) -> tuple[list[LibTrack | None], list[Issue5]]:
        """每份素材对上曲库里的哪个文件。先看选定的；再按文件名精确对；再按相似度（同 v4）。
        文件名一样、内容不一样的有好几个 → 不猜，报出来让人选。"""
        lib = self.s.lib_tracks()
        by_id = {t.id: t for t in lib}
        pins = self.pins(p.id)
        fuzzy, _ = match_tracks(self.as_plan(p), lib)
        out: list[LibTrack | None] = []
        issues: list[Issue5] = []
        for a, guess in zip(p.assets, fuzzy):
            pinned = pins.get(a.id) or a.track_id
            if pinned and pinned in by_id:
                out.append(by_id[pinned])
                continue
            name = Path(a.file).name.lower()
            same = [t for t in lib if name and Path(t.path).name.lower() == name]
            if len({t.id for t in same}) > 1:
                issues.append(Issue5("error", "asset_ambiguous", f"素材 {a.id}",
                                     f"曲库里有 {len(same)} 个都叫「{Path(a.file).name}」但内容不同的文件（"
                                     + "、".join(f"{t.id} {t.duration:.0f}s" for t in same) + "）", a.line,
                                     "在首页选定用哪一个"))
                out.append(None)
            elif same:
                out.append(same[0])
            else:
                out.append(guess)
        return out, issues

    def status(self, path: Path) -> dict[str, Any]:
        """首页「放进打碟谱」那一屏：读出来是什么、每份素材对上了哪个文件、哪里写错了。"""
        r = self.read(path)
        errs = [str(i) for i in r.errors]
        if r.project is None:
            return {"file": path.name, "v5": True, "title": r.title or path.stem,
                    "error": "；\n".join(errs[:12]) + (f"\n……共 {len(errs)} 处" if len(errs) > 12 else ""),
                    "issues": [i.to_dict() for i in r.issues]}
        p = r.project
        matches, issues = self.match(p)
        by_id = {t.id: t for t in self.s.store.all()}
        rows = []
        for i, (a, lt) in enumerate(zip(p.assets, matches), 1):
            t = by_id.get(lt.id) if lt else None
            name = Path(a.file).name.lower()
            cands = [{"track": x.id, "display": x.display, "duration": x.duration}
                     for x in by_id.values() if name and Path(x.path).name.lower() == name]
            rows.append({"no": i, "asset": a.id, "title": a.title or a.id, "bpm": a.bpm, "file": a.file,
                         "track": t.id if t else None, "display": t.display if t else None,
                         "analyzed": bool(t and t.analysis), "mixxx": False, "grid_ok": True,
                         "choices": cands if len({c["track"] for c in cands}) > 1 else []})
        return {"file": path.name, "v5": True, "project": p.id, "title": p.name, "n_tracks": len(p.assets),
                "n_clips": len(p.clips), "n_scenes": len(p.scenes), "n_transitions": len(p.scenes), "tracks": rows,
                "techniques": [], "warnings": [str(i) for i in r.issues + issues if i.level == "hint"],
                "issues": [i.to_dict() for i in r.issues + issues]}

    # ------------------------------------------------------------ 素材的网格

    def infos(self, p: Project, matches: list[LibTrack | None]) -> tuple[dict[str, AssetInfo], list[Issue5]]:
        """每份素材的网格、文件、音量统一的增益。谱上写了「第 1 小节（秒）」的按它定小节线：离网格拍 0.2 拍以上
        就把网格整体挪过去（BPM 不变）；它不是文件里最早的小节线也没关系，谱上的「1.1」就是那一拍。"""
        from ..studio import track_gain_db

        out: dict[str, AssetInfo] = {}
        issues: list[Issue5] = []
        for i, (a, lt) in enumerate(zip(p.assets, matches), 1):
            if lt is None:
                continue
            t = self.s.store.get(lt.id)
            pt = PlanTrack(no=i, title=a.title or a.id, bpm_orig=a.bpm, bpm_play=a.bpm, keylock=False, bars=a.bars,
                           entry_in_prev=None, technique=None, file_name=a.file, bar1_s=a.bar1_s)
            al, notes = align(pt, lt)
            for n in notes:
                if n.level != "info":
                    issues.append(Issue5("error" if n.level == "error" else "hint", "grid", f"素材 {a.id}", n.msg, a.line))
            if al is None or lt.grid is None:
                continue
            g = lt.grid
            g = Grid(g.bpm, g.first_beat + al.grid_shift_s, al.downbeat, g.duration)
            anchor = a.bar1_s if a.bar1_s is not None else g.bar_time(1)
            gain_db = track_gain_db((t.analysis or {}) if t else {})
            out[a.id] = AssetInfo(a.id, g, path=t.path if t else lt.path,
                                  gain=round(10 ** (gain_db / 20.0), 5) if gain_db is not None else None,
                                  title=a.title or (t.display if t else a.id), track_id=lt.id,
                                  bar1=int(round(g.time_beat(anchor))))
        return out, issues

    def compile(self, p: Project, caps: Caps = BUILTIN) -> Compiled5:
        matches, m_issues = self.match(p)
        table, g_issues = self.infos(p, matches)
        c = compile_project(p, lambda a: table.get(a.id), caps, samples=self.sample_resolver(p.id, caps.sample_rate))
        c.issues = m_issues + [i for i in g_issues if not any(x.code == "asset_missing" and x.where == i.where for x in c.issues)] + c.issues
        if not c.ok:
            c.plan = None
        return c

    # ------------------------------------------------------------ 旧谱（v1–v4）看成 v5

    def from_legacy(self, set_id: str) -> tuple[Project, Compiled5, list[str]]:
        """一套旧的 set（djset/2）→ v5 的项目 + 按新编译器编出来的结果 + 和旧编译结果逐点比对的差异（空 = 等价）。
        只读：原来的 set 和谱都不动。"""
        from ..studio import StudioError
        from . import legacy

        st = self.s.load(set_id)
        old = self.s.compile(st)
        if not old.ok:
            raise StudioError("这套 set 自己还没编过：" + "；".join(str(i) for i in old.errors[:5]))
        p = legacy.from_set(st, old)
        by_id = {tr.id: tr for tr in self.s.store.all()}
        paths = {tk.id: by_id[tk.id].path for tk in st.tracks if tk.id in by_id}
        # 素材表里写上文件名：另存出来的谱换个地方也能按文件名找到曲子
        names = {legacy._aid(tid): Path(path).name for tid, path in paths.items()}
        p = p.model_copy(update={"assets": [a.model_copy(update={"file": a.file or names.get(a.id, "")}) for a in p.assets]})
        table = legacy.asset_infos(old, paths)
        new = compile_project(p, lambda a: table.get(a.id), BUILTIN)
        return p, new, (legacy.check(old, new) if new.ok else [str(i) for i in new.errors])

    # ------------------------------------------------------------ 采样

    def samples_dir(self, pid: str) -> Path:
        """这个项目自带的采样文件（导入文件夹时从谱旁边拷过来的）。"""
        return self.projects_dir / f"{pid}.samples"

    def sample_file(self, pid: str, name: str) -> Path | None:
        """谱上写的采样文件名 → 原文件：先看项目自己的，再看曲库里有没有同名的文件（拖进来的音频都在曲库里）。"""
        f = self.samples_dir(pid) / Path(name).name
        if f.is_file():
            return f
        for tr in self.s.store.all():
            if Path(tr.path).name == Path(name).name and Path(tr.path).is_file():
                return Path(tr.path)
        return None

    def sample_resolver(self, pid: str, sr: int) -> Callable[[str], Any]:
        from ..library.audio import decode_mono

        builtin = samples.resolver(self.s.cfg.home / "samples", sr=sr)

        def find(name: str) -> Any:
            info = builtin(name)
            if info is not None or not name.lower().endswith(samples.AUDIO_EXTS):
                return info
            f = self.sample_file(pid, name)
            return samples.resolver(self.s.cfg.home / "samples", [f.parent], sr, decode_mono)(f.name) if f else None

        return find

    # ------------------------------------------------------------ 项目

    def has(self, pid: str) -> bool:
        return (self.projects_dir / f"{pid}.json").is_file()

    def load(self, pid: str) -> Project:
        from ..studio import StudioError

        f = self.projects_dir / f"{pid}.json"
        if not f.is_file():
            raise StudioError(f"没有这个项目：{pid}")
        return load_project(f)

    def save(self, p: Project) -> None:
        save_project(p, self.projects_dir / f"{p.id}.json")
        self._compiled.pop(p.id, None)

    def compiled(self, pid: str) -> Compiled5:
        f = self.projects_dir / f"{pid}.json"
        mt = f.stat().st_mtime if f.exists() else 0.0
        hit = self._compiled.get(pid)
        if hit and hit[0] == mt:
            return hit[1]
        c = self.compile(self.load(pid))
        self._compiled[pid] = (mt, c)
        return c

    def chart(self, pid: str) -> dict[str, Any]:
        """演出页 / 交接说明画图用的那一份（和交给引擎的计划同一次编译）。"""
        c = self.compiled(pid)
        ch = chart(c) if c.tempo else {"id": pid, "issues": [i.to_dict() for i in c.issues]}
        ch["v5"] = True
        ch["ok"] = c.ok
        return ch

    def score_md(self, pid: str) -> str:
        """这个项目写成 v5 的谱（旧谱「另存为 v5」）。"""
        return write(self.load(pid))

    # ------------------------------------------------------------ 一键：谱 → 可以演出

    def auto(self, job: "Job", path: Path) -> dict[str, Any]:
        from ..studio import StudioError

        s = self.s

        def step(name: str, detail: str = "") -> None:
            job.step(job.stages.index(name), detail)

        step("音频分析")
        s._wait_analysis(job, 0.0, 1.0, "音频分析")
        step("读打碟谱", path.name)
        r = self.read(path)
        if r.project is None:
            raise StudioError("曲谱里有写错的地方：\n" + "\n".join(str(i) for i in r.errors[:20]))
        p = r.project
        job.say(f"「{p.name}」（规范 v5）：{len(p.assets)} 份素材，{len(p.clips)} 个片段，{len(p.scenes)} 个场景")
        matches, m_issues = self.match(p)
        bad = [i for i in m_issues if i.level == "error"]
        if bad:
            raise StudioError("\n".join(str(i) for i in bad))
        missing = [f"{a.id} {a.title or a.file}" for a, m in zip(p.assets, matches) if m is None]
        if missing:
            raise StudioError("这些素材还没放进来：" + "、".join(missing))
        ids = [m.id for m in matches if m]
        stale = s._stale(ids)
        if stale:
            job.say(f"{len(stale)} 首按现在的解码器 / 网格重新分析 …")
            for i in stale:
                s.library.enqueue(i)
            s._wait_analysis(job, 0.3, 1.0, "重新分析")
        s._grid_from_score(job, self.as_plan(p), matches)  # 谱上的原 BPM 和自动估的不一样：按谱上的把 kick 对一遍
        matches, _ = self.match(p)

        step("生成 set")
        p = p.model_copy(update={"assets": [a.model_copy(update={"track_id": m.id if m else ""})
                                            for a, m in zip(p.assets, matches)]})
        table, g_issues = self.infos(p, matches)
        c = compile_project(p, lambda a: table.get(a.id), BUILTIN, samples=self.sample_resolver(p.id, BUILTIN.sample_rate))
        for i in g_issues + c.issues:
            job.say(("!! " if i.level == "error" else "提示 ") + str(i))
        if not c.ok or any(i.level == "error" for i in g_issues):
            raise StudioError("曲谱没编过：\n" + "\n".join(str(i) for i in g_issues + c.issues if i.level == "error"))
        self.save(p)
        job.say(f"项目「{p.name}」已生成（{p.id}）：{c.length / 4:g} 小节，约 {c.duration / 60:.1f} 分钟，计划 {c.plan['exec']}")

        step("准备内置引擎")
        link = open_engine(c.library_rows(), speed=1.0, audio=True)
        try:
            if not link.wait(lambda: link.status is not None, 15):
                raise StudioError("内置引擎没有启动起来")
            got = Caps.from_engine(link.call({"cmd": "v5.caps"}))
            if got.signature() != BUILTIN.signature():
                raise StudioError("这个声音引擎的能力和应用里记的不一样（应用没装完整 / 版本对不上）：重新下载 DJ Studio")
            audio = link.audio()
            if audio and audio.get("on"):
                job.say(f"内置引擎就绪（四 Deck 调度，{link.name}），声音从「{audio.get('device')}」出")
            elif audio:
                job.say(f"!! 内置引擎就绪，但没有声音：{audio.get('error') or '打不开声音输出'}")
            else:
                job.say("内置引擎就绪（模拟器，没有声音）")
        finally:
            link.close()
        (s.cfg.home / "prepared.json").write_text(json.dumps({"set": p.id, "at": time.time(), "engine": "builtin", "v5": True}))
        hints = [i for i in g_issues + c.issues if i.level == "hint"]
        return {"set": p.id, "v5": True, "plan": path.name, "duration": c.duration, "bars": c.length / 4,
                "scenes": [{"no": sc.no, "name": sc.name, "beat": float(sc.start)} for sc in p.scenes],
                "notes": [{"level": "warn", "where": i.where + (f"（第 {i.line} 行）" if i.line else ""), "msg": i.msg + (f"（{i.fix}）" if i.fix else "")}
                          for i in hints],
                "issues": [i.to_dict() for i in c.issues]}

    # ------------------------------------------------------------ 演出

    def close_run(self) -> None:
        r, self.run = self.run, None
        if r:
            r["closing"].set()
            r["link"].close()

    def play(self, pid: str, beat: float = 0.0, speed: float = 1.0, sim: bool = False) -> Progress5:
        from ..studio import StudioError

        s = self.s
        if s.conductor and s.conductor.running:
            raise StudioError("正在演出，先停下")
        if s.recording and s.recording["state"] == "running":
            raise StudioError("正在录 MP3，录完再开始（或者先取消录音）")
        if s.job and s.job.state == "running":
            raise StudioError(f"任务进行中：{s.job.name}")
        s._close_rehearsal()  # 任何时候只有一个引擎在出声
        self.close_run()
        c = self.compiled(pid)
        if not c.ok:
            raise StudioError("曲谱没编过：" + "；".join(str(i) for i in c.errors[:5]))
        problem = c.restore_problem(beat)
        if problem:
            raise StudioError(problem)
        try:
            link = open_engine(c.library_rows(), speed=speed, audio=speed == 1.0, sim=sim)
        except Engine5Error as e:
            raise StudioError(str(e)) from e
        log_path = s.cfg.logs_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{pid}.log"

        def on_log(line: str) -> None:
            try:
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass
            s.emit("log", line)

        runner = Runner5(c, link, beat=beat, speed=speed, on_progress=lambda pr: s.emit("progress", pr.to_dict()),
                         on_log=on_log)
        run = {"runner": runner, "link": link, "project": pid, "speed": speed, "audio": None, "audio_at": 0.0,
               "closing": threading.Event()}
        try:
            run["audio"] = link.audio()
        except Engine5Error:
            pass
        self.run = run
        self.last = None
        s.conductor = runner  # type: ignore[assignment]  # 网页看的是同一个「在不在放、进度」
        s.last_play = {"set": pid, "v5": True, "beat": beat, "speed": speed}
        s.resume_point = None

        def reap() -> None:  # 放完留几秒最后的画面再关引擎；手动停的淡出完就关
            runner.join(None)
            if runner.progress.stage == "stopped" and runner.stopped_beat is not None:
                b = max(0, math.floor(runner.stopped_beat))
                self.last = {"set": pid, "v5": True, "beat": float(b), "speed": speed}
                s.resume_point = {"set": pid, "v5": True, "beat": float(b), "speed": speed, "track": 0, "bar": b / 4 + 1,
                                  "label": runner.pos_text(float(b))}
            run["closing"].wait(0.0 if runner.progress.stage in ("stopped", "error") else 3.0)
            link.close()
            if self.run is run:
                self.run = None
            s.emit("progress", runner.progress.to_dict())

        runner.start()
        threading.Thread(target=reap, name="runner5-reaper", daemon=True).start()
        return runner.progress

    def runner(self) -> Runner5:
        from ..studio import StudioError

        r = self.run
        if not r or not r["runner"].running:
            raise StudioError("现在没有在演出")
        return r["runner"]

    def engine_state(self) -> dict[str, Any] | None:
        r = self.run
        if not r:
            return None
        link, runner = r["link"], r["runner"]
        now = time.monotonic()
        if r["audio"] is not None and now - r["audio_at"] > 2.0 and not r["closing"].is_set() and not link.closed:
            r["audio_at"] = now
            try:
                r["audio"] = link.audio(timeout=1.0) or r["audio"]
            except Engine5Error:
                pass
        audio = r["audio"]
        lat = float(audio.get("latency_ms") or 0) + float(audio.get("eq_delay_ms") or 0) if audio and audio.get("on") else 0.0
        # 用到分频的计划：输出比 deck 晚固定的若干帧（引擎报的 latency），播放头也要算上
        lat += float((link.status or {}).get("latency") or 0) / runner.c.caps.sample_rate * 1000.0 / max(r["speed"], 1e-9)
        status = link.status
        if status:
            # 旋钮的实际位置换成谱上的单位（界面不用自己懂增益怎么换钟点位）
            status = {**status, "decks": [{**d, "human": controls_human(d.get("controls") or {})} for d in status.get("decks", [])]}
        return {"connected": True, "online": link.online(), "v5": True, "version": "v5", "link": link.name,
                "rehearsal": True, "builtin": True, "speed": r["speed"], "set": r["project"], "from": 0, "track": 0, "bar": 1,
                "audio": audio, "status": status, "at": round(link.status_wall * 1000 + lat, 1) if link.status_wall else None,
                "receipts": dict(runner.receipts), "from_beat": runner.from_beat, "stage": runner.progress.stage}

    # ------------------------------------------------------------ 整场录成 MP3

    def recordable(self, pid: str) -> Compiled5:
        """录之前：谱编得过、用到的原文件都还在（缺一个的话引擎照样放别的 Deck，录完才发现少了一路）。"""
        from ..studio import StudioError

        c = self.compiled(pid)
        if not c.ok:
            raise StudioError("曲谱没编过：" + "；".join(str(i) for i in c.errors[:5]))
        gone = [f"{row['id']}（{row['path']}）" for row in c.library_rows() if not Path(row["path"] or "").is_file()]
        if gone:
            raise StudioError("这些素材的原文件不在了：" + "、".join(gone))
        return c

    def record_once(self, rec: dict[str, Any], c: Compiled5, speed: float, part: Path, last: bool) -> dict[str, Any]:
        """把整场录一遍（Studio.record 的 v5 那一步；状态、放慢重录、文件名、标签在 Studio 里，和 v1–v4 共用）。

        声音引擎起成调度器模式，不出声，和演出同一份计划、同一个 Runner5，从第 0 拍开始。调度按音频帧数走、不看墙钟，
        放多快混出来的都是同一份声音；speed 只是上限，实际多快看电脑（每块之间引擎还要让读盘线程一下）。
        引擎的 record 带 trim：装载和走带起点前那几块数字静音不录，结尾的静音丢掉。效果的尾音不用另外等：
        它们算在全场长度里（Compiled5.length = 片段的停止、效果的结束 + 尾音里最晚的那个），引擎放到那里才报 done。

        倍速时会出两种岔子，哪一种录出来都和原速演出不一样：读盘没跟上（某台 Deck 一块静音，引擎数得到）；
        动作没赶上（片段的准备时间按音乐算，放得快墙钟上就只剩几分之一：回执 late / skipped / failed）。
        一出就不往下录，返回 redo，Studio 放慢重录。已经是最慢的一档（last）：读盘的和 v4 一样照录、留着次数；
        片段没装上的报错，不交一份缺了一路的录音。"""
        from ..studio import StudioError, _mmss

        try:
            link = open_engine(c.library_rows(), speed=speed, audio=False)
        except Engine5Error as e:
            raise StudioError(str(e)) from e
        bad: list[dict[str, Any]] = []  # 没按计划执行的动作（回执不是 ok 的）
        link.listeners.append(lambda m: bad.append(m) if m.get("t") == "rc" and m.get("st") != "ok" and len(bad) < 100 else None)
        try:
            if link.name != "mixxx-engine":
                raise StudioError("没有声音引擎，录不了")
            if not link.wait(lambda: link.status is not None, 15):
                raise StudioError("声音引擎没有启动起来")
            runner = Runner5(c, link, beat=0.0, speed=speed, stop_fade_ms=50.0)

            def trouble(misses: int) -> str:
                if bad:
                    m = bad[0]
                    text = next((a["text"] for a in runner.actions if a["rc"] == m.get("id")), str(m.get("id")))
                    text += f"：{m['msg']}" if m.get("msg") else ""
                    more = f"，共 {len(bad)} 处" if len(bad) > 1 else ""
                    if any(x.get("st") == "failed" for x in bad):
                        return f"有片段没装上（{text}{more}）"
                    return f"放得太快，有动作没赶上（{text}{more}）"
                return f"电脑太忙，读盘跟不上 {misses} 次（录音里会有缺口）" if misses > 0 else ""

            try:
                link.call({"cmd": "record", "path": str(part), "trim": True})
            except Engine5Error as e:
                raise StudioError(f"录音文件打不开：{e}") from e
            link.call({"cmd": "v5.perf"})  # 读盘没跟上的次数从这里重新数
            runner.start()
            t0: tuple[float, float] | None = None  # 开始走带时的（墙钟，音乐秒）
            rate, last_emit = 0.0, 0.0
            while runner.running and not rec["cancel"].is_set():
                st = link.status or {}
                if st.get("state") == "running":
                    t = c.tempo.sec_at(max(0.0, float(st.get("beat", 0.0))))
                    now = time.monotonic()
                    if t0 is None:
                        t0 = (now, t)
                    if now - t0[0] > 0.5:
                        rate = (t - t0[1]) / (now - t0[0])  # 实际的倍速（从开始走带算起）
                    rec["progress"] = round(min(0.99, t / max(1.0, c.duration)), 4)
                    rec["detail"] = (f"{rate:.0f} 倍速，" if rate >= 1.0 else "") + f"录到 {_mmss(t)} / {_mmss(c.duration)}"
                if (bad or int(st.get("misses") or 0) > 0) and (not last or any(m.get("st") == "failed" for m in bad)):
                    break  # 这一遍已经不能用了：不录到底
                if time.monotonic() - last_emit > 1.0:
                    last_emit = time.monotonic()
                    self.s._emit_recording()
                runner.join(0.2)
            if runner.running:
                runner.request_stop()
            runner.join(30)
            stop: dict[str, Any] = {}
            misses = 0
            try:
                stop = link.call({"cmd": "record_stop"}, timeout=120)
                misses = int(link.call({"cmd": "v5.perf"}).get("cache_misses") or 0)
            except Engine5Error:
                pass  # 引擎没了：下面按 Runner5 报的错说
            if rec["cancel"].is_set():
                return {}
            why = trouble(misses)
            if why and not last:
                return {"redo": why, "cache_misses": misses}
            if any(m.get("st") == "failed" for m in bad):
                raise StudioError(f"{why}，录出来会缺这一段")
            if runner.progress.stage != "done" or "seconds" not in stop:
                raise StudioError(runner.progress.error or f"录到一半停了（{runner.progress.stage}）")
            return {"seconds": float(stop["seconds"]), "cache_misses": misses, "late": len(bad), "rate": round(rate, 1)}
        finally:
            try:
                link.call({"cmd": "quit"}, timeout=5)
            except Exception:  # noqa: BLE001
                pass
            link.close()

    # ------------------------------------------------------------ 文件夹导入 / 演出包

    def import_folder(self, folder: Path) -> dict[str, Any]:
        """一个文件夹：里面一份 v5 的谱（.md）+ 谱上写的原曲。按谱里的相对路径取文件，导进托管曲库，谱放进「打碟谱」。
        文件夹里有演出包清单（djstudio-package.json）的，逐个核对内容哈希：对不上的不导，指出是哪一个。"""
        from ..studio import StudioError

        folder = folder.expanduser().resolve()
        if not folder.is_dir():
            raise StudioError(f"不是文件夹：{folder}")
        scores = sorted(f for f in folder.iterdir() if self.is_score(f))
        if not scores:
            raise StudioError(f"「{folder.name}」里没有 v5 的曲谱（开头写着「- 规范：v5」的 .md）")
        if len(scores) > 1:
            raise StudioError(f"「{folder.name}」里有 {len(scores)} 份曲谱（{'、'.join(f.name for f in scores)}）：一个文件夹放一份")
        score = scores[0]
        r = parse(score)
        if r.project is None:
            raise StudioError(f"「{score.name}」里有写错的地方：\n" + "\n".join(str(i) for i in r.errors[:20]))
        manifest: dict[str, Any] = {}
        mf = folder / MANIFEST
        if mf.is_file():
            try:
                manifest = {a["file"]: a for a in json.loads(mf.read_text(encoding="utf-8")).get("assets", [])}
            except (ValueError, KeyError, TypeError) as e:
                raise StudioError(f"{MANIFEST} 读不了：{e}") from e
        missing, changed, paths = [], [], []
        for a in r.project.assets:
            f = (folder / a.file).resolve() if a.file else None
            if f is None or folder not in f.parents or not f.is_file():
                missing.append(f"{a.id} {a.file or a.title}（第 {a.line} 行）")
                continue
            want = manifest.get(a.file)
            if want and (f.stat().st_size != want.get("size") or file_sha256(f) != want.get("sha256")):
                changed.append(f"{a.id} {a.file}")
                continue
            paths.append(str(f))
        if changed:
            raise StudioError("这些文件和演出包清单里记的内容不一样（被换过 / 没拷完整）：" + "、".join(changed))
        if missing:
            raise StudioError(f"「{folder.name}」里缺这些原曲（谱上写的相对路径）：" + "、".join(missing))
        # 谱上写的采样文件（效果表里「采样」那几行的目标）：从谱旁边拷到项目里
        wanted = sorted({f.targets[0] for f in r.project.fx if f.kind == "sample" and f.targets
                         and f.targets[0].lower().endswith(samples.AUDIO_EXTS)})
        lost = [n for n in wanted if not (folder / n).is_file()]
        if lost:
            raise StudioError(f"「{folder.name}」里缺这些采样文件：" + "、".join(lost))
        if wanted:
            sd = self.samples_dir(r.project.id)
            sd.mkdir(parents=True, exist_ok=True)
            for n in wanted:
                shutil.copyfile(folder / n, sd / Path(n).name)
        res = self.s.library.import_paths(sorted(set(paths)), trusted=True)
        if res.failed:
            raise StudioError("这些文件导不进来：" + "、".join(f"{Path(p).name}（{why}）" for p, why in res.failed))
        self.s.plans_dir.mkdir(parents=True, exist_ok=True)
        dest = self.s.plans_dir / score.name
        shutil.copyfile(score, dest)
        dest.touch()
        with self.s._auto_lock:
            self.s._auto_key = None
        self.s.maybe_auto()
        return {"score": score.name, "project": r.project.id, "assets": len(paths), "added": len(res.added),
                "existing": len(res.existing)}

    def export_package(self, pid: str, parent: Path | None = None) -> dict[str, Any]:
        """演出包：一个目录，谱（.md）和选中的全部原曲在同一层。原曲的文件名、内容原样（不转码，不出混音）；
        同一份素材只拷一次；两份素材文件名一样内容不一样 → 导出前就报错，不悄悄改名。
        先写进临时目录，拷完、逐个核对过哈希再整体换成正式的名字——拷到一半不会留下一个看起来完整的包。"""
        from ..studio import StudioError

        p = self.load(pid)
        c = self.compiled(pid)
        if not c.ok:
            raise StudioError("曲谱没编过，先解决这些再导出：" + "；".join(str(i) for i in c.errors[:5]))
        parent = (parent or self.s.cfg.library.parent / "DJ Studio 演出包").expanduser()
        name = "".join(ch if ch not in '/\\:*?"<>|' else "_" for ch in (p.name or p.id)).strip() or p.id
        files: dict[str, tuple[Path, list[str]]] = {}  # 包里的文件名 → (原文件, 用它的素材)
        for a in p.assets:
            info = next((k.info for k in c.clips if k.clip.asset == a.id), None)
            if info is None:
                continue  # 没有片段用的素材不带
            src = Path(info.path or "")
            if not src.is_file():
                raise StudioError(f"素材 {a.id} 的原文件不在了：{src}")
            fname = Path(a.file).name or src.name
            if fname in files and files[fname][0] != src:
                raise StudioError(f"素材 {files[fname][1][0]} 和 {a.id} 的文件都叫「{fname}」但不是同一个文件：改其中一个的文件名再导出")
            files.setdefault(fname, (src, []))[1].append(a.id)
        extra: dict[str, Path] = {}  # 自带的采样文件：和谱、原曲放同一层
        for f in p.fx:
            if f.kind == "sample" and f.targets and f.targets[0].lower().endswith(samples.AUDIO_EXTS):
                src = self.sample_file(pid, f.targets[0])
                fname = Path(f.targets[0]).name
                if src is None:
                    raise StudioError(f"效果 {f.id} 的采样文件「{fname}」不在了")
                if fname in files and files[fname][0] != src:
                    raise StudioError(f"采样「{fname}」和一份素材的文件同名但不是同一个文件：改其中一个的文件名再导出")
                extra[fname] = src
        parent.mkdir(parents=True, exist_ok=True)
        final = parent / name
        tmp = parent / f".{name}.{os.getpid()}.tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir()
        try:
            assets = []
            for fname, (src, ids) in sorted(files.items()):
                dst = tmp / fname
                shutil.copyfile(src, dst)
                want, got = file_sha256(src), file_sha256(dst)
                if want != got:
                    raise StudioError(f"「{fname}」拷过去内容不一样（磁盘满了？）")
                st = src.stat()
                info = next(k.info for k in c.clips if k.clip.asset == ids[0])
                assets.append({"file": fname, "assets": ids, "sha256": got, "size": st.st_size, "mtime": st.st_mtime,
                               "track": info.track_id, "grid": {"bpm": info.grid.bpm, "first_beat": info.grid.first_beat,
                                                               "bar1_s": round(info.beat_time(0.0), 5)}})
            sample_rows = []
            for fname, src in sorted(extra.items()):
                if fname not in files:
                    shutil.copyfile(src, tmp / fname)
                if file_sha256(src) != file_sha256(tmp / fname):
                    raise StudioError(f"「{fname}」拷过去内容不一样（磁盘满了？）")
                sample_rows.append({"file": fname, "sha256": file_sha256(src), "size": src.stat().st_size})
            md_name = Path(p.source).name if p.source.lower().endswith((".md", ".markdown")) else f"{name}.md"
            src_md = self.s.plans_dir / md_name
            text = src_md.read_text(encoding="utf-8-sig") if src_md.is_file() and not p.legacy else write(p)
            # 包里原曲和谱在同一层：谱上的文件名只留最后一段
            pk = p.model_copy(update={"assets": [a.model_copy(update={"file": Path(a.file).name}) for a in p.assets]})
            if any(Path(a.file).name != a.file for a in p.assets):
                text = write(pk)
            (tmp / md_name).write_text(text, encoding="utf-8")
            manifest = {"schema": "djstudio-package/1", "project": p.id, "name": p.name, "score": md_name, "spec": p.spec,
                        "exec": c.plan["exec"], "caps": c.caps.to_dict(), "duration": round(c.duration, 1),
                        "bars": c.length / 4, "assets": assets, "samples": sample_rows,
                        "exported_at": time.strftime("%Y-%m-%d %H:%M:%S")}
            (tmp / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            old = None
            if final.exists():
                old = parent / f".{name}.{os.getpid()}.old"
                shutil.rmtree(old, ignore_errors=True)
                final.rename(old)
            tmp.rename(final)
            if old:
                shutil.rmtree(old, ignore_errors=True)
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        return {"path": str(final), "name": name, "score": md_name,
                "files": [a["file"] for a in assets] + [s["file"] for s in sample_rows if s["file"] not in files],
                "bytes": sum(a["size"] for a in assets) + sum(s["size"] for s in sample_rows if s["file"] not in files)}
