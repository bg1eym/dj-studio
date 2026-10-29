# DJ Studio

**Mixxx 驱动的自动打碟 Web App**：从 Mac 本地文件夹导入音频 → 分析结构 → 按编排方案生成 set → 一键准备 → Mixxx 按谱面自动混完整套，网页上实时监控。

- 混音由 **Mixxx** 完成（我们的引擎脚本 `DJStudio.js` 作为 Mixxx 控制器映射运行，通过 IAC 虚拟 MIDI 总线与后端通信）。
- 运行时**零 AI 依赖**：分析、编译、执行都是确定性的代码；编排方案（你给的）由 Claude 转成 set 文件，也可以在网页上直接编辑。
- 过渡手法：**长交接**（longhand）/ **瞬时切换**（instant）/ **自定义**（custom，逐小节旋钮位）。
- 只绑 `127.0.0.1`，只在本机浏览器使用。

> 当前状态：所有逻辑在模拟器里端到端测过，也在真 Mixxx 2.5.6 的引擎代码（测试台，见 `tools/mixxx-harness`）上跑过整套 set；
> **还没有在 Mac 上的 Mixxx 程序里跑过**（IAC / CoreMIDI、进程控制要真机验证）。首次上机请按 [docs/DEVICE_TEST.md](docs/DEVICE_TEST.md) 一步步来。

## 安装（Mac）

```bash
brew install python ffmpeg node
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

浏览器会打开 http://127.0.0.1:8430/ 。node 用于「预演」和演示模式（`djstudio demo`，http://127.0.0.1:8431/）。代码放在 iCloud 之外（例如 `~/dj-studio`），别放进「文稿」。

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

网页打开就是「开始」页，三步：

1. **放进来**：把这套 set 的曲子和打碟谱 PDF 一起拖进网页。曲子复制进托管曲库（按内容去重）并开始分析；打碟谱一栏马上列出谱上每首对上了哪个文件、还缺哪首。
2. **开始分析**：一个进度条，不用管——
   音频分析 → 读打碟谱 → 让 Mixxx 扫描并分析（会重启 Mixxx）→ 网格核对（Mixxx 把网格放在反拍上的自动换成自估网格并锁定，先备份）
   → 按谱生成 set 并校验（小节号按谱上「第 1 小节第 1 拍」对齐，旋钮钟点位换成 dB，见 [docs/SET_FORMAT.md](docs/SET_FORMAT.md)）
   → 准备 Mixxx（部署引擎脚本、写 AutoDJ 队列、启动、等引擎上线）。有需要你看一眼的地方会列在进度条下面。
3. **▶ 开始演出**：切到演出页，conductor 逐段装载、核对身份、下发过渡程序。**■ 淡出停止**随时可用。

要手动改的时候，「高级」里还有曲库（波形、小节线、校正网格）、Set（逐段编辑、曲线预览、JSON）和体检。
不是《打碟谱》模板的编排方案，交给 Claude 转成 set JSON 后在 Set 页粘贴。

> 从旧版本更新后要重新装一次依赖（新增了读 PDF 的 pypdf）：`cd ~/dj-studio && git pull && ~/.venvs/djstudio/bin/pip install -e '.[dev]'`

### 演出页：看着它打碟

混音只在 Mixxx 里做——过渡全部是拨 Mixxx 的旋钮 / 推子（EQ、FILTER、ECHO / REVERB、推子、BPM），音频文件从不改动。
演出页把 Mixxx 那边的状态每 100ms 画一遍，像站在 DJ 旁边看：

- **两条滚动波形**：播放头居中，小节号、乐句线（每 16 小节），段落底色；标出「过渡开始 / 交出」和 B 的「进」点。
  波形颜色按当前 EQ / 滤波变化（切了低频，红色就没了），亮度跟推子。小节线上下对齐 = 两边拍子对上了。
- **混音台**：每个通道 HIGH / MID / LOW / FILTER / ECHO / REVERB 旋钮、电平表、推子，中间主输出电平和 crossfader。
  这些都是**从 Mixxx 读回的实际值**（不是计划值），正在动的旋钮会亮一下；电平是 Mixxx 自己的 `vu_meter`（推子前）。
  旋钮按钟点位显示（和打碟谱同一种读法：「10点半」「左9点」，旁边小字是 dB），推子按 0–10 显示（谱上的「推子 5」）。
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
