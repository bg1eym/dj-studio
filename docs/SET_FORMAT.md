# set 文件格式 djset/2

一套 set = 一个 JSON 文件，放在 `~/DJStudio/sets/<id>.json`（网页 Set 页的「JSON」可以直接粘贴）。
**全部以小节为单位、人读单位**；编译器负责换算成拍和 Mixxx 原生值。

## 小节号

- 小节号从 1 开始，和网页波形上标的数字一致（第 1 小节 = 曲中最早的小节线；网格来自 Mixxx，小节线以曲库页「小节线已确认」为准）。
- 可以写小数：`64.5` = 第 64 小节的后半。
- 写编排方案时，**以网页波形上的小节号为准**。方案里若用的是别的编号（例如从第一个 kick 算起），转换时先对齐。

## 结构

```jsonc
{
  "schema": "djset/2",
  "id": "rw2023",                       // 文件名，字母数字 - _
  "name": "CDW @ Rock Werchter 2023 复刻",
  "created": "2026-10-28",
  "source": "CDW 技法实战 set 打碟谱 v1",  // 编排方案出处
  "note": "",
  "tracks": [
    {
      "id": "3f2a9c0d1e4b",             // 曲库 id（网页曲库 / API 里看）；或用 "file": "子文件夹/文件名.mp3"
      "title": "Track One",             // 只是给人看
      "start": 1,                       // 仅开场曲：从第几小节起播
      "play_bpm": null,                 // 这首独奏时的主控 BPM；null = 原生 BPM
      "downbeat": null,                 // 一般不用：临时覆盖小节相位（0..3，相对 Mixxx 网格）
      "cues": {"drop": 65, "break": 129}, // 命名小节标记，仅展示 / 备忘
      "note": ""
    }
  ],
  "transitions": [                      // 第 i 段 = tracks[i] → tracks[i+1]，数量必须是 曲数-1
    {
      "technique": "longhand",          // longhand | instant | custom
      "start": 161,                     // A（出曲）的第几小节 = 这段时间线的第 0 小节
      "b_in": 1,                        // B（进曲）起播时所在的 B 小节
      "play_at": null,                  // B 在时间线第几小节起播；null = 手法默认（longhand 0，instant = pre_bars）
      "bars": 48,                       // 时间线总长；null = 手法默认
      "params": {},                     // 手法参数，见下
      "lanes": {},                      // 自定义自动化曲线（整条覆盖手法里的同名曲线）
      "events": [],                     // 追加事件
      "tempo": null,                    // 主控 BPM 曲线；null = 默认
      "note": "16 小节 groove 叠加后换 bass"
    }
  ],
  "ending": {"mode": "fade", "bar": 177, "bars": 8}   // 终曲：fade 从第 bar 小节淡出 bars 小节；playout 播到曲末
}
```

## 时间线

每段过渡有自己的时间线，单位小节：

```
A:  …… start ────────────────────────────── start+bars
时间线: 0 ─── play_at ──────────────────────── bars
B:            b_in ─────────────────────────── b_in + (bars - play_at)
```

- A 到 `start` 小节时时间线开始；B 在时间线 `play_at` 处从自己的 `b_in` 小节起播（拍对齐）。
- 过渡结束时 B 所在小节 = `b_in + bars - play_at`；下一段的 `start` 不能早于它（校验会报错）。
- A 必须在 `start + bars` 之前不播完（除非 `a_end: "playout"`）。

## 自动化曲线（lanes）

通道名 = `A.` 或 `B.` + 参数：

| 参数 | 单位 | 中性值 |
|---|---|---|
| `vol` | 推子 0..1 | 1（B 在起播前默认 0） |
| `lo` / `mid` / `hi` | dB，-60..+12；≤ -40 视为 kill | 0 |
| `filter` | -1..1，负 = LPF，正 = HPF | 0 |
| `echo` / `reverb` | 湿度 0..1（效果单元 1 / 2） | 0 |

点：`[小节, 值]` 线性插值（EQ 按 dB 线性）；`[小节, 值, "step"]` 在该小节跳变。
第一个点之前保持第一个点的值。例：B 低频从 kill 在第 16 小节瞬间换满：

```json
"lanes": {"B.lo": [[0, -60], [16, 0, "step"]], "A.lo": [[16, -60, "step"]]}
```

## 事件（events）

`[小节, "动作"]`：`A.stop` / `B.stop`、`A.loop:8`（从当前拍起 8 拍 loop）、`A.loop_off`。

## 主控 BPM（tempo）

`[[小节, bpm], …]`，两个 deck 一起跟随。默认：同速不动；longhand 在交接段前半程从 A 的主控 BPM 渐变到 B 的；
instant 在切点跳变；custom 在整段内渐变。Mixxx 的变速范围是 ±8%（默认），偏离原生 >6% 会提示，>16% 报错。

## 手法与参数

### longhand 长交接

时长默认 48 小节（研究实测 25–57，中位 39）。默认形状：

- B 在 `play_at` 以半音量进（`b_vol_half` 0.7），`b_full_after` 小节后用 4 小节推满（默认 min(32, 交接段 2/3)）。
- 低频不杀：在交接段 `lo_from`..`lo_to`（默认 25%..75%）里，B 低频从 `lo_floor_db`（-15）升到 0，A 低频同步降到 -15。
- A 高频从 B 进场起每小节 `a_hi_slope_db`（-0.19 dB）；A 中频在低频换完后降到 `a_mid_end_db`（-6）。
- A 结束 `a_end`：`fade`（最后 `a_fade_bars`=4 小节淡出 + reverb 尾巴 `a_reverb_tail`=0.4）/ `stop` / `playout`（A 自然播完，`bars` 不写时自动算到曲末）。

### instant 瞬时切换

drop 对 drop：时间线 0..`pre_bars`（4）A 做扫频（`sweep`: hpf/lpf/none，`sweep_amount` 0.6），最后一小节加 echo（`echo_out` 0.5），
切点前留 `vacuum_beats`（1 拍）静音"真空"，B 在 `play_at`（= pre_bars）满音量从 `b_in`（一般写 B 的 drop 小节）起播，A 停。

### custom 自定义

编排方案里逐小节写明旋钮位时用。必须写 `bars` 和 `play_at`；曲线全部自己给（没写 `B.vol` 时 B 在起播时直接满）；没写 `A.stop` 时在结尾停 A。

## 校验

网页上实时校验，或 `djstudio validate <id>`。常见错误：

- 曲目不在曲库 / 不在 Mixxx 库 / Mixxx 没分析；
- 下一段过渡开始时上一段还没结束；
- 过渡用到 A 或 B 曲末之后的小节；
- 曲线点超出时间线、没按小节排序、值超范围；
- 主控 BPM 偏离原生太多。
