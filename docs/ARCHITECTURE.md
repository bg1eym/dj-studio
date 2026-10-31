# 架构

```
浏览器（原生 JS，无构建）
  曲库/波形 · Set 编辑与校验 · 演出监控 · 体检
        ⇅ HTTP JSON + SSE（实时状态 ~5Hz、日志、任务进度）
FastAPI（djstudio/web/app.py，只绑 127.0.0.1）
        │
Studio 服务层（djstudio/studio.py）—— 网页和命令行唯一入口
  ├─ library/   导入（复制到托管曲库、按内容去重）、ffmpeg 解码、确定性分析、studio.sqlite
  ├─ setfile + techniques + compiler   djset/2（小节、人读单位）→ 引擎程序（拍、Mixxx 原生值）+ 校验
  ├─ mixxx/     mixxxdb 读写（网格解码、AutoDJ 队列）、进程控制、mixxx.cfg
  ├─ engine/    protocol（分帧）· link（IAC / 模拟器）· client（状态、确认、失联）· conductor（逐段指挥）
  └─ probe      真机逐条验证 Mixxx 行为
        ⇅ IAC 虚拟 MIDI 总线：SysEx 分帧 JSON（备用：CC 流）
Mixxx + DJStudio.js（控制器映射）
  20ms 时间线 · 自动化曲线 · B 起播 + 相位校正 · AutoDJ 装载器 · 100ms 状态回传
```

## 两种引擎（`Config.engine`，`DJSTUDIO_ENGINE`）

- **builtin（默认）**：不连本机的 Mixxx——`Studio._mixxx_tracks()` 返回空（不读 Mixxx 曲库），连 Mixxx 的操作（同步、校正 Mixxx 网格、
  真机探测、IAC 连接）直接报错。网格用自己分析的 `own_grid`；一键分析只有 音频分析 → 读打碟谱 → 生成 set → 准备内置引擎 四步；
  「开始演出」= 在**声音引擎** `djstudio-engine`（`tools/mixxx-engine`：只编了 Mixxx 2.5.6 的引擎核心）里跑同一份 `DJStudio.js` 和同一个 conductor
  （和预演同一套，`Studio.rehearse(builtin=True)`）。原速时出声（PortAudio → 系统默认输出，引擎在声卡回调里处理，同 Mixxx 的 SoundManager）；
  可以倍速（没有声音）、从第 k 段过渡开始。找不到声音引擎时退回 Node 模拟器（`sim/fake_mixxx.mjs`，没有声音）。
  分析也用声音引擎解码（`--decode`），和播放是同一个 Mixxx 解码器；分析结果记下解码器，换了就重新分析。
- **mixxx**：下面的结构图——`DJStudio.js` 作为控制器映射跑在本机的 Mixxx 里，经 IAC 通信。

## 分工：快的在引擎，慢的在 Python

| 层 | 做什么 | 不做什么 |
|---|---|---|
| `DJStudio.js`（Mixxx 内） | 按「过渡程序」执行：每 20ms 根据 A 的播放位置算时间线拍位，插值所有曲线并写控件；到点起播 B（补偿 tick 延迟）、定期相位校正；执行事件（停 A、loop）；AutoDJ 脉冲装载；回传状态 | 不做任何决策，不知道整套 set |
| conductor（Python） | 装载下一首并用时长 + 网格 BPM 核对身份；提前下发下一段程序；监控进度；失联 / 停滞 / 超时处理；淡出停止 | 不做实时控制 |

过渡程序在 B 装载完成后就下发（通常提前几分钟），**即使 Python 卡住，这一段也会按谱面执行完**。

### 一键：打碟谱 → 可以演出

