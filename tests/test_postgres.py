"""在 Compose 测试容器中验证真实 PostgreSQL；每例使用独立临时 schema。"""

import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import pytest

from gateway.adapters.demo import DemoAdapter
from gateway.agent import AgentGateway
from gateway.events import EventType, RunStatus
from gateway.model import RunInput
from gateway.store import RunStore

URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="通过 Compose test profile 测试 PostgreSQL")


@pytest.fixture
def pg_store():
    import psycopg
    from psycopg import sql

    schema = "test_gateway_" + uuid4().hex
    with psycopg.connect(URL, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        parts = urlsplit(URL)
        query = dict(parse_qsl(parts.query))
        query["options"] = f"-csearch_path={schema}"
        scoped_url = urlunsplit((*parts[:3], urlencode(query), parts.fragment))
        store = RunStore(scoped_url)
        try:
            yield store, scoped_url
        finally:
            store.close()
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


async def test_postgres_tool_loop_and_checkpoint_durability(pg_store):
    store, _ = pg_store
    gateway = AgentGateway(store, DemoAdapter(0))
    run = gateway.create(RunInput(prompt="12+30", tools=True))
    await gateway.wait(run["id"])
    assert store.get(run["id"])["status"] == "completed"
    assert [c["kind"] for c in store.checkpoints(run["id"])] == ["final", "step", "initial"]
    assert store.database_stats()["engine"] == "PostgreSQL"
    assert store.run_summary(run["id"])["terminalCount"] == 1
    assert store.inspect_table("events")["total"] > 10


def test_postgres_rollback_and_single_worker_lease(pg_store, monkeypatch):
    store, url = pg_store
    run = store.create(RunInput(prompt="atomic PG"))
    checkpoint = {"step": 1, "messages": []}

    def fail(*_):
        raise RuntimeError("checkpoint failed")

    monkeypatch.setattr(store, "_save_checkpoint", fail)
    with pytest.raises(RuntimeError):
        store.append(run["id"], EventType.CHECKPOINT_SAVED, {}, checkpoint=checkpoint)
    assert store.get(run["id"])["lastSeq"] == 1
    assert store.get(run["id"])["checkpoint"]["step"] == 0
    assert len(store.events(run["id"])) == 1
    with pytest.raises(RuntimeError, match="worker"):
        RunStore(url)
    store.finish(run["id"], RunStatus.CANCELLED, {})


def test_postgres_reconnect_recovers_interrupted_run_and_checkpoint(pg_store):
    store, url = pg_store
    run = store.create(RunInput(prompt="recover PostgreSQL"))
    snapshot = {"step": 1, "messages": [{"role": "user", "content": "persisted"}]}
    store.append(run["id"], EventType.CHECKPOINT_SAVED, {"step": 1}, checkpoint=snapshot)
    store.close()
    restored = RunStore(url)
    try:
        restored.recover_interrupted()
        assert restored.get(run["id"])["checkpoint"] == snapshot
        assert restored.get(run["id"])["status"] == "failed"
        assert restored.events(run["id"])[-1]["data"]["code"] == "PROCESS_INTERRUPTED"
        assert len(restored.checkpoints(run["id"])) == 2
    finally:
        restored.close()
