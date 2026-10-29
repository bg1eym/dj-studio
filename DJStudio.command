#!/bin/bash
# 双击打开 DJ Studio。第一次会先安装；以后每次打开都会自动更新到最新版本。
cd "$(dirname "$0")" || exit 1
VENV="$HOME/.venvs/djstudio"
if [ ! -x "$VENV/bin/djstudio" ]; then
  echo "第一次运行：安装 DJ Studio（几分钟）…"
  python3 -m venv "$VENV" && "$VENV/bin/pip" install -q -e '.[dev]' || { echo "安装失败，按回车关闭"; read -r; exit 1; }
fi
exec "$VENV/bin/djstudio"
