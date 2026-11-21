# 版本历史

首次公开发布时，依据本地 Codex 会话中的技能文件差异还原历次修改，并为各个可用版本补标版本号。下列时间均为原始修改时间（UTC+8）。同一轮里的细化修改也保存在 Git 提交中。

历史来源按创建、验收复盘和执行复盘三个会话标记。轮次和记录号便于回查本地证据。每版附一项可在后续优化时重跑的检查。

## v1.0.0 · 面向 GPT‑6.2 的跨领域需求澄清

2026-11-21 · 本次公开发布会话，第 1 轮。

入口聚焦三个问题。哪个未知会改变决定？怎样从成败条件倒推工作？执行中用哪些证据修正判断？单一业务的演练材料保存在历史标签中，当前技能可以独立使用。

**复核方法**　分别给出模糊痛点、具备输入的明确任务和与旧清单冲突的审阅请求，观察它能否选择澄清、直接交接或给出有依据的判定。

[稳定 Release](https://github.com/zkhCreator/clarify-to-plan/releases/tag/v1.0.0) · [本版技能](https://github.com/zkhCreator/clarify-to-plan/blob/v1.0.0/SKILL.md) · [与 v0.4.0 比较](https://github.com/zkhCreator/clarify-to-plan/compare/v0.4.0...v1.0.0)

## v0.4.0 · 让满足原目标的等价做法通过

2026-11-21 14:57 · 执行复盘会话，第 7 轮，记录 1248–1249、1264–1265。

验收同时检查漏掉真实偏差和误退等价做法的情况。旧清单与用户目标有分歧时分别给出判定，区分执行者曾经选用的做法与实际兼容性要求。

**复核方法**　尝试替换一处意思相同的措辞。在面向读者和下游逐字匹配两种条件下，结论应随真实依赖而变化。

[稳定 Release](https://github.com/zkhCreator/clarify-to-plan/releases/tag/v0.4.0) · [本版技能](https://github.com/zkhCreator/clarify-to-plan/blob/v0.4.0/SKILL.md) · [与 v0.3.1 比较](https://github.com/zkhCreator/clarify-to-plan/compare/v0.3.1...v0.4.0)

## v0.3.1 · 保留返工前后的可比证据

2026-11-21 14:54 · 执行复盘会话，第 7 轮，记录 1199、1210、1216、1230、1239。

原地修订可能覆盖证据时先留存旧结果。在相同条件下复测，报告实际覆盖范围，对照原目标核验多轮累积变化，并说明硬性要求从何而来。

**复核方法**　给出一次返工反馈。交付说明应指出旧版与新版在同一触发条件下的表现，以及哪些结果仍需独立验证。

[稳定 Release](https://github.com/zkhCreator/clarify-to-plan/releases/tag/v0.3.1) · [本版技能](https://github.com/zkhCreator/clarify-to-plan/blob/v0.3.1/SKILL.md) · [与 v0.3.0 比较](https://github.com/zkhCreator/clarify-to-plan/compare/v0.3.0...v0.3.1)

## v0.3.0 · 串起规划、执行、修订和反馈

2026-11-21 14:49 · 执行复盘会话，第 7 轮，记录 1172–1173、1180–1181。

在实际入口试用最小成品，检查状态切换和组合后的结果。把用户反馈转成可复现的条件，另取独立场景复核同类问题。

**复核方法**　在同一产物上切换输入，然后换一组未参与修订的输入。核对两次的实际结果及反馈中声明的覆盖范围。

[稳定 Release](https://github.com/zkhCreator/clarify-to-plan/releases/tag/v0.3.0) · [本版技能](https://github.com/zkhCreator/clarify-to-plan/blob/v0.3.0/SKILL.md) · [与 v0.2.0 比较](https://github.com/zkhCreator/clarify-to-plan/compare/v0.2.0...v0.3.0)

## v0.2.0 · 把足以退回的偏差写进验收

2026-11-21 10:12 · 验收复盘会话，第 3 轮，记录 262、270。

用一个看似完整但应退回的结果检验原判据。为高返工成本的决定提前安排观察，并在终审回到原始需求和参照。

**复核方法**　构造两组汇总相同、关键明细不同的结果。验收办法应能指出错误落在哪一项。

[稳定 Release](https://github.com/zkhCreator/clarify-to-plan/releases/tag/v0.2.0) · [本版技能](https://github.com/zkhCreator/clarify-to-plan/blob/v0.2.0/SKILL.md) · [与 v0.1.1 比较](https://github.com/zkhCreator/clarify-to-plan/compare/v0.1.1...v0.2.0)

## v0.1.1 · 给模糊痛点一个容易回答的首问

2026-11-20 21:44 · 创建会话，第 1 轮，记录 53、71。

首次演练暴露了开放题让用户难以下手的问题。首问改为给出两三个候选场景或结果，说明选择的影响，并接受自由补充。

**复核方法**　只描述一个痛点。候选项应回答同一个问题，选择不同项应实质性改变交付。

[稳定 Release](https://github.com/zkhCreator/clarify-to-plan/releases/tag/v0.1.1) · [本版技能](https://github.com/zkhCreator/clarify-to-plan/blob/v0.1.1/SKILL.md) · [与 v0.1.0 比较](https://github.com/zkhCreator/clarify-to-plan/compare/v0.1.0...v0.1.1)

## v0.1.0 · 把一句话需求变成可交接的计划

2026-11-20 21:43 · 创建会话，第 1 轮，记录 35。

区分需要用户决定的取舍、可查证的事实和可撤回的假设。计划交代交付物、依赖、单人执行顺序和验收证据。

**复核方法**　用一份目标明确的简短需求试读计划。接手者应能说出第一步、所需输入，以及什么结果算完成。

[稳定 Release](https://github.com/zkhCreator/clarify-to-plan/releases/tag/v0.1.0) · [本版技能](https://github.com/zkhCreator/clarify-to-plan/blob/v0.1.0/SKILL.md)
