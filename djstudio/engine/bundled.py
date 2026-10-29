"""DJ Studio 声音引擎（tools/mixxx-engine 编出来的 djstudio-engine）在哪。

找的顺序：环境变量 DJSTUDIO_ENGINE_BIN（= none 表示不用）→ 打包好的应用里（DJ Studio.app 的 engine/ 目录）→ 仓库里的
tools/mixxx-engine/dist/djstudio-engine（自己编的）。都没有就返回 None（这时内置引擎退回 Node 模拟器，没有声音）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ENGINE_NAME = "djstudio-engine"
REPO_DIST = Path(__file__).resolve().parents[2] / "tools" / "mixxx-engine" / "dist"


def _candidates() -> list[Path]:
    out = []
    env = os.environ.get("DJSTUDIO_ENGINE_BIN")
    if env == "none":  # 明确不用声音引擎（测试 Node 模拟器那条路）
        return []
    if env:
        out.append(Path(env).expanduser())
    if getattr(sys, "frozen", False):  # PyInstaller 打包的应用
        exe = Path(sys.executable).resolve()
        out += [exe.parent / "engine" / ENGINE_NAME, exe.parents[1] / "Resources" / "engine" / ENGINE_NAME]
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            out.append(Path(meipass) / "engine" / ENGINE_NAME)
    out.append(REPO_DIST / ENGINE_NAME)
    return out


def engine_binary() -> Path | None:
    for p in _candidates():
        if p.is_file() and os.access(p, os.X_OK):
            return p
    return None


def system_mappings(binary: Path) -> Path | None:
    """放 common-controller-scripts.js 的目录：打包时在引擎旁边的 res/controllers；
    直接用 Mixxx 源码树里编出来的（mixxx-src/build/djstudio-engine）时在 mixxx-src/res/controllers。"""
    dirs = [binary.parent / "res" / "controllers", binary.resolve().parents[1] / "res" / "controllers"]
    if getattr(sys, "frozen", False):  # 应用包里：程序在 Frameworks，脚本这类数据文件在 Resources
        exe = Path(sys.executable).resolve()
        dirs += [exe.parents[1] / "Resources" / "engine" / "res" / "controllers"]
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            dirs.append(Path(meipass) / "engine" / "res" / "controllers")
    for d in dirs:
        if (d / "common-controller-scripts.js").exists():
            return d
    return None


def engine_env(binary: Path) -> dict[str, str]:
    """跑引擎用的环境变量（不开窗口、不打调试日志）。"""
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_LOGGING_RULES": "*.debug=false"}
    sysdir = system_mappings(binary)
    if sysdir:
        env["DJSTUDIO_SYSTEM_MAPPINGS"] = str(sysdir)
    return env
