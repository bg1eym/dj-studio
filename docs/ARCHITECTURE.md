# 架构

```
浏览器（原生 JS，无构建）
  曲库/波形 · Set 编辑与校验 · 演出监控 · 体检
        ⇅ HTTP JSON + SSE（实时状态 ~5Hz、日志、任务进度）
FastAPI（djstudio/web/app.py，只绑 127.0.0.1）
        │
Studio 服务层（djstudio/studio.py）—— 网页和命令行唯一入口
  ├─ library/   导入（复制到托管曲库、按内容去重）、ffmpeg 解码、确定性分析、studio.sqlite
  ├─ setfile + techniques + compiler   djset/2（小节、人读单位）→ 引擎程序（拍、Mixxx 原生值）+ 校验
  ├─ mixxx/     mixxxdb 读写（网格解码、AutoDJ 队列）、进程控制、mixxx.cfg
  ├─ engine/    protocol（分帧）· link（IAC / 模拟器）· client（状态、确认、失联）· conductor（逐段指挥）
  └─ probe      真机逐条验证 Mixxx 行为
        ⇅ IAC 虚拟 MIDI 总线：SysEx 分帧 JSON（备用：CC 流）
Mixxx + DJStudio.js（控制器映射）
  20ms 时间线 · 自动化曲线 · B 起播 + 相位校正 · AutoDJ 装载器 · 100ms 状态回传
```

## 分工：快的在引擎，慢的在 Python

| 层 | 做什么 | 不做什么 |
|---|---|---|
| `DJStudio.js`（Mixxx 内） | 按「过渡程序」执行：每 20ms 根据 A 的播放位置算时间线拍位，插值所有曲线并写控件；到点起播 B（补偿 tick 延迟）、定期相位校正；执行事件（停 A、loop）；AutoDJ 脉冲装载；回传状态 | 不做任何决策，不知道整套 set |
| conductor（Python） | 装载下一首并用时长 + 网格 BPM 核对身份；提前下发下一段程序；监控进度；失联 / 停滞 / 超时处理；淡出停止 | 不做实时控制 |

过渡程序在 B 装载完成后就下发（通常提前几分钟），**即使 Python 卡住，这一段也会按谱面执行完**。

## 关键设计决定

- **小节号**：网格来自 Mixxx（sync / quantize 都基于它）。第 1 小节 = 曲中最早的小节起点（网格向前延伸），
  所以编号只取决于 BPM 和小节相位，与 Mixxx 把「第 0 拍」放在哪里无关。小节相位存成「第 1 小节起点的秒数」（`bar1`），换网格后自动重算。
- **SysEx 分帧 JSON**：一条消息 ≤200 字节一片；方向用帧头区分（`44 53` Python→引擎，`44 45` 引擎→Python），
  因为 IAC 总线会把消息回送给所有监听者（包括发送者自己）。状态位置精度到毫秒（旧系统 7-bit 约 2.5 秒）。
- **AutoDJ 只当装载器**：引擎先清空目标 deck、静音，再开 AutoDJ，目标 deck 一装上立即关 AutoDJ、停播、推子归中。
  队列在「准备」时写进 mixxxdb（Mixxx 必须已退出），启动后再核对。
- **身份核对**：时长 ±1s + 网格 BPM ±0.1。对不上就停，宁停不错放。
- **失联**：状态帧 >1.5s 没来 = 失联 → 告警、停发命令；恢复后继续；超过 60s 放弃。Mixxx 重载映射（收到 boot hello）= 状态丢失，停止。
- **所有等待都有上限**；时间线 8s 不动 = 停滞，报错。
- **来不及（late）**：程序到达时时间线已过 0 点 >1 拍，引擎拒绝；conductor 改发应急切换（下一个小节线，1 小节 HPF + 硬切）。
- **mixxxdb 只在 Mixxx 退出时写**，写前自动备份到 `~/DJStudio/backups/`（保留 10 份）。

## 从旧系统继承的坑（仍然适用）

- `pgrep -x mixxx` 在用户的 Mac 上匹配不到 → 三种方式判断进程。
- 有 deck 在播时 `osascript quit` 会被确认框挂住 → 退出前确认 deck 都停了。
- 替换 `controllers/DJStudio.js` 会热重载映射并清空引擎状态 → 只在「准备」（Mixxx 退出时）部署。
- iCloud「仅云端」文件会让读取阻塞 → 代码和托管曲库都放在 iCloud 之外；导入时会标出云端文件。
- Mixxx 网格可能误判（2/3、1.5 倍），或整条放在反拍上（真曲实测 5 首里 3 首）→ 分析时和自估网格比对，曲库页标「网格存疑」，set 校验报错；「校正网格」把自估网格写进 mixxxdb 并 `bpm_lock`。自估网格的线放在 kick 起音点上（`analysis.kick_phase`）。
- **不要用 kick 相似度自动决定接法**（研究里 25 次实测相关性只有 +0.19）。

## 测试

- `tests/test_compiler.py`：set 格式、手法展开、单位换算、时序校验。
- `tests/test_sim_e2e.py`：真正的 `DJStudio.js` 在 Node 假 Mixxx 里（`sim/fake_mixxx.mjs`，虚拟时钟 40 倍速），
  conductor 指挥整套：拍位对齐 < 0.05 拍、BPM 渐变、失联检测、装错曲检测、淡出停止。
- `tests/test_studio.py`：导入合成音频 → 分析（BPM / 首拍 / 小节相位 / 段落）→ 假 mixxxdb → 准备 → 播完。
- `tests/test_web.py`：API 冒烟、路径限制。

- `tests/test_mixxx_harness.py`：同样的 conductor 驱动**真 Mixxx 2.5.6 的代码**（`tools/mixxx-harness`，编进 Mixxx 自己的测试程序：
  真 MidiController、脚本引擎、deck、EQ / 滤波、同步），并录下主输出，在**音频**上检查过渡中两个 kick 没有双击。需要先构建 Mixxx，否则跳过。

Node 模拟器按 Mixxx 源码的行为写成（docs/MIXXX_NOTES.md）；真引擎测试台跑的是 Mixxx 本身的代码，但没有 CoreMIDI / IAC、
PlayerManager 和真 AutoDJProcessor。两者都**不能替代 Mac 上的真机**，真机要验证的见 [DEVICE_TEST.md](DEVICE_TEST.md)。
