# DJ Studio 声音引擎（djstudio-engine）

一个小程序，只用 Mixxx 2.5.6 里打碟要用的部分：

- deck / EngineBuffer（播放、变速、同步）、EngineMixer（推子、crossfader、主输出、电平表）
- EQ、快速效果（滤波）、效果单元（Echo / Reverb）
- MidiController + 脚本引擎：跑 `DJStudio.js`（和装进 Mixxx 的是同一份）
- 解码（SoundSourceProxy；Mac 上是 CoreAudio）

没有界面、曲库、皮肤、广播、黑胶控制、HID，也不带测试框架。Mixxx 的核心是一个静态库，链接时只把用到的目标文件放进程序。
声音从系统默认输出设备出（PortAudio，和 Mixxx 的 SoundManager 一样在声卡回调里处理引擎）。
**跟着系统走**：主线程每 0.5 秒问一次 CoreAudio 现在的默认输出（`kAudioHardwarePropertyDefaultOutputDevice`），换了（插耳机、连 AirPods、在菜单栏换输出）就关掉声音输出、`Pa_Terminate` + `Pa_Initialize`（PortAudio 只在初始化时列设备）、在新的默认设备上重开；中间零点几秒引擎不处理，两个 deck 一起停这一下，彼此仍然对齐。`{"cmd":"audio","reopen":true}` 强制重开一次（CI 冒烟测试用）。

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

## 四 Deck 调度器模式（曲谱 v5）

`DJSTUDIO_V5=1` 起成调度器模式：不装 DJStudio.js、不用 AutoDJ，四台 Deck 由引擎里的原生调度器（`djs5/`，纯 C++17 头文件）按一份执行计划来放——
起 / 停落在采样上，自己的混音（门、推子、处理组、发送效果、采样、主输出）。命令和消息见 [docs/ENGINE5.md](../../docs/ENGINE5.md)。

| 文件 | 内容 |
|---|---|
| `djs5/core.hpp` | 计划的数据结构和解析、速度图 |
| `djs5/engine5.hpp` | 调度器：控制步（装载 / 定位 / loop / EQ）和出声（门、推子、路由、主输出）、命令、状态、回执 |
| `djs5/bus.hpp` | 处理组（插入 / 组插入 / 总输出）：现场效果什么时候开、接在哪、怎么收 |
| `djs5/dsp.hpp` | DSP 模块：线性相位三段分频、Trans、Roll、滤波、Spiral、Noise、Flanger、Phaser、刹车 / 回转、采样器 |
| `djs5/fx.hpp` | 发送效果用的延迟线（Mixxx Echo 的算法）、混响的接口 |
| `djs5/sim5.cpp` | 独立模拟器：同一个调度器 + 假 Deck（没有声音引擎时开发 / 测试用，Python 第一次用时现编） |
| `djs5/test_dsp.cpp` | DSP 模块自己的检查：`g++ -O2 -std=c++17 -ffast-math djs5/test_dsp.cpp -o test_dsp && ./test_dsp` |

真引擎里的冒烟 + 基准：`python packaging/macos/smoke_v5.py tools/mixxx-engine/dist/djstudio-engine`（量出来的数记在 [docs/V5_G0.md](../../docs/V5_G0.md)）。

## 构建

Mac 版由 GitHub 的构建机做（`.github/workflows/mac-app.yml`，连同 DJ Studio.app 一起）。自己编：

```bash
tools/mixxx-engine/build.sh
```

先装好 Mixxx 的构建环境（Linux：Mixxx 源码里的 `tools/debian_buildenv.sh setup`；macOS：`tools/macos_buildenv.sh setup`），
macOS 上加 `CMAKE_ARGS_EXTRA="-DCOREAUDIO=ON -DQT6=ON -DVCPKG_TARGET_TRIPLET=…"`。输出在 `tools/mixxx-engine/dist/`，
DJ Studio 会自动找到它（或设 `DJSTUDIO_ENGINE_BIN`）。测试：`DJSTUDIO_ENGINE_BIN=… pytest tests/test_mixxx_engine.py tests/test_v5_engine.py`。
