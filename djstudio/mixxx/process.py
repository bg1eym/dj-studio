"""Mixxx 进程控制（macOS）与 mixxx.cfg 读写。

- 判断运行：`pgrep -x mixxx` 在用户的 Mac 上匹配不到（旧系统踩过坑），所以三种方式都试。
- 退出：osascript quit。有 deck 在播时 Mixxx 会弹确认框导致挂住——调用方必须先把 deck 停掉。
- 启动：open -b org.mixxx.mixxx。
"""
from __future__ import annotations

import platform
import subprocess
import time
from pathlib import Path

from ..config import MIXXX_BUNDLE_ID


def _run(args: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def mixxx_running() -> bool:
    for args in (["pgrep", "-x", "mixxx"], ["pgrep", "-x", "Mixxx"], ["pgrep", "-f", "Mixxx.app/Contents/MacOS/"]):
        try:
            if _run(args, 5).returncode == 0:
                return True
        except (OSError, subprocess.TimeoutExpired):
            continue
    return False


def is_mac() -> bool:
    return platform.system() == "Darwin"


def quit_mixxx(timeout: float = 30.0) -> bool:
    if not mixxx_running():
        return True
    if not is_mac():
        raise RuntimeError("只在 macOS 上支持自动退出 Mixxx")
    try:
        _run(["osascript", "-e", f'tell application id "{MIXXX_BUNDLE_ID}" to quit'], 15)
    except subprocess.TimeoutExpired:
        pass  # 多半是弹了确认框；下面的等待会超时并报错
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not mixxx_running():
            time.sleep(1.0)  # 留时间把库写完
            return True
        time.sleep(0.5)
    return False


def launch_mixxx() -> None:
    if not is_mac():
        raise RuntimeError("只在 macOS 上支持自动启动 Mixxx")
    r = _run(["open", "-b", MIXXX_BUNDLE_ID], 15)
    if r.returncode != 0:
        raise RuntimeError(f"启动 Mixxx 失败：{r.stderr.strip()}")


# ---------------------------------------------------------------- mixxx.cfg

def read_cfg(path: Path) -> dict[str, dict[str, str]]:
    """Mixxx 的配置格式：[Group] 行，然后每行 `key value`。"""
    out: dict[str, dict[str, str]] = {}
    if not path.exists():
        return out
    group = ""
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.rstrip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            group = line
            out.setdefault(group, {})
            continue
        key, _, val = line.partition(" ")
        out.setdefault(group, {})[key] = val
    return out


def set_cfg_value(path: Path, group: str, key: str, value: str) -> bool:
    """改一个配置项（Mixxx 必须已退出）。返回是否有改动。"""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out: list[str] = []
    cur = ""
    done = False
    changed = False
    for line in lines:
        if line.startswith("[") and line.endswith("]"):
            if cur == group and not done:
                out.append(f"{key} {value}")
                done = changed = True
            cur = line
            out.append(line)
            continue
        if cur == group and line.split(" ", 1)[0] == key:
            if not done:
                new = f"{key} {value}"
                changed = line != new
                out.append(new)
                done = True
            continue
        out.append(line)
    if not done:
        if cur != group:
            if out and out[-1] != "":
                out.append("")
            out.append(group)
        out.append(f"{key} {value}")
        changed = True
    if changed:
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return changed


def mapping_status(cfg: dict[str, dict[str, str]]) -> tuple[bool, str]:
    """是否有控制器在用 DJStudio 映射。返回 (找到, 说明)。"""
    for group, kv in cfg.items():
        for k, v in kv.items():
            if v.endswith("DJStudio.midi.xml"):
                enabled = cfg.get("[Controller]", {}).get(k)
                if enabled is not None and enabled.strip() not in ("1", "true"):
                    return False, f"控制器 {k} 选了 DJ Studio 映射，但没有启用"
                return True, f"控制器 {k} 使用 DJ Studio 映射"
    return False, "没有任何控制器使用 DJ Studio 映射"
