# DJ Studio

**Mixxx 驱动的自动打碟 Web App**：从 Mac 本地文件夹导入音频 → 分析结构 → 按编排方案生成 set → 一键准备 → Mixxx 按谱面自动混完整套，网页上实时监控。

- 混音由 **Mixxx** 完成（我们的引擎脚本 `DJStudio.js` 作为 Mixxx 控制器映射运行，通过 IAC 虚拟 MIDI 总线与后端通信）。
- 运行时**零 AI 依赖**：分析、编译、执行都是确定性的代码；编排方案（你给的）由 Claude 转成 set 文件，也可以在网页上直接编辑。
- 过渡手法：**长交接**（longhand）/ **瞬时切换**（instant）/ **自定义**（custom，逐小节旋钮位）。
- 只绑 `127.0.0.1`，只在本机浏览器使用。

> 当前状态：所有逻辑在模拟器里端到端测过（真正的 `DJStudio.js` 跑在假 Mixxx 里），**还没有在真 Mixxx 上跑过**。
> 首次上机请按 [docs/DEVICE_TEST.md](docs/DEVICE_TEST.md) 一步步来，把「真机探测」的结果发给 Claude。

## 安装（Mac）

```bash
brew install python ffmpeg
```

```bash
git clone https://github.com/bg1eym/dj-studio ~/dj-studio
```

```bash
cd ~/dj-studio && python3 -m venv ~/.venvs/djstudio && ~/.venvs/djstudio/bin/pip install -e '.[dev]'
```

```bash
~/.venvs/djstudio/bin/djstudio
```

浏览器会打开 http://127.0.0.1:8430/ 。代码放在 iCloud 之外（例如 `~/dj-studio`），别放进「文稿」。

### 默认位置（可用环境变量覆盖）

| 内容 | 默认 | 环境变量 |
|---|---|---|
| 托管曲库（导入的音频） | `~/Music/DJStudio` | `DJSTUDIO_LIBRARY` |
| 应用数据（studio.sqlite、sets/、logs/、backups/） | `~/DJStudio` | `DJSTUDIO_HOME` |
| Mixxx 设置目录 | 沙盒版 `~/Library/Containers/org.mixxx.mixxx/Data/Library/Application Support/Mixxx` | `DJSTUDIO_MIXXX_DIR` |
| MIDI 口 | 名字含 `IAC` 的第一个口 | `DJSTUDIO_MIDI_PORT` |
| 网页端口 | 8430 | `DJSTUDIO_PORT` |

### 一次性设置

1. 「音频 MIDI 设置」→ 窗口 → 显示 MIDI 工作室 → IAC 驱动程序 → 勾选「设备在线」。
2. Mixxx → 偏好设置 → 曲库 → 添加 `~/Music/DJStudio`。
3. 网页「体检」页确认其余各项；第一次「准备」后，在 Mixxx → 偏好设置 → 控制器 → IAC 总线：映射选 **DJ Studio** → 启用。（旧的 AgentMCP 映射不要再启用。）
4. Mixxx → 偏好设置 → 效果：效果单元 1 第一个槽 = Echo，效果单元 2 第一个槽 = Reverb。

## 使用流程

1. **曲库 → 导入音频…**：在 Mac 上选文件夹 / 文件，复制进托管曲库（按内容去重），自动分析（三频段波形、自估网格、小节线、段落）。
2. **同步到 Mixxx**：重启 Mixxx 让它扫描托管曲库，并逐首装载触发 Mixxx 分析（Mixxx 的网格是最终依据）。
   分析完会把 Mixxx 的网格和自估网格比对；Mixxx 把网格放在反拍上（真曲上常见，RW2023 的 5 首里有 3 首）或判错倍数时，曲库页标「网格存疑」、set 校验报错。点「校正网格」：备份 Mixxx 曲库后把自估网格（线在 kick 起音上）写进去并锁定（`bpm_lock`），Mixxx 不会再改。
