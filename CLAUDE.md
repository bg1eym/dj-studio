# DJ Studio — 给 Claude 的说明

- 用户 Qiang 用**中文**交流；在学 techno DJ，现场碟机 Denon Prime 4+。界面文案、日志、文档都用中文。
- 这是 Mixxx 驱动的自动打碟 Web App。先读 README.md 和 docs/ARCHITECTURE.md。
- **默认是内置引擎**（`DJSTUDIO_ENGINE=builtin`）：DJStudio.js 在内置的模拟 Mixxx 里跑，不碰用户本机的 Mixxx。用户明确说过：
  现在只要看 EQ / 效果器怎么变，不要调用本机的 Mixxx（不稳定）。连 Mixxx 的模式保留（`DJSTUDIO_ENGINE=mixxx`），别改回默认。
- **运行时零 AI 依赖**是硬约束：分析、编译、执行必须是确定性代码，结果能在网页上手动改。

## 开发

- 测试：`pytest`（需要 node 和 ffmpeg）。改了引擎脚本、协议、conductor 后必须跑 `tests/test_sim_e2e.py`。
- 改 `DJStudio.js` 时同步改 `protocol.py` / `sim/fake_mixxx.mjs`，并把 `DJStudio.VERSION` 加一。脚本只用 ES5。
- 涉及 Mixxx 行为的判断**先查 Mixxx 源码**（docs/MIXXX_NOTES.md 记录了已核对的结论和行号），不能凭印象；平台行为（进程名、MIDI 口名、CoreMIDI）还**必须真机验证**：模拟器是按我们的理解写的，测过不代表 Mixxx 真这样。不确定的地方写进 docs/DEVICE_TEST.md 的假设表。
- 演示模式 `djstudio demo` 可以在没有 Mixxx 的环境里跑完整流程（含网页）。

## 把编排方案转成 set 文件

用户每次会给一份编排方案（PDF / 打碟谱 / 文字）。

**《打碟谱》模板（如《CDW 技法实战 set 打碟谱》）由网页首页自动转换**（`djstudio/plan/`，确定性规则，见 docs/SET_FORMAT.md「从打碟谱自动生成」），不需要 Claude 手工转。模板变了就扩展 `djstudio/plan/cdw.py` 并加测试。其它格式的方案按下面的步骤手工转：

1. 在曲库里找到每首歌的 id（`GET /api/tracks` 或网页曲库）；找不到的先让用户导入。
2. **小节号以本应用的波形为准**（第 1 小节 = 曲中最早的小节线，小节线由用户确认）。方案若用别的编号，先对齐，并在 note 里写明。
3. 每段过渡选手法：
   - 方案是长时间叠加、渐变 EQ、不拉推子 → `longhand`，按方案给 `start`/`b_in`/`bars`，需要时调 params；
   - drop 对 drop、硬切、"抽真空" → `instant`，`b_in` = B 的 drop 小节；
   - 方案逐小节（如每 4 小节）写了旋钮位 → `custom`，把旋钮位逐点写进 lanes（dB / 0..1 / -1..1），切点用 `"step"`。
4. 主控 BPM：方案有爬坡计划就写 `play_bpm` 或 `tempo`；否则留默认。
5. 写好后跑 `djstudio validate <file>`（或网页实时校验）到零错误，再交给用户。
6. 格式细节见 docs/SET_FORMAT.md。

## 研究结论（选手法时参考）

- 职业过渡分两类：长交接 25–57 小节（中位 39），不杀低频，低频凹陷 0.4–6.5 dB；瞬时切换 0–6 小节（中位 2），常配 breakdown / 真空落点。
- A 的 HIGH 从 B 的 kick 进场起每小节缓降 ≈0.19 dB；出曲推子不拉；推子与 EQ 解耦（半音量 → 满音量两级）。
- **不要用 kick 相似度自动决定接法**（实测相关只有 +0.19）。
