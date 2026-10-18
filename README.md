# Streaming Agent Gateway（Python）

这是 Agent 课程的学习仓库，包含 Python 练习、原始课件、清楚教学 skill 与可跨电脑继续的学习进度。

- **换电脑继续学习**：先看 [继续学习.md](继续学习.md)，里面有启动命令和可直接发给 Codex 的续学提示词。
- **当前进度**：[learning/进度.md](learning/进度.md)，停在课件第 20 / 46 页，有一道待回答问题。
- **教学方式**：[.agents/skills/teach-clearly/SKILL.md](.agents/skills/teach-clearly/SKILL.md)，完整复制原有 skill 与配套资料。
- **课件**：[1-2-Streaming 流式输出.pdf](materials/1-2-Streaming%20流式输出.pdf)。

按课后练习顺序实现：可观测、可取消、可恢复、可审计的流式 Agent 网关。后端与 CLI 均使用 **Python 3.11+**，阿里云百炼通过 `httpx` 直接发起 HTTP 请求，**不依赖 OpenAI / DashScope 模型 SDK**。

图片中“IMPLEMENT ORDER · 11 步”实际仅列出 1–10，本项目对应实现这 10 步。底部“10 标准”仅展示 5 条可辨认要求；未展示的条目不作猜测，下方另列本项目的验收方法。

## 推荐：Docker 一次启动网关与 PostgreSQL

新电脑只需要 Git 和已安装的 Docker Desktop，无需在本机安装 Python 或 Node。首次运行建议使用脚本，自动创建未跟踪的 `.env` 并生成随机数据库密码，默认使用不需要模型密钥的 `demo` 模式：

```powershell
powershell -ExecutionPolicy Bypass -File .\start-docker.ps1
```

打开 <http://127.0.0.1:3000>。使用 `-Check` 可同时运行容器内全部测试。已有本地 `.env` 后也可直接执行 `docker compose up -d --build --wait`。真实模型密钥需在新电脑本地配置，不随 Git 同步。

Compose 包含 `gateway`（Python/FastAPI）与 `postgres`（PostgreSQL 17）两个服务。网关等待数据库健康后才启动；数据库持久化到 `streaming-agent-gateway_gateway-postgres` 命名卷。密钥通过运行时环境变量注入，`.dockerignore` 防止 `.env` 和旧数据进入镜像。PostgreSQL 不向主机开放端口，网关仅绑定本机 3000 端口。

首次向空 PostgreSQL 启动时，会只读导入 `data/gateway.sqlite` 的既有 Run 与事件，保留 ID 和最新安全快照。旧版没有保存的快照历史明确标记为 `legacy`，不会伪造初始/中间历史；新 Run 保存完整的 initial / step / final 快照。

```powershell
docker compose ps                                    # 两个服务应显示 healthy
docker compose logs --tail 50 gateway                # 服务日志
docker compose --profile test run --build --rm tests # 包含真实 PostgreSQL 的测试
docker compose restart gateway                      # 重启网关，保留数据库
docker compose down                                 # 停止并移除容器，保留数据库卷
docker compose up -d --wait                          # 再次启动，历史仍在
```

不要在希望保留历史时加 `down -v`，该参数会删除持久化卷。原有 `start.ps1` 和下面的 Python 入口保留为免 Docker 的开发模式；不要同时启动两个入口占用 3000 端口。

## 页面可见内容

- **数据库状态**：PostgreSQL/SQLite、连接健康、schema 版本、大小、表记录数。
- **Run Store**：运行状态、事件数、执行任务数、模型/工具调用次数、用量、唯一终态计数、父 Run。
- **TTFT**：当前 Run 首个正文延迟，以及最近已完成 Run 的 P50/P95；聚合可能包含导入的历史演示样本，不作为真实模型性能测试。
- **Checkpoint 历史**：每个快照的 ID、step、关联 seq、类型、消息数和完整消息；失败/取消后可选择任意已保存的安全边界创建后续 Run。
- **Replay 与审计**：断开/恢复订阅、从头回放当前 Run、Trace 导出、错误 code/message/retryable。
- **真实表浏览**：runs/events/checkpoints 分页记录与列结构，点击记录打开关联 Run。

