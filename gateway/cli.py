"""第 8 步：Python CLI，创建/取消/订阅/恢复；Ctrl+C 只结束订阅。"""

import argparse
import asyncio
import json
import os
import sys

import httpx

from gateway.events import TERMINAL_EVENTS
from gateway.sse import decode_sse


async def api(client: httpx.AsyncClient, path: str, data: dict | None = None) -> dict:
    response = await (client.get(path) if data is None else client.post(path, json=data))
    if not response.is_success:
        raise RuntimeError(response.text)
    return response.json()


async def watch(client: httpx.AsyncClient, run_id: str, after: int = 0):
    cursor = after
    for _ in range(10):
        try:
            run = await api(client, f"/api/runs/{run_id}")
            if run["status"] != "running" and cursor >= run["lastSeq"]:
                return
            async with client.stream(
                "GET", f"/api/runs/{run_id}/events", headers={"Last-Event-ID": str(cursor)}, timeout=None
            ) as response:
                response.raise_for_status()
                async for frame in decode_sse(response.aiter_bytes()):
                    event = json.loads(frame.data)
                    if event["seq"] <= cursor:
                        continue
                    cursor = event["seq"]
                    if event["type"] == "model.delta":
                        print(event["data"]["text"], end="", flush=True)
                    elif event["type"] == "model.reasoning":
                        print(event["data"]["text"], end="", flush=True, file=sys.stderr)
                    else:
                        print(
                            f"\n[{cursor} {event['type']}] {json.dumps(event['data'], ensure_ascii=False)}",
                            file=sys.stderr,
                        )
                    if event["type"] in TERMINAL_EVENTS:
                        print()
                        return
        except (httpx.HTTPError, ValueError) as exc:
            print(f"订阅中断，最后游标 {cursor}：{type(exc).__name__}，正在重连", file=sys.stderr)
        await asyncio.sleep(1)
    raise RuntimeError(f"重连次数已用完。可运行 python -m gateway.cli watch {run_id} --after {cursor}")


async def execute(args):
    async with httpx.AsyncClient(base_url=args.url, timeout=30) as client:
        if args.command == "run":
            run = await api(
                client,
                "/api/runs",
                {
                    "prompt": args.prompt,
                    "model": args.model,
                    "tools": args.tools,
                    "thinking": args.thinking,
                },
            )
            print(f"Run: {run['id']}", file=sys.stderr)
            await watch(client, run["id"])
        elif args.command == "watch":
            await watch(client, args.run_id, args.after)
        elif args.command == "resume":
            payload = {"checkpointId": args.checkpoint} if args.checkpoint else {}
            run = await api(client, f"/api/runs/{args.run_id}/resume", payload)
            print(f"新 Run: {run['id']}", file=sys.stderr)
            await watch(client, run["id"])
        elif args.command == "cancel":
            print(
                json.dumps(
                    await api(client, f"/api/runs/{args.run_id}/cancel", {}), ensure_ascii=False, indent=2
                )
            )
        elif args.command == "get":
            print(json.dumps(await api(client, f"/api/runs/{args.run_id}"), ensure_ascii=False, indent=2))
        elif args.command == "checkpoints":
            print(
                json.dumps(
                    await api(client, f"/api/runs/{args.run_id}/checkpoints"), ensure_ascii=False, indent=2
                )
            )
        elif args.command == "storage":
            print(json.dumps(await api(client, "/api/storage"), ensure_ascii=False, indent=2))


def main():
    # Windows 终端与重定向输出统一使用 UTF-8，避免中文事件乱码。
    for output in (sys.stdout, sys.stderr):
        if hasattr(output, "reconfigure"):
            output.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Streaming Agent Gateway CLI")
    parser.add_argument("--url", default=os.getenv("GATEWAY_URL", "http://127.0.0.1:3000"))
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("prompt")
    run.add_argument("--model", default="qwen-plus")
    run.add_argument("--tools", action="store_true")
    run.add_argument("--thinking", action="store_true")
    commands.add_parser("storage")
    for command in ("watch", "cancel", "resume", "get", "checkpoints"):
        sub = commands.add_parser(command)
        sub.add_argument("run_id")
        if command == "watch":
            sub.add_argument("--after", type=int, default=0)
        if command == "resume":
            sub.add_argument("--checkpoint", help="从指定历史 checkpoint 恢复，默认使用最新安全边界")
    try:
        asyncio.run(execute(parser.parse_args()))
    except KeyboardInterrupt:
        print(
            "\n已退出订阅，Run 继续执行；显式取消请运行 python -m gateway.cli cancel <runId>。",
            file=sys.stderr,
        )
    except (RuntimeError, httpx.HTTPError) as exc:
        parser.exit(1, f"错误：{exc}\n")


if __name__ == "__main__":
    main()
