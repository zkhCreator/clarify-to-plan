# Responses API 缓存测量

本轮在 GPT‑6.2 Astra Standard 短上下文档位重放 24 道公开题的 216 份任务、252 轮输入。多轮回放复用 Codex 首轮答复，第二轮用户输入取自已核定的披露表。另有 9 个独立的长目录对照单元。耗时与用量用相同的 API 输入配对。原 Codex 盲评仍属于原答卷。[题目与原始答卷](results/codex-2026-11-22/subjects.jsonl)

## 干预与采集

- 自然前缀由公共说明和指定技能包组成。先请求服务端前缀 token 计数，对至少 1,024 token 的前缀用单独缓存键试写。首次试写成功、零读取、写入至少 1,024 token 且零输出时，用实际写入数固定断点长度。长度不足与资格未核实分别记账，两者都只运行显式无缓存对照。另用同一份 128 条纸本目录资料对三版本各跑 3 次。这一独立长背景实验有自己的结果表。[OpenAI 缓存说明](https://developers.openai.com/api/docs/guides/prompt-caching)
- 每个题目、版本、重复与对话轮次分配独立的实验条件。显式无缓存不设置断点。冷写和热读在同一开发者消息的末尾设置同一断点，正式请求的全部可见输入一致。各条件顺序随机，独立缓存键包含运行、轮次和条件标识，每个热组预热完成后即作一次正式读取。并发为 4，推理档位为 medium，输出上限 1,024 token。
- 记录每次尝试的 HTTP 状态、开始时间、首个可见文本、完整等待、实际模型与服务档位、读写用量及输出长度。超时、暂时限流和 5xx 最多补试一次。全部尝试归入所分配的组。永久访问或协议错误停止后续调用。进程中断时尚在途的请求单列为结果未知。采集文件只收录公开输入哈希、观测值和受限的错误类别。
- 核算时扣除已按读取或写入计价的输入 token，按各 TTL 分别计写入，输出 token 计一次。价格快照为 2026-11-22 的 Standard 短上下文价。预热费用列在热组并按实际一次复用摊销。额度、账单折扣和发票须由项目账单另行核实。默认 API 入口的实际处理区域不可见。[固定的费率表](api_cache_rates.json) · [OpenAI 价格](https://developers.openai.com/api/docs/pricing)

## 分母和配对校验

启动时固定 261 个单元及其输入指纹，在正式回放前持久化根据校准结果生成的完整分配。报告分别列出计划、已尝试、首试完成和最终完成数。即使一次正式请求尚未发出，各无缓存组的计划数也可重建。校准前冷写和热读的分母待定。校准不合格的前缀单列。旧格式若只留下已尝试单元，缺少可验证的计划总体，新报告器会指出分母无法恢复。

每个耗时与费用差值都要求两边实际返回相同的模型标识和所请求的服务档位。热读还配对自己的预热请求。未知身份、实际模型快照或档位不同的记录计入计划分母，并列出数量。完整命中分析还要求预热写入和正式读取均达到服务端标定的断点。用量按冻结的 Standard 费率估算，账单金额待项目账单核对。[模型价格](https://developers.openai.com/api/docs/models/gpt-6.2-astra?site_locale=en)

## 本轮回执

本轮完成 615 个正式请求及 177 次热组预热。正式组包括 261 个显式无缓存请求、177 个冷写和 177 个热读，另有接入预检、六次前缀计数与五次资格试写。可核算费用以回执 token 按冻结费率逐项相加。[实测结果](CACHE_RUN.md) · [脱敏回执](results/responses-cache-corrected-2026-11-22/ledger.jsonl)

## 本机运行

在你自己已配置 OpenAI API 认证的终端运行以下命令。采集和离线复算均使用 Python 标准库。采集支持用户环境中的 `OPENAI_API_KEY`，也支持用 `--key-stdin` 从本机凭据工具的私有管道传入。工作目录放在仓库外。

~~~sh
python3 benchmark/api_cache.py /tmp/clarify-api-cache-run
python3 benchmark/api_cache_report.py /tmp/clarify-api-cache-run /tmp/clarify-api-cache-report
python3 benchmark/api_cache_report.py --verify /tmp/clarify-api-cache-report
~~~
