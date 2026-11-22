# Responses API 缓存测量

本轮在 GPT‑6.2 Astra Standard 短上下文档位重放 24 道公开题的 216 份任务、252 轮输入。历史答复和用户补充均取自固定的首轮 Codex 记录。耗时与用量用相同的 API 输入配对。原 Codex 盲评仍属于原答卷。[题目与原始答卷](results/codex-2026-11-22/subjects.jsonl)

## 干预与采集

- 自然前缀由公共说明和指定技能包组成。先请求服务端前缀 token 计数，对至少 1,024 token 的前缀用单独缓存键试写。首次试写成功、零读取、写入至少 1,024 token 且零输出时，用实际写入数固定断点长度。长度不足与资格未核实分别记账，两者都只运行显式无缓存对照。另用同一份 128 条纸本目录资料对三版本各跑 3 次。这一独立长背景实验有自己的结果表。[OpenAI 缓存说明](https://developers.openai.com/api/docs/guides/prompt-caching)
- 每个题目、版本、重复与对话轮次分配独立的实验条件。显式无缓存不设置断点。冷写和热读在同一开发者消息的末尾设置同一断点，正式请求的全部可见输入一致。各条件顺序随机，独立缓存键包含运行、轮次和条件标识，每个热组预热完成后即作一次正式读取。并发为 4，推理档位为 medium，输出上限 1,024 token。
- 记录每次尝试的 HTTP 状态、开始时间、首个可见文本、完整等待、实际模型与服务档位、读写用量及输出长度。超时、暂时限流和 5xx 最多补试一次。全部尝试归入所分配的组。永久访问或协议错误停止后续调用。进程中断时尚在途的请求单列为结果未知。采集文件只收录公开输入哈希、观测值和受限的错误类别。
- 核算时扣除已按读取或写入计价的输入 token，按各 TTL 分别计写入，输出 token 计一次。价格快照为 2026-11-22 的 Standard 短上下文价。预热费用列在热组并按实际一次复用摊销。额度、账单折扣和发票须由项目账单另行核实。默认 API 入口的实际处理区域不可见。[固定的费率表](api_cache_rates.json) · [OpenAI 价格](https://developers.openai.com/api/docs/pricing)

## 本机运行

在你自己已配置 OpenAI API 认证的终端运行以下命令。采集和离线复算均使用 Python 标准库。采集支持用户环境中的 `OPENAI_API_KEY`，也支持用 `--key-stdin` 从本机凭据工具的私有管道传入。工作目录放在仓库外。

~~~sh
python3 benchmark/api_cache.py /tmp/clarify-api-cache-run
python3 benchmark/api_cache_report.py /tmp/clarify-api-cache-run /tmp/clarify-api-cache-report
python3 benchmark/api_cache_report.py --verify /tmp/clarify-api-cache-report
~~~
