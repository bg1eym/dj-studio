# 第一次上机（Mac + Mixxx）

这些在云端 / 模拟器里都无法验证，需要在真机上逐条确认。每一步做完把输出发给 Claude。

## 0. 安装并体检

按 README 安装后：

```bash
~/.venvs/djstudio/bin/djstudio doctor
```

预期：ffmpeg ✓、Mixxx 曲库 ✓、IAC 总线 ✓。「Mixxx 曲库目录」若报错，在 Mixxx 偏好设置 → 曲库里添加 `~/Music/DJStudio`。

## 1. 导入 + 同步（验收：RW2023 的 14 首）

1. 网页曲库 → 导入音频… → 选 `CDW_RW2023_USB/CDW RW2023/` 文件夹。
2. 等分析完，点一首看波形和小节号。
3. 「同步到 Mixxx」：会退出并重启 Mixxx（开启「启动时重新扫描」），再逐首装进 deck1 让 Mixxx 分析。
   - 要验证：重启后托管曲库的歌是否进了 Mixxx 库；装载能否触发分析；分析结果是否写回库。
4. 曲库页每首都应显示「Mixxx ✓」；有「网格存疑」的记下来。

## 2. 映射 + 真机探测

1. 「同步」或「准备」会把 `DJStudio.js` / `DJStudio.midi.xml` 部署到 Mixxx 的 controllers 目录。
2. Mixxx → 偏好设置 → 控制器 → IAC 总线：映射选 **DJ Studio**，勾「启用」，确定。右上角应变成「引擎在线 0.1.0」。
3. 往 deck1 拖一首已分析的歌（不播放），把播放位置拖到曲中，然后在「体检」页点**真机探测**（或 `djstudio probe`）。

探测逐条验证这些假设：

| 假设 | 不成立时的影响 / 对策 |
|---|---|
| Mixxx 把 SysEx 交给 `DJStudio.incomingData`，`midi.sendSysexMsg` 能发出 | 改用 CC 备用传输（探测会测） |
| `[EqualizerRack1_[ChannelN]_Effect1],parameter1..3`、`[QuickEffectRack1_[ChannelN]],super1`、`[EffectRack1_EffectUnitN],mix` 存在 | 控件名要改 |
| `[ChannelN],duration` 可读（否则用 track_samples） | 自动回退 |
| `[ChannelN],bpm` 可写（引擎靠它变速 / 同速） | 改用 rate |
| 网格 blob 位置单位是「立体声样本」（`SAMPLES_PER_POS = 2`） | 探测会给出结论；不对就改 `djstudio/mixxx/beats.py` |
| 推子归中时两边都是满音量 | 要听：探测不能测。若中间位置偏小，在 Mixxx 偏好设置 → 混音器里把推子曲线改成「加性 / 快切」 |

## 3. 第一次播放

1. 先做一套短 set（3 首、两段过渡）：Set 页新建，或把 Claude 生成的 JSON 粘贴进去。
2. 演出页 → 准备（约 1 分钟）→ ▶ 开始。
3. 要验证：
   - AutoDJ 装载：没有误播、推子归中、装载后身份核对通过；
   - 过渡：B 准时起播（日志「B 起播（晚了 x 拍）」应 < 0.1）、没有「相位校正」刷屏、没有「小节错位」；
   - EQ / 滤波 / 推子曲线听感；echo / reverb 是否挂在对的 deck；
   - 淡出停止。
4. 日志在 `~/DJStudio/logs/`，整份发给 Claude。

## 4. 故障注入（可选）

播放中退出 Mixxx：网页应在 2 秒内报「引擎失联」，conductor 停发命令，60 秒后报错停止。
