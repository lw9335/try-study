import json
import sqlite3

import pytest

from gateway.adapters.demo import DemoAdapter
from gateway.agent import AgentGateway
from gateway.events import EventType, GatewayError, RunStatus
from gateway.model import RunInput
from gateway.store import RunStore


async def test_checkpoint_history_and_database_explorer():
    store = RunStore()
    try:
        gateway = AgentGateway(store, DemoAdapter(0))
        run = gateway.create(RunInput(prompt="12+30", tools=True))
        await gateway.wait(run["id"])
        saved = store.checkpoints(run["id"])
        assert [item["kind"] for item in saved] == ["final", "step", "initial"]
        assert [item["step"] for item in saved] == [2, 1, 0]
        for checkpoint in saved:
            assert checkpoint["seq"] <= store.get(run["id"])["lastSeq"]
            detail = store.checkpoint(run["id"], checkpoint["id"])
            assert len(detail["snapshot"]["messages"]) == checkpoint["messageCount"]
        stats = store.database_stats()
        assert stats["connected"] and stats["schemaVersion"] == 2
        assert stats["ttft"]["sampleCount"] == 1
        assert stats["ttft"]["p50Ms"] >= 0
        assert {t["name"]: t["count"] for t in stats["tables"]}["checkpoints"] == 3
        assert store.inspect_table("runs")["rows"][0]["status"] == "completed"
        assert store.inspect_table("events", limit=2)["hasMore"]
        assert store.run_summary(run["id"])["terminalCount"] == 1
        with pytest.raises(GatewayError):
            store.inspect_table("runs; DROP TABLE runs")
    finally:
        store.close()


async def test_resume_from_selected_historical_checkpoint():
    store = RunStore()
    gateway = AgentGateway(store, DemoAdapter(0))
    try:
        # 已完成一个工具步骤但尚未结束的 Run，由显式取消留下两个安全快照。
        parent = store.create(RunInput(prompt="1+2", tools=True))
        cp = {
            "step": 1,
            "messages": [
                {"role": "user", "content": "1+2"},
                {"role": "tool", "tool_call_id": "x", "content": '{"result":3}'},
            ],
        }
        store.append(parent["id"], EventType.CHECKPOINT_SAVED, {"step": 1}, checkpoint=cp)
        store.finish(parent["id"], RunStatus.CANCELLED, {})
        initial = store.checkpoints(parent["id"])[-1]
        child = gateway.resume(parent["id"], initial["id"])
        await gateway.wait(child["id"])
        assert child["parentRunId"] == parent["id"]
        assert child["sourceCheckpointId"] == initial["id"]
        assert store.run_summary(child["id"])["toolCalls"] == 1
        assert store.checkpoints(child["id"])[-1]["sourceCheckpointId"] == initial["id"]
        other = store.create(RunInput(prompt="other"))
        store.finish(other["id"], RunStatus.CANCELLED, {})
        with pytest.raises(GatewayError, match="不属于"):
            gateway.resume(other["id"], initial["id"])
    finally:
        await gateway.shutdown()
        store.close()


def test_event_state_checkpoint_rollback_is_atomic(monkeypatch):
    store = RunStore()
    try:
        run = store.create(RunInput(prompt="atomic"))
        before = store.get(run["id"])

        def fail(*_):
            raise RuntimeError("simulate checkpoint persistence failure")

        monkeypatch.setattr(store, "_save_checkpoint", fail)
        with pytest.raises(RuntimeError):
            store.finish(run["id"], RunStatus.COMPLETED, {}, checkpoint={"step": 1, "messages": []})
        assert store.get(run["id"]) == before
        assert len(store.events(run["id"])) == 1
        assert len(store.checkpoints(run["id"])) == 1
    finally:
        store.close()


def test_old_schema_migration_preserves_latest_snapshot(tmp_path):
    source = tmp_path / "old.sqlite"
    original = RunStore(str(tmp_path / "template.sqlite"))
    run = original.create(RunInput(prompt="legacy"))
    event = original.events(run["id"])[0]
    original.close()
    with sqlite3.connect(source) as old:
        old.executescript(
            "CREATE TABLE runs(id TEXT PRIMARY KEY,doc TEXT);"
            "CREATE TABLE events(run_id TEXT,seq INTEGER,doc TEXT,PRIMARY KEY(run_id,seq));"
        )
        old.execute("INSERT INTO runs VALUES(?,?)", (run["id"], json.dumps(run)))
        old.execute("INSERT INTO events VALUES(?,?,?)", (run["id"], 1, json.dumps(event)))
    store = RunStore(str(source))
    try:
        assert store.checkpoints(run["id"])[0]["kind"] == "legacy"
        assert store.inspect_table("events")["rows"][0]["type"] == "run.created"
        assert store.database_stats()["schemaVersion"] == 2
    finally:
        store.close()


def test_import_is_read_only_preserves_ids_and_is_idempotent(tmp_path):
    source = tmp_path / "source.sqlite"
    store = RunStore(str(source))
    run = store.create(RunInput(prompt="import"))
    store.finish(run["id"], RunStatus.CANCELLED, {})
    original_ids = [c["id"] for c in store.checkpoints(run["id"])]
    store.close()
    original_bytes = source.read_bytes()
    target = RunStore()
    try:
        report = target.import_sqlite(str(source))
        assert report["runs"] == 1 and report["events"] == 2
        assert [c["id"] for c in target.checkpoints(run["id"])] == original_ids
        assert target.import_sqlite(str(source)) == {"imported": False}
        assert source.read_bytes() == original_bytes
        assert target.database_stats()["legacyImport"]["runs"] == 1
    finally:
        target.close()
