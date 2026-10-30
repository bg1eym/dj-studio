# DJ Studio 声音引擎（djstudio-engine）

一个小程序，只用 Mixxx 2.5.6 里打碟要用的部分：

- deck / EngineBuffer（播放、变速、同步）、EngineMixer（推子、crossfader、主输出、电平表）
- EQ、快速效果（滤波）、效果单元（Echo / Reverb）
- MidiController + 脚本引擎：跑 `DJStudio.js`（和装进 Mixxx 的是同一份）
- 解码（SoundSourceProxy；Mac 上是 CoreAudio）

没有界面、曲库、皮肤、广播、黑胶控制、HID，也不带测试框架。Mixxx 的核心是一个静态库，链接时只把用到的目标文件放进程序。
声音从系统默认输出设备出（PortAudio，和 Mixxx 的 SoundManager 一样在声卡回调里处理引擎）。

## 四种用法

| 命令 | 用途 |
|---|---|
| `djstudio-engine` | 演出：stdin / stdout 行协议（与 `sim/fake_mixxx.mjs` 相同），Python 侧是 `EngineLink`（`djstudio/engine/link.py`） |
| `djstudio-engine --probe <文件>` | 打印 JSON：时长、采样率、声道、码率、标签 |
| `djstudio-engine --decode <文件> <输出> <采样率>` | 解码成单声道 float32，重采样到给定采样率（分析用；分析和播放用同一个解码器）。输出第 i 点 = 播放时的第 i 帧（Mac 的 CoreAudio 解码器起点不是 0，前面补静音） |
| `djstudio-engine --render <文件> <输出> <秒数> [起点秒]` | 离线渲染：deck 1 原速播放，主输出下混成单声道 float32（44100 Hz），打印起播帧号。对时检查用（`packaging/macos/timing_check.py`） |

环境变量：`DJSTUDIO_MAPPING`（DJStudio.midi.xml）、`DJSTUDIO_SYSTEM_MAPPINGS`（放 common-controller-scripts.js 的目录，
默认程序旁的 `res/controllers`）、`DJSTUDIO_SPEED`（不是 1 时不出声）、`DJSTUDIO_AUDIO=0`（不出声）、
`DJSTUDIO_AUDIO_FAKE=<路径.wav>`（测试：按真实时钟取数据的假声卡，把声音写成 WAV）。

## 构建

Mac 版由 GitHub 的构建机做（`.github/workflows/mac-app.yml`，连同 DJ Studio.app 一起）。自己编：

```bash
tools/mixxx-engine/build.sh
```

先装好 Mixxx 的构建环境（Linux：Mixxx 源码里的 `tools/debian_buildenv.sh setup`；macOS：`tools/macos_buildenv.sh setup`），
macOS 上加 `CMAKE_ARGS_EXTRA="-DCOREAUDIO=ON -DQT6=ON -DVCPKG_TARGET_TRIPLET=…"`。输出在 `tools/mixxx-engine/dist/`，
DJ Studio 会自动找到它（或设 `DJSTUDIO_ENGINE_BIN`）。测试：`DJSTUDIO_ENGINE_BIN=… pytest tests/test_mixxx_engine.py`。