具体作业逐项对照见 `docs/作业验收清单.md`，数据库设计见 `docs/数据库与Checkpoint.md`。`docs/docker-verification.json` 保存真实工具调用、显式取消、指定 checkpoint 恢复和容器 down/up 的脱敏验收记录。

## 本地 Python 运行（可选）

已有 `.venv` 并安装依赖时，在项目目录用 PowerShell 运行：

```powershell
.\.venv\Scripts\python.exe -m gateway.main
```

打开 <http://127.0.0.1:3000>。交互式接口文档：<http://127.0.0.1:3000/docs>。

也可运行 `powershell -ExecutionPolicy Bypass -File .\start.ps1`，脚本只对本次进程设置执行策略，不更改系统设置。

在新电脑初始化：

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m gateway.main
```

`requirements-lock.txt` 记录本次验证过的精确依赖版本；需要复现环境时可先安装它，再执行 `pip install -e . --no-deps`。

未配置 `.env` 时默认为 `MODEL_PROVIDER=demo`，无需密钥，也不调用阿里云。项目支持配置地址 `https://maas.qianwenaiapi.com/compatible-mode/v1`，真实密钥仅保存在各台电脑本地的 `.env`。如需无费用演示，将 `MODEL_PROVIDER` 改回 `demo` 并重启。

输入“请计算 12 + 30”，可以看到两轮模型调用、加法工具、checkpoint 和流式答案。演示模式仅用于检查网关行为，不代表所选真实模型的能力或延迟。

## 切换阿里云多模型

```powershell
Copy-Item .env.example .env # 仅首次初始化时使用；已存在 .env 时不要覆盖。
```

编辑 `.env` 后重启服务：

```dotenv
MODEL_PROVIDER=aliyun
DASHSCOPE_API_KEY=你的百炼API_KEY
DASHSCOPE_BASE_URL=https://maas.qianwenaiapi.com/compatible-mode/v1
```

千问 AI 平台密钥配 `maas.qianwenaiapi.com` 地址；百炼北京等地域密钥则配对应的 DashScope 或业务空间域名，不要混用。Base URL 以 `/compatible-mode/v1` 结尾，代码追加 `/chat/completions`。不要使用面向编程工具的 Coding Plan / Token Plan 地址作为本服务后端。

| 模型配置 | 用途 | 思考参数策略 |
| --- | --- | --- |
| `qwen-plus` | 均衡通用对话 | 显式传 `enable_thinking`，默认关闭 |
| `qwen3.8-flash` | 快速对话 | 可切换思考，默认关闭 |
| `qwen3.7-plus` | 通用任务 | 可切换思考，默认关闭 |
| `qwen-max` | 复杂任务 | 不传思考参数 |
| `deepseek-v4.1-flash` | DeepSeek 快速任务 | 可切换思考，默认关闭 |
| `deepseek-v4-pro` | DeepSeek 推理任务 | 可切换思考，默认关闭 |

模型能力配置集中在 `gateway/model.py` 的 `MODEL_PROFILES`。这 6 个 ID 已与配置地址的 `/models` 列表核对，并逐个通过真实文本流式调用。平台列表中没有无版本的 `qwen-turbo`，因此已替换该旧配置。未来能否继续调用取决于账号权限与平台更新。新增同协议模型时添加 `ModelProfile`，设置 `family/thinking_mode/tools`；不同协议的新厂商另实现 `StreamingModel.stream()`。

API Key 保存在服务端 `.env`，已被 `.gitignore` 排除，配置对象的 repr 也隐藏密钥；浏览器不接触密钥。真实接入的脱敏验证记录位于 `docs/models-verification.json`，逐项差异见 `docs/模型协议对照.md`。测试范围包括 6 个型号的简短文本流，以及 Qwen Plus / DeepSeek V4.1 Flash 的工具循环与思考工具循环。

