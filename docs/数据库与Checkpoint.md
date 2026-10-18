# 数据库、Run Store 与 Checkpoint

本机已用 Docker Compose 启动 PostgreSQL 17 与 Python 网关。不是页面静态数据：页面的计数、表格、快照和审计事件全部通过 API 查询数据库。

## 三张业务表

| 表 | 作用 | 关键字段与约束 |
| --- | --- | --- |
| runs | 当前状态与最新安全边界 | id 主键；status/model/trace_id/parent_run_id/last_seq/created_at；doc 为完整 Run 投影 |
| events | 不可变事件日志 | 复合主键 (run_id, seq)，run_id 外键关联 runs；type/created_at/doc |
| checkpoints | 安全边界的完整历史 | id 主键；(run_id,seq) 唯一且外键关联 events；step/kind/snapshot/source_checkpoint_id |

另有 schema_migrations（迁移版本）和 store_metadata（旧数据导入记录）两张内部表。页面只开放上面的三张业务表只读浏览，不接收用户任意 SQL。

## Store 事务与唯一终态

1. 创建 Run 在一个事务里写 runs、run.created 事件和 initial checkpoint。
2. 每条增量先落库，再通知 SSE 订阅者；通知只负责唤醒，SSE 始终从数据库按序号读取。
3. 工具步骤结束，在一个事务里写 checkpoint.saved、更新 Run 最新快照并插入 step checkpoint。
4. 最终答案在一个事务里写 run.completed、状态和 final checkpoint。
5. 只有 running 可以迁移到终态；已 completed/cancelled/failed 后拒绝再次追加事件或覆盖终态。
6. 快照写入失败时整笔事务回滚，不能出现“事件已提交而 checkpoint 没保存”的半成功状态。

PostgreSQL 连接启用 autocommit，Store 显式 BEGIN/COMMIT/ROLLBACK；SQLite 则使用 BEGIN IMMEDIATE。PostgreSQL 的 schema 级 advisory lock 与 SQLite 文件锁防止两个执行 worker 同时管理同一组 Run。

## Checkpoint 记录什么

快照内容是 `{step, messages}`，包含已完整完成的 assistant/tool 消息，以及必要的内部 reasoning 上下文。不会保存尚未完整输出的工具 JSON 或假装保存供应商隐藏 token 状态。

| kind | 保存时机 |
| --- | --- |
| initial | Run 创建，step 通常为 0；子 Run 则从指定父快照的 step 开始 |
| step | 一轮模型请求工具、全部工具执行并写入消息后 |
| final | 完整答案结束，与 completed 终态同事务 |
| legacy | 从旧版“只保留最新快照”的数据库导入，不能还原已经丢失的中间历史 |

选择指定历史 checkpoint 恢复时，新 Run 记录 parentRunId 和 sourceCheckpointId，并保留自己的 runId/traceId/seq。恢复最新工具边界时不重新执行已有工具；如果主动选择更早的初始边界，则会重新执行之后的步骤，这正是选择旧快照的含义。

## 三种恢复场景

- **SSE 断线**：使用 Last-Event-ID 从 events 读取剩余事件，原 Run 继续，不再启动模型请求。
- **用户取消**：唯一 cancelled 事件落库后取消后台 asyncio 任务，httpx 上游流随之关闭；可从已提交快照显式新建子 Run。
- **进程/容器重启**：启动时将遗留 running 标为 failed / PROCESS_INTERRUPTED，不自动重跑不确定的云端请求；历史事件与快照仍可查询和恢复。

## Docker 持久化与旧数据

PostgreSQL 数据在 `streaming-agent-gateway_gateway-postgres` 命名卷中，`docker compose down` 后重新 up 仍保留。网关镜像不保存数据库或密钥，重建网关不会覆盖历史。

`./data` 只读挂载到 `/legacy`。仅当目标 PostgreSQL 没有任何 Run，且存在旧 SQLite 文件时才导入；原文件不会被修改，导入整体使用事务，目标已有记录时不会再次导入。

## 如何直接检查真实 SQL 数据

```powershell
docker compose exec postgres psql -U gateway -d gateway -c "SELECT id,status,model,last_seq,parent_run_id FROM runs ORDER BY created_at DESC LIMIT 10;"
docker compose exec postgres psql -U gateway -d gateway -c "SELECT run_id,seq,type FROM events ORDER BY created_at DESC LIMIT 20;"
docker compose exec postgres psql -U gateway -d gateway -c "SELECT id,run_id,step,kind,seq FROM checkpoints ORDER BY created_at DESC LIMIT 20;"
```

## API 与 CLI

`GET /api/storage` 展示数据库健康与统计；`GET /api/storage/tables/runs` 查看关系字段；`GET /api/runs/{id}/checkpoints` 列出快照；`GET /api/runs/{id}/checkpoints/{checkpointId}` 查看完整消息。

```powershell
docker compose exec gateway python -m gateway.cli checkpoints <runId>
docker compose exec gateway python -m gateway.cli resume <runId> --checkpoint <checkpointId>
docker compose exec gateway python -m gateway.cli storage
```

在页面选择失败/取消的 Run → Checkpoint 历史 → 选择快照 → “从 Checkpoint 新建”，完成相同操作。

## 已做的验证

- SQLite 的旧 schema 迁移、只读导入、事件与快照整体回滚、历史快照选择恢复。
- 真实 PostgreSQL 的工具循环、三类快照、事务失败回滚、单 worker 租约、断开连接后重启恢复。
- 实际容器中的真实模型调用：工具完成后取消，从指定 step=1 快照创建子 Run，子 Run 返回 42，重复工具执行次数为 0。
- 实际执行 Docker down/up，核对父子 Run ID、状态、事件数与快照数完全保留。记录见 `docker-verification.json`。
