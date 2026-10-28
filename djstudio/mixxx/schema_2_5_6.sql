-- 由 Mixxx 2.5.6 的 res/schema.xml（schema 版本 39）逐个 revision 应用后导出，只保留 DJ Studio 用到的表。
-- 生成脚本：tools/mixxx_schema.py。仅用于测试和演示模式造一个结构相同的假曲库。

CREATE TABLE PlaylistTracks (
        id INTEGER PRIMARY KEY,
        playlist_id INTEGER REFERENCES Playlists(id),
        track_id INTEGER REFERENCES "library_old"(id),
        position INTEGER, pl_datetime_added);

CREATE TABLE Playlists (
        id INTEGER PRIMARY KEY,
        name varchar(48),
        position INTEGER,
        hidden INTEGER DEFAULT 0 NOT NULL,
        date_created datetime,
        date_modified datetime, locked INTEGER DEFAULT 0);

CREATE TABLE directories (
        directory TEXT UNIQUE
      );

CREATE TABLE library (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        artist varchar(64),
        title varchar(64),
        album varchar(64),
        year varchar(16),
        genre varchar(64),
        tracknumber varchar(3),
        location INTEGER REFERENCES track_locations(location),
        comment varchar(256),
        url varchar(256),
        duration FLOAT,
        bitrate INTEGER,
        samplerate INTEGER,
        cuepoint INTEGER,
        bpm FLOAT,
        wavesummaryhex BLOB,
        channels INTEGER,
        datetime_added DEFAULT CURRENT_TIMESTAMP,
        mixxx_deleted INTEGER,
        played INTEGER,
        header_parsed INTEGER DEFAULT 0, filetype varchar(8) DEFAULT '?', replaygain FLOAT DEFAULT 0, timesplayed INTEGER DEFAULT 0, rating INTEGER DEFAULT 0, key varchar(8) DEFAULT '', beats BLOB, beats_version TEXT, composer varchar(64) DEFAULT '', bpm_lock INTEGER DEFAULT 0, beats_sub_version TEXT DEFAULT '', keys BLOB, keys_version TEXT, keys_sub_version TEXT, key_id INTEGER DEFAULT 0, grouping TEXT DEFAULT '', album_artist TEXT DEFAULT '', coverart_source INTEGER DEFAULT 0, coverart_type INTEGER DEFAULT 0, coverart_location TEXT DEFAULT '', coverart_hash INTEGER DEFAULT 0, replaygain_peak REAL DEFAULT -1.0, tracktotal TEXT DEFAULT '//', color INTEGER, coverart_color INTEGER, coverart_digest BLOB, last_played_at DATETIME DEFAULT NULL, source_synchronized_ms INTEGER DEFAULT NULL);

CREATE TABLE track_locations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        location varchar(512) UNIQUE,
        filename varchar(512),
        directory varchar(512),
        filesize INTEGER,
        fs_deleted INTEGER,
        needs_verification INTEGER);

CREATE INDEX idx_PlaylistTracks_playlist_id_track_id ON PlaylistTracks (
          playlist_id,
          track_id
      );

CREATE INDEX idx_PlaylistTracks_track_id ON PlaylistTracks (
          track_id
      );