## 按作业顺序阅读

| 步骤 | 实现位置 | 要观察的内容 |
| --- | --- | --- |
| 1. 定义 RunEvent | `gateway/events.py` | 统一事件类型、版本、runId、traceId、seq、错误协议 |
| 2. 实现 SSE 编解码 | `gateway/sse.py` | UTF-8 跨包、CR/LF/CRLF、多行 data、心跳、半帧丢弃 |
| 3. 抽象 StreamingModel | `gateway/model.py` | Protocol、ModelRequest、ModelChunk、模型能力配置 |
| 4. 接入模型 Adapter | `gateway/adapters/aliyun.py` | HTTP 参数映射、SSE 转换、推理/正文分离、工具参数聚合 |
| 5. 实现 Run Store | `gateway/store.py` | 独立 runs/events/checkpoints 表，单调 seq，事务写入、通知订阅者 |
| 6. 实现 Agent Loop | `gateway/agent.py` | 模型 → 工具 → 模型，多步限制，取消与超时 |
| 7. 创建订阅取消接口 | `gateway/api.py` | FastAPI 创建、订阅、取消、重放、恢复、审计 |
| 8. 开发 Web/CLI | `web/`、`gateway/cli.py` | 增量展示、取消、断开/恢复订阅、Trace 导出 |
| 9. 增加异常测试 | `tests/` | 断流、错误帧、取消、超时、恢复、真实 TCP 验收 |
| 10. 数据库与 checkpoint | `gateway/database.py`、`gateway/store.py`、`compose.yaml` | PostgreSQL/SQLite、快照历史、原子事务、持久化卷、进程恢复与单 worker 锁 |

### 重点：适配层怎么读

先读 `docs/适配层详解.md`，再读 `AliyunAdapter.build_payload()` 和 `_decode()`：

```text
用户输入 → RunInput → AgentGateway
                         ↓ ModelRequest
                    StreamingModel 协议
                         ↓
                AliyunAdapter.build_payload
                         ↓ HTTP POST + SSE
              阿里云百炼 /chat/completions
                         ↓ choices[].delta
                 AliyunAdapter._decode
                         ↓ ModelChunk
       Agent Loop → RunEvent → PostgreSQL / SQLite → SSE → Web / CLI
```

平台的 `choices`、`reasoning_content`、`tool_calls[index]`、`finish_reason`、`[DONE]` 都只在适配层处理。Agent 不解析厂商 JSON。Qwen 和 DeepSeek 的文本兼容接口共用一个 Adapter，由 6 个 Profile 控制能力差异。工具循环的思考上下文在内部使用 `reasoning` 字段，发往平台时由 Adapter 转为 `reasoning_content`。

## 四项能力的行为约定

**可观测**：每个 Run 有独立 `runId` / `traceId`；每条持久事件有递增 `seq`。正文、思考、用量、工具与终态均可追踪。TTFT 定义为“Run 创建到首个非空答案 delta”，不把连接成功、心跳、思考过程算作正文首字；另记录 `firstDeltaMs`（首个正文或思考片段）。这是网关端测量，不包含浏览器渲染延迟。页面显示数据库实时计数、模型/工具调用次数、用量、终态数和 TTFT 分位数。

**可取消**：`cancel` 先原子写入 `run.cancelled`，再对独立运行任务调用 `Task.cancel()`。取消传播到 `httpx` 流并关闭上游响应。终态只能从 running 迁移一次，取消后不再追加正文或第二个终态。关闭本地连接不能承诺云端计费系统在同一时刻停止处理。

**可恢复**：

