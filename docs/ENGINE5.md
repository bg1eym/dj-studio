# v5 调度器（四 Deck）：结构和协议

> 代码：`tools/mixxx-engine/djs5/`（纯 C++17 头文件，声音引擎和独立模拟器 `sim5` 共用同一份）、`djstudio/v5/`（Python）。
> v1–v4 的谱不走这里：它们还是 DJStudio.js + AutoDJ 那条路（[ENGINE.md](ENGINE.md)），一行没动。

## 1 为什么另写一个调度器

v4 的执行在控制器脚本里：20 ms 一次 tick，靠 Mixxx 的 sync 对拍、AutoDJ 装下一首，只有两台 Deck。四台 Deck 同一拍起播、
loop 转 100 圈不偏、效果各有各的实例，这些在 tick 里做不到「同一个音频帧」。所以 v5 把调度放进引擎，跟着音频时钟走：

```
谱（.md）──解析──▶ 项目 djset/3 ──编译──▶ 执行计划 djplan/1 ──┐
                                   └────▶ 图谱数据（网页 / 交接说明）   │  准备阶段一次交给引擎
                                                                      ▼
      Python（Runner5）：只做慢的事——回执、日志、停 / 暂停 / 跳转 / 手动接管     引擎里的 Engine5（每个音频块）
                                                                      ├ 控制步：装载、定位、速率、loop、EQ / Filter（块边界）
                                                                      └ 出声：门、推子、处理组、发送效果、采样、主输出（落在采样上）
```

浏览器和 Python 都不参与逐拍的调度：它们卡住，声音照常。

## 2 时钟

- **全场拍**从处理过的音频帧数算（不是墙钟）。速度图 `tempo` 是 BPM 对全场拍的分段直线，秒数按积分算（`core.hpp TempoMap`，Python 里同一套公式）。
- 每台 Deck 的速率 = 全场 BPM ÷ 素材 BPM。每个块核对一次「这一拍原曲该读到哪」和 Deck 实际的位置：偏差用速率微调（最多 ±0.4%）拉回；
  超过 25 ms 直接重新定位，并发一条 `<片段>.resync` 的回执。
- **起 / 停落在采样上**：Deck 提前 4 个块起播（预卷），主输出前面一道门在起播那一帧已经全开、在停止那一帧已经关完（门的沿 1 ms / 3 ms）。
  Deck 自己的控件（loop 开关、跳段、EQ、Filter）落在块边界；loop 的回绕由 Mixxx 在采样上做。
- 跳段的目标提前告诉读盘线程（预读），跳的那一块不会出现一块静音。
- 不出声（没接声卡、倍速：测试、录音）时引擎按自己的时钟出块，落后了会连着补：连着的块之间让读盘线程 150 µs。不让的话它会跑到读盘前面（Mixxx 只往前预读两块），某台 Deck 掉一块（[MIXXX_NOTES.md](MIXXX_NOTES.md)）。接着声卡时一次回调一块，不涉及。

## 3 混音链路

```
Deck（Mixxx：解码 → 变速 → 增益 → EQ → Filter，推子前）
  → 门（片段的起 / 停）
  → [插入处理组] → 推子 ──┬─▶ 主混音
                          └─▶ 发送：Delay / Echo / Reverb（各自的实例，返回只有效果声）─▶ 主混音
  组插入：成员各自过完推子后相加 → 处理组 → 主混音（成员不再各自直通；它们的发送照旧）
  采样 ─▶ 主混音
主混音 → [总输出处理组] → 主输出的门（开演、暂停、停止的淡入淡出）→ 声卡 / 录音
```

**沿的规矩**：所有的门、交叉淡化都在音乐上的那个点**之前走完**，不是从那个点才开始——片段的起 / 停、发送的开 / 关、Trans 的开和关、Roll 每一遍的起点、Release 收干声和放回来。正好落在点上的鼓头要么完整地在，要么完全没有，不会被削掉一半。

处理组（`bus.hpp`）里的件按固定顺序走：分频段的 Trans / Roll → 整路的 Trans → Roll → Filter → Flanger → Phaser → Spiral → Echo → Noise → Release。

**分频和延迟**：三段分频（250 Hz / 2.5 kHz）是线性相位 FIR，三段加起来 = 输入延迟 264 帧（44.1 kHz，6.0 ms），残差 −160 dBFS 以下。
计划里只要有处理组用到分频，这一级所有的路（不管在不在组里）都延迟同样的 264 帧——没分频的走一条等长的延迟线；
所以一条路在块边界上从「直通」换到「进组」，换的那一刻两边是同一个信号。片段 / 组一级、总输出一级各算各的（都用到 = 528 帧）。
没有分频的计划没有这个延迟，输出和只有四 Deck 基础时逐位相同。`v5.plan` 的回答里 `latency` = 这份计划的输出比 Deck 晚多少帧。

