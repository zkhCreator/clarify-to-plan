# clarify-to-plan benchmark

这套回归题比较无 skill、发布标签 v1.0.0 和本轮开始时的 main。冻结的提交、模型、推理档位、重复次数及裁判指令见 [protocol.json](protocol.json)。[24 道题](cases.json) 分属澄清、单人交接、等价做法、证据范围、试行门槛和任务承接六个题族。题目公开后可用于回归，后续泛化测试需另留新题。

**当前质量结果**采用修正模拟用户后的 216 份答卷。Astra 判定无 skill、v1.0.0、main 分别通过 59/72、71/72、70/72。跨家族裁判 Qwen 对三组均判 72/72。每份答卷都可以回溯到冻结单轮或复用原始首轮后的新第二轮。[修订报告](results/corrected-2026-11-22/report.md) · [逐题判定](results/corrected-2026-11-22/flips.csv) · [无上下文独立复核](REVISION_REVIEW.md)

首轮包含 216 份模型答卷。[2026-11-22 实测](results/codex-2026-11-22/report.md) · [模拟用户复核与结果解读](results/codex-2026-11-22/review.md)

同一批答卷另由 Qwen3‑8B 独立盲评，逐题比较换裁判后的变化，并抽样交人工复核。[跨模型家族盲评](CROSS_FAMILY.md)

从完整 Git 克隆的仓库根目录可以离线复算首轮结果。

~~~sh
python3 benchmark/report.py --verify benchmark/results/codex-2026-11-22
~~~

## 复算修订结果

以下校验从冻结的 216 份原始记录、36 条替换和两套盲评记录重建结果。它核对原始 Astra 评分表、Qwen 实际判卷记录、固定的重读名单、第二轮用户信息、盲号与仲裁分配。离线复算使用 Python 标准库和包含两个历史 skill 提交的 Git 克隆。

~~~sh
python3 benchmark/corrected_report.py --verify benchmark/results/corrected-2026-11-22
~~~

单独复做这次实验时，第一条命令在新线程恢复模型可见的首轮用户和助手原文，各生成一次第二轮；第二条命令由本地固定权重的 Qwen 评分。历史模拟器保留用于复算首轮实验。修订版按已核对的原答复哈希及直接提问的引文披露事实，遇到新答复会要求补充判定。[模拟用户披露表](second_turn_disclosures.json)

~~~sh
python3 benchmark/second_turn.py /tmp/clarify-second-turn --codex /path/to/original/codex
HF_HUB_OFFLINE=1 python3 benchmark/second_turn_qwen.py /tmp/clarify-second-turn /path/to/pinned-qwen-weights /tmp/clarify-second-qwen
python3 benchmark/corrected_report.py /tmp/clarify-second-turn /tmp/clarify-second-qwen /tmp/clarify-second-report
~~~

## 原始实验口径

- 每个“题目 × 重复 × 版本”独立开一个临时 Codex 对话。按题目与重复组成配对区组，区组及组内版本顺序使用固定随机种子打乱。重复编号用来配对，模型本身仍可产生不同答复。
- 关闭当前可发现的所有技能并检查结果，使用空工作目录和空环境，拒绝工具调用。无 skill 组只接收公共指令。另外两组在相同位置注入固定提交的 SKILL.md 及其引用的 Markdown 文件，保存各文件和实际注入文本的 SHA-256。这测量加载后的文字任务能力。自动发现和按需打开参考文件属于另一种实验。
- Q01–Q04 为两轮题。用户模拟器按预先登记的问句和同义词匹配透露对应事实，分别记录实际问句和已透露字段。没有问到的字段保持未知。第二轮与第一轮在同一对话内。其余题一次交付。模拟器为有限词表，复核者可查看完整对话判断是否漏配。
- 每份有效答卷按本题三项判据运行两次独立 Codex 盲评。判据全部通过才算该份达标。裁判各用独立顺序与盲号，看不到版本、重复号、耗时或同伴判定。任务通过判定相反时，另开第三次盲评作多数裁决。四份校准答案和每位裁判十二次重复阅读单独计分。
- 按版本给出有效答卷通过率，同时保留技术失败、首试和重试的总数。逐题列出三次重复、跨版本同次翻转、三次多数结论及同版本重复间分歧。裁判一致性报告裁决前的同意数、分母和 Cohen’s κ。各模型裁判取样自同一模型家族，人工裁判一致性须另测。
- 单轮耗时从客户端发起 turn/start 计到 turn/completed，多轮任务加总实际对话等待。原始记录同时保留每次尝试的墙钟时间和客户端可见用量。Codex 模式没有分配可验证的 API 冷写或预热条件，也没有账单。冷热时间、冷热差值及金额在结果中用 null 表示。

## 运行

需要 macOS 或 Linux、Python 3.9+、Git，以及已登录并支持目标模型和所用 app-server 协议的 Codex。运行目录由使用者选择，内含可续跑的逐次尝试。建议将运行目录设在仓库之外。

~~~sh
python3 benchmark/run.py /tmp/clarify-benchmark --codex /path/to/codex
python3 benchmark/grade.py /tmp/clarify-benchmark --codex /path/to/codex
python3 benchmark/report.py /tmp/clarify-benchmark /tmp/clarify-benchmark-report
python3 benchmark/report.py --verify /tmp/clarify-benchmark-report
python3 -m unittest discover -s benchmark
~~~

运行前检查实际模型和技能禁用状态。每个技术失败最多额外尝试一次，保留原始失败。语义错误不触发重试。已完成的样本在续跑时直接读取。不同的模型、题集、指令、实验代码或采集并发数需要新的运行目录。

盲评会以新盲号和不同顺序打包，每个包中同一题最多出现一次。先完成所有题目的生成，再开始盲评。报告可直接从已公布的材料离线复算，无需登录 Codex。

如果多轮模拟器暴露出偏差，可以独立复核同一批答卷的首轮。该复核仅向裁判发送用户原问、第一轮答复及该题预先冻结的首轮判据。两次独立评判分歧时，另开第三次判定。生成的附加记录单独保存。

~~~sh
python3 benchmark/first_turn.py /tmp/clarify-benchmark-report --run /tmp/clarify-first-turn --codex /path/to/codex
cp /tmp/clarify-first-turn/first-turn.json /tmp/clarify-benchmark-report/
python3 benchmark/report.py /tmp/clarify-benchmark /tmp/clarify-benchmark-report
python3 benchmark/report.py --verify /tmp/clarify-benchmark-report
~~~

## 受控 API 缓存测量

按 252 个自然输入轮次及 9 个独立长目录对照单元建档。服务端校准后，符合条件的单元分配显式无缓存、冷写和热读。即使中断，报告仍保留已计划但尚未送出的单元。耗时和金额配对须核对实际模型标识、服务档位及预热记录。[实验方法与采集口径](API_CACHE.md) · [本轮冷热实测与费用](CACHE_RUN.md)

本轮 615 个正式请求均取得终态回执，177 次热组预热全部完成。严格冷热配对覆盖 173/177。机器可读材料和报告可直接离线复算。

~~~sh
python3 -B benchmark/api_cache_report.py --verify benchmark/results/responses-cache-corrected-2026-11-22
~~~
