# clarify-to-plan benchmark

这套回归题比较无 skill、发布标签 v1.0.0 和本轮开始时的 main。冻结的提交、模型、推理档位、重复次数及裁判指令见 [protocol.json](protocol.json)。[24 道题](cases.json) 分属澄清、单人交接、等价做法、证据范围、试行门槛和任务承接六个题族。题目公开后可用于回归，后续泛化测试需另留新题。

首轮包含 216 份模型答卷。[2026-11-22 实测](results/codex-2026-11-22/report.md) · [模拟用户复核与结果解读](results/codex-2026-11-22/review.md)

同一批答卷另由 Qwen3‑8B 独立盲评，逐题比较换裁判后的变化，并抽样交人工复核。[跨模型家族盲评](CROSS_FAMILY.md)

从完整 Git 克隆的仓库根目录可以离线复算首轮结果。

~~~sh
python3 benchmark/report.py --verify benchmark/results/codex-2026-11-22
~~~

## 实验口径

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

## API 缓存对照待测项

本轮已实现并运行 Codex 质量对照。Responses API 的受控实测留待有该入口时执行。后续采集沿用以下口径。

1. 冻结版本、题目、模型、推理与服务档位、区域、并发度和价格生效日期。对同一版本、题目和重复，保存各条件下模型可见输入的相同哈希。多轮性能回放固定历史和用户回复，使各条件所处理的内容一致。独立对话的质量对照另行评分。
2. 无缓存质量对照使用 `prompt_cache_options.mode = explicit` 且不设置断点，校验读写计数均为零。冷写和热读均将同一个显式断点放在首个开发者消息中稳定内容的末尾。先核实该前缀达到模型的 1,024 个可见输入 token 下限。短前缀标记为不适用，保持原题长度。若另设统一长背景，作为单独的性能实验报告。[OpenAI 缓存说明](https://developers.openai.com/api/docs/guides/prompt-caching)
3. 同一区组随机安排冷写与预热组的发送顺序，分配独立的缓存键，组内只复用自己的键和前缀。冷写要求 `cached_tokens = 0` 且 `cache_write_tokens > 0`。热组先等预热请求完成，再发送同前缀的正式请求。校验正式请求读到目标断点的长度且没有新写入。未命中、部分命中、超时及重试留在原分配中，另列完整命中子集。[OpenAI 缓存说明](https://developers.openai.com/api/docs/guides/prompt-caching)
4. 分别记录首个可见 token 和完整响应的墙钟时间。热组同时报告用户等待、单次预热的时间与费用、以及按实际复用次数摊销的费用。成本按普通输入、缓存读取、各 TTL 的写入及输出分项，使用运行当天该模型和服务档位的价格。注明按实际用量与价格核算的数额及账单核对状态。没有用量或账单依据的金额保留为空。[OpenAI 用量与成本口径](https://developers.openai.com/api/docs/guides/prompt-caching)
