"""无密钥本地演示；走与真实模型相同的 StreamingModel → Agent → Store 链路。"""

import asyncio
import json
import re
from collections.abc import AsyncIterator

from gateway.model import ModelChunk, ModelRequest, ToolCall


class DemoAdapter:
    def __init__(self, delay: float = 0.035):
        self.delay = delay

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelChunk]:
        last = request.messages[-1]
        if request.tools and last["role"] != "tool":
            prompt = next(m["content"] for m in request.messages if m["role"] == "user")
            match = re.search(r"(-?\d+(?:\.\d+)?)\s*[+＋]\s*(-?\d+(?:\.\d+)?)", prompt)
            if match:
                await asyncio.sleep(self.delay)
                yield ModelChunk(
                    "finish",
                    reason="tool_calls",
                    tool_calls=[
                        ToolCall("demo_add", "add", json.dumps({"a": float(match[1]), "b": float(match[2])}))
                    ],
                )
                return
        if request.thinking:
            yield ModelChunk("reasoning", text="这是本地演示的思考片段。")
        if last["role"] == "tool":
            text = (
                f"工具计算完成，结果为 {json.loads(last['content'])['result']:g}。\n"
                "这是本地演示模式；切换 aliyun 后将使用所选千问模型。"
            )
        else:
            text = (
                f"你好！这是 Streaming Agent Gateway 的本地演示。\n模型配置：{request.model}。\n"
                "你正在逐字接收事件，可以取消、断开订阅再恢复，并查看 Trace 与 Checkpoint。\n"
                "试试输入“计算 12 + 30”观察完整的工具调用循环。"
            )
        for char in text:
            await asyncio.sleep(self.delay)
            yield ModelChunk("delta", text=char)
        yield ModelChunk("usage", usage={"demo": True, "note": "本地演示不产生真实 token 用量"})
        yield ModelChunk("finish", reason="stop")
