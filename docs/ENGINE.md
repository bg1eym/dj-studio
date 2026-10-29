# 引擎协议（Python ⇄ DJStudio.js）

改这里的任何东西都要同时改 `djstudio/engine/mapping/DJStudio.js`、`djstudio/engine/protocol.py`、
`sim/fake_mixxx.mjs`（如涉及 Mixxx 行为），并把 `DJStudio.VERSION` 加一（准备时会核对版本）。

## 传输

- 消息 = JSON 对象（ASCII，非 ASCII 转义成 `\uXXXX`），切成 ≤200 字节的分片：`[seq, part, nparts, …payload]`，全部 0..127。
- SysEx：`F0 7D 44 53 <分片> F7`（Python→引擎），`F0 7D 44 45 <分片> F7`（引擎→Python）。`7D` = 非商业厂商号。
- 备用 CC 流：`CC2 0` 开始、`CC1 <byte>` × N、`CC3 0` 结束；Python→引擎 ch16（`BF`），引擎→Python ch15（`BE`）。
  引擎用最后一次收到命令的传输方式回话。
- 方向用帧头 / 通道区分：IAC 总线会把消息回送给所有监听者，包括发送者自己。
- rtmidi 默认丢弃 SysEx，Python 侧必须 `ignore_types(sysex=False)`。

## Python → 引擎

每条命令可带 `"c": <id>`，引擎回 `{"t":"ack","c":id,"ok":bool,"err"?:…}`。

| t | 字段 | 作用 |
|---|---|---|
| `hello` / `ping` | | 回 ack 带 `v`（版本）、`mode`（传输） |
| `program` | 见下 | 武装一段过渡程序；正在运行另一段 → `busy`；时间线已过 0 点 >1 拍 → `late` |
| `abort` | | 中止当前程序（不动控件） |
| `load` | `deck` | AutoDJ 脉冲装载队首到该 deck（静音）；结果用事件 `loaded` / `load_failed` 报告 |
| `cue` | `deck`, `ms` | 定位（曲内毫秒） |
| `play` | `deck`, `on` | 播放 / 停止 |
| `bpm` | `deck`, `v` | 设 BPM（Mixxx `bpm` 控件） |
| `vol` | `deck`, `v` | 通道推子 |
| `neutral` | `deck` | EQ 归 1、滤波归中、关效果路由、关 loop |
| `eject` | `deck` | 弹出（在播时拒绝） |
| `set` / `get` | `g`,`k`,`v` / `keys:[[g,k],…]` | 调试与探测用 |

### program

```jsonc
{
  "t": "program", "id": "tr3", "idx": 2,
  "a": {"deck": 1, "start": 640, "grid": [t0_ms, bpm], "dur": 360000},   // start = A 网格拍号：时间线 0
  "b": {"deck": 2, "in": 4, "grid": [t0_ms, bpm], "dur": 400000},        // in = B 起播时的 B 网格拍号；终曲程序 b = null
  "play": 0,          // B 在时间线第几拍起播
  "len": 192,         // 时间线长度（拍）
  "playout": false,   // true：时间线到头后还要等 A 自己停
  "lanes": {"A.vol": [[拍, 原生值], …], …},   // 同一拍两个点 = 阶跃
  "bpm": [[拍, bpm], …],
  "events": [[拍, "A.stop"], …]
}
```

时间线拍位：A 在播时 = A 网格拍号 − `a.start`；A 停了以后 = B 网格拍号 − `b.in` + `play`。
原生值：音量 0..1；EQ 增益 0..4（1 = 0 dB）；滤波 `super1` 0..1（0.5 中性）；echo / reverb = 效果单元 1 / 2 的 mix。

## 引擎 → Python

| t | 内容 |
|---|---|
| `st`（每 100ms） | `d: [[loaded, playing, pos_ms, dur_ms, bpm, file_bpm, vol, load_seq], …×2]`，`m: [[hi, mid, lo, filter, echo, reverb, vu], …×2]`（0.4.0 起：从 Mixxx 读回的旋钮原生值，网页混音台用；echo / reverb = 效果单元对该通道开着时的 mix，否则 0；vu = `[ChannelN],vu_meter`，推子前），`x`（crossfader），`mv`（`[Main],vu_meter`），`pg: {id, st, tl, b}`，`ld`（装载中的 deck），`ad`（AutoDJ），`n`，`up` |
| `hello` | `v`，`boot: 1`（映射刚加载 = 状态已丢失） |
| `ev` | `started` / `b_play`（`over` 拍）/ `fired`（事件）/ `phase_fix` / `bar_slip` / `done` / `loaded`（`deck, dur, fbpm`）/ `load_failed` |
| `err` | `m` |

程序状态 `pg.st`：`armed` → `running` → `done`（或 `aborted`）。
