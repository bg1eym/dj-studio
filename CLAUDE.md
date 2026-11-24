# DJ Studio — 给 Claude 的说明

- 用户 Qiang 用**中文**交流；在学 techno DJ，现场碟机 Denon Prime 4+。界面文案、日志、文档都用中文。
- 这是 Mixxx 驱动的自动打碟 Web App。先读 README.md 和 docs/ARCHITECTURE.md。
- **默认是内置引擎**（`DJSTUDIO_ENGINE=builtin`）：DJStudio.js 在内置的模拟 Mixxx 里跑，不碰用户本机的 Mixxx。用户明确说过：
  现在只要看 EQ / 效果器怎么变，不要调用本机的 Mixxx（不稳定）。连 Mixxx 的模式保留（`DJSTUDIO_ENGINE=mixxx`），别改回默认。
- **运行时零 AI 依赖**是硬约束：分析、编译、执行必须是确定性代码，结果能在网页上手动改。

## 开发

- 测试：`pytest`（需要 node、ffmpeg 和 C++ 编译器）。改了引擎脚本、协议、conductor 后必须跑 `tests/test_sim_e2e.py`。
- 改 `DJStudio.js` 时同步改 `protocol.py` / `sim/fake_mixxx.mjs`，并把 `DJStudio.VERSION` 加一。脚本只用 ES5。
- 涉及 Mixxx 行为的判断**先查 Mixxx 源码**（docs/MIXXX_NOTES.md 记录了已核对的结论和行号），不能凭印象；平台行为（进程名、MIDI 口名、CoreMIDI）还**必须真机验证**：模拟器是按我们的理解写的，测过不代表 Mixxx 真这样。不确定的地方写进 docs/DEVICE_TEST.md 的假设表。
- 演示模式 `djstudio demo` 可以在没有 Mixxx 的环境里跑完整流程（含网页）。
- **每次发布（版本号加一）都在 [docs/CHANGELOG.md](docs/CHANGELOG.md) 最上面写一段**（中文，写用户能感觉到的变化，带日期）：`tests/test_changelog.py` 查当前版本有没有写；CI 把这一段写进 Release 说明，并单独发一个 `v版本号` 的 Release（带应用）。

## 四 Deck（曲谱 v5）

- 两代谱两条路：v1–v4 走 DJStudio.js + AutoDJ（上面那些规矩）；**v5 走引擎里的原生调度器**（`tools/mixxx-engine/djs5/`，协议和混音链路见
  docs/ENGINE5.md），Python 在 `djstudio/v5/`。v5 只在内置引擎里放。改 v5 不要动 v4 那条路的行为（`state` 这类 v4 命令仍然只报两台 Deck）。
- 谱法以 [docs/SCORE_SPEC_V5.md](docs/SCORE_SPEC_V5.md) 为准，示例 docs/score_v5_example.md 必须零错误零提示地编过。
  规范里第 8 节的效果表由 `tests/test_v5_score.py` 和代码（`livefx.PARAMS`、`compile.FX_PARAMS`）比对：改一边要同时改另一边。
- 元素表（SCORE_SPEC_V5.md 第 14 节：每份素材的各个声部在原曲哪几小节有）由 `djstudio/v5/elements.py` 读，只给演出画面画元素格子图用，不影响声音；
  规范里的声部表和 `elements.ELEMENTS` 由 `tests/test_v5_elements.py` 比对。程序不分析声部：这张表由谱提供（运行时零 AI）。
- 主线 / 工具、主要的交接由 `djstudio/v5/mainline.py` 按角色分（SCORE_SPEC_V5.md 第 13 节）：只给界面、hot cue、交接说明分类用，不影响声音。
- v5 的 hot cue 不写在谱里，由 `djstudio/v5/hotcue.py` 按谱算（名字、颜色、取舍 = docs/SET_SPEC.md 第 7 节的那一套，点从片段来）；
  SCORE_SPEC_V5.md 第 12 节的效果名字表由 `tests/test_v5_hotcue.py` 和代码（`hotcue.FX_NAME`）比对。
- 改了 `djs5/*.hpp`：跑 `tests/test_v5_engine.py`、`tests/test_v5_live.py`（模拟器 `sim5` 会按源码哈希自动重编），
  DSP 模块另跑 `g++ -O2 -std=c++17 -Wall -Wextra -ffast-math tools/mixxx-engine/djs5/test_dsp.cpp`；有声音引擎时再重编引擎、
  跑 `python packaging/macos/smoke_v5.py <引擎>` 和带 `DJSTUDIO_ENGINE_BIN` 的全量 pytest（引擎是四台 Deck，v4 的引擎测试也要过）。