- 网页断开 / 刷新 / 网络断线：Run 继续执行。用 `Last-Event-ID` 或 `after` 从数据库回放，再追上实时事件，**不重新调用模型**。客户端按 seq 去重；SSE 是可重放传输，不承诺网络层 exactly-once。
- 进程崩溃：遗留的 running Run 在启动时记为 `PROCESS_INTERRUPTED`，不偷偷重试；正常关闭服务时，正在执行的 Run 记为 `SERVER_SHUTDOWN`。用户可显式创建新 Run，带 `parentRunId`，从最后完整工具轮次的 checkpoint 继续。
- checkpoint 不包含厂商隐藏状态或半截输出，不能从模型生成的一半 token 精确续传。尚未完成的那一轮会在新 Run 中重新请求，可能产生新的调用费用。初始 checkpoint 为 step 0；工具一轮完成后保存新边界，最终 checkpoint 与 completed 原子提交。
- 当前工具 `add` 是无副作用纯函数。以后若接入转账、发信等工具，必须另加幂等键与工具执行日志，不能直接宣称已有 exactly-once 外部执行能力。
- 每次初始边界、完整工具轮次和最终答案快照都写入独立 `checkpoints` 表。可选择历史快照恢复，子 Run 同时保存 parentRunId 和 sourceCheckpointId；不会修改原 Run。

**可审计**：Trace 分页保存在 PostgreSQL/SQLite，Web 可导出完整 JSON；失败包含 `code/message/retryable`。不自动重试不确定的请求。请求密钥和厂商原始错误不写入公开事件。用户输入、思考与工具结果会持久化在本地数据库。

## 接口

| 方法 | 路径 | 行为 |
| --- | --- | --- |
| GET | `/api/health` | 健康状态、当前 provider |
| GET | `/api/models` | 六个模型的配置、能力与公共接入地址（无密钥） |
| GET | `/api/storage` | 数据库健康、schema、大小、表计数、运行状态分布、TTFT 分位数 |
| GET | `/api/storage/tables/{table}?offset=0&limit=20` | 白名单三表只读分页浏览 |
| POST | `/api/runs` | 创建独立后台 Run |
| GET | `/api/runs` | 最近 50 个 Run |
| GET | `/api/runs/{id}` | 状态、TTFT、游标、checkpoint |
| GET | `/api/runs/{id}/events` | SSE；支持 `Last-Event-ID` 或 `?after=seq` |
| POST | `/api/runs/{id}/cancel` | 幂等取消 |
| POST | `/api/runs/{id}/resume` | 从最新或请求体 checkpointId 指定的历史快照创建子 Run |
| GET | `/api/runs/{id}/checkpoint` | 最近安全步骤快照 |
| GET | `/api/runs/{id}/checkpoints` | 全部快照元数据列表 |
| GET | `/api/runs/{id}/checkpoints/{checkpointId}` | 指定快照的完整消息与来源 |
| GET | `/api/runs/{id}/summary` | 模型/工具次数、用量、终态计数、父 Run 与快照来源 |
| GET | `/api/runs/{id}/trace?after=0` | 最多 256 条事件，返回 nextCursor / hasMore |

创建请求示例：

```json
{"prompt":"计算 12 + 30","model":"qwen-plus","tools":true,"thinking":false}
```

SSE 事件示例（序号仅作格式展示）：

```text
id: 4
event: model.delta
data: {"version":1,"runId":"...","traceId":"...","seq":4,"type":"model.delta","timestamp":"...","data":{"step":0,"text":"你好"}}

```

运行中每 15 秒无事件发送注释心跳。读取到终态后客户端关闭 EventSource，避免浏览器在正常完成后反复重连。非法或超前游标返回错误，不静默跳过事件。

## Python CLI

服务启动后，在另一个 PowerShell 窗口运行：

```powershell
.\.venv\Scripts\python.exe -m gateway.cli run "计算 12 + 30" --model qwen-plus --tools
.\.venv\Scripts\python.exe -m gateway.cli run "解释什么是 SSE" --model qwen3.8-flash
.\.venv\Scripts\python.exe -m gateway.cli run "设计一个事件协议" --model qwen-max
.\.venv\Scripts\python.exe -m gateway.cli run "计算 12 + 30" --model deepseek-v4.1-flash --tools
.\.venv\Scripts\python.exe -m gateway.cli run "解释断线重放" --model deepseek-v4-pro --thinking
.\.venv\Scripts\python.exe -m gateway.cli watch <runId> --after 5
.\.venv\Scripts\python.exe -m gateway.cli cancel <runId>
.\.venv\Scripts\python.exe -m gateway.cli resume <runId>
.\.venv\Scripts\python.exe -m gateway.cli checkpoints <runId>
.\.venv\Scripts\python.exe -m gateway.cli resume <runId> --checkpoint <checkpointId>
.\.venv\Scripts\python.exe -m gateway.cli storage
```

