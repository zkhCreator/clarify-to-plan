# 跨模型家族盲评

在原有 24 道回归题上，复用无 skill、v1.0.0 和 main 各 72 份完整答卷。原裁判为 GPT‑6.2 Astra，新裁判为 Qwen3‑8B 的官方 MLX 4-bit 模型。两家模型看到的题面、作答和三项判据相同。每项均通过才计作一次任务通过。[冻结协议](cross_family_protocol.json) · [原始答卷](results/codex-2026-11-22/subjects.jsonl) · [Qwen 固定权重](https://huggingface.co/Qwen/Qwen3-8B-MLX-4bit/tree/383413e909f3bc5303ce195ebbdf0339c5a1a2a3)

## 阅读结果

Qwen 将 216 份正式答卷全部判为通过。Astra 最终通过 200 份，所以两者同判 200/216（92.6%），Cohen’s κ 为 0。Qwen 的 12 份重读全部同判，但 4 份校准样例只判对 2 份，一份误放、一份误拒。在这组答卷上，Qwen 的通过率没有区分三个版本。人工复核结果待回收。

报告给出各组通过数、同一答卷换裁判后的升降、同题同次重复换 skill 后的升降，以及两家判卷的混淆矩阵和 Cohen’s κ。逐题表保留三次独立重复，分歧可追溯至各条判据。参加本轮人工复核的读者可先填交另外收到的匿名盲审包，再打开逐题记录。[对照报告](results/cross-family-2026-11-22/report.md) · [逐题翻转](results/cross-family-2026-11-22/flips.csv)

## 判卷条件

- 原来的 A、B 两次独立阅读在任务结论上分歧时，请同家族的 C 仲裁。Qwen 对每份原答卷提供一次主判。额外随机重读原来抽取的 12 份答卷，并混入原来 4 份带预设结论的校准样例。这 16 次阅读单独计分。两家模型的一致性以同一答卷配对，并分别与 A、B 及 Astra 最终结论比较。
- Qwen 的每个对话上下文只有一个随机盲号、本题判据、原用户消息和完整原答。四道互动题保留模拟用户当时实际给出的补充和答题模型的第二轮回答。MLX 在本机并行处理不同上下文，每份有独立注意力缓存。判卷端关闭联网，校验原始材料和模型文件指纹。解码使用温度 0.7、top-p 0.8、top-k 20，关闭 Qwen 思考模式，每次至多生成 384 个 token。
- 冻结顺序、抽样和协议后再开始推理。仅输出无法解析或缺少所需字段时，按预设规则允许一次技术重试。已得到的有效分数立即锁定。尝试记录保留输入、输出指纹、用量和格式验证状态。[新裁判投票](results/cross-family-2026-11-22/votes.jsonl) · [逐次尝试](results/cross-family-2026-11-22/attempts.jsonl)
- 本轮比较两套实际判卷流程。模型大小、量化、解码方式和主判次数随流程一起变化。两者共用同一套公开回归题与判据。人工复核完成前，版本排名仅描述这些答卷在相应裁判下的表现。

## 人工复核

先收录全部发生过分歧的原答卷。原 A、B、可用的仲裁 C、新模型主判和可用的新模型重读，任一项判据不同均算分歧。再从全部同判的答卷中随机抽 24 份，三个版本各以 8 份为目标，每组尽量包含 2 份一致未通过的答卷。抽样不放回。各来源混排并重新生成盲号。人工填写每项是否达到判据及可选备注。评完后凭独立保存的对照表还原匹配。由于分歧答卷全收、同判答卷抽样，盲审包内的占比只描述该包。

本轮实际交付 42 份。16 份是 Qwen 与 Astra 最终结论不同，另 2 份的 Astra 初评有过分歧。其余 24 份从全体裁判同判通过的答卷随机抽取，每个版本 8 份。该轮一致未通过的答卷为 0 份。

要给另一位评审者制作独立盲号，在仓库外建立两个独立目录。评审者只需收到第一个目录，打开其中的 `index.html`，随时可导出 JSON 进度或导入上次的文件。第二条命令由保管对照表的人在收到人工导出后运行。三项尚未填齐的答卷标为待完成。

~~~sh
python3 benchmark/human_review.py benchmark/results/cross-family-2026-11-22/votes.jsonl ../reviewer-copy ../review-mapping
python3 benchmark/human_review.py reconcile /path/to/human-review-answers.json ../review-mapping
~~~

## 复算与重跑

从完整克隆的仓库根目录运行。前两条命令只使用 Python 标准库，复核映射、用量记录中的输入指纹及报告统计。最后一条命令用于在具备 Apple Silicon、MLX 和本地模型权重的环境中重新推理。实测依赖版本见运行清单。[环境清单](results/cross-family-2026-11-22/run.json)

~~~sh
python3 benchmark/cross_family_judge.py --verify benchmark/results/cross-family-2026-11-22
python3 benchmark/cross_family_report.py benchmark/results/cross-family-2026-11-22/votes.jsonl /tmp/clarify-cross-report
HF_HUB_OFFLINE=1 python3 benchmark/cross_family_judge.py /path/to/pinned-qwen-weights /tmp/clarify-qwen-run
~~~
