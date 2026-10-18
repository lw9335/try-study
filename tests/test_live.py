"""真实 TCP/HTTP 验收，验证 ASGI 内存测试无法覆盖的流式断线与进程重启。"""

import asyncio
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from gateway.locking import database_lease
from gateway.sse import decode_sse


class LiveServer:
    def __init__(self, tmp_path):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]
        self.database = str(tmp_path / "live.sqlite")
        self.root = Path(__file__).resolve().parents[1]
        self.process = None

    async def start(self):
        env = {
            **os.environ,
            "HOST": "127.0.0.1",
            "PORT": str(self.port),
            "MODEL_PROVIDER": "demo",
            "DATABASE_PATH": self.database,
            "DATABASE_URL": "",
            "PYTHONUTF8": "1",
        }
        self.process = await asyncio.to_thread(
            subprocess.Popen,
            [sys.executable, "-m", "gateway.main"],
            cwd=self.root,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        async with httpx.AsyncClient() as client:
            for _ in range(100):
                if self.process.poll() is not None:
                    raise RuntimeError("Live server exited before startup")
                try:
                    response = await client.get(f"http://127.0.0.1:{self.port}/api/health")
                    if response.is_success:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.05)
        raise TimeoutError("Live server failed to start")

    async def stop(self):
        if self.process and self.process.poll() is None:
            self.process.kill()  # 故意模拟进程崩溃，验证 checkpoint 与事件耐久性。
            await asyncio.to_thread(self.process.wait, 5)


@pytest.fixture
async def live(tmp_path):
    server = LiveServer(tmp_path)
    try:
        await server.start()
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{server.port}", timeout=10) as client:
            yield client, server
    finally:
        await server.stop()


async def test_socket_disconnect_replay_cancel_and_crash_recovery(live):
    client, server = live
    run = (await client.post("/api/runs", json={"prompt": "test streaming"})).json()
    key = run["id"]
    received = []
    async with client.stream("GET", f"/api/runs/{key}/events") as response:
        async for frame in decode_sse(response.aiter_bytes()):
            event = json.loads(frame.data)
            received.append(event)
            if event["type"] == "model.delta":
                break
    # 关闭真实 TCP 订阅连接后，模型 Run 必须继续。
    latest = (await client.get(f"/api/runs/{key}")).json()
    assert latest["status"] == "running"
    assert latest["ttftMs"] < 2000
    await asyncio.sleep(0.1)
    assert (await client.get(f"/api/runs/{key}")).json()["lastSeq"] > received[-1]["seq"]
    cursor = received[-1]["seq"]
    await client.post(f"/api/runs/{key}/cancel")
    async with client.stream(
        "GET", f"/api/runs/{key}/events", headers={"Last-Event-ID": str(cursor)}
    ) as response:
        replay = [json.loads(f.data) async for f in decode_sse(response.aiter_bytes())]
    assert replay[0]["seq"] == cursor + 1
    assert replay[-1]["type"] == "run.cancelled"
    trace = (await client.get(f"/api/runs/{key}/trace")).json()
    assert sum(e["type"] == "model.started" for e in trace["events"]) == 1

    # 新 Run 执行一轮工具后，停止进程；安全 checkpoint 应保存到工具结果。
    interrupted = (await client.post("/api/runs", json={"prompt": "12 + 30", "tools": True})).json()
    interrupted_id = interrupted["id"]
    async with client.stream("GET", f"/api/runs/{interrupted_id}/events") as response:
        async for frame in decode_sse(response.aiter_bytes()):
            if json.loads(frame.data)["type"] == "checkpoint.saved":
                break
    await server.stop()
    await server.start()
    restored = (await client.get(f"/api/runs/{interrupted_id}")).json()
    assert restored["status"] == "failed"
    assert restored["checkpoint"]["step"] == 1
    trace = (await client.get(f"/api/runs/{interrupted_id}/trace")).json()
    assert trace["events"][-1]["data"]["code"] == "PROCESS_INTERRUPTED"
    child = (await client.post(f"/api/runs/{interrupted_id}/resume")).json()
    assert child["id"] != interrupted_id
    assert child["checkpoint"]["messages"][-1]["role"] == "tool"
    await client.post(f"/api/runs/{child['id']}/cancel")


def test_database_lease_prevents_multiple_workers(tmp_path):
    database = str(tmp_path / "lease.sqlite")
    with database_lease(database):
        with pytest.raises(RuntimeError, match="worker"):
            with database_lease(database):
                pass
