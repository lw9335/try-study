"""第 6 步：Agent Loop；任务生命周期独立于 SSE 连接。"""

import asyncio
import logging
from contextlib import aclosing

from gateway.events import EventType as E
from gateway.events import GatewayError, RunStatus
from gateway.model import ModelRequest, RunInput, StreamingModel, ToolCall
from gateway.store import RunStore, clone
from gateway.tools import execute_tool

logger = logging.getLogger(__name__)


class AgentGateway:
    def __init__(
        self,
        store: RunStore,
        model: StreamingModel,
        timeout: float = 120,
        max_steps: int = 8,
        max_concurrent: int = 32,
    ):
        self.store = store
        self.model = model
        self.timeout = timeout
        self.max_steps = max_steps
        self.max_concurrent = max_concurrent
        self._jobs: dict[str, asyncio.Task] = {}

    def create(self, request: RunInput, parent: dict | None = None) -> dict:
        if len(self._jobs) >= self.max_concurrent:
            raise GatewayError("CAPACITY_EXCEEDED", "运行任务已达并发上限", True)
        run = self.store.create(request, parent)
        task = asyncio.create_task(self._execute(run["id"]), name=f"run:{run['id']}")
        self._jobs[run["id"]] = task
        task.add_done_callback(lambda t: self._on_done(run["id"], t))
        return run

    def _on_done(self, run_id: str, task: asyncio.Task):
        self._jobs.pop(run_id, None)
        if not task.cancelled() and task.exception():
            # 不记录异常 repr 或 headers，防止网络库携带敏感请求内容。
            logger.error("Run task failed outside error handler: run_id=%s", run_id)

    def cancel(self, run_id: str) -> dict:
        run = self.store.get(run_id)
        if run["status"] == RunStatus.RUNNING:
            self.store.finish(run_id, RunStatus.CANCELLED, {"reason": "user_requested"})
            if task := self._jobs.get(run_id):
                task.cancel()  # CancelledError 传播至 httpx stream，上游连接随即关闭。
        return self.store.get(run_id)

    def resume(self, run_id: str, checkpoint_id: str | None = None) -> dict:
        parent = self.store.get(run_id)
        if parent["status"] not in (RunStatus.FAILED, RunStatus.CANCELLED):
            raise GatewayError("RUN_NOT_RESUMABLE", "仅失败或取消的 Run 可从 checkpoint 新建")
        if checkpoint_id:
            saved = self.store.checkpoint(run_id, checkpoint_id)
            parent["checkpoint"] = saved["snapshot"]
            parent["resumeCheckpointId"] = checkpoint_id
        else:
            latest = self.store.checkpoints(run_id)
            parent["resumeCheckpointId"] = latest[0]["id"] if latest else None
        if parent["checkpoint"]["step"] >= self.max_steps:
            raise GatewayError("RUN_NOT_RESUMABLE", "checkpoint 已达到步骤上限，请创建新任务")
        return self.create(RunInput(**parent["input"]), parent)

    def stats(self) -> dict:
        return {
            "activeJobs": len(self._jobs),
            "maxConcurrent": self.max_concurrent,
            "maxSteps": self.max_steps,
            "timeoutSeconds": self.timeout,
        }

    async def wait(self, run_id: str):
        if task := self._jobs.get(run_id):
            await asyncio.gather(task, return_exceptions=True)

    async def shutdown(self):
        tasks = list(self._jobs.items())
        for run_id, task in tasks:
            self.store.finish(
                run_id,
                RunStatus.FAILED,
                {
                    "code": "SERVER_SHUTDOWN",
                    "message": "服务已停止",
                    "retryable": True,
                },
            )
            task.cancel()
        await asyncio.gather(*(task for _, task in tasks), return_exceptions=True)

    async def _execute(self, run_id: str):
        try:
            async with asyncio.timeout(self.timeout):
                await self._loop(run_id)
        except asyncio.CancelledError:
            # 显式取消/服务停止通常已写终态。兜底不会覆盖已有终态。
            self.store.finish(run_id, RunStatus.CANCELLED, {"reason": "task_cancelled"})
            raise
        except TimeoutError:
            self.store.finish(
                run_id,
                RunStatus.FAILED,
                {
                    "code": "RUN_TIMEOUT",
                    "message": "Run 超过执行时限",
                    "retryable": True,
                },
            )
        except GatewayError as exc:
            self.store.finish(run_id, RunStatus.FAILED, exc.as_dict())
        except Exception:
            logger.error("Run execution error: run_id=%s", run_id)
            self.store.finish(
                run_id,
                RunStatus.FAILED,
                {
                    "code": "INTERNAL_ERROR",
                    "message": "Run 执行失败",
                    "retryable": False,
                },
            )

    async def _loop(self, run_id: str):
        run = self.store.get(run_id)
        checkpoint = run["checkpoint"]
        messages = clone(checkpoint["messages"])
        self.store.append(run_id, E.RUN_STARTED, {"checkpointStep": checkpoint["step"]})
        output_size = 0
        for step in range(checkpoint["step"], self.max_steps):
            self.store.append(run_id, E.MODEL_STARTED, {"step": step, "model": run["input"]["model"]})
            text = ""
            reasoning = ""
            finish = None
            calls: list[ToolCall] = []
            request = ModelRequest(
                model=run["input"]["model"],
                messages=clone(messages),
                tools=run["input"]["tools"],
                thinking=run["input"]["thinking"],
            )
            async with aclosing(self.model.stream(request)) as stream:
                async for chunk in stream:
                    if finish is not None:
                        raise GatewayError("UPSTREAM_PROTOCOL", "模型结束后仍返回增量")
                    if chunk.type in ("delta", "reasoning"):
                        output_size += len(chunk.text)
                        if output_size > 100_000:
                            raise GatewayError("OUTPUT_LIMIT", "Run 输出超过 100000 字符")
                        if not chunk.text:
                            continue
                        if chunk.type == "delta":
                            text += chunk.text
                        else:
                            reasoning += chunk.text
                        self.store.append(
                            run_id,
                            E.MODEL_DELTA if chunk.type == "delta" else E.MODEL_REASONING,
                            {"step": step, "text": chunk.text},
                        )
                    elif chunk.type == "usage":
                        self.store.append(run_id, E.MODEL_USAGE, {**chunk.usage, "step": step})
                    elif chunk.type == "finish":
                        finish, calls = chunk.reason, chunk.tool_calls
            if finish is None:
                raise GatewayError("UPSTREAM_TRUNCATED", "模型没有正常结束", True)
            self.store.append(
                run_id, E.MODEL_FINISHED, {"step": step, "reason": finish, "toolCalls": len(calls)}
            )
            if finish not in ("stop", "tool_calls"):
                raise GatewayError("MODEL_INCOMPLETE", f"模型未完整回答：{finish}")
            if calls:
                if not request.tools or finish != "tool_calls":
                    raise GatewayError("UNEXPECTED_TOOL_CALL", "模型返回了未允许的工具调用")
                if len({call.id for call in calls}) != len(calls):
                    raise GatewayError("UPSTREAM_PROTOCOL", "工具调用 ID 重复")
                assistant_message = {
                    "role": "assistant",
                    "content": text or None,
                    "tool_calls": [call.as_message() for call in calls],
                }
                if reasoning:
                    # 使用网关内部字段；厂商 reasoning_content 字段由 Adapter 映射。
                    assistant_message["reasoning"] = reasoning
                messages.append(assistant_message)
                for call in calls:
                    self.store.append(
                        run_id,
                        E.TOOL_STARTED,
                        {
                            "step": step,
                            "callId": call.id,
                            "name": call.name,
                            "arguments": call.arguments,
                        },
                    )
                    result = execute_tool(call.name, call.arguments)
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
                    self.store.append(
                        run_id,
                        E.TOOL_COMPLETED,
                        {
                            "step": step,
                            "callId": call.id,
                            "name": call.name,
                            "result": result,
                        },
                    )
                # 只提交完整模型轮次与全部工具结果，绝不把半截 JSON 放入 checkpoint。
                self.store.append(
                    run_id,
                    E.CHECKPOINT_SAVED,
                    {"step": step + 1},
                    checkpoint={"step": step + 1, "messages": messages},
                )
            else:
                if finish != "stop":
                    raise GatewayError("UPSTREAM_PROTOCOL", "缺少工具调用参数")
                assistant_message = {"role": "assistant", "content": text}
                if reasoning:
                    assistant_message["reasoning"] = reasoning
                messages.append(assistant_message)
                # 最后 checkpoint 与 completed 事件在同一事务中保存。
                self.store.finish(
                    run_id,
                    RunStatus.COMPLETED,
                    {
                        "steps": step + 1,
                        "ttftMs": self.store.get(run_id)["ttftMs"],
                    },
                    checkpoint={"step": step + 1, "messages": messages},
                )
                return
        raise GatewayError("STEP_LIMIT", f"Agent 超过 {self.max_steps} 步限制")