3. **核对小节线**：点开一首歌，看波形上的小节号；不对就用「◀ 1 拍 / 1 拍 ▶」挪，勾「小节线已确认」。
4. **Set**：把编排方案交给 Claude 生成 set JSON（格式见 [docs/SET_FORMAT.md](docs/SET_FORMAT.md)），在 Set 页「JSON → 应用 → 保存」；或直接在页面上编辑。校验面板会指出时间冲突、超出曲末等问题，每段过渡都有自动化曲线预览。
5. **演出 → 准备**：退出 Mixxx → 部署引擎脚本 → 备份并写 AutoDJ 队列 → 启动 Mixxx → 等引擎上线。
6. **▶ 开始**：conductor 逐段装载、核对身份、下发过渡程序；页面实时显示两个 deck、当前小节、过渡进度和日志。**■ 淡出停止**随时可用。

### 演出页：看着它打碟

混音只在 Mixxx 里做——过渡全部是拨 Mixxx 的旋钮 / 推子（EQ、FILTER、ECHO / REVERB、推子、BPM），音频文件从不改动。
演出页把 Mixxx 那边的状态每 100ms 画一遍，像站在 DJ 旁边看：

- **两条滚动波形**：播放头居中，小节号、乐句线（每 16 小节），段落底色；标出「过渡开始 / 交出」和 B 的「进」点。
  波形颜色按当前 EQ / 滤波变化（切了低频，红色就没了），亮度跟推子。小节线上下对齐 = 两边拍子对上了。
- **混音台**：每个通道 HIGH / MID / LOW / FILTER / ECHO / REVERB 旋钮、电平表、推子，中间主输出电平和 crossfader。
  这些都是**从 Mixxx 读回的实际值**（不是计划值），正在动的旋钮会亮一下；电平是 Mixxx 自己的 `vu_meter`（推子前）。
- **过渡时间线**：这段过渡的每条曲线一行（A 橙 / B 蓝按 deck 着色），白色游标是现在的位置；下面列出「正在做」和「接下来」的动作
  （例：「第 13 小节 B（Deck 2）LOW -15 dB → 0 dB，用 24 小节」）。
- 顶部整套时间表，当前段高亮。

### 预演（不开 Mixxx）

演出页「预演」：选从头或「过渡 k 前 8 小节」、倍速（1–8×），点「▶ 预演」。在模拟器里用**真正的引擎脚本**按这套 set 跑一遍，
小节、过渡时刻、每个旋钮怎么动都和真机一样（时长 / BPM / 网格用同一份解析结果），只是没有声音。不需要准备，也不碰 Mixxx。

## 没有 Mixxx 也能试：演示模式

```bash
~/.venvs/djstudio/bin/djstudio demo
```

合成 4 首 techno 片段 + 假 Mixxx 曲库 + 模拟引擎，整套流程（准备 → 播放）都能在网页上走一遍（听不到声音）。

## 命令行

| 命令 | 作用 |
|---|---|
| `djstudio` / `djstudio serve` | 启动网页 |
| `djstudio demo` | 演示模式 |
| `djstudio doctor` | 环境体检 |
| `djstudio import <文件或文件夹>…` | 导入并分析 |
| `djstudio validate <set id 或 json 路径>` | 校验 set |
| `djstudio prepare <set id>` | 准备 |
| `djstudio play <set id>` | 终端里播放（Ctrl-C = 淡出停止） |
| `djstudio probe` | 真机探测 |

## 开发

```bash
~/.venvs/djstudio/bin/pytest
```

测试需要 `node`（运行模拟器）和 `ffmpeg`。文档：[架构](docs/ARCHITECTURE.md) · [set 格式](docs/SET_FORMAT.md) · [引擎协议](docs/ENGINE.md) · [Mixxx 源码核对](docs/MIXXX_NOTES.md) · [上机测试](docs/DEVICE_TEST.md)。
