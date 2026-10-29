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
2. 准备时会自动把 mixxx.cfg 里 IAC 总线的映射换成 DJ Studio（沿用原来 AgentMCP 用的设备）。若日志提示找不到 IAC 设备，就在 Mixxx → 偏好设置 → 控制器 → IAC 总线：映射选 **DJ Studio**，勾「启用」。右上角应显示「引擎在线 0.4.0」。
3. 往 deck1 拖一首已分析的歌（不播放），把播放位置拖到曲中，然后在「体检」页点**真机探测**（或 `djstudio probe`）。

Mixxx 的行为已经按 2.5.6 源码逐条核对过（见 [MIXXX_NOTES.md](MIXXX_NOTES.md)），探测在真机上再确认一遍：

| 检查 | 不通过时 |
|---|---|
| SysEx 双向（Python→引擎 需要 XML 里的 0xF0 绑定） | 映射没换成 DJ Studio / 不是 0.4.0；准备时会自动改 mixxx.cfg |
| CC 备用传输 | 仅备用 |
| EQ / 滤波 / 效果单元控件能写能读 | 效果单元里没放效果，或 Mixxx 版本不同 |
| `bpm` 控件可写 | 版本不同 |
| 网格解码（对照 Mixxx 自己的 `beat_distance`） | 把结果发给 Claude |
| 推子模式 = 加性、deck 朝向左/右 | 引擎 init 没跑 |

听感项（探测测不了）：推子居中时两边音量是否一样；echo / reverb 是否挂在对的 deck。

## 3. 第一次播放

1. 先做一套短 set（3 首、两段过渡）：Set 页新建，或把 Claude 生成的 JSON 粘贴进去。
2. 演出页 → 准备（约 1 分钟）→ ▶ 开始。
3. 要验证：
   - AutoDJ 装载：没有误播、推子归中、装载后身份核对通过；
   - 过渡：B 准时起播（日志「B 起播（晚了 x 拍）」应 < 0.1）、没有「相位校正」刷屏、没有「小节错位」；
   - EQ / 滤波 / 推子曲线听感；echo / reverb 是否挂在对的 deck；
   - 演出页的旋钮 / 推子 / 电平和 Mixxx 自己窗口里的一致（同一时刻对照看几次；网页读的是同一批控件）；
   - 主输出电平：长交接两首满音量叠加时不应长时间顶红（体检里 ReplayGain 应为「已开启」）；
   - 淡出停止。
4. 日志在 `~/DJStudio/logs/`，整份发给 Claude。

## 4. 故障注入（可选）

播放中退出 Mixxx：网页应在 2 秒内报「引擎失联」，conductor 停发命令，60 秒后报错停止。
