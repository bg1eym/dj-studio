"""读写 Mixxx 的曲库数据库 mixxxdb.sqlite。

规则：
- 读：任何时候都可以（只读连接）。
- 写：只在 Mixxx 完全退出时，且先备份。Mixxx 运行时它会覆盖我们的修改，甚至损坏库。
- 路径比较一律先做 Unicode NFC 归一化（macOS 文件名可能是 NFD）。
"""
from __future__ import annotations

import shutil
import sqlite3
import time
import unicodedata
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .beats import MixxxGrid, camelot, decode

AUTODJ_HIDDEN = 1  # Playlists.hidden：1 = AutoDJ 队列，2 = 历史记录

REQUIRED = {
    "library": {"id", "artist", "title", "duration", "samplerate", "bpm", "key_id", "beats", "beats_version",
                "location", "mixxx_deleted"},
    "track_locations": {"id", "location", "filename", "directory", "fs_deleted"},
    "Playlists": {"id", "name", "hidden"},
    "PlaylistTracks": {"id", "playlist_id", "track_id", "position"},
    "directories": {"directory"},
}


def nfc(p: str) -> str:
    return unicodedata.normalize("NFC", p)


@dataclass
class MixxxTrack:
    id: int
    location: str
    title: str
    artist: str
    duration: float
    samplerate: int
    bpm: float
    key: str | None
    grid: MixxxGrid | None
    deleted: bool

    @property
    def analyzed(self) -> bool:
        return self.grid is not None and self.bpm > 0


class MixxxDBError(RuntimeError):
    pass


class MixxxDB:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def exists(self) -> bool:
        return self.path.exists()

    def _ro(self) -> sqlite3.Connection:
        if not self.path.exists():
            raise MixxxDBError(f"找不到 Mixxx 曲库：{self.path}")
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def _rw(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------ 检查

    def schema_problems(self) -> list[str]:
        out = []
        with closing(self._ro()) as c:
            for table, cols in REQUIRED.items():
                have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
                if not have:
                    out.append(f"缺表 {table}")
                elif cols - have:
                    out.append(f"{table} 缺列 {', '.join(sorted(cols - have))}")
        return out

    def directories(self) -> list[str]:
        with closing(self._ro()) as c:
            return [r["directory"] for r in c.execute("SELECT directory FROM directories")]

    # ------------------------------------------------------------ 曲目

    _SELECT = """
        SELECT l.id, tl.location, l.title, l.artist, l.duration, l.samplerate, l.bpm, l.key_id,
               l.beats, l.beats_version, l.mixxx_deleted
        FROM library l JOIN track_locations tl ON tl.id = l.location
    """

    @staticmethod
    def _row(r: sqlite3.Row) -> MixxxTrack:
        sr = int(r["samplerate"] or 0)
        try:
            grid = decode(r["beats"], r["beats_version"], sr)
        except (ValueError, IndexError):
            grid = None
        return MixxxTrack(
            id=int(r["id"]), location=r["location"], title=r["title"] or "", artist=r["artist"] or "",
            duration=float(r["duration"] or 0), samplerate=sr, bpm=float(r["bpm"] or 0),
            key=camelot(r["key_id"]), grid=grid, deleted=bool(r["mixxx_deleted"]),
        )

    def tracks_by_location(self, paths: Iterable[str]) -> dict[str, MixxxTrack]:
        """返回 {传入路径: 曲目}；找不到的不在结果里。已删除（mixxx_deleted）的忽略。"""
        want = {nfc(str(p)): str(p) for p in paths}
        out: dict[str, MixxxTrack] = {}
        if not want:
            return out
        with closing(self._ro()) as c:
            for r in c.execute(self._SELECT + " WHERE l.mixxx_deleted = 0 AND tl.fs_deleted = 0"):
                key = nfc(r["location"])
                if key in want:
                    out[want[key]] = self._row(r)
        return out

    def track(self, track_id: int) -> MixxxTrack | None:
        with closing(self._ro()) as c:
            r = c.execute(self._SELECT + " WHERE l.id = ?", (track_id,)).fetchone()
            return self._row(r) if r else None

    # ------------------------------------------------------------ AutoDJ 队列

    @staticmethod
    def _autodj_id(c: sqlite3.Connection) -> int | None:
        r = c.execute("SELECT id FROM Playlists WHERE hidden = ? ORDER BY id LIMIT 1", (AUTODJ_HIDDEN,)).fetchone()
        return int(r["id"]) if r else None

    def autodj_queue(self) -> list[int]:
        with closing(self._ro()) as c:
            pid = self._autodj_id(c)
            if pid is None:
                return []
            return [int(r["track_id"]) for r in
                    c.execute("SELECT track_id FROM PlaylistTracks WHERE playlist_id = ? ORDER BY position", (pid,))]

    def write_autodj_queue(self, track_ids: list[int]) -> None:
        """整体替换 AutoDJ 队列。调用方必须保证 Mixxx 已退出。"""
        with closing(self._rw()) as c:
            with c:
                pid = self._autodj_id(c)
                if pid is None:
                    raise MixxxDBError("Mixxx 库里没有 AutoDJ 播放列表（先正常打开一次 Mixxx）")
                c.execute("DELETE FROM PlaylistTracks WHERE playlist_id = ?", (pid,))
                now = time.strftime("%Y-%m-%dT%H:%M:%S")
                cols = {r["name"] for r in c.execute("PRAGMA table_info(PlaylistTracks)")}
                for pos, tid in enumerate(track_ids, start=1):
                    if "pl_datetime_added" in cols:
                        c.execute("INSERT INTO PlaylistTracks (playlist_id, track_id, position, pl_datetime_added)"
                                  " VALUES (?, ?, ?, ?)", (pid, tid, pos, now))
                    else:
                        c.execute("INSERT INTO PlaylistTracks (playlist_id, track_id, position) VALUES (?, ?, ?)",
                                  (pid, tid, pos))

    # ------------------------------------------------------------ 备份

    def backup(self, dest_dir: Path, keep: int = 10) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dst = dest_dir / f"mixxxdb-{time.strftime('%Y%m%d-%H%M%S')}.sqlite"
        with closing(self._ro()) as src, closing(sqlite3.connect(dst)) as out:
            src.backup(out)
        old = sorted(dest_dir.glob("mixxxdb-*.sqlite"))[:-keep]
        for p in old:
            p.unlink(missing_ok=True)
        return dst


def copy_if_changed(src: Path, dst: Path) -> bool:
    if dst.exists() and dst.read_bytes() == src.read_bytes():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    return True
