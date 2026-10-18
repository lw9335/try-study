import asyncio
import json
from contextlib import aclosing

import httpx
import pytest

from gateway.adapters.aliyun import AliyunAdapter
from gateway.events import GatewayError
from gateway.model import MODEL_PROFILES, ModelProfile, ModelRequest
from gateway.sse import SSEFrame, encode_sse


def wire(value):
    return encode_sse(SSEFrame(json.dumps(value, ensure_ascii=False)))


def choice(delta=None, finish=None):
    return {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}


REQUEST = ModelRequest("qwen-plus", [{"role": "user", "content": "你好"}])


class BytesStream(httpx.AsyncByteStream):
    def __init__(self, data, blocked=False):
        self.data = data
        self.closed = False
        self.blocked = blocked

    async def __aiter__(self):
        for index in range(0, len(self.data), 3):
            yield self.data[index : index + 3]
        if self.blocked:
            await asyncio.sleep(60)

    async def aclose(self):
        self.closed = True


async def collect(data, status=200, content_type="text/event-stream"):
    stream = BytesStream(data)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(status, headers={"content-type": content_type}, stream=stream)
        )
    ) as client:
        adapter = AliyunAdapter("secret", client=client)
        result = [chunk async for chunk in adapter.stream(REQUEST)]
    assert stream.closed
    return result


@pytest.mark.parametrize("profile", MODEL_PROFILES)
async def test_all_profiles_request_mapping_and_no_sdk(profile):
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=wire(choice({"content": "好"}, "stop")) + b"data: [DONE]\n\n",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = AliyunAdapter("secret", client=client)
        request = ModelRequest(profile.id, REQUEST.messages, tools=True)
        result = [chunk async for chunk in adapter.stream(request)]
    payload = json.loads(seen[0].content)
    assert seen[0].url.path == "/compatible-mode/v1/chat/completions"
    assert seen[0].headers["authorization"] == "Bearer secret"
    assert payload["model"] == profile.id and payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}
    assert payload["tools"][0]["function"]["name"] == "add"
    assert ("enable_thinking" in payload) == (profile.thinking_mode == "optional")
    assert result[-1].reason == "stop"


async def test_deepseek_and_qwen_share_protocol_but_respect_capabilities():
    adapter = AliyunAdapter("secret")
    qwen = adapter.build_payload(ModelRequest("qwen-plus", REQUEST.messages, thinking=True))
    deepseek = adapter.build_payload(ModelRequest("deepseek-v4.1-flash", REQUEST.messages, thinking=True))
    assert qwen.keys() == deepseek.keys()
    assert qwen["messages"] == deepseek["messages"]
    assert deepseek["enable_thinking"] is True
    ordinary = adapter.build_payload(ModelRequest("qwen-max", REQUEST.messages))
    assert "enable_thinking" not in ordinary
    await adapter.aclose()


async def test_required_thinking_does_not_send_disable_flag_and_tools_are_checked():
    profiles = [ModelProfile(id="reasoner", label="测试", thinking_mode="required", tools=False)]
    adapter = AliyunAdapter("secret", profiles=profiles)
    try:
        payload = adapter.build_payload(ModelRequest("reasoner", REQUEST.messages))
        assert "enable_thinking" not in payload
        with pytest.raises(GatewayError, match="工具"):
            adapter.build_payload(ModelRequest("reasoner", REQUEST.messages, tools=True))
    finally:
        await adapter.aclose()


async def test_internal_reasoning_maps_to_vendor_history_without_mutating_messages():
    messages = [{"role": "assistant", "content": None, "reasoning": "saved context", "tool_calls": []}]
    adapter = AliyunAdapter("secret")
    try:
        for model in ("qwen-plus", "deepseek-v4.1-flash"):
            payload = adapter.build_payload(ModelRequest(model, messages, thinking=True))
            assert payload["messages"][0]["reasoning_content"] == "saved context"
            assert "reasoning" not in payload["messages"][0]
        assert "reasoning_content" not in messages[0]
        assert messages[0]["reasoning"] == "saved context"
    finally:
        await adapter.aclose()


