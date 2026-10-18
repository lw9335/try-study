import asyncio

import pytest

from gateway.adapters.demo import DemoAdapter
from gateway.agent import AgentGateway
from gateway.events import TERMINAL_EVENTS, EventType, GatewayError, RunStatus
from gateway.model import ModelChunk, RunInput, ToolCall
from gateway.store import RunStore
from gateway.tools import execute_tool


@pytest.fixture
def store():
    value = RunStore()
    yield value
    value.close()


async def test_complete_tool_loop_checkpoint_trace_and_ttft(store):
    gateway = AgentGateway(store, DemoAdapter(delay=0))
    run = gateway.create(RunInput(prompt="12 + 30", tools=True, thinking=True))
    await gateway.wait(run["id"])
    final = store.get(run["id"])
    events = store.events(run["id"], limit=1000)
    types = [e["type"] for e in events]
    assert final["status"] == "completed"
    assert types.count("run.completed") == 1
    assert types.index("tool.started") < types.index("tool.completed") < types.index("checkpoint.saved")
    assert final["checkpoint"]["step"] == 2
    assert "42" in final["checkpoint"]["messages"][-1]["content"]
    assert final["ttftMs"] is not None
    assert final["firstDeltaMs"] <= final["ttftMs"]
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert all(e["traceId"] == run["traceId"] for e in events)


async def test_cancel_really_stops_producer_and_has_one_terminal(store):
    closed = asyncio.Event()
    emitted = asyncio.Event()

    class SlowModel:
        async def stream(self, _):
            try:
                yield ModelChunk("delta", text="first")
                emitted.set()
                await asyncio.sleep(60)
                yield ModelChunk("delta", text="must not appear")
            finally:
                closed.set()

    gateway = AgentGateway(store, SlowModel())
    run = gateway.create(RunInput(prompt="test"))
    await asyncio.wait_for(emitted.wait(), 1)
    gateway.cancel(run["id"])
    gateway.cancel(run["id"])
    await gateway.wait(run["id"])
    assert closed.is_set()
    events = store.events(run["id"])
    assert events[-1]["type"] == "run.cancelled"
    assert sum(e["type"] in TERMINAL_EVENTS for e in events) == 1
    assert "must not appear" not in str(events)
    seq = store.get(run["id"])["lastSeq"]
    assert store.finish(run["id"], RunStatus.COMPLETED, {}) is None
    assert store.get(run["id"])["lastSeq"] == seq


async def test_cancel_before_task_start(store):
    gateway = AgentGateway(store, DemoAdapter(0))
    run = gateway.create(RunInput(prompt="test"))
    gateway.cancel(run["id"])
    await gateway.wait(run["id"])
    assert [e["type"] for e in store.events(run["id"])] == ["run.created", "run.cancelled"]


async def test_resume_uses_safe_checkpoint_and_new_run(store):
    class FailAfterTool:
        async def stream(self, request):
            if request.messages[-1]["role"] == "tool":
                yield ModelChunk("delta", text="partial")
                raise GatewayError("UPSTREAM_NETWORK", "断线", True)
            yield ModelChunk(
                "finish", reason="tool_calls", tool_calls=[ToolCall("one", "add", '{"a":1,"b":2}')]
            )

    gateway = AgentGateway(store, FailAfterTool())
    first = gateway.create(RunInput(prompt="1 + 2", tools=True))
    await gateway.wait(first["id"])
    parent = store.get(first["id"])
    assert parent["status"] == "failed" and parent["checkpoint"]["step"] == 1
    assert parent["checkpoint"]["messages"][-1]["role"] == "tool"
    gateway.model = DemoAdapter(0)
    child = gateway.resume(first["id"])
    await gateway.wait(child["id"])
    assert child["id"] != first["id"] and child["parentRunId"] == first["id"]
    assert child["traceId"] != first["traceId"]
    assert store.get(child["id"])["status"] == "completed"
    assert "tool.started" not in [e["type"] for e in store.events(child["id"])]
    assert "partial" not in str(store.get(child["id"])["checkpoint"])
    with pytest.raises(GatewayError):
        gateway.resume(child["id"])


