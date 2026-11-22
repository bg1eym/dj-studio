"""曲谱 v5 / djset/3：四 Deck 编排——素材、片段、场景、全场速度图、参数变化、Loop、效果。

- pos：位置（「小节.拍」「16.4+1/2」）和时值（「8拍」「2小节」「1/2拍」），内部是有理数拍
- model：djset/3 项目模型（人读单位，和 djset/2 的曲线同一套：EQ dB、Filter -1..1、推子 0..1）
- score：v5 Markdown 谱 → 项目（报行号；错误和提示分开）
- legacy：v1–v4 的 set（djset/2）→ 项目，以及和旧编译结果的等价检查
- compile：项目 + 网格 → 执行计划（djplan/1，交给引擎里的原生调度器）、整场图谱数据、任意拍的状态
- caps：引擎能力表与能力签名
- runner：演出时的指挥（准备、开始、回执、故障、接管、练习起点）
- package：文件夹导入、演出包导出

规范：docs/SCORE_SPEC_V5.md；引擎协议：docs/ENGINE5.md。
"""
