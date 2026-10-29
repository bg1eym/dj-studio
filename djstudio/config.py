"""路径与运行配置。所有路径都可以用环境变量覆盖，方便测试和换机器。

- DJSTUDIO_HOME     应用数据：studio.sqlite、sets/、logs/、cache/（默认 ~/DJStudio）
- DJSTUDIO_LIBRARY  托管曲库（音频文件）。默认 ~/Music/DJStudio：沙盒版 Mixxx
                    默认就能读「音乐」文件夹，且不在 iCloud 文稿里。
- DJSTUDIO_MIXXX_DIR  Mixxx 设置目录（含 mixxxdb.sqlite、controllers/）
- DJSTUDIO_MIDI_PORT  IAC 总线端口名的子串（默认匹配第一个 IAC 口）
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

MIXXX_BUNDLE_ID = "org.mixxx.mixxx"

_MIXXX_CANDIDATES = [
    # 沙盒版（App Store / 官方签名包）
    "~/Library/Containers/org.mixxx.mixxx/Data/Library/Application Support/Mixxx",
    # 非沙盒版
    "~/Library/Application Support/Mixxx",
    # Linux（开发用）
    "~/.mixxx",
]


def _env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name) or default).expanduser()


def find_mixxx_dir() -> Path:
    env = os.environ.get("DJSTUDIO_MIXXX_DIR")
    if env:
        return Path(env).expanduser()
    for c in _MIXXX_CANDIDATES:
        p = Path(c).expanduser()
        if (p / "mixxxdb.sqlite").exists():
            return p
    return Path(_MIXXX_CANDIDATES[0]).expanduser()


def _find_dirs() -> list[Path]:
    env = os.environ.get("DJSTUDIO_FIND_DIRS")
    if env is not None:
        return [Path(p).expanduser() for p in env.split(os.pathsep) if p]
    return [Path.home() / d for d in ("Downloads", "Desktop", "Documents", "Music")]


@dataclass
class Config:
    home: Path = field(default_factory=lambda: _env_path("DJSTUDIO_HOME", "~/DJStudio"))
    library: Path = field(default_factory=lambda: _env_path("DJSTUDIO_LIBRARY", "~/Music/DJStudio"))
    mixxx_dir: Path = field(default_factory=find_mixxx_dir)
    midi_port: str = field(default_factory=lambda: os.environ.get("DJSTUDIO_MIDI_PORT", "IAC"))
    # 网页里允许浏览 / 导入的根目录（防止网页读到任意路径）
    browse_roots: list[Path] = field(default_factory=lambda: [Path.home(), Path("/Volumes")])
    # 只放进打碟谱时，去这些文件夹里按谱上的文件名找曲子（DJSTUDIO_FIND_DIRS 用冒号分隔可改）
    find_dirs: list[Path] = field(default_factory=lambda: _find_dirs())
    host: str = "127.0.0.1"
    port: int = int(os.environ.get("DJSTUDIO_PORT", "8430"))

    @property
    def db_path(self) -> Path:
        return self.home / "studio.sqlite"

    @property
    def sets_dir(self) -> Path:
        return self.home / "sets"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def cache_dir(self) -> Path:
        return self.home / "cache"

    @property
    def mixxx_db(self) -> Path:
        return self.mixxx_dir / "mixxxdb.sqlite"

    @property
    def mixxx_controllers(self) -> Path:
        return self.mixxx_dir / "controllers"

    def ensure_dirs(self) -> None:
        for p in (self.home, self.sets_dir, self.logs_dir, self.cache_dir, self.library):
            p.mkdir(parents=True, exist_ok=True)


_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config()
    return _config


def set_config(cfg: Config) -> None:
    global _config
    _config = cfg