## 4 执行计划 `djplan/1`

编译器（`djstudio/v5/compile.py`、`livefx.py`）出的 JSON，位置都是全场拍（1.1 = 0），旋钮是 Mixxx 的原生值：

| 字段 | 内容 |
|---|---|
| `exec` | 执行编号 = 计划 + 编译器版本 + 能力签名的哈希。回执、状态都带着它；换了计划留下的旧消息按它丢掉 |
| `tempo` | `[[拍, BPM], …]`，同一拍两个点 = 一下换速 |
| `clips[]` | `id, asset, deck, gen`（装载代数）`, load, start, stop, bpm, keylock, segs[], init{}, curves{}` |
| `clips[].segs[]` | 这个片段读原曲的分段：`g`（全场拍）`, src`（原曲秒）`, loop`（圈长，0 = 直放）`, lsrc`（圈头）`, id`（回执编号）。直放 / loop / 出圈 / 跳段都是一段 |
| `fx[]` | 发送效果：`id, kind (delay / echo / reverb), clip, slot`（用第几个实例）`, start, end, tail, p{}, send[], wet[]` |
| `buses[]` | 处理组：`id, route (insert / group / master), clips[], units[]` |
| `buses[].units[]` | `id, kind, start, end, tail, bands[3], p{}, c{}`（随拍变的参数）`, ev[]`（Roll 改长度）`, back, until`（Release） |
| `samples[]`、`hits[]` | 采样（引擎采样率的 float32 立体声裸数据的路径、帧数）和触发（`slot, beat, gain`；`stop` = 到点全部收掉） |

引擎收到计划会自己再查一遍（Deck 数、效果种类、实例数、同一台 Deck 上片段的先后…），不认识的直接拒绝，不会略过照放。

## 5 行协议

声音引擎用 `DJSTUDIO_V5=1` 起成调度器模式（不装 DJStudio.js，不用 AutoDJ）。标准输入一行一条命令，标准输出一行一条消息。

**命令**：`#` + JSON，带 `_id`，回一条带同样 `_id` 的 JSON。

| 命令 | 作用 |
|---|---|
| `v5.caps` | 能力表（见下） |
| `v5.plan {plan}` | 交计划（只在空闲时）→ `{exec, latency}` |
| `v5.start {beat, prerun}` | 从全场第 beat 拍开始；`prerun` < `beat` 时先从 prerun 静音预跑到 beat，把效果的历史补出来 |
| `v5.pause` / `v5.resume` | 全场暂停：Deck 和效果原地冻住，短淡出；继续时保持原来的全场拍 |
| `v5.stop {fade_ms}` | 淡出停止 |
| `v5.takeover {deck, param, value}` | 手动接管一个旋钮（原生值）：只有它不再跟谱 |
| `v5.return {deck, param, when: beat / bar / now, ramp}` | 回谱：下一拍 / 下一小节起，用 ramp 拍走到那时的谱值 |
| `v5.state` | 现在的状态（同下面的 `st5`） |
| `library`、`audio`、`record`、`quit` | 曲库行、声音输出、录音、退出（和 v4 的引擎一样） |

**消息**：`@` + JSON。

| `t` | 内容 |
|---|---|
| `rc` 回执 | 一个动作执行了：`id`（`C1.load` / `C1.start` / `C1.stop` / loop 和跳段的编号 / 效果的编号 / `C1.resync`）、`st`（`ok` / `late` / `skipped` / `failed`）、`beat`（计划的拍）、`at`（实际的拍）、`frame`、`msg` |
| `ev5` 事件 | `preparing` / `prerun` / `running` / `paused` / `resumed` / `stopped` / `done`，带 `frame`、`beat` |
| `st5` 状态 | 约 50 ms 一条，见下 |

界面上「做完了没有」只认回执和状态，不看浏览器自己的计时。

**状态 `st5`**：`exec, seq, state, frame, beat, bpm, late, latency, misses, decks[], fx[], master{}`。

- `misses`（只有声音引擎报）：这次开始以来读盘没跟上的次数——每一次是某台 Deck 一小段静音（一块约 12 ms）。正常是 0；Runner5 看到它涨会写进日志，演出页也显示。
  数到的是下限：一台 Deck 每次定位后的头一回 Mixxx 不报（[MIXXX_NOTES.md](MIXXX_NOTES.md)）。`v5.perf` 的 `cache_misses` 是同一个数，按「上次问到现在」算