`Studio.auto_prepare()` 是一个分 6 步的后台任务（`Job.stages` / `progress`，网页一个进度条）：音频分析 → 读打碟谱
（`plan/cdw.py`：pypdf 取带坐标的文字，按表头的 x 分列）→ Mixxx 扫描 + 分析（`_sync`）→ 网格核对与自动校正（`_fix_grids`）
→ 打碟谱转 set（`plan/convert.py`：曲目匹配、小节对齐、钟点位换算；小节相位按谱写回曲库后再转一次）→ 准备（`_prepare`）。
网页上传走 `PUT /api/upload?name=…`（请求体就是文件），音频复制进托管曲库，打碟谱（PDF 或 .md）存到 `~/DJStudio/plans/`（最新的就是当前打碟谱）。
读谱按扩展名分：`.md` → `plan/mdplan.py`（《打碟谱规范》docs/SCORE_SPEC.md，按表头认列，报错带行号），`.pdf` → `plan/cdw.py`；两者都产出 `cdw.Plan`。
`GET /api/score/spec.md`、`/api/score/template.md` 给出规范和模板（打包的 App 里在 `djstudio/docs/`）。
一份谱 = 一套 set：`GET /api/plans` 列出放进来过的谱（`Studio.plans`），`POST /api/plans/select` 换成某一份（改它的修改时间，最近的 = 当前的，再找曲子 / 自动分析）。一键分析的 `Job.plan` 记着是哪份谱的，首页只把当前谱的分析结果当「可以演出了」。
谱上写的 hot cue IN / OUT / STOP / FADE 说了算（SCORE_SPEC.md 5.3）：小节.拍写的在 `mdplan` 读谱时核对，时间写的记成 `Plan.cue_checks`，曲子分析完后在 `convert` 里换成拍核对。

放进打碟谱后 `Studio.find_plan_tracks()` 在后台把谱上还缺的歌去 `Config.find_dirs`（默认 下载 / 桌面 / 文稿 / 音乐）里找：
`plan/convert.py` 的 `file_matches` 按文件名比（去掉编号和标点后的字符相似度、词集合相似度取大的，门槛 0.85），
`duration_ok` 核对时长，找到的导入托管曲库。之后（以及每个音频上传完）调 `Studio.maybe_auto()`：谱上的歌都齐了就自动开始一键分析，
同一份谱 + 同一组歌只自动开始一次。网页首页（`app.js` 的 `renderHome`）按状态换屏：放打碟谱 → 进度条 → 可以演出 → 演出画面。

启动（`selfupdate.py`）：从 git 仓库运行时先 `git pull --ff-only`，有新提交就（依赖变了先重装）`os.execv` 重启自己；
端口被之前开着的 DJ Studio 占着就先关掉它。网页文件都带 `Cache-Control: no-cache`，右上角显示版本和提交号。

### 网页怎么「看见」旋钮

引擎每 100ms 的状态帧里带 `m`（每个通道从 Mixxx **读回**的 hi / mid / lo / filter / echo / reverb / vu_meter）、`x`（crossfader）、
`mv`（主输出电平），以及 deck 位置和程序时间线位置 `pg.tl`。Python 原样经 SSE 每 100ms 推给网页；`web/static/live.js` 画两条滚动波形、
混音台和过渡时间线，两帧之间按播放速度外推位置。计划（编译出的曲线）和实际（读回的旋钮）分开画：时间线上是计划，旋钮是实际。

画面和声音对齐：Python 收到状态帧时记下墙钟，`engine_state()` 给出 `at` = 收到时刻 + 声卡报的输出延迟（Unix 毫秒）
= 帧里的位置从扬声器出来的时刻。网页按 `at`（换算到页面时钟）外推播放头，而不是按网页收到 SSE 的时刻
（中间隔着 50–150ms 不等的轮询）。拍线、波形来自分析，分析用的解码和播放是同一个解码器、同一套帧号（见 MIXXX_NOTES「CoreAudio」一条）。

**预演**：同一套 conductor + `DJStudio.js`，在声音引擎里跑但不出声（没有声音引擎时用 Node 模拟器；`Studio.rehearsal_link_factory` 可以换）。

**任何时候只有一个引擎在出声**：开始演出 / 预演要先启动引擎进程（Mac 上几秒）。`Studio.rehearse` 用一把锁挡住同时到的
第二个请求（「正在启动，稍等」），开新的之前先关掉上一场留着最后画面的引擎；网页按钮在请求进行中变灰。0.3.2 以前连点两次会开出两个
引擎、两套声音叠在一起，点「停止」只停网页正在看的那个。**停止**：内置引擎淡出 1 秒后立刻关掉引擎进程（声音一定停），
`last_play` 记下这一场从哪开始（set / 第几首 / 第几小节 / 倍速），网页按它找编译结果。

