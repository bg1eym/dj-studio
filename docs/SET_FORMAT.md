# set 文件格式 djset/2

一套 set = 一个 JSON 文件，放在 `~/DJStudio/sets/<id>.json`（网页 Set 页的「JSON」可以直接粘贴）。
**全部以小节为单位、人读单位**；编译器负责换算成拍和 Mixxx 原生值。
**值怎么定（EQ / Filter / 效果器 / 推子的换算表，Prime 4+ 的 hot cue）见播放规范 [SET_SPEC.md](SET_SPEC.md)**；这里只讲文件语法。

## 小节号

- 小节号从 1 开始，和网页波形上标的数字一致（第 1 小节 = 曲中最早的小节线；网格来自 Mixxx，小节线以曲库页「小节线已确认」为准）。
- 可以写小数：`64.5` = 第 64 小节的后半。
- 写编排方案时，**以网页波形上的小节号为准**。方案里若用的是别的编号（例如从第一个 kick 算起），转换时先对齐。

## 结构

```jsonc
{
  "schema": "djset/2",
  "spec": "2",                          // 按播放规范 SET_SPEC.md 第几版生成；手写的可以空着
  "id": "rw2023",                       // 文件名，字母数字 - _
  "name": "CDW @ Rock Werchter 2023 复刻",
  "created": "2026-10-28",
  "source": "CDW 技法实战 set 打碟谱 v1",  // 编排方案出处
  "note": "",
  "keylock": false,                     // 变速保持音高；默认关（Mixxx 时间拉伸会让变速的 deck 晚 20–30ms，叠 kick 有双击感）
  "tracks": [
    {
      "id": "3f2a9c0d1e4b",             // 曲库 id（网页曲库 / API 里看）；或用 "file": "子文件夹/文件名.mp3"
      "title": "Track One",             // 只是给人看
      "start": 1,                       // 仅开场曲：从第几小节起播
      "play_bpm": null,                 // 这首独奏时的主控 BPM；null = 原生 BPM
      "downbeat": null,                 // 一般不用：临时覆盖小节相位（0..3，相对 Mixxx 网格）
      "cues": {"drop": 65, "break": 129}, // 命名小节标记（谱上的段落），小节对齐时拿来和分析切出的段落核对
      "plan_bar_offset": 0,             // 谱上第 b 小节 = 本应用第 b + 这个数 小节
      "main_cue": 1,                    // Prime 4+ 的主 Cue（CUE 键），规范里 = IN
      "hot_cues": [                     // Prime 4+ 的 hot cue：打击垫 1–8，没有固定用途，按 set 放的规则见 SET_SPEC.md 第 7 节
        {"pad": 1, "name": "IN", "bar": 1, "color": "#22D45A", "note": "开场：从第 1 小节起播", "auto": true},
        {"pad": 8, "name": "VOCAL", "bar": 41, "color": "#FF5FA2", "note": "人声进来"}   // auto 不写 = 手写的，重新填时不动
      ],
      "loops": [],                      // Prime 4+ 的保存 loop：{"pad", "name", "bar", "beats", "note", "auto"}
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
      "marks": [],                      // 谱上的「动作」：[[时间线小节, "文字"], …]，写进那一刻的 hot cue 注释
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
| `vol` | **推子位置** 0..1（和 Mixxx 推子同一条曲线：0.5 = -12.4 dB，0.7 = -7.0 dB，1 = 0 dB，全表见 SET_SPEC.md；编译时换成 Mixxx 的 volume 值） | 1（B 在起播前默认 0） |
| `lo` / `mid` / `hi` | dB，-60..+12；≤ -40 视为 kill | 0 |
| `filter` | -1..1，负 = LPF，正 = HPF | 0 |
| `echo` / `reverb` | 湿度 0..1（效果单元 1 / 2） | 0 |

点：`[小节, 值]` 线性插值（EQ 按 dB 线性，推子按位置线性）；`[小节, 值, "step"]` 在该小节跳变。
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

- B 在 `play_at` 以半推子进（`b_vol_half` 0.5，推子位置），`b_full_after` 小节后用 4 小节推满（默认 min(32, 交接段 2/3)）。
- 低频不杀：在交接段 `lo_from`..`lo_to`（默认 25%..75%）里，B 低频从 `lo_floor_db`（-15）升到 0，A 低频同步降到 -15。
- A 高频从 B 进场起每小节 `a_hi_slope_db`（-0.19 dB）；A 中频在低频换完后降到 `a_mid_end_db`（-6）。
- A 结束 `a_end`：`fade`（最后 `a_fade_bars`=4 小节淡出 + reverb 尾巴 `a_reverb_tail`=0.4）/ `stop` / `playout`（A 自然播完，`bars` 不写时自动算到曲末）。

### instant 瞬时切换

drop 对 drop：时间线 0..`pre_bars`（4）A 做扫频（`sweep`: hpf/lpf/none，`sweep_amount` 0.6），最后一小节加 echo（`echo_out` 0.5），
切点前留 `vacuum_beats`（1 拍）静音"真空"，B 在 `play_at`（= pre_bars）满音量从 `b_in`（一般写 B 的 drop 小节）起播，A 停。

### custom 自定义

编排方案里逐小节写明旋钮位时用。必须写 `bars` 和 `play_at`；曲线全部自己给（没写 `B.vol` 时 B 在起播时直接满）。
`a_end`：`stop`（默认：没写 `A.stop` 时在时间线结尾停 A）/ `playout`（A 自己播完，时间线到头后等 A 停下）。
过渡上可以写 `label`（显示用的手法名，例如打碟谱生成的 custom 过渡写「长交接」「瞬时切换」）。

## 从打碟谱自动生成

网页首页放进打碟谱——按《打碟谱规范》（[SCORE_SPEC.md](SCORE_SPEC.md)）写的 Markdown（`djstudio/plan/mdplan.py`），
或《打碟谱》PDF（ReportLab 生成、有文字层的那种模板，见 `djstudio/plan/cdw.py`）——分析时自动转成 set
（`djstudio/plan/convert.py`，全是固定规则；两种谱读出来是同一套结构）：

- **曲目**：按谱上的文件名 / 曲名 + 文件名前的编号匹配曲库，再用「小节数 × 原 BPM」核对时长；`play_bpm` = 谱上的「播放」。
- **小节号**：谱上第 1 小节 = Engine DJ 清单里「第 1 小节第 1 拍」的秒数，吸附到这首歌的网格拍上。小节相位和曲库里自动估的不同时，
  按谱写回曲库（确认过的小节线）；本应用的第 1 小节仍是曲中最早的小节线，所以「谱上第 b 小节 = 本应用第 b + k 小节」，k 写在 note 里。
  谱上的 breakdown / drop 小节与本应用切出的段落交叉核对（RW2023：40 多处里绝大多数完全一致）。
- **每段过渡** = `custom`：时间线从表格第一行（B 还没进那一行）开始，`play_at` = B 进场的那一小节（PDF 模板里 B 总是从第 1 小节进；
  Markdown 谱可以写「B 的第 33 小节落在 A 的第 97 小节」），`a_end: playout`（出曲自己播完；Markdown 谱最后一行写「停」= `a_end: stop`，A 在那一小节第 1 拍停）。
- **旋钮**（谱上是 Prime 4+ 的钟点位）：

  | 谱 | 本应用 |
  |---|---|
  | EQ 12 点 / 11 点半 / 11 点 / 10 点半 / 10 点 / 9 点 / 8 点 / 7 点（切） | 0 / -0.7 / -2.0 / -3.8 / -6.0 / -11.3 / -17.7 / -25 dB（12 点以左 -25·(钟头/5)^1.56；5 点 = +6 dB） |
  | Filter 关 / 左 N 点 / 右 N 点 | 0 / 负 / 正，离 12 点几个钟头 ÷ 5（左 9 点 = -0.6 =「切一半」） |
  | 推子 0–10 | 推子位置 0..1 |
  | 特效「Main FX Reverb，第 a–b 小节，Wet/Dry 1/3」 | 该 deck 的 `reverb` 从 a 起 1/3，b 结束时关（A 的效果开到 A 结束 / 停那一小节就不关，留尾巴） |

  两行之间直线过渡；相邻两行只差 1 小节而变化 ≥ 2 个钟头（推子 ≥ 3 格）= 在后一行那一拍跳变（谱里的「一拍回中」「同一拍拧到底」）。
- **主速**：页眉「主速 X」；「B 第 m–n 小节把主速 X → Y」换算到时间线，时间线延长到换完。
- **Key Lock**：谱上要求开的曲子先关掉并提示（Mixxx 的时间拉伸有 17–29ms 延迟，叠放时会出双击）。
- 钟点位 → dB 的曲线是按谱里的话（「B LOW 给到一半」写 10 点）定的假设，真机要核对，见 DEVICE_TEST.md 的假设表。

## 校验

网页上实时校验，或 `djstudio validate <id>`。常见错误：

- 曲目不在曲库 / 不在 Mixxx 库 / Mixxx 没分析；
- 下一段过渡开始时上一段还没结束；
- 过渡用到 A 或 B 曲末之后的小节；
- 曲线点超出时间线、没按小节排序、值超范围；
- 主控 BPM 偏离原生太多。
