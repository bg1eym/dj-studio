"""从 Mac 本地文件夹导入音频到托管曲库，并排队做分析。

- 浏览：只允许家目录和外接卷（/Volumes）下的路径。
- 导入：按内容 sha1 去重；复制到 <托管曲库>/<来源文件夹名>/<文件名>（重名加后缀）。
  已经在托管曲库里的文件原地登记，不复制。
- iCloud「仅云端」的文件读取时会触发下载，可能很慢：浏览时会标出来。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .audio import AUDIO_EXTS, AudioError, decode_mono, decoder_name, probe, ANALYSIS_SR
from .analysis import ANALYSIS_VERSION, analyze
from .store import Store, Track

SF_DATALESS = 0x40000000  # macOS：文件内容不在本地（iCloud 仅云端）


def check_allowed(p: Path, roots: list[Path]) -> Path:
    p = p.expanduser().resolve()
    rs = [r.expanduser().resolve() for r in roots]
    if not any(p == r or r in p.parents for r in rs):
        raise PermissionError(f"不允许访问 {p}（只能浏览：{', '.join(map(str, rs))}）")
    return p


def is_dataless(p: Path) -> bool:
    try:
        return bool(getattr(os.stat(p), "st_flags", 0) & SF_DATALESS)
    except OSError:
        return False


def browse(path: str | None, roots: list[Path]) -> dict[str, Any]:
    if not path:
        rs = [r for r in roots if r.exists()]
        return {"path": "", "parent": None, "files": [],
                "dirs": [{"name": str(r), "path": str(r)} for r in rs]}
    p = check_allowed(Path(path), roots)
    if not p.is_dir():
        raise NotADirectoryError(str(p))
    dirs, files = [], []
    for e in sorted(p.iterdir(), key=lambda x: x.name.lower()):
        if e.name.startswith("."):
            continue
        try:
            if e.is_dir():
                dirs.append({"name": e.name, "path": str(e)})
            elif e.suffix.lower() in AUDIO_EXTS:
                st = e.stat()
                files.append({"name": e.name, "path": str(e), "size": st.st_size, "cloud": is_dataless(e)})
        except OSError:
            continue
    rs = [r.expanduser().resolve() for r in roots]
    parent = str(p.parent) if p not in rs and any(p.parent == r or r in p.parent.parents for r in rs) else ""
    return {"path": str(p), "parent": parent, "dirs": dirs, "files": files}


def sha1_id(p: Path) -> str:
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def _unique_dest(d: Path, name: str) -> Path:
    dest = d / name
    stem, suf = dest.stem, dest.suffix
    n = 2
    while dest.exists():
        dest = d / f"{stem} ({n}){suf}"
        n += 1
    return dest


@dataclass
class ImportResult:
    added: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)


class Library:
    """托管曲库 + 后台分析队列。"""

    def __init__(self, store: Store, root: Path, mixxx_grid: Callable[[Track], tuple[float, float] | None] | None = None,
                 browse_roots: list[Path] | None = None) -> None:
        self.store = store
        self.root = root
        self.browse_roots = browse_roots or [Path.home()]
        self.mixxx_grid = mixxx_grid  # track → (bpm, first_beat)，有 Mixxx 网格时用它切小节
        self._q: deque[str] = deque()
        self._cv = threading.Condition()
        self._busy: str | None = None
        self._worker: threading.Thread | None = None
        self.on_change: Callable[[str], None] | None = None

    # ------------------------------------------------------------ 导入

    def import_paths(self, paths: list[str], analyze_now: bool = True, trusted: bool = False) -> ImportResult:
        """trusted：路径是我们自己写的（网页上传的暂存文件），不做浏览范围检查。"""
        res = ImportResult()
        files: list[Path] = []
        for s in paths:
            p = Path(s).resolve() if trusted else check_allowed(Path(s), [*self.browse_roots, self.root])
            if p.is_dir():
                files += sorted(f for f in p.rglob("*") if f.suffix.lower() in AUDIO_EXTS and not f.name.startswith("."))
            elif p.suffix.lower() in AUDIO_EXTS:
                files.append(p)
        self.root.mkdir(parents=True, exist_ok=True)
        root = self.root.resolve()
        for f in files:
            try:
                tid = sha1_id(f)
                if self.store.get(tid):
                    res.existing.append(tid)
                    continue
                if root in f.parents:
                    dest = f
                else:
                    sub = root / f.parent.name
                    sub.mkdir(parents=True, exist_ok=True)
                    dest = _unique_dest(sub, f.name)
                    shutil.copy2(f, dest)
                    os.chmod(dest, os.stat(dest).st_mode | stat.S_IRUSR | stat.S_IWUSR)
                pr = probe(dest)
                self.store.add(Track(
                    id=tid, path=str(dest), source=str(f), title=pr.title or f.stem, artist=pr.artist,
                    album=pr.album, duration=pr.duration, samplerate=pr.samplerate, channels=pr.channels,
                    codec=pr.codec, bitrate=pr.bitrate, size=dest.stat().st_size,
                    added_at=time.strftime("%Y-%m-%d %H:%M:%S")))
                res.added.append(tid)
                if analyze_now:
                    self.enqueue(tid)
            except (OSError, AudioError) as e:
                res.failed.append((str(f), str(e)))
        return res

    def scan_root(self) -> ImportResult:
        """把托管曲库里还没登记的文件（例如在访达里直接拖进去的）登记进来。"""
        return self.import_paths([str(self.root)])

    # ------------------------------------------------------------ 分析

    def enqueue(self, track_id: str) -> None:
        with self._cv:
            if track_id not in self._q and track_id != self._busy:
                self._q.append(track_id)
            self._cv.notify_all()
        if not self._worker or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._work, name="analysis", daemon=True)
            self._worker.start()

    def queue_state(self) -> dict[str, Any]:
        with self._cv:
            return {"busy": self._busy, "queued": list(self._q)}

    def wait_idle(self, timeout: float = 60.0) -> bool:
        with self._cv:
            return self._cv.wait_for(lambda: not self._q and self._busy is None, timeout)

    def _work(self) -> None:
        while True:
            with self._cv:
                if not self._q:
                    self._busy = None
                    self._cv.notify_all()
                    return
                tid = self._q.popleft()
                self._busy = tid
            try:
                self.analyze_one(tid)
            except Exception as e:  # noqa: BLE001
                self.store.update(tid, analysis_error=str(e)[:500])
            finally:
                if self.on_change:
                    self.on_change(tid)

    def analyze_one(self, tid: str) -> None:
        t = self.store.get(tid)
        if not t:
            return
        decoder = decoder_name()
        x = decode_mono(Path(t.path), ANALYSIS_SR)
        grid = self.mixxx_grid(t) if self.mixxx_grid else None
        result, wf = analyze(x, ANALYSIS_SR, grid, t.bar1 if t.bar1_confirmed else None)
        result["decoder"] = decoder
        self.store.save_waveform(tid, wf)
        fields: dict[str, Any] = {"analysis": result, "analysis_version": ANALYSIS_VERSION, "analysis_error": None}
        # 用户确认的是「哪一拍是第 1 小节」；具体时间跟着网格走（换了解码器重新分析时，网格可能挪了几十毫秒）
        if not t.bar1_confirmed or (t.bar1 is not None and abs(result["bar1"] - t.bar1) < 0.1):
            fields["bar1"] = result["bar1"]
        self.store.update(tid, **fields)