- 引擎跟着 Mixxx 用 `-ffast-math` 编：调度器和 DSP 里不能用 NaN / 无穷大当「没有」，也不能用 `isnan` / `isfinite`。
- **所有的沿都在音乐上的那个点之前走完**（起 / 停、门、交叉淡化、Release 的收和放）：正好落在点上的鼓头要么完整、要么没有。新加的效果照这个规矩，并用「脉冲正好在点上」的测试钉住。
- 能力表两边要一致：引擎的 `v5.caps` 和 `djstudio/v5/caps.py`（测试比对）。算法、处理组里的顺序、时间常数变了：`kDspVersion` / `kBusVersion` 加一，
  `caps.FX_VERSIONS` 跟着改（存下来的谱按版本提示「声音可能不一样」）。
- 量出来的数字（对齐、每块耗时、分频残差）和没做的事记在 docs/V5_G0.md；只有 Mac 真机才知道的写进 docs/DEVICE_TEST.md 的假设表。

## 把编排方案转成 set 文件

用户每次会给一份编排方案（PDF / 打碟谱 / 文字）。

**所有 set 都按播放规范 [docs/SET_SPEC.md](docs/SET_SPEC.md) 生成**：EQ / Filter / 效果器 / 推子 / 速度 / 音量的取值和换算，
以及 Denon Prime 4+ 格式的 hot cue（8 个打击垫的用途、名字、颜色、位置、中文注释）。生成的 set 写 `"spec": "2"`。
规范的表和代码由 `tests/test_setspec.py` 比对，改一边要同时改另一边。

**用户自己写谱**，按《打碟谱规范》[docs/SCORE_SPEC.md](docs/SCORE_SPEC.md)（Markdown，模板 docs/score_template.md），拖到网页首页自动转换（`djstudio/plan/mdplan.py`）。
规范的表由 `tests/test_score.py` 比对代码，模板必须能解析、换算、编译通过；规范改了升版本号（`mdplan.SCORE_SPEC_VERSION`）。
帮用户把别的格式的方案改写成打碟谱时，照 SCORE_SPEC.md 写 .md，拖进去零错误再交给用户。

**《打碟谱》PDF 模板（如《CDW 技法实战 set 打碟谱》）也由网页首页自动转换**（`djstudio/plan/`，确定性规则，见 docs/SET_FORMAT.md「从打碟谱自动生成」），不需要 Claude 手工转。模板变了就扩展 `djstudio/plan/cdw.py` 并加测试。其它格式的方案按下面的步骤手工转：

1. 在曲库里找到每首歌的 id（`GET /api/tracks` 或网页曲库）；找不到的先让用户导入。
2. **小节号以本应用的波形为准**（第 1 小节 = 曲中最早的小节线，小节线由用户确认）。方案若用别的编号，先对齐，并在 note 里写明。
3. 每段过渡选手法：
   - 方案是长时间叠加、渐变 EQ、不拉推子 → `longhand`，按方案给 `start`/`b_in`/`bars`，需要时调 params；
   - drop 对 drop、硬切、"抽真空" → `instant`，`b_in` = B 的 drop 小节；
   - 方案逐小节（如每 4 小节）写了旋钮位 → `custom`，把旋钮位逐点写进 lanes（dB / 0..1 / -1..1），切点用 `"step"`。
4. 主控 BPM：方案有爬坡计划就写 `play_bpm` 或 `tempo`；否则留默认。
5. hot cue 按 docs/SET_SPEC.md 第 7 节（v2：打击垫没有固定用途，每首按这套 set 放 IN / 处理点 / OUT / STOP，按时间从 1 号垫排）：
   网页生成的 set 会自动填；手工转的 set 写上 `"spec": "2"`，在网页 Set 页保存时空着的垫子自动放（`Studio.fill_hot_cues`，
   `auto: false` 的垫子不动）。用户说过：不要给垫子定固定用途，set 只关心进、出和中间要处理的点。
6. 写好后跑 `djstudio validate <file>`（或网页实时校验）到零错误，再交给用户。
7. 格式细节见 docs/SET_FORMAT.md，取值规范见 docs/SET_SPEC.md。

## 研究结论（选手法时参考）

- 职业过渡分两类：长交接 25–57 小节（中位 39），不杀低频，低频凹陷 0.4–6.5 dB；瞬时切换 0–6 小节（中位 2），常配 breakdown / 真空落点。
- A 的 HIGH 从 B 的 kick 进场起每小节缓降 ≈0.19 dB；出曲推子不拉；推子与 EQ 解耦（半音量 → 满音量两级）。
- **不要用 kick 相似度自动决定接法**（实测相关只有 +0.19）。