- `decks[]`：`deck_id, clip_id, next_clip, load_generation, state (idle / loading / ready / playing / looping / stopped / error), source_sec, global_beat, phase_ms, seg, loop_lap, controls{lo, mid, hi, filter, vol}, manual[], gate, level, out, audible`
- `fx[]`：在响的发送效果 `{id, kind, clip, slot, send, wet, tail, level}` 和处理组里的件 `{id, kind, bus, route, on, tail, muted}`
- `master{}`：`level, peak`（dBFS）`, over`（过载的采样数）`, over_now`

**能力表**（`v5.caps`，Python 里的对照表在 `djstudio/v5/caps.py`，测试比对两边）：最多几台 Deck、每种效果的算法版本、同时几个发送实例、
有哪些路由、几个处理组、分频点和延迟、采样位和同时发声数、采样率和块大小、起停 / 控件各落在哪、定位时最多往回预跑多少拍。
编译结果绑定能力签名：换了引擎，Runner5 会拒绝旧的编译结果，要重新分析。

## 6 从中间开始、暂停、出错

- **从中间开始**：所有状态清零，从 `prerun` 起静音、尽快地把 Deck 和效果跑到 `beat`，再放出声音。预跑的起点由编译结果算（`Compiled5.prerun_from`）：
  发送效果的尾音往回算到 −80 dB；Roll 从抓取点之前；Release 从它开始之前；最多 256 拍。抓取型的效果离得比这还远 → 不让从这里开始，
  告诉用户从哪之前开始（`restore_problem`），不拿静音冒充恢复好了。
- **装载晚了**：片段到点还没装好 → 装好的那一刻按当时的全场拍接上（`late` 回执，带晚了多少）；别的 Deck 不等它。
- **装载失败**：这个片段的动作标 `failed`，别的 Deck 照常放，不临时编一个循环顶上。
- **引擎进程没了**：Runner5 停在那里报错，不自动重启重放。界面重连只是重新读状态，不会让引擎重做任何动作。

## 7 模拟器 `sim5`

`sim5.cpp` = 同一个 `Engine5` + 假 Deck（每拍一个脉冲；测试用的白噪声 / 扫频），没有声卡，可以倍速。Python 第一次用时现编一个
（按源码的哈希缓存，`djstudio/v5/link.py`）。它只在没有声音引擎时用（开发、测试、`djstudio demo5`）；模拟器是按我们对 Mixxx 的理解写的，
涉及 Mixxx 行为的结论都在真引擎上另测过（[MIXXX_NOTES.md](MIXXX_NOTES.md)、[V5_G0.md](V5_G0.md)）。

多出来的命令：`sim.record {path}`（主输出录成 float32 裸数据）、`sim.set {load_ms | fail | speed | nudge | wave}`（故障注入）、`sim.decks`。

## 8 录成 MP3

`Studio.record` 对 v5 的项目（`Service5.record_once`）：调度器模式的声音引擎，不出声（`DJSTUDIO_AUDIO=0`），`DJSTUDIO_SPEED` 是倍速的上限。

- 顺序：`record {path: ….mp3, trim: true}` → `v5.perf`（把读盘的计数清零）→ `v5.plan` → `v5.start {beat: 0}` → 等 `done` → `record_stop` → `v5.perf`。
  和演出一样由 Runner5 交计划、看回执；录的是主输出那一块（`outBuffer()`，只在不出声的那条路上录）。
- `trim`：装载、走带起点前那几块数字静音不录——录音从第 0 拍所在的那一块开始；结尾的静音丢掉，中间的照录。
- 尾音：发送效果的尾音、处理组里 Echo / Spiral 的尾音都算在计划的长度里（`len`），引擎放过 `len`、发送实例都收完才报 `done`（之后 `halt`，没有还在响的东西）。
  所以录到 `done` 就是整场，不另外留时间。
- 放多快都是同一份声音（调度按帧数走）。会让某一遍和原速不一样的只有两件事，都查得到：读盘没跟上（`v5.perf` 的 `cache_misses`；
  `record_stop` 回的那个在调度器模式下是 −1，它只在 v4 的引擎时钟下数）、片段来不及装（回执 `late` / `skipped` / `failed`）。出了就按下一档倍速重录。
- 模拟器 `sim5` 没有 `record`（只有 `sim.record` 录裸数据）：没有声音引擎时不录。
