"""DJ Studio.app 的入口（PyInstaller 打包）：双击打开 = `djstudio app`；`DJ Studio selftest` = 自检。"""
import sys

from djstudio.cli import main

if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-psn_")]  # 老版本 macOS 双击时会带 -psn_ 参数
    sys.exit(main(args or ["app"]))
