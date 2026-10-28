# 真 Mixxx 测试台

把 `DJStudio.js` / `DJStudio.midi.xml` 装进 **Mixxx 2.5.6 自己的** MidiController 和脚本引擎，驱动真正的
deck、EngineBuffer、效果链和同步逻辑，并可以把主输出录成 WAV。Python 侧用 `HarnessLink`
（`djstudio/engine/link.py`）驱动，协议与 Node 模拟器相同，所以 conductor 和测试不用改。

它比 Node 模拟器更接近真机（脚本 API、SysEx 路由、控件、相位同步、EQ / 滤波都是 Mixxx 的代码），
但仍不是完整的 Mixxx：没有 PlayerManager、没有真 AutoDJProcessor（`[AutoDJ]` 的装载逻辑是移植的），
也没有 CoreMIDI / IAC。

## 构建（Ubuntu 24.04）

```bash
git clone --depth 1 --branch 2.5.6 https://github.com/mixxxdj/mixxx mixxx-src
```

```bash
cd mixxx-src && tools/debian_buildenv.sh setup && sudo apt-get install -y ninja-build
```

```bash
cp /path/to/dj-studio/tools/mixxx-harness/djstudio_harness_test.cpp mixxx-src/src/test/
```

在 `mixxx-src/CMakeLists.txt` 的测试源文件列表里（`src/test/midicontrollertest.cpp` 那一行后面）加上
`src/test/djstudio_harness_test.cpp`，然后：

```bash
cd mixxx-src && mkdir -p build && cd build && cmake -G Ninja -DCMAKE_BUILD_TYPE=Release -DQML=OFF -DENGINEPRIME=OFF -DKEYFINDER=OFF -DBROADCAST=OFF -DQTKEYCHAIN=OFF -DHID=OFF -DBATTERY=OFF -DBUILD_TESTING=ON .. && ninja mixxx-test
```

## 运行

```python
from djstudio.engine.link import HarnessLink
link = HarnessLink(Path("mixxx-src/build/mixxx-test"),
                   [{"id": "a", "path": "/abs/a.mp3", "bpm": 128.0, "first_beat": 0.12}], speed=2)
```

`speed` 是音频引擎相对墙钟的倍速（脚本定时器按墙钟走，所以不宜超过 4）。
`link.sim({"cmd": "record", "path": "/abs/out.wav"})` 开始录主输出，`record_stop` 结束。
