# clarify-to-plan

一个面向 GPT‑6.2 的 Codex skill。如果你知道哪里不顺，却还说不清要改变什么，或者已经有了方向，却不知道做到什么程度才算完成，可以用它把这件事整理成可交接的计划。

它先找出会改变结果的那个未知。事实能查就查，真正的取舍交给用户，可撤回的细节由执行者说明假设后继续。落到计划时，接手者能找到第一步和所需输入，验收者能说出看到什么应该通过、出现什么偏差应该退回。[阅读技能指令](SKILL.md)

## 在 Codex 中使用

在目标项目的根目录执行下面两行，将公开稳定版安装为项目技能。[Codex 的技能目录说明](https://learn.chatgpt.com/docs/build-skills)

```sh
mkdir -p .agents/skills
git clone --branch v1.0.0 https://github.com/zkhCreator/clarify-to-plan.git .agents/skills/clarify-to-plan
```

在 GPT‑6.2 会话里，可以这样写自己的需求。

> `$clarify-to-plan` 我希望改善……。请先找出哪个取舍会影响结果，再给我一份可以交给别人执行和验收的计划。

只要计划时，技能会交付执行顺序和验收办法。已经要求实施时，它会沿用原授权来执行和复验。模型由当前会话选择。[GPT‑6.2 官方指南](https://developers.openai.com/api/docs/guides/latest-model?model=gpt-6.2-astra)

## 版本

每个发布版本用一个带注释的 Git 标签固定到对应提交，并有独立的 GitHub Release。修改记录注明修改原因、原始会话轮次和复核办法。[版本历史](CHANGELOG.md) · [全部 Releases](https://github.com/zkhCreator/clarify-to-plan/releases)