async def test_reasoning_text_usage_only_chunk_and_interleaved_tool_fragments():
    data = b"".join(
        [
            wire(choice({"reasoning_content": "思考", "content": None})),
            wire(choice({"content": "正在计算"})),
            wire(
                choice(
                    {
                        "tool_calls": [
                            {"index": 1, "id": "b", "function": {"name": "add", "arguments": '{"a":3,'}},
                            {"index": 0, "id": "a", "function": {"name": "add", "arguments": '{"a":1,'}},
                        ]
                    }
                )
            ),
            wire(
                choice(
                    {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": '"b":2}'}},
                            {"index": 1, "function": {"arguments": '"b":4}'}},
                        ]
                    },
                    "tool_calls",
                )
            ),
            wire({"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}),
            b"data: [DONE]\n\n",
        ]
    )
    result = await collect(data)
    assert [c.type for c in result] == ["reasoning", "delta", "usage", "finish"]
    assert result[2].usage["total_tokens"] == 12
    assert [c.id for c in result[-1].tool_calls] == ["a", "b"]
    assert json.loads(result[-1].tool_calls[1].arguments) == {"a": 3, "b": 4}


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (wire(choice({"content": "半截"})), "UPSTREAM_TRUNCATED"),
        (wire(choice({}, "stop")), "UPSTREAM_TRUNCATED"),
        (b"data: [DONE]\n\n", "UPSTREAM_TRUNCATED"),
        (b"data: not-json\n\n", "UPSTREAM_PROTOCOL"),
        (wire({"error": {"message": "sensitive"}}), "UPSTREAM_ERROR"),
        (wire({"choices": None}), "UPSTREAM_PROTOCOL"),
        (wire(choice({}, "tool_calls")) + b"data: [DONE]\n\n", "UPSTREAM_PROTOCOL"),
        (wire(choice({"tool_calls": [{"index": -1}]})), "UPSTREAM_PROTOCOL"),
        (wire(choice({"content": ["image"]})), "UPSTREAM_PROTOCOL"),
    ],
)
async def test_bad_upstream_never_looks_successful(data, code):
    with pytest.raises(GatewayError) as error:
        await collect(data)
    assert error.value.code == code
    assert "sensitive" not in error.value.message


@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_http_errors_are_sanitized(status):
    with pytest.raises(GatewayError) as error:
        await collect(b"secret", status=status)
    assert error.value.code == f"UPSTREAM_HTTP_{status}"
    assert error.value.retryable == (status == 429 or status >= 500)
    assert "secret" not in str(error.value)


async def test_cancel_closes_actual_http_stream():
    stream = BytesStream(wire(choice({"content": "first"})), blocked=True)
    started = asyncio.Event()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)
        )
    ) as client:
        adapter = AliyunAdapter("secret", client=client)

        async def consume():
            async with aclosing(adapter.stream(REQUEST)) as chunks:
                async for _ in chunks:
                    started.set()

        task = asyncio.create_task(consume())
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed


async def test_timeout_network_and_missing_key():
    for exception, code in [
        (httpx.ReadTimeout("secret"), "UPSTREAM_TIMEOUT"),
        (httpx.ConnectError("secret"), "UPSTREAM_NETWORK"),
    ]:

        def respond(_, error=exception):
            raise error

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(GatewayError) as error:
                _ = [x async for x in AliyunAdapter("secret", client=client).stream(REQUEST)]
            assert error.value.code == code
            assert "secret" not in str(error.value)
    adapter = AliyunAdapter("")
    try:
        with pytest.raises(GatewayError, match="DASHSCOPE_API_KEY"):
            _ = [x async for x in adapter.stream(REQUEST)]
    finally:
        await adapter.aclose()