**停下后「继续」**：`Studio.stop` 先记下停在哪（`resume_point`：A 还在放就是 A 当前的拍，A 已经交给 B 就是 B 的），
`Studio.resume` 用 `rehearsal_set(start=(第几首, 第几小节))` 截出从那里开始的 set（`compile_set(resume=True)` 允许第 1 首
从第 1 段过渡中途开始）。conductor 的 resume 流程：起播前用 `param` 命令把 A 的推子 / 旋钮摆到时间线当前位置的值 → A 起播
（两边都没在播时只能往 deck1 装，所以 B 要等 A 起播后才装）→ 装 B → 下发带 `resume` 的程序：`DJStudio.js` 不按 late 拒绝，
`startB` 按已经过去的拍数把 B 定位到对应的拍再起播（Mixxx 起播时对齐相位），曲线从时间线当前位置接着走。实测 A 单独放约 0.3 秒后 B 接上，
B 的位置与时间线一致（误差 < 10ms）。

**演出页**（`web/static/live.js`）：波形是 Denon（Engine DJ）的三频段画法（`web/static/wavedraw.js`）：三个频段各是一条独立的波形，高度 = 这一段自己的音量，按 蓝（低频）→ 绿（中频）→ 白（高频）的顺序直接叠画（不混色），低频最高满高、中频 3/4、高频一半；分析时按 Denon 的分界（≈500 Hz / 2 kHz，`analysis.WAVE_BANDS`）取每 20 ms 里最响那一帧的有效值、按满幅标定（三段比例是真实的）。演出页的滚动波形（再乘当前 EQ / 滤波）、交接行和曲库页三处同一画法；EQ 旋钮和曲线也用这三个颜色（LOW 蓝、MID 绿、HIGH 白）。顶部「交接」行画当前（或下一段）过渡的 A、B 整首波形，标出过渡开始 / B 起播 / 交出（或放到曲尾）、
B 的进点 / 接满，播放头和小节计数；EQ 三段用固定颜色（LOW 红、MID 绿、HIGH 蓝，和波形的颜色一致）；
时间线下面大字显示「现在的 EQ 操作」（正在动的：从几点到几点、现在几点、还剩几小节；保持在非 12 点的；下一个 EQ 动作）。
过渡时间线按通道分两组（CH 1 在上、CH 2 在下，和混音台一样），每组 7 行，顺序和旋钮一样（HIGH / MID / LOW / FILTER / ECHO / REVERB / 推子），
没有动作的行也画（整段过渡里它停在哪）；纵坐标 = 旋钮转角（7 点在下、12 点在中间、5 点在上；推子 = 推子位置）；每行左边的小旋钮是引擎读回的实际值，
游标处的点是计划值，两者重合。旋钮指针用 SVG 的 transform 属性旋转（Safari 不认 CSS `transform-box: view-box`）。

**音量和电平表**：每首按响度统一到 -14 LUFS（`studio.track_gain_db`，分析里的 `loudness` / `peak`，往上提时峰值不超过 -1 dBFS），
增益随 library 命令给引擎，走 Mixxx 的 ReplayGain。电平表是 dBFS（引擎量的推子前 / 主输出 RMS 和峰值，状态帧 `m[d][7..8]`、`ml`），
CH1 / MASTER / CH2 三条同高同刻度；真 Mixxx 没有这两项时按 `vu_meter` 反推。
从第 k 段开始时用截短的 set（`Studio.rehearsal_set`：第 k 首从过渡前 8 小节起播）。

## 关键设计决定

- **小节号**：网格来自 Mixxx（sync / quantize 都基于它）。第 1 小节 = 曲中最早的小节起点（网格向前延伸），
  所以编号只取决于 BPM 和小节相位，与 Mixxx 把「第 0 拍」放在哪里无关。小节相位存成「第 1 小节起点的秒数」（`bar1`），换网格后自动重算。
- **SysEx 分帧 JSON**：一条消息 ≤200 字节一片；方向用帧头区分（`44 53` Python→引擎，`44 45` 引擎→Python），
  因为 IAC 总线会把消息回送给所有监听者（包括发送者自己）。状态位置精度到毫秒（旧系统 7-bit 约 2.5 秒）。
