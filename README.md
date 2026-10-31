# DJ Studio

**自动打碟 Web App**：放进打碟谱（自己按规范写的 .md，或 PDF 模板）→ 自动找到曲子并分析 → 按谱在 Mixxx 的引擎里自动混完整套，网页上看每个旋钮怎么动，耳朵听结果。

- **独立的 Mac 应用（DJ Studio.app）**：网页服务、Python、声音引擎都在里面，不用装 Homebrew / Python / ffmpeg / Node，也不碰你装的 Mixxx。
- **声音引擎** `djstudio-engine`：只用 Mixxx 2.5.6 源码里打碟要用的部分（deck、混音器、EQ / 滤波 / Echo / Reverb、同步、控制器脚本引擎、解码），
  过渡全部是按谱拨 Mixxx 的旋钮和推子，音频文件从不改动。原速时从电脑的声音输出放出来。见 [tools/mixxx-engine](tools/mixxx-engine/README.md)。
- 运行时**零 AI 依赖**：分析、编译、执行都是确定性的代码，结果能在网页上手动改。
- 过渡手法：**长交接**（longhand）/ **瞬时切换**（instant）/ **自定义**（custom，逐小节旋钮位）。
- 只绑 `127.0.0.1`，只在本机浏览器使用。
- 还保留连本机 Mixxx 的模式（`DJSTUDIO_ENGINE=mixxx`，经 IAC 虚拟 MIDI），见 [docs/DEVICE_TEST.md](docs/DEVICE_TEST.md)。

## 打碟谱规范（自己写谱）

谱由你写，DJ Studio 照谱打碟：[docs/SCORE_SPEC.md](docs/SCORE_SPEC.md)《打碟谱规范》说明怎么写——曲目表、Prime 4+ 的 Hot Cue（8 个打击垫）、
每段过渡逐小节的旋钮位（EQ / Filter 钟点位、推子 0–10、Echo / Reverb 湿度、换速）。
复制模板 [docs/score_template.md](docs/score_template.md)（一份能直接跑的 3 首例子）改成自己的，存成 .md 拖到首页；写错了网页会报第几行。
首页也能下载规范和模板。

## Set 播放规范

所有 set（打碟谱自动生成的、手工转的）都按 [docs/SET_SPEC.md](docs/SET_SPEC.md)：EQ / Filter / 效果器 / 推子 / 速度 / 音量的取值和换算表，
以及 Denon Prime 4+ 格式的 hot cue：打击垫没有固定用途，每首放的是它在这套 set 里的点——IN（进）、进来时要处理的点、OUT（出）、出去时要处理的点、STOP / FADE——按时间先后从 1 号垫排，颜色表示点的种类，注释写谱上的动作和旋钮怎么拧。
网页上「Hot Cue 表（Prime 4+）」导出每首歌的 cue 位置（小节.拍 / 谱上小节 / 时间），照着在 Engine DJ 里设。

## 安装（Mac）

只支持 Apple 芯片（M1 及以后）的 Mac，不做 Intel 版。在 macOS 26 Tahoe 上打包和测试（GitHub 的 macOS 26 构建机），每次构建会查应用要求的最低系统版本，不能高于 macOS 26。打开终端，粘贴这一行（装进「应用程序」并打开；以后更新也是这一行）：

```bash
curl -fL -o /tmp/djstudio.zip "https://github.com/bg1eym/dj-studio/releases/download/mac-app-latest/DJ-Studio-mac-arm64.zip" && rm -rf "/Applications/DJ Studio.app" && ditto -x -k /tmp/djstudio.zip /Applications && open "/Applications/DJ Studio.app"
```

