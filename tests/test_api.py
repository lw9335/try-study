import json
from uuid import uuid4

import httpx
import pytest

from gateway.adapters.demo import DemoAdapter
from gateway.agent import AgentGateway
from gateway.api import create_app
from gateway.config import Settings
from gateway.model import MODEL_PROFILES, RunInput
from gateway.sse import decode_sse
from gateway.store import RunStore


@pytest.fixture
async def setup_api():
    store = RunStore()
    gateway = AgentGateway(store, DemoAdapter(0))
    app = create_app(Settings(), gateway)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client, gateway
    await gateway.shutdown()
    store.close()


async def test_routes_and_replay_do_not_restart_model(setup_api):
    client, gateway = setup_api
    response = await client.post("/api/runs", json={"prompt": "计算 12 + 30", "tools": True})
    assert response.status_code == 201
    run = response.json()
    key = run["id"]
    await gateway.wait(key)
    before = gateway.store.get(key)["lastSeq"]
    response = await client.get(f"/api/runs/{key}/events?after=0", headers={"Last-Event-ID": "3"})
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = [f async for f in decode_sse(response.aiter_bytes())]
    assert int(frames[0].id) == 4
    assert json.loads(frames[-1].data)["type"] == "run.completed"
    assert gateway.store.get(key)["lastSeq"] == before
    assert (await client.get(f"/api/runs/{key}/checkpoint")).json()["step"] == 2
    assert (await client.post(f"/api/runs/{key}/cancel")).json()["status"] == "completed"
    assert (await client.post(f"/api/runs/{key}/resume")).status_code == 409
    assert (await client.get("/api/runs")).json()[0]["id"] == key
    assert not gateway.store._listeners


@pytest.mark.parametrize(
    "payload",
    [
        {"prompt": ""},
        {"prompt": "  "},
        {"prompt": "x", "model": "unknown"},
        {"prompt": "x", "tools": "yes"},
        {"prompt": "x", "model": "qwen-max", "thinking": True},
        {"prompt": "x", "api_key": "secret"},
        {"prompt": "x" * 16001},
    ],
)
async def test_invalid_inputs(setup_api, payload):
    client, _ = setup_api
    response = await client.post("/api/runs", json=payload)
    assert response.status_code in (400, 422)
    assert "secret" not in response.text


@pytest.mark.parametrize("cursor", ["-1", "1.2", "nan", "999999", "", "abc"])
async def test_invalid_cursor(setup_api, cursor):
    client, gateway = setup_api
    run = gateway.create(RunInput(prompt="x"))
    await gateway.wait(run["id"])
    response = await client.get(f"/api/runs/{run['id']}/events", headers={"Last-Event-ID": cursor})
    assert response.status_code == 400


async def test_health_assets_errors_and_origin(setup_api):
    client, _ = setup_api
    assert (await client.get("/api/health")).json() == {"ok": True, "provider": "demo"}
    public = (await client.get("/api/models")).json()
    assert len(public["models"]) == len(MODEL_PROFILES)
    assert {p["family"] for p in public["models"]} == {"Qwen", "DeepSeek"}
    assert "api_key" not in public
    assert (await client.get("/")).status_code == 200
    assert (await client.get("/app.js")).status_code == 200
    assert (await client.get(f"/api/runs/{uuid4()}")).status_code == 404
    assert (await client.post("/api/runs", content="broken")).status_code == 422
    assert (await client.post("/api/runs", content="x" * 65537)).status_code == 413
    response = await client.post("/api/runs", json={"prompt": "x"}, headers={"Origin": "http://evil"})
    assert response.status_code == 403


async def test_storage_dashboard_checkpoint_history_and_summary(setup_api):
    client, gateway = setup_api
    run = (await client.post("/api/runs", json={"prompt": "1+2", "tools": True})).json()
    await gateway.wait(run["id"])
    state = (await client.get("/api/storage")).json()
    assert state["connected"] and state["engine"] == "SQLite"
    assert len(state["tables"]) == 3
    assert "DATABASE_URL" not in state
    rows = (await client.get("/api/storage/tables/runs")).json()
    assert rows["rows"][0]["id"] == run["id"]
    saved = (await client.get(f"/api/runs/{run['id']}/checkpoints")).json()["checkpoints"]
    assert len(saved) == 3
    detail = (await client.get(f"/api/runs/{run['id']}/checkpoints/{saved[-1]['id']}")).json()
    assert detail["snapshot"]["step"] == 0
    summary = (await client.get(f"/api/runs/{run['id']}/summary")).json()
    assert summary["modelCalls"] == 2 and summary["toolCalls"] == 1
    assert summary["checkpointCount"] == 3 and summary["terminalCount"] == 1
    assert (await client.get("/api/storage/tables/unknown")).status_code == 400
    assert (await client.get("/api/storage/tables/runs?limit=101")).status_code == 422
    assert (await client.get("/api/storage/tables/runs?offset=-1")).status_code == 422
