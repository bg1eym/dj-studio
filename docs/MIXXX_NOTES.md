# Mixxx 行为核对记录（按 2.5.6 源码）

引擎脚本、数据库读写、AutoDJ 用法都依赖 Mixxx 的具体行为。这里逐条记录**从 Mixxx 2.5.6 源码核对**的结论
（tag `2.5.6`，commit `3ebac44`），附文件和行号。改相关代码前先看这里；Mixxx 升级后要重新核对。

标记：✅ 原来的理解正确　❌ 原来理解错了，已修正　⚠️ 需要注意的行为

## 控制器脚本与 MIDI

| | 结论 | 源码 |
|---|---|---|
| ❌ | **SysEx 不会自动交给脚本**。`MidiController::receive(QByteArray)` 按 (status=0xF0, control=0xFF) 查 XML 输入映射，只有存在带 `<script-binding/>` 的 0xF0 映射时才调用 `handleIncomingData`。原来的 XML 没有这条，Python 发的命令会被静默丢弃（引擎状态照样能回传，看起来"在线"）。已在 `DJStudio.midi.xml` 加上。 | `src/controllers/midi/midicontroller.cpp:577-616`；`midimessage.cpp:17`（非两字节消息的 control 记为 0xFF） |
| ✅ | `incomingData(data, length)` 收到的是 `Uint8Array`，包含首尾的 F0 / F7；同一映射里所有前缀的 `incomingData` 都会被调用。 | `controllerscriptenginelegacy.cpp:126-165, 199-215` |
| ✅ | PortMidi 收 SysEx 的缓冲是 1024 字节（我们的分片 ≤ 208 字节）。 | `portmidicontroller.h:33-36`，`portmidicontroller.cpp:118-190` |
| ✅ | `midi.sendSysexMsg(list, length)`：length 被忽略，数据必须以 F7 结尾。 | `midicontroller.h:69`，`portmidicontroller.cpp:225` |
| ✅ | `engine.beginTimer(ms, function, oneShot)`；传字符串会报错；最短 20ms。 | `controllerscriptinterfacelegacy.cpp:483-511` |
| ✅ | `engine.makeConnection(group, key, cb)`，回调参数 `(value, group, key)`。 | `controllerscriptinterfacelegacy.cpp` |
| ⚠️ | 脚本文件被 Mixxx 监视，改动会**热重载**映射（引擎状态清空，重新 `init`）。只在 Mixxx 退出时部署。 | `controllerscriptenginelegacy.cpp:226-235` |
| ✅ | 设备名 = PortMidi 设备名；`mixxx.cfg` 里 `[ControllerPreset] <设备名>` = 映射文件绝对路径，`[Controller] <设备名>` = 1 才启用；设备名里的空格、`/`、`\` 换成 `_`。准备时自动改写（沿用原来 AgentMCP 用的那个设备键）。 | `controllermanager.cpp:47-49, 240-275, 415-440` |
| ✅ | `mixxx.cfg` 格式：`[组]` 行，其后每行 `键 值`；读取时值会 trim。 | `preferences/configobject.cpp:190-230, 260-275` |

## 播放位置与时间精度

| | 结论 | 源码 |
|---|---|---|
| ❌ | **`playposition` 每秒只更新 15 次**（约 67ms 一次）。原来用它每 20ms 算拍位，最多滞后 0.14 拍（128 BPM）。 | `engine/enginebuffer.cpp:53, 1475-1481` |
| ✅ | `beat_distance` 在每个音频 buffer 的 `postProcess` 里更新。现在拍位 = `playposition` 估出的整数拍 + `beat_distance` 小数部分。 | `enginebuffer.cpp:1389-1399`；`controls/bpmcontrol.cpp:1244` |
| ✅ | 写 `playposition` = 按比例 seek。**暂停时 seek 不做相位调整**（`processSeek` 里 `!paused && SEEK_PHASE`），所以 B 能停在 `b_in` 的精确整拍上。 | `enginebuffer.cpp:158-162, 698, 1305-1370` |
| ✅ | **quantize 开着时按 play 会请求相位同步**：引擎把这一 deck 对齐到另一个正在播的 deck（`getBeatMatchPosition`，就近 ±0.5 拍；对方已过拍点 7/8 以上时对齐上一拍）。B 起播因此是采样级对齐，不再由脚本自己 seek 补偿。 | `enginebuffer.cpp:776-791`；`bpmcontrol.cpp:41, 877-1030`；`sync/enginesync.cpp:516-560`（同步目标 = 另一个在播 deck） |
| ✅ | `beatsync_phase` 走同一个相位同步。 | `bpmcontrol.cpp` |
| ✅ | 写 `bpm` → `rate_ratio = bpm / local_bpm`；速率滑块允许越界，不受 rateRange 限制。 | `bpmcontrol.cpp:185-197, 1105-1119`；`ratecontrol.cpp:30-60, 291` |
| ✅ | `duration` 是曲目元数据时长（秒）；`file_bpm` 是网格 BPM；`track_loaded` 只读。 | `mixer/basetrackplayer.cpp:94-97, 466, 582-602`；`enginebuffer.cpp:180, 549, 618` |

## 控件名与取值

| | 结论 | 源码 |
|---|---|---|
| ✅ | EQ：`[EqualizerRack1_[ChannelN]_Effect1],parameter1/2/3` = 低/中/高增益；默认 EQ 是 BiquadFullKillEQ，范围 0..4、中性 1、0 = kill（Bessel / LR8 也一样；只有 3-band biquad 上限 2）。 | `effects/chains/equalizereffectchain.cpp:41-45`；`effectknobparameterslot.h:23`；`backends/builtin/equalizer_util.h:10-30`；`presets/effectchainpresetmanager.cpp:689` |
| ✅ | 滤波：`[QuickEffectRack1_[ChannelN]],super1`，默认快速效果是 Filter，LPF 连左、HPF 连右，0.5 中性。 | `quickeffectchain.cpp:38`；`presets/effectchainpresetmanager.cpp:696`；`builtin/filtereffect.cpp:39-64` |
| ✅ | 效果单元：`[EffectRack1_EffectUnitN]` 的 `mix`（0..1）、`super1`、`enabled`、`group_[ChannelN]_enable`。 | `effects/effectchain.cpp:53-73, 305` |
| ⚠️ | 效果单元是**推子后**（Postfader），EQ 是推子前：A 推子拉到 0 以后 reverb / echo 尾巴仍然能听到。 | `standardeffectchain.cpp:13`；`equalizereffectchain.cpp:14` |
| ✅ | 推子 `[Master],crossfader`（-1..1）；deck 朝向 `[ChannelN],orientation` 0=左 1=中 2=右。 | `engine/enginemixer.cpp:104`；`channels/enginechannel.h:15-19` |
| ❌ | **推子模式必须是「加性」**：加性模式居中时两边增益都是 1，推到一侧时这一侧仍是 1；恒功率模式居中时两边都小于 1。AutoDJ 装载时会把推子甩到在播 deck 那一侧——恒功率下在播的 A 会突然变响。引擎 `init` 里把 `[Mixer Profile],xFaderMode` 设为 0（加性），`shutdown` 恢复。 | `enginexfader.cpp:6, 16-60`；`enginexfader.h:6-7`；`enginemixer.cpp:170-182` |
| ⚠️ | 推钮类控件（ControlPushButton）不忽略重复写入：写两次 1 会触发两次。 | `control/controlpushbutton.cpp:10`；`control/control.cpp:293` |
| ✅ | 电平表：通道 `[ChannelN],vu_meter` 在 pregain 与推子前效果（EQ、QuickEffect 滤波）之后、**推子之前**计算，0..1，每秒更新 30 次；主输出是 **`[Main],vu_meter`**——`[Master]` 下只有旧名 `VuMeter` 的别名，`[Master],vu_meter` 不存在（读出来恒为 0，测试台抓到的）。crossfader 仍是 `[Master],crossfader`（EngineMixer 用 `[Master]` 构造）。 | `engine/channels/enginedeck.cpp:69-81`；`engine/enginevumeter.cpp:10, 20-29`；`engine/enginemixer.cpp:104, 120`；`coreservices.cpp:491-496` |
| ⚠️ | **ReplayGain 默认开启**（`[ReplayGain] ReplayGainEnabled` 缺省为真，分析器也默认开）：EnginePregain 把每首按 ReplayGain 值缩放到参考响度，没有 RG 值时用 DefaultBoost（默认 -6 dB）。现在的 techno 母带峰值都到满幅，测试台里没开 RG，长交接两首满音量叠加时主输出每 30 秒有上千个满幅样本；真机上开着 RG 就有余量。体检会检查这一项。 | `preferences/replaygainsettings.cpp:18, 45-48`；`engine/enginepregain.cpp:66-80` |

## 弹出与装载

| | 结论 | 源码 |
|---|---|---|
| ❌ | **空 deck 上 `eject` 会把上一首被弹出的歌装回来**；500ms 内连按两次会装回倒数第二首；在播时 eject 被忽略。引擎的 `eject` 命令现在只弹已装载的 deck。 | `mixer/basetrackplayer.cpp:373-406`；`basetrackplayer.h:24` |
| ✅ | 2.5.6 没有"启动时恢复上次 deck 里的歌"的功能（旧系统观察到的"会话恢复"不是这个原因）。 | 全局搜索无相关代码 |
| ✅ | 曲目一装进 deck 就排队分析（BPM / 网格 / 调性 / 波形）；分析调度按曲目 ID，分析器自己持有曲目引用，弹出后也会做完；结果在曲目离开缓存时写库。 | `mixer/playermanager.cpp:184-202, 731-745`；`track/globaltrackcache.cpp:274-411` |

## AutoDJ（只当装载器用）

| | 结论 | 源码 |
|---|---|---|
| ✅ | 左 / 右 deck = 第一个朝向为 0 / 2 的 deck；缺一个就拒绝（`ADJ_NOT_TWO_DECKS`）。两边都在播也拒绝；队列空也拒绝。被拒时 `[AutoDJ],enabled` 保持 0（这个控件需要确认，开启请求在主线程异步处理）。引擎装载时开 AutoDJ 后 1.5 秒仍是 0 就报 `autodj_refused`。 | `library/autodj/autodjprocessor.cpp:358-441, 588-590, 1766-1784` |
| ⚠️ | **两边都没在播**：如果左 deck 里的歌正好是队首且位置 < 66%，直接播它；否则把队首装进**左 deck 并起播**，推子甩到 -1，进入 P1LOADED；等左 deck 位置更新后才把它从队列删掉，并把下一首装进右 deck。引擎因此要求冷启动只往 deck1 装。 | `autodjprocessor.cpp:414-429, 538-552, 683-735` |
| ⚠️ | **一边在播**：先把在播 deck 的歌从队首删掉（如果它在队首），再把新的队首装进另一边（**不起播、不删除**），推子甩向在播一侧。 | `autodjprocessor.cpp:409-413, 553-571` |
| ⚠️ | **队首删除是滞后的**：装载时不删，下一次开启 AutoDJ 时如果在播 deck 的歌在队首才删。我们的节奏（下一首总在上一首已在播时装载）正好让队列一致；冷启动的竞态由身份核对兜底。 | `autodjprocessor.cpp:933-972` |
| ✅ | `skip_next`：左 deck 没在播 → 删左 deck 的歌（若在队首）并往左装下一首；否则右 deck 没在播 → 同理往右装。队列空时 AutoDJ 自己关闭并弹出该 deck。「同步到 Mixxx」靠它逐首装载触发分析，以 AutoDJ 自己关闭为结束信号。 | `autodjprocessor.cpp:321-356, 916-931` |
| ⚠️ | AutoDJ 开着时如果在播 deck 越过它算出的淡化点会自己开始混音——我们装完立刻关 AutoDJ，装载发生在曲子前段，碰不到。 | `autodjprocessor.cpp:661-` |
| ✅ | AutoDJ 队列 = `Playlists` 里 `name = 'Auto DJ'`（`hidden = 1`）的表；`PlaylistTracks.position` 从 1 起，`pl_datetime_added = CURRENT_TIMESTAMP`。 | `library/dao/trackschema.h:11`；`autodj/autodjfeature.cpp:34-38`；`dao/playlistdao.cpp:19-22, 440-475` |

## 曲库数据库

| | 结论 | 源码 |
|---|---|---|
| ✅ | 用到的表和列都在 2.5.6 的结构里（schema 版本 39）：`library`（`duration FLOAT`、`samplerate`、`bpm`、`key_id`、`beats`、`beats_version`、`beats_sub_version`、`bpm_lock`、`location`、`mixxx_deleted`）、`track_locations`、`Playlists`、`PlaylistTracks`、`directories`。测试用的假曲库直接由 `res/schema.xml` 生成（`tools/mixxx_schema.py` → `djstudio/mixxx/schema_2_5_6.sql`）。 | `res/schema.xml`；`database/schemamanager.cpp:195-260` |
| ❌ | **网格位置单位是帧**（每声道一个样本），不是立体声样本。原来按 ÷2 解码，所有首拍时间都会减半、小节号全错。 | `track/beats.cpp:286-305, 345-373` |
| ✅ | 网格格式：`BeatGrid-1.0/2.0` 先按 protobuf `BeatGrid` 解析，失败且长 16 字节再按旧结构 `{double bpm; double firstBeat}`；`BeatMap-1.0` 是 `repeated Beat`（不看 enabled）。恒定网格存的是"最后一个标记"的位置，网格向两边延伸。采样率取 `library.samplerate`。 | `beats.cpp:17-20, 257-340`；`proto/beats.proto`；`library/dao/trackdao.cpp:1271-1280` |
| ⚠️ | **按 BPM 标签建的临时网格**：`beats_sub_version` 为空且首拍在 0。装载时 Mixxx 会重新分析，相位不可信——视为"没分析"。 | `analyzer/analyzerbeats.cpp:129-170` |
| ✅ | `key_id` = ChromaticKey 枚举（1–12 大调 C…B，13–24 小调 C…B）。 | `proto/keys.proto:13-39` |
| ✅ | `[Library] RescanOnStartup` 为真时启动扫描曲库目录。 | `library/library_prefs.cpp:16-19`；`coreservices.cpp:612-617` |

## 真引擎实测（测试台，现在是 tools/mixxx-engine 的声音引擎）

这些不是读源码得出的，是把 DJStudio.js 装进 Mixxx 2.5.6 真正的 MidiController / 引擎、录下主输出后测出来的。
（「测试台 analyze」那几条用的是当时编进测试台的 Mixxx 分析器；声音引擎里没有带分析器。）

| | 结论 | 测法 |
|---|---|---|
| ⚠️ | **Linux 上 Mixxx 解码 mp3（libmad）比 ffmpeg 晚约 25ms**（≈ 1105 个采样：编码器延迟 + 解码器延迟，ffmpeg 按 LAME 头去掉了，libmad 这条路没去）。Mac 上 Mixxx 用 CoreAudio 解码，处理方式又可能不同。→ 分析改用声音引擎自己的解码（`--decode`），和播放同一个解码器；分析结果记下解码器，换了就重新分析。 | 同一个 320k mp3 用两边解码，波形互相关：−553 个采样 @22050 |
| ⚠️ | **解码器的帧号不一定从 0 开始**：Mac 的 CoreAudio 解码器从 `leadingFrames`（`AudioConverterPrimeInfo`）开始。播放时 Mixxx 的帧号就是解码器的帧号，起点之前补静音（preroll）。`--decode` 原来从起点读、从 0 写，起点不是 0 时分析（拍线、波形）会比声音早。**实测（CI，macOS 14 Apple 芯片）：LAME mp3、afconvert 的 AAC、wav 起点都是 0**，所以 0.3.0 在这些文件上没有偏差；`--decode` 现在照样在前面补静音（起点 < 0 就丢掉负的那段），解码器版本记为 `mixxx-engine/2`。`--probe` 报 `first_frame`。CI 每次在 Apple 芯片构建机上跑 `packaging/macos/timing_check.py`：`--decode` 和 `--render`（真正的播放路径）逐帧比，差 1 帧（0.02ms）。 | `soundsourcecoreaudio.cpp:169-197`；`cachingreader.cpp:351-380`；`timing_check.py` |
| ✅ | **网页播放头原来比声音慢 50–150ms 且抖动**：网页把状态帧里的位置当作「收到 SSE 的时刻」的位置（SSE 每 100ms 推一次、每 50ms 才看一次），也没算声卡输出延迟。用户在 Mac 上看到「kick 不在拍线上、画面和声音对不上」主要是这个。→ 状态帧带 `at`（收到时刻 + 声卡延迟），新帧一到就推。 | `djstudio/studio.py` `_heard_at`；`web/static/live.js` |
| ✅ | **原速单放时引擎不改动声音**：EQ 居中、没有效果、keylock 关，`--render` 输出和解码结果逐点相同（差 −120 dB），只晚 1 帧（线性变速器插值）。 | `timing_check.py`（Linux：wav / mp3 / aac） |
| ✅ | 整套 set（长交接 + 瞬时切换 + 终曲淡出）在真引擎里按谱面跑完；过渡中两边时间线拍位差 < 0.05 拍。冷启动时 AutoDJ 抢先预载 deck2 的竞态真实出现，身份核对正确处理。 | `tests/test_mixxx_engine.py` |
| ✅ | 映射加载时 Mixxx 总会先加载系统目录里的 `common-controller-scripts.js`（真机上在 Mixxx 的 app 资源里；声音引擎把它放在程序旁边的 `res/controllers`）。 | `legacycontrollermappingfilehandler.cpp:233` |
| ✅ | 用 Mixxx 自己的 `AnalyzerBeats` 分析后序列化出的 blob，我们的解码器读出的 BPM / 首拍与 Mixxx 完全一致（帧单位再次确认）。分析器的 sub_version 形如 `rounding=V4|vamp_plugin_id=qm-tempotracker:0`。 | 测试台 `analyze` 命令 |
| ❌ | **Mixxx 的节拍分析（qm-tempotracker）会把整条网格放到反拍上**。先在合成曲上发现，再用用户的真曲确认：RW2023 #10–#14 五首里，#11 Doppler、#12 Kali、#13 Formula 的网格比 kick 偏了 0.41–0.50 拍；#10、#14 的网格在 kick 起音上（与我们的起音点差 +6.3 / −6.1ms）。Mixxx 同步对齐的是网格，所以用原网格做 #10→#11，两首 kick 在录音里相差 **210ms**（半拍）；换成校正后的网格，相差 0.0ms。→ 分析时必须核对，存疑的用「校正网格」写入自估网格并锁定。 | 测试台 `analyze`；录音用波形互相关定位两首各自的播放位置（`HarnessLink` + 录主输出） |
| ✅ | **同步精度**：EQ 不动、两首都有 kick 的段落叠 16 小节，录音里两条网格的相对位置 −0.5…+0.6ms，16 小节内不漂。B 起播触发晚 0.03–0.14 拍时 Mixxx 起播的相位同步都把它拉了回来。 | 诊断 set（4 段 custom 过渡），波形互相关；互相关方法自检：人为错开 0 / 3 / −12.5 / 30ms 全部测回 |
| ⚠️ | 测「两个 kick 差多少」不能用低频包络相加或低频波形互相关：两个 45–50Hz 的 kick 会相位干涉，结果每 16–20ms 跳一次。能测的是网格相对位置（互相关，两首在窗口里都要够响），以及每首自己的起音点（单曲分析）。 | 同上 |
| ❌ | **keylock 开着、deck 不在原速时，听到的声音比引擎报告的位置晚 17–29ms**（SoundTouch、RubberBand Faster、RubberBand Finer 都一样；keylock 关时完全对齐）。两个 kick 叠在一起会听出「双击」。引擎现在默认关 keylock（0.3.0 起），set 可以用 `"keylock": true` 打开，校验会提示。 | 两首合成曲，B 以非原速起播，录主输出后检测 kick 起音间隔：keylock 开时成对起音相隔 17–29ms，关时 0 |

## 平台差异（仍需真机确认）

- macOS 版 Mixxx 用 CoreAudio 解码 MP3/AAC（`COREAUDIO` 选项，优先级 Higher：`soundsourcecoreaudio.cpp:44-50`）。
  连本机 Mixxx 的模式下我们的分析用 ffmpeg，两者对编码器延迟的处理可能差几十毫秒，
  表现为曲库页「网格核对」有很小的相位差（< 0.1 拍内不报警）。内置引擎模式下分析和播放是同一个解码器，没有这个问题。
- IAC 总线会把消息回送给所有监听者（包括发送者）——这是 CoreMIDI 的行为，不在 Mixxx 源码里。协议已按方向区分帧头。
