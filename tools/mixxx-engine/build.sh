#!/bin/bash
# 用 Mixxx 2.5.6 的源码编 DJ Studio 声音引擎（djstudio-engine）：只编这一个程序，Mixxx 的可选功能全关。
# 输出：tools/mixxx-engine/dist/djstudio-engine 和 dist/res/controllers/common-controller-scripts.js
#
# 用法：tools/mixxx-engine/build.sh [Mixxx 源码目录]（默认 tools/mixxx-engine/mixxx-src，没有就 git clone）
# 依赖：Mixxx 自己的构建环境（Linux：tools/debian_buildenv.sh setup；macOS：tools/macos_*_buildenv.sh setup）。
# 额外的 cmake 参数放在 CMAKE_ARGS_EXTRA 里（macOS：-DCOREAUDIO=ON -DQT6=ON -DVCPKG_TARGET_TRIPLET=…）。
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
SRC=${1:-$HERE/mixxx-src}
if [ ! -d "$SRC" ]; then
  git clone --depth 1 --branch 2.5.6 https://github.com/mixxxdj/mixxx "$SRC"
fi
SRC=$(cd "$SRC" && pwd)
if ! grep -q DJSTUDIO_ENGINE_DIR "$SRC/CMakeLists.txt"; then
  printf '\n# DJ Studio 声音引擎（只链接 Mixxx 的核心静态库）\nset(DJSTUDIO_ENGINE_DIR "%s")\ninclude("${DJSTUDIO_ENGINE_DIR}/engine.cmake")\n' "$HERE" >> "$SRC/CMakeLists.txt"
fi
BUILD="$SRC/build-djstudio"
mkdir -p "$BUILD"
cd "$BUILD"
# 我们只要：deck / 引擎 / EQ / 效果 / 同步 / 控制器脚本 / 解码。界面以外能关的都关掉。
cmake -DCMAKE_BUILD_TYPE=Release \
  -DBUILD_TESTING=OFF -DBUILD_BENCH=OFF -DQML=OFF -DOPTIMIZE=portable \
  -DBATTERY=OFF -DBROADCAST=OFF -DBULK=OFF -DHID=OFF -DHSS1394=OFF -DKEYFINDER=OFF -DLILV=OFF \
  -DMODPLUG=OFF -DOPUS=OFF -DQTKEYCHAIN=OFF -DVINYLCONTROL=OFF -DWAVPACK=OFF -DENGINEPRIME=OFF \
  -DLOCALECOMPARE=OFF -DDOWNLOAD_MANUAL=OFF -DINSTALL_USER_UDEV_RULES=OFF -DMACOS_BUNDLE=OFF \
  ${CMAKE_ARGS_EXTRA:-} "$SRC"
JOBS=$( (sysctl -n hw.ncpu 2>/dev/null || nproc) )
cmake --build . --target djstudio-engine --parallel "$JOBS"
rm -rf "$HERE/dist"
mkdir -p "$HERE/dist/res/controllers"
cp djstudio-engine "$HERE/dist/"
cp "$SRC/res/controllers/common-controller-scripts.js" "$HERE/dist/res/controllers/"
echo "✓ $HERE/dist/djstudio-engine"