async def test_timeout_and_missing_finish(store):
    class HangingModel:
        async def stream(self, _):
            await asyncio.sleep(60)
            yield ModelChunk("finish", reason="stop")

    gateway = AgentGateway(store, HangingModel(), timeout=0.02)
    run = gateway.create(RunInput(prompt="timeout"))
    await gateway.wait(run["id"])
    assert store.events(run["id"])[-1]["data"]["code"] == "RUN_TIMEOUT"

    class PartialModel:
        async def stream(self, _):
            yield ModelChunk("delta", text="half")

    gateway.model = PartialModel()
    run = gateway.create(RunInput(prompt="partial"))
    await gateway.wait(run["id"])
    assert store.events(run["id"])[-1]["data"]["code"] == "UPSTREAM_TRUNCATED"


async def test_step_limit_and_concurrency(store):
    class RepeatingModel:
        async def stream(self, _):
            yield ModelChunk(
                "finish", reason="tool_calls", tool_calls=[ToolCall("x", "add", '{"a":1,"b":2}')]
            )

    gateway = AgentGateway(store, RepeatingModel(), max_steps=2, max_concurrent=1)
    run = gateway.create(RunInput(prompt="repeat", tools=True))
    with pytest.raises(GatewayError, match="并发"):
        gateway.create(RunInput(prompt="another"))
    await gateway.wait(run["id"])
    assert store.events(run["id"])[-1]["data"]["code"] == "STEP_LIMIT"
    with pytest.raises(GatewayError):
        gateway.resume(run["id"])


def test_durable_restart_and_atomic_checkpoint(tmp_path):
    path = str(tmp_path / "test.sqlite")
    store = RunStore(path)
    run = store.create(RunInput(prompt="recover"))
    checkpoint = {"step": 1, "messages": [{"role": "user", "content": "saved"}]}
    store.append(run["id"], EventType.CHECKPOINT_SAVED, {"step": 1}, checkpoint=checkpoint)
    store.close()
    store = RunStore(path)
    store.recover_interrupted()
    store.recover_interrupted()
    assert store.get(run["id"])["checkpoint"] == checkpoint
    assert store.get(run["id"])["status"] == "failed"
    assert store.events(run["id"])[-1]["data"]["code"] == "PROCESS_INTERRUPTED"
    assert len(store.events(run["id"])) == 3
    store.close()


@pytest.mark.parametrize(
    "raw",
    ['{"a":true,"b":2}', '{"a":NaN,"b":2}', "{}", "broken", '{"a":1,"b":2,"extra":3}', '{"a":"1","b":2}'],
)
def test_invalid_tool_arguments(raw):
    with pytest.raises(GatewayError):
        execute_tool("add", raw)


async def test_store_notifications_are_bounded_and_unsubscribed(store):
    run = store.create(RunInput(prompt="notifications"))
    with store.subscribe(run["id"]) as signal:
        for _ in range(1000):
            store.append(run["id"], EventType.MODEL_DELTA, {"text": "x"})
        assert signal.is_set()
    assert not store._listeners


async def test_thinking_is_preserved_across_tool_loop_and_checkpoint(store):
    class ThinkingModel:
        async def stream(self, request):
            if request.messages[-1]["role"] != "tool":
                yield ModelChunk("reasoning", text="choose ")
                yield ModelChunk("reasoning", text="add")
                yield ModelChunk(
                    "finish", reason="tool_calls", tool_calls=[ToolCall("r", "add", '{"a":1,"b":2}')]
                )
            else:
                assert request.messages[-2]["reasoning"] == "choose add"
                yield ModelChunk("delta", text="3")
                yield ModelChunk("finish", reason="stop")

    gateway = AgentGateway(store, ThinkingModel())
    run = gateway.create(RunInput(prompt="1+2", tools=True, thinking=True))
    await gateway.wait(run["id"])
    final = store.get(run["id"])
    assert final["status"] == "completed"
    assert final["checkpoint"]["messages"][2]["reasoning"] == "choose add"
