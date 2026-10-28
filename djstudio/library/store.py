"""DJ Studio 自己的曲库（studio.sqlite）：托管曲库里每个文件一行 + 我们的分析结果。

曲目 id = 文件内容 sha1 的前 12 位：同一个文件重复导入会被识别出来。
波形数据存在 cache/waveforms/<id>.json（较大，不进数据库）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
  id TEXT PRIMARY KEY,
  path TEXT NOT NULL UNIQUE,
  source TEXT,
  title TEXT, artist TEXT, album TEXT,
  duration REAL, samplerate INTEGER, channels INTEGER, codec TEXT, bitrate INTEGER, size INTEGER,
  added_at TEXT,
  bar1 REAL,
  bar1_confirmed INTEGER NOT NULL DEFAULT 0,
  analysis_json TEXT,
  analysis_version INTEGER,
  analysis_error TEXT,
  note TEXT
);
"""


@dataclass
class Track:
    id: str
    path: str
    source: str | None = None
    title: str = ""
    artist: str = ""
    album: str = ""
    duration: float = 0.0
    samplerate: int = 0
    channels: int = 0
    codec: str = ""
    bitrate: int = 0
    size: int = 0
    added_at: str = ""
    bar1: float | None = None  # 第 1 小节起点（秒）。与网格无关，换网格后用它重新推算小节相位
    bar1_confirmed: bool = False
    analysis: dict[str, Any] | None = None
    analysis_version: int | None = None
    analysis_error: str | None = None
    note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def display(self) -> str:
        if self.artist and self.title:
            return f"{self.artist} - {self.title}"
        return self.title or Path(self.path).stem


_COLS = ["id", "path", "source", "title", "artist", "album", "duration", "samplerate", "channels", "codec",
         "bitrate", "size", "added_at", "bar1", "bar1_confirmed", "analysis_json", "analysis_version",
         "analysis_error", "note"]


class Store:
    def __init__(self, db_path: Path, cache_dir: Path) -> None:
        self.db_path = db_path
        self.wave_dir = cache_dir / "waveforms"
        self._lock = threading.Lock()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.wave_dir.mkdir(parents=True, exist_ok=True)
        with closing(self._conn()) as c, c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    @staticmethod
    def _track(r: sqlite3.Row) -> Track:
        d = dict(r)
        aj = d.pop("analysis_json")
        d["analysis"] = json.loads(aj) if aj else None
        d["bar1_confirmed"] = bool(d["bar1_confirmed"])
        for k in ("title", "artist", "album", "codec", "note"):
            d[k] = d[k] or ""
        return Track(**d)

    def add(self, t: Track) -> None:
        with self._lock, closing(self._conn()) as c, c:
            c.execute(
                "INSERT INTO tracks (id, path, source, title, artist, album, duration, samplerate, channels, codec,"
                " bitrate, size, added_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (t.id, t.path, t.source, t.title, t.artist, t.album, t.duration, t.samplerate, t.channels, t.codec,
                 t.bitrate, t.size, t.added_at or time.strftime("%Y-%m-%d %H:%M:%S")))

    def get(self, track_id: str) -> Track | None:
        with closing(self._conn()) as c:
            r = c.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
            return self._track(r) if r else None

    def by_path(self, path: str) -> Track | None:
        with closing(self._conn()) as c:
            r = c.execute("SELECT * FROM tracks WHERE path = ?", (path,)).fetchone()
            return self._track(r) if r else None

    def all(self) -> list[Track]:
        with closing(self._conn()) as c:
            return [self._track(r) for r in c.execute("SELECT * FROM tracks ORDER BY added_at DESC, path")]

    def search(self, q: str, limit: int = 20) -> list[Track]:
        like = f"%{q}%"
        with closing(self._conn()) as c:
            rows = c.execute("SELECT * FROM tracks WHERE title LIKE ? OR artist LIKE ? OR path LIKE ? LIMIT ?",
                             (like, like, like, limit))
            return [self._track(r) for r in rows]

    def update(self, track_id: str, **fields: Any) -> None:
        if "analysis" in fields:
            a = fields.pop("analysis")
            fields["analysis_json"] = json.dumps(a, ensure_ascii=False) if a is not None else None
        if "bar1_confirmed" in fields:
            fields["bar1_confirmed"] = int(bool(fields["bar1_confirmed"]))
        bad = set(fields) - set(_COLS)
        if bad:
            raise KeyError(f"未知字段 {bad}")
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._lock, closing(self._conn()) as c, c:
            c.execute(f"UPDATE tracks SET {sets} WHERE id = ?", (*fields.values(), track_id))

    def delete(self, track_id: str) -> None:
        with self._lock, closing(self._conn()) as c, c:
            c.execute("DELETE FROM tracks WHERE id = ?", (track_id,))
        (self.wave_dir / f"{track_id}.json").unlink(missing_ok=True)

    # 波形
    def save_waveform(self, track_id: str, wf: dict[str, Any]) -> None:
        (self.wave_dir / f"{track_id}.json").write_text(json.dumps(wf, separators=(",", ":")))

    def waveform_path(self, track_id: str) -> Path:
        return self.wave_dir / f"{track_id}.json"