也可以在 [Release 页](https://github.com/bg1eym/dj-studio/releases/tag/mac-app-latest)下载 `DJ-Studio-mac-arm64.zip`，
解压后把 DJ Studio 拖进「应用程序」。这样下载的第一次打开要：右键 → 打开 → 打开（macOS 15 以后：双击一次，再到 系统设置 → 隐私与安全性 → 「仍要打开」），
因为应用没有经过苹果公证。

打开后会自动打开浏览器 http://127.0.0.1:8430/ 。应用没有窗口，退出点网页右上角「退出」；再打开 DJ Studio 就回来。
日志在 `~/DJStudio/logs/app.log`。

### 从源码运行（开发）

```bash
git clone https://github.com/bg1eym/dj-studio ~/dj-studio && cd ~/dj-studio && python3 -m venv ~/.venvs/djstudio && ~/.venvs/djstudio/bin/pip install -e '.[dev]'
```

```bash
~/.venvs/djstudio/bin/djstudio
```

从源码运行时每次启动会自动 `git pull`。声音引擎要另外编（`tools/mixxx-engine/build.sh`）或用 `DJSTUDIO_ENGINE_BIN` 指过去；
没有引擎时分析用 ffmpeg、内置引擎退回 Node 模拟器（没有声音）。

### 默认位置（可用环境变量覆盖）

| 内容 | 默认 | 环境变量 |
|---|---|---|
| 托管曲库（导入的音频） | `~/Music/DJStudio` | `DJSTUDIO_LIBRARY` |
| 应用数据（studio.sqlite、sets/、logs/、backups/） | `~/DJStudio` | `DJSTUDIO_HOME` |
| Mixxx 设置目录 | 沙盒版 `~/Library/Containers/org.mixxx.mixxx/Data/Library/Application Support/Mixxx` | `DJSTUDIO_MIXXX_DIR` |
| MIDI 口 | 名字含 `IAC` 的第一个口 | `DJSTUDIO_MIDI_PORT` |
| 网页端口 | 8430 | `DJSTUDIO_PORT` |

### 一次性设置（只有连 Mixxx 时才需要）

内置引擎（默认）不需要下面这些。

1. 「音频 MIDI 设置」→ 窗口 → 显示 MIDI 工作室 → IAC 驱动程序 → 勾选「设备在线」。
2. Mixxx → 偏好设置 → 曲库 → 添加 `~/Music/DJStudio`。
3. 网页「体检」页确认其余各项；第一次「准备」后，在 Mixxx → 偏好设置 → 控制器 → IAC 总线：映射选 **DJ Studio** → 启用。（旧的 AgentMCP 映射不要再启用。）
4. Mixxx → 偏好设置 → 效果：效果单元 1 第一个槽 = Echo，效果单元 2 第一个槽 = Reverb。

## 使用流程

网页只有一页：

1. **放进打碟谱**：把打碟谱（按《打碟谱规范》写的 .md，或 PDF 模板）拖进网页（曲子可以一起拖，文件或整个文件夹都行）。
   谱上的曲子没拖进来的，程序按谱上的文件名去「下载 / 桌面 / 文稿 / 音乐」里找（编号、下划线、艺人写法不同都认；时长不对的不要），
   找到就复制进托管曲库。找不到的会列出来，拖进来就行。
2. **分析**：曲子齐了自动开始，一个进度条，不用管——
   音频分析 → 读打碟谱 → 按谱生成 set 并校验（小节号按谱上「第 1 小节第 1 拍」对齐，旋钮钟点位换成 dB，见 [docs/SET_FORMAT.md](docs/SET_FORMAT.md)）
   → 试启动一次声音引擎（检查声音输出）。不碰你装的 Mixxx，14 首大约两三分钟。
3. **▶ 开始演出**：可以选从头或「过渡 k 前 8 小节」开始；原速时有声音（电脑当前的声音输出），2–8 倍速时没有声音。
   同一页换成演出画面（波形、混音台、过渡时间线），**■ 停止**随时可用（淡出停止）。

连 Mixxx 时（`DJSTUDIO_ENGINE=mixxx`）第 2 步还会：让 Mixxx 扫描并分析（会重启 Mixxx）→ 网格核对（Mixxx 把网格放在反拍上的自动换成自估网格并锁定，先备份）
→ 准备 Mixxx（部署引擎脚本、写 AutoDJ 队列、启动、等引擎上线）；开始演出只能从头、原速。

右上角「高级」里是控制台（预演、单独准备）、曲库（波形、小节线、校正网格）、Set（逐段编辑、曲线预览、JSON）和体检。
别的格式的编排方案：照《打碟谱规范》改写成 .md（或交给 Claude 转成 set JSON 后在 Set 页粘贴）。

### 更新

应用：再运行一次上面「安装」那一行（会换成最新的 DJ Studio.app；曲库、分析结果、set 都在 `~/DJStudio` 和 `~/Music/DJStudio`，不受影响）。
右上角显示版本和提交号。从源码运行时每次启动自动 `git pull`（`DJSTUDIO_NO_UPDATE=1` 关掉）。

### 演出画面：看着它打碟

混音只在 Mixxx 里做——过渡全部是拨 Mixxx 的旋钮 / 推子（EQ、FILTER、ECHO / REVERB、推子、BPM），音频文件从不改动。
演出画面把 Mixxx 那边的状态每 100ms 画一遍，像站在 DJ 旁边看：

- **两条滚动波形**：播放头居中，小节号、乐句线（每 16 小节），段落底色；标出「过渡开始 / 交出」和 B 的「进」点。
  波形颜色按当前 EQ / 滤波变化（切了低频，红色就没了），亮度跟推子。小节线上下对齐 = 两边拍子对上了。
- **混音台**：每个通道 HIGH / MID / LOW / FILTER / ECHO / REVERB 旋钮、电平表、推子，中间主输出电平和 crossfader。
  这些都是**从 Mixxx 读回的实际值**（不是计划值），正在动的旋钮会亮一下；电平是 Mixxx 自己的 `vu_meter`（推子前）。
  旋钮按钟点位显示（和打碟谱同一种读法：「10点半」「左9点」，旁边小字是 dB），推子按 0–10 显示（谱上的「推子 5」）。
- **过渡时间线**：这段过渡的每条曲线一行（A 橙 / B 蓝按 deck 着色），白色游标是现在的位置；下面列出「正在做」和「接下来」的动作
  （例：「第 13 小节 B（Deck 2）LOW -15 dB → 0 dB，用 24 小节」）。
- 顶部整套时间表，当前段高亮。

### 预演（不开 Mixxx）

分析完的那一屏点「先在模拟器里预演」，或者「高级 → 控制台」的「预演」：选从头或「过渡 k 前 8 小节」、倍速（1–8×），点「▶ 预演」。在模拟器里用**真正的引擎脚本**按这套 set 跑一遍，
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
