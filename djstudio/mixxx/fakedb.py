"""按我们对 Mixxx 曲库结构的理解造一个最小的 mixxxdb.sqlite（测试和演示模式用）。"""
import sqlite3
from pathlib import Path

from djstudio.mixxx.beats import encode_grid_v2

SCHEMA = """
CREATE TABLE library (id INTEGER PRIMARY KEY, artist TEXT, title TEXT, album TEXT, duration REAL,
  samplerate INTEGER, bpm REAL, key TEXT, key_id INTEGER, beats BLOB, beats_version TEXT, location INTEGER,
  mixxx_deleted INTEGER DEFAULT 0, bpm_lock INTEGER DEFAULT 0);
CREATE TABLE track_locations (id INTEGER PRIMARY KEY, location TEXT, filename TEXT, directory TEXT,
  filesize INTEGER, fs_deleted INTEGER DEFAULT 0, needs_verification INTEGER DEFAULT 0);
CREATE TABLE Playlists (id INTEGER PRIMARY KEY, name TEXT, position INTEGER, hidden INTEGER DEFAULT 0,
  date_created TEXT, date_modified TEXT, locked INTEGER DEFAULT 0);
CREATE TABLE PlaylistTracks (id INTEGER PRIMARY KEY, playlist_id INTEGER, track_id INTEGER, position INTEGER,
  pl_datetime_added TEXT);
CREATE TABLE directories (directory TEXT UNIQUE);
INSERT INTO Playlists (id, name, hidden) VALUES (1, 'Auto DJ', 1);
INSERT INTO Playlists (id, name, hidden) VALUES (2, 'History', 2);
"""


def create(path: Path, directories=()):
    c = sqlite3.connect(path)
    c.executescript(SCHEMA)
    for d in directories:
        c.execute("INSERT INTO directories VALUES (?)", (str(d),))
    c.commit()
    c.close()


def add_track(path: Path, location: str, duration: float, bpm: float | None, first_beat: float = 0.0,
              samplerate: int = 44100, title: str = "", key_id: int | None = None) -> int:
    c = sqlite3.connect(path)
    cur = c.execute("INSERT INTO track_locations (location, filename, directory) VALUES (?, ?, ?)",
                    (location, Path(location).name, str(Path(location).parent)))
    loc = cur.lastrowid
    beats = encode_grid_v2(bpm, first_beat, samplerate) if bpm else None
    cur = c.execute("INSERT INTO library (artist, title, duration, samplerate, bpm, key_id, beats, beats_version, location)"
                    " VALUES (?,?,?,?,?,?,?,?,?)", ("", title or Path(location).stem, duration, samplerate, bpm or 0,
                                                   key_id, beats, "BeatGrid-2.0" if bpm else None, loc))
    tid = cur.lastrowid
    c.commit()
    c.close()
    return tid
