"""第 4 步：阿里云百炼 OpenAI 兼容协议适配，直接使用 httpx，不使用模型 SDK。"""

import json
from collections.abc import AsyncIterator

import httpx

from gateway.events import GatewayError
from gateway.model import MODEL_PROFILES, ModelChunk, ModelProfile, ModelRequest, ToolCall
from gateway.sse import decode_sse
from gateway.tools import TOOL_DEFINITIONS


class AliyunAdapter:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
        profiles: list[ModelProfile] | None = None,
        client: httpx.AsyncClient | None = None,
    ):
        self._api_key = api_key
        self._endpoint = base_url.rstrip("/") + "/chat/completions"
        self._profiles = {p.id: p for p in (profiles if profiles is not None else MODEL_PROFILES)}
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(45, connect=10))
        self._owns_client = client is None

    def build_payload(self, request: ModelRequest) -> dict:
        """请求映射：模型差异来自 capability profile，而非复制三份 adapter。"""
        profile = self._profiles.get(request.model)
        if not profile:
            raise GatewayError("UNKNOWN_MODEL", "模型尚未配置")
        if request.thinking and profile.thinking_mode == "none":
            raise GatewayError("UNSUPPORTED_THINKING", "此模型配置不支持思考模式")
        if request.tools and not profile.tools:
            raise GatewayError("UNSUPPORTED_TOOLS", "此模型配置不支持工具")
        messages = []
        for raw in request.messages:
            message = {key: value for key, value in raw.items() if key != "reasoning"}
            if raw.get("reasoning") and profile.thinking:
                # 推理+工具循环须回传完整思考上下文；不把内部字段直接发给厂商。
                message["reasoning_content"] = raw["reasoning"]
            messages.append(message)
        payload = {
            "model": request.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        # 直接调用 HTTP 时 enable_thinking 在 JSON 顶层；extra_body 是 SDK 写法。
        if profile.thinking_mode == "optional":
            payload["enable_thinking"] = request.thinking
        # 纯推理模型不发送关闭开关；不能把 UI 的 false 生硬传给所有模型。
        # 当前默认列表均为混合模式或非思考模型，required 留给后续扩展。
        if request.tools:
            payload["tools"] = TOOL_DEFINITIONS
        return payload

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelChunk]:
        if not self._api_key:
            raise GatewayError("MISSING_API_KEY", "请在 .env 中配置 DASHSCOPE_API_KEY")
        payload = self.build_payload(request)
        try:
            # asyncio.CancelledError 穿透此上下文，确保上游响应被立即关闭。
            async with self._client.stream(
                "POST",
                self._endpoint,
                headers={"Authorization": f"Bearer {self._api_key}", "Accept": "text/event-stream"},
                json=payload,
            ) as response:
                if not response.is_success:
                    status = response.status_code
                    raise GatewayError(
                        f"UPSTREAM_HTTP_{status}",
                        f"模型平台返回 HTTP {status}，请检查地址、密钥、额度和模型权限",
                        status == 429 or status >= 500,
                    )
                if "text/event-stream" not in response.headers.get("content-type", ""):
                    raise GatewayError("UPSTREAM_PROTOCOL", "模型平台未返回 SSE 响应")
                async for chunk in self._decode(response):
                    yield chunk
        except GatewayError:
            raise
        except httpx.TimeoutException:
            raise GatewayError("UPSTREAM_TIMEOUT", "模型连接或读取超时", True) from None
        except httpx.HTTPError:
            raise GatewayError("UPSTREAM_NETWORK", "模型连接中断", True) from None
        except (ValueError, TypeError, KeyError, AttributeError):
            raise GatewayError("UPSTREAM_PROTOCOL", "模型流格式不符合协议") from None

    async def _decode(self, response: httpx.Response) -> AsyncIterator[ModelChunk]:
        calls: dict[int, dict[str, str]] = {}
        finish = None
        async for frame in decode_sse(response.aiter_bytes()):
            if frame.data == "[DONE]":
                if not finish:
                    raise GatewayError("UPSTREAM_TRUNCATED", "收到 DONE 但缺少 finish_reason", True)
                tool_calls = [ToolCall(**calls[index]) for index in sorted(calls)]
                if (finish == "tool_calls") != bool(tool_calls) or any(
                    not c.id or not c.name for c in tool_calls
                ):
                    raise GatewayError("UPSTREAM_PROTOCOL", "工具调用参数或结束标记不完整")
                yield ModelChunk("finish", reason=finish, tool_calls=tool_calls)
                return
            chunk = json.loads(frame.data)
            if not isinstance(chunk, dict) or chunk.get("error") or chunk.get("code"):
                raise GatewayError("UPSTREAM_ERROR", "模型在流中返回错误")
            if not isinstance(chunk.get("choices"), list):
                raise GatewayError("UPSTREAM_PROTOCOL", "响应缺少 choices")
            if usage := chunk.get("usage"):
                # 只透传计数，避免将未知厂商字段带进公开事件。
                clean_usage = {
                    key: usage[key]
                    for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                    if type(usage.get(key)) is int and usage[key] >= 0
                }
                yield ModelChunk("usage", usage=clean_usage)
            for choice in chunk["choices"]:
                if choice.get("index") != 0:
                    continue
                delta = choice.get("delta") or {}
                for field, kind in (("reasoning_content", "reasoning"), ("content", "delta")):
                    text = delta.get(field)
                    if text is not None and not isinstance(text, str):
                        raise GatewayError("UPSTREAM_PROTOCOL", "非文本增量不在此练习范围内")
                    if text:
                        yield ModelChunk(kind, text=text)
                for part in delta.get("tool_calls") or []:
                    index = part.get("index")
                    if type(index) is not int or not 0 <= index < 32:
                        raise GatewayError("UPSTREAM_PROTOCOL", "工具索引无效")
                    call = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                    function = part.get("function") or {}
                    # function.arguments 是分片 JSON，必须按 index 拼接完再解析。
                    call["id"] += part.get("id") or ""
                    call["name"] += function.get("name") or ""
                    call["arguments"] += function.get("arguments") or ""
                    if sum(map(len, call.values())) > 65536:
                        raise GatewayError("UPSTREAM_PROTOCOL", "工具参数过长")
                if reason := choice.get("finish_reason"):
                    finish = reason
        raise GatewayError("UPSTREAM_TRUNCATED", "模型连接提前结束，未收到 DONE", True)

    async def aclose(self):
        if self._owns_client:
            await self._client.aclose()