Docker 环境无需本机 Python，命令前缀换成 `docker compose exec gateway python -m gateway.cli` 即可。

`Ctrl+C` 退出当前 CLI 订阅，不取消后台 Run。更换服务地址：`python -m gateway.cli --url http://127.0.0.1:3001 run "你好"`。

## 测试与验收

```powershell
.\.venv\Scripts\python.exe -m ruff check gateway tests
.\.venv\Scripts\python.exe -m pytest -q
```

| 验收点 | 操作与可见结果 |
| --- | --- |
| 首个 delta 快速展示 | 演示模式逐字返回，TTFT 直接显示；真实模型速度由模型服务决定 |
| 不依赖模型 SDK | 检查 `pyproject.toml` 与 AliyunAdapter；仅 httpx 处理 HTTP |
| Run 单一终态 | 反复点击取消或与完成竞态，Trace 只出现一次最终状态 |
| 取消真停止 | 测试确认生成器 finally 与真实 HTTP 响应 aclose 执行，取消后无正文 |
| 断线不重跑 | 点击“断开订阅”再“恢复订阅”，同一个 runId、序号延续、model.started 数不增加 |
| 工具循环 | 输入 `12 + 30` 并启用工具，Trace 出现 tool.started/completed 与第二轮 model.started |
| 协议异常 | 覆盖无效 JSON、HTTP 401/403/429/500、截断、缺失 DONE、工具索引错误 |
| checkpoint 耐久性 | 真实子进程崩溃并重启后保留工具边界，显式 resume 创建新 Run |
| Trace 与 TTFT | 查看时间线、首个答案延迟、Checkpoint，导出 JSON |
| 请求保护 | 验证模型白名单、布尔参数、输入上限、同源限制与数据库单实例锁 |

`tests/test_live.py` 使用临时 SQLite 与空闲端口启动真实 Uvicorn 子进程，不占用演示服务的数据库。测试会主动终止该测试子进程以验证崩溃恢复。

该练习默认绑定本机地址、单进程，最多 32 个同时运行的 Run，每 Run 最多 8 个步骤、100000 个输出字符，默认总超时 120 秒。Compose 使用 PostgreSQL 命名卷，免 Docker 开发入口使用 SQLite WAL；二者共用 Store 和事务测试。同步数据库短事务适合本地练习，没有实现多租户鉴权或多节点调度。反向代理需关闭 SSE buffering。PostgreSQL 使用 session advisory lock，SQLite 使用文件锁，确保一个存储只有一个执行 worker。

## 官方参考

- [千问 AI 平台：API Key 与接入地址](https://platform.qianwenai.com/docs/api-reference/preparation/api-key)
- [千问 AI 平台：选择模型](https://platform.qianwenai.com/docs/developer-guides/getting-started/model-selection)
- [千问 AI 平台：OpenAI Chat API](https://platform.qianwenai.com/docs/api-reference/chat/openai-chat)
- [千问 AI 平台：思考模式](https://platform.qianwenai.com/docs/developer-guides/text-generation/thinking)
- [阿里云：Base URL 总览](https://help.aliyun.com/zh/model-studio/base-url)
- [阿里云：通义千问模型的流式输出](https://help.aliyun.com/zh/model-studio/stream)
- [阿里云：Function Calling](https://help.aliyun.com/zh/model-studio/qwen-function-calling)

模型接入参数按以上文档核对；实际地域、权限与模型更新仍应以百炼控制台为准。
