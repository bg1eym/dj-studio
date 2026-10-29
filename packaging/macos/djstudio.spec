# -*- mode: python -*-
# PyInstaller：把网页服务（Python）和声音引擎（djstudio-engine）打成一个独立的 DJ Studio.app。
# 先编好引擎（tools/mixxx-engine/dist/），再在仓库根目录运行：pyinstaller packaging/macos/djstudio.spec
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))
ENGINE = os.path.join(ROOT, "tools", "mixxx-engine", "dist")
VERSION = os.environ.get("DJSTUDIO_APP_VERSION", "0.0.0")

datas = collect_data_files("djstudio")  # 网页文件、引擎脚本（DJStudio.js / .midi.xml）
datas.append((os.path.join(ENGINE, "res", "controllers", "common-controller-scripts.js"),
              os.path.join("engine", "res", "controllers")))
binaries = [(os.path.join(ENGINE, "djstudio-engine"), "engine")]
hidden = collect_submodules("uvicorn") + collect_submodules("djstudio")

a = Analysis([os.path.join(SPECPATH, "app.py")], pathex=[ROOT], binaries=binaries, datas=datas,
             hiddenimports=hidden, excludes=["tkinter", "pytest", "IPython", "matplotlib"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="DJ Studio", console=False, argv_emulation=False)
coll = COLLECT(exe, a.binaries, a.datas, name="DJ Studio")
app = BUNDLE(coll, name="DJ Studio.app", bundle_identifier="io.github.bg1eym.djstudio",
             info_plist={
                 "CFBundleName": "DJ Studio",
                 "CFBundleDisplayName": "DJ Studio",
                 "CFBundleShortVersionString": VERSION,
                 "CFBundleVersion": VERSION,
                 "LSMinimumSystemVersion": "11.0",
                 "LSUIElement": True,  # 没有窗口：打开后在浏览器里用，从网页右上角「退出」
                 "NSHighResolutionCapable": True,
             })
