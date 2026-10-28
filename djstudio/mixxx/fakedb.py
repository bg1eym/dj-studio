"""造一个与 Mixxx 2.5.6 结构相同的 mixxxdb.sqlite（测试和演示模式用）。

表结构来自 schema_2_5_6.sql（由 Mixxx 的 res/schema.xml 生成，见 tools/mixxx_schema.py）；
「Auto DJ」播放列表按 PlaylistDAO::createPlaylist 的写法创建；曲目行的写法对应 Mixxx 扫描 + 分析后的结果。
"""
import sqlite3
from pathlib import Path

from .beats import encode_grid_v2

SCHEMA_SQL = Path(__file__).with_name("schema_2_5_6.sql")
# 分析器写入的 sub_version 形如 "plugin_id=...|rounding=..."（BeatFactory::getPreferredSubVersion）；非空即可
ANALYZER_SUB_VERSION = "fast_analysis=0|plugin_id=qm-tempotracker:0|rounding=0.05"


def create(path: Path, directories=()):
    c = sqlite3.connect(path)
    c.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    c.execute("INSERT INTO Playlists (name, position, hidden, date_created, date_modified) "
              "VALUES ('Auto DJ', 1, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
    for d in directories:
        c.execute("INSERT INTO directories VALUES (?)", (str(d),))
    c.commit()
    c.close()


def add_track(path: Path, location: str, duration: float, bpm: float | None, first_beat: float = 0.0,
              samplerate: int = 44100, title: str = "", key_id: int | None = None,
              sub_version: str = ANALYZER_SUB_VERSION) -> int:
    """bpm=None：只扫描进库、还没分析。sub_version="" + first_beat=0：按 BPM 标签建的临时网格。"""
    c = sqlite3.connect(path)
    p = Path(location)
    cur = c.execute("INSERT INTO track_locations (location, filename, directory, filesize, fs_deleted, "
                    "needs_verification) VALUES (?, ?, ?, ?, 0, 0)",
                    (location, p.name, str(p.parent), p.stat().st_size if p.exists() else 0))
    loc = cur.lastrowid
    beats = encode_grid_v2(bpm, first_beat, samplerate) if bpm else None
    cur = c.execute(
        "INSERT INTO library (artist, title, duration, samplerate, bpm, key_id, beats, beats_version, "
        "beats_sub_version, location, mixxx_deleted, filetype) VALUES (?,?,?,?,?,?,?,?,?,?,0,?)",
        ("", title or p.stem, duration, samplerate, bpm or 0, key_id or 0, beats,
         "BeatGrid-2.0" if bpm else None, sub_version if bpm else "", loc, p.suffix.lstrip(".").lower()))
    tid = cur.lastrowid
    c.commit()
    c.close()
    return tid