- **AutoDJ 只当装载器**：引擎先清空目标 deck、静音，再开 AutoDJ，目标 deck 一装上立即关 AutoDJ、停播、推子归中。
  队列在「准备」时写进 mixxxdb（Mixxx 必须已退出），启动后再核对。
- **身份核对**：时长 ±1s + 网格 BPM ±0.1。对不上就停，宁停不错放。
- **失联**：状态帧 >1.5s 没来 = 失联 → 告警、停发命令；恢复后继续；超过 60s 放弃。Mixxx 重载映射（收到 boot hello）= 状态丢失，停止。
- **所有等待都有上限**；时间线 8s 不动 = 停滞，报错。
- **来不及（late）**：程序到达时时间线已过 0 点 >1 拍，引擎拒绝；conductor 改发应急切换（下一个小节线，1 小节 HPF + 硬切）。
- **mixxxdb 只在 Mixxx 退出时写**，写前自动备份到 `~/DJStudio/backups/`（保留 10 份）。

## 从旧系统继承的坑（仍然适用）

- `pgrep -x mixxx` 在用户的 Mac 上匹配不到 → 三种方式判断进程。
- 有 deck 在播时 `osascript quit` 会被确认框挂住 → 退出前确认 deck 都停了。
- 替换 `controllers/DJStudio.js` 会热重载映射并清空引擎状态 → 只在「准备」（Mixxx 退出时）部署。
- iCloud「仅云端」文件会让读取阻塞 → 代码和托管曲库都放在 iCloud 之外；导入时会标出云端文件。
- Mixxx 网格可能误判（2/3、1.5 倍），或整条放在反拍上（真曲实测 5 首里 3 首）→ 分析时和自估网格比对，曲库页标「网格存疑」，set 校验报错；「校正网格」把自估网格写进 mixxxdb 并 `bpm_lock`。自估网格的线放在 kick 起音点上（`analysis.kick_phase`）。
- **不要用 kick 相似度自动决定接法**（研究里 25 次实测相关性只有 +0.19）。

## 测试

- `tests/test_compiler.py`：set 格式、手法展开、单位换算、时序校验。
- `tests/test_sim_e2e.py`：真正的 `DJStudio.js` 在 Node 假 Mixxx 里（`sim/fake_mixxx.mjs`，虚拟时钟 40 倍速），
  conductor 指挥整套：拍位对齐 < 0.05 拍、BPM 渐变、失联检测、装错曲检测、淡出停止。
- `tests/test_studio.py`：导入合成音频 → 分析（BPM / 首拍 / 小节相位 / 段落）→ 假 mixxxdb → 准备 → 播完。
- `tests/test_web.py`：API 冒烟、路径限制。

- `tests/test_mixxx_engine.py`：同样的 conductor 驱动**声音引擎**（`tools/mixxx-engine`，Mixxx 2.5.6 的真 MidiController、脚本引擎、deck、
  EQ / 滤波、同步），录下主输出在**音频**上检查过渡中两个 kick 没有双击；解码与 ffmpeg 一致；出声时引擎跟着（假）声卡的时钟走、不断音。
  需要先构建引擎（`DJSTUDIO_ENGINE_BIN`），否则跳过。

## 独立的 Mac 应用

`.github/workflows/mac-app.yml` 在 GitHub 的 macOS 构建机上：用 Mixxx 的预编译依赖编 `djstudio-engine`（Apple 芯片版交叉编译，
和 Mixxx 官方一样），检查它只依赖系统库；PyInstaller（`packaging/macos/djstudio.spec`）把 Python、网页服务和引擎打成 DJ Studio.app，
在 .app 里跑 `selftest`，ad-hoc 签名后放到 Release `mac-app-latest`。应用没有窗口（`LSUIElement`），打开后在浏览器里用，
从网页右上角「退出」；同一版本已经开着时再打开只会打开网页。

Node 模拟器按 Mixxx 源码的行为写成（docs/MIXXX_NOTES.md）；声音引擎跑的是 Mixxx 本身的代码，但没有 PlayerManager 和真 AutoDJProcessor
（AutoDJ 的装载逻辑是移植的）。
