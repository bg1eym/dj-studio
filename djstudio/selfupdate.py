"""从 git 仓库直接运行（pip install -e）时：启动前自动拉最新代码、关掉还开着的旧网页服务。

都是「尽力而为」：没网、没装 git、本地改过代码，都只提示一句，用当前版本照常启动。
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from functools import lru_cache
from pathlib import Path

from . import __version__

ROOT = Path(__file__).resolve().parent.parent


def _git(*args: str, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}  # 要密码时直接失败，别卡住启动
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=timeout, env=env)


def in_git_checkout() -> bool:
    return (ROOT / ".git").exists() and (ROOT / "pyproject.toml").exists()


@lru_cache(maxsize=1)
def version_text() -> str:
    """网页右上角显示的版本：0.2.0 · 提交号（看得出跑的是不是最新代码）。"""
    rev = ""
    if in_git_checkout():
        try:
            rev = _git("rev-parse", "--short", "HEAD", timeout=5).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            rev = ""
    else:
        try:  # 打包好的应用：构建时写进去的提交号
            from ._build import COMMIT

            rev = COMMIT[:7]
        except ImportError:
            rev = ""
    return f"{__version__} · {rev}" if rev else __version__


def self_update(argv: list[str]) -> None:
    """git pull --ff-only；拉到新提交就（必要时重装依赖后）用同样的参数重启自己。"""
    if os.environ.get("DJSTUDIO_NO_UPDATE") or not in_git_checkout():
        return
    try:
        before = _git("rev-parse", "HEAD").stdout.strip()
        r = _git("pull", "--ff-only", "-q")
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"! 没能检查更新（{e}），用当前版本启动")
        return
    if r.returncode != 0:
        why = (r.stderr.strip().splitlines() or ["git pull 失败"])[-1]
        print(f"! 没能自动更新（{why}），用当前版本启动")
        return
    after = _git("rev-parse", "HEAD").stdout.strip()
    if not after or after == before:
        return
    print(f"已更新到最新版本（{after[:7]}）")
    changed = _git("diff", "--name-only", before, after).stdout.split()
    if "pyproject.toml" in changed:
        print("依赖有变化，安装中（一两分钟）…")
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-e", str(ROOT)], check=False)
    os.environ["DJSTUDIO_NO_UPDATE"] = "1"  # 重启后的那次不再拉
    os.execv(sys.executable, [sys.executable, "-m", "djstudio", *argv])


def _listening(host: str, port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def free_port(host: str, port: int) -> bool:
    """端口被占：是之前开着的 DJ Studio（多半是旧版本）就关掉它。返回端口现在能不能用。"""
    if not _listening(host, port):
        return True
    try:
        pids = subprocess.run(["lsof", "-nP", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],
                              capture_output=True, text=True, timeout=10).stdout.split()
    except (OSError, subprocess.TimeoutExpired):
        pids = []
    ours = []
    for pid in pids:
        try:
            cmd = subprocess.run(["ps", "-ww", "-p", pid, "-o", "command="], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.TimeoutExpired):
            continue
        if "djstudio" in cmd.lower().replace(" ", "") and int(pid) != os.getpid():  # 也认 DJ Studio.app
            ours.append(int(pid))
    if not ours:
        print(f"✗ 端口 {port} 被别的程序占用了。换一个端口：DJSTUDIO_PORT=8432 djstudio")
        return False
    print(f"之前打开的 DJ Studio 还在运行（进程 {'、'.join(map(str, ours))}），先关掉它")
    for sig in (signal.SIGTERM, signal.SIGKILL):  # 旧版本关得慢（网页还连着）就强制结束
        for pid in ours:
            try:
                os.kill(pid, sig)
            except OSError:
                pass
        for _ in range(30):
            if not _listening(host, port):
                return True
            time.sleep(0.1)
    print(f"✗ 之前的 DJ Studio 没有退出（端口 {port}）。关掉之前那个终端窗口再试")
    return False
