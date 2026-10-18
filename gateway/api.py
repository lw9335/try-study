"""第 7 步：创建/订阅/取消/回放/恢复/审计接口。/docs 可交互查看 API。"""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from gateway.adapters.aliyun import AliyunAdapter
from gateway.adapters.demo import DemoAdapter
from gateway.agent import AgentGateway
from gateway.config import Settings
from gateway.events import GatewayError, RunStatus
from gateway.locking import database_lease
from gateway.model import MODEL_PROFILES, ResumeInput, RunInput
from gateway.sse import SSEFrame, encode_sse
from gateway.store import RunStore

WEB = Path(__file__).resolve().parent.parent / "web"


def validate_input(value: RunInput):
    profile = next((p for p in MODEL_PROFILES if p.id == value.model), None)
    if not value.prompt.strip() or not profile:
        raise GatewayError("INVALID_REQUEST", "请提供非空 prompt 和有效 model")
    if value.thinking and not profile.thinking:
        raise GatewayError("UNSUPPORTED_THINKING", "此模型配置不支持思考模式")
    if value.tools and not profile.tools:
        raise GatewayError("UNSUPPORTED_TOOLS", "此模型配置不支持工具")


def parse_cursor(raw: str | None, last_seq: int) -> int:
    try:
        if raw is not None and (not raw.isascii() or not raw.isdecimal()):
            raise ValueError
        cursor = int(raw or "0")
        if not 0 <= cursor <= last_seq:
            raise ValueError
        return cursor
    except (ValueError, TypeError):
        raise GatewayError("INVALID_CURSOR", "事件游标必须是有效范围内的非负整数") from None


def create_app(settings: Settings | None = None, gateway: AgentGateway | None = None) -> FastAPI:
    config = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if gateway is not None:
            app.state.gateway = gateway
            yield
            return
        location = config.database_url or config.database
        with database_lease(location):
            store = RunStore(location)
            model = None
            try:
                if config.legacy_sqlite:
                    store.import_sqlite(config.legacy_sqlite)
                store.recover_interrupted()
                model = (
                    DemoAdapter()
                    if config.provider == "demo"
                    else AliyunAdapter(
                        config.api_key,
                        config.base_url,
                    )
                )
                service = AgentGateway(store, model, timeout=config.timeout)
                app.state.gateway = service
                try:
                    yield
                finally:
                    await service.shutdown()
            finally:
                if isinstance(model, AliyunAdapter):
                    await model.aclose()
                store.close()

    app = FastAPI(title="Streaming Agent Gateway", version="1.0.0", lifespan=lifespan)
    if gateway is not None:
        app.state.gateway = gateway

    @app.middleware("http")
    async def local_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        expected = f"{request.url.scheme}://{request.headers.get('host')}"
        if origin and origin != expected:
            return JSONResponse(
                {"error": {"code": "FORBIDDEN", "message": "仅允许同源请求"}}, status_code=403
            )
        # 请求体限额与 JSON 验证在读取前拦截，包括无 Content-Length 的分块传输。
        if request.method == "POST":
            chunks = []
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > 65536:
                    return JSONResponse(
                        {"error": {"code": "INVALID_REQUEST", "message": "请求体过大"}}, status_code=413
                    )
                chunks.append(chunk)
            request._body = b"".join(chunks)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = response.headers.get("Cache-Control", "no-store")
        # /docs 使用 FastAPI 自带 CDN，业务页面则严格限定本地资源。
        if request.url.path in ("/", "/app.js", "/style.css"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                "frame-ancestors 'none'"
            )
        return response

    @app.exception_handler(GatewayError)
    async def gateway_error(_request: Request, exc: GatewayError):
        status = {
            "RUN_NOT_FOUND": 404,
            "CHECKPOINT_NOT_FOUND": 404,
            "RUN_NOT_RESUMABLE": 409,
            "CAPACITY_EXCEEDED": 503,
        }.get(exc.code, 400)
        return JSONResponse({"error": exc.as_dict()}, status_code=status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, _exc: RequestValidationError):
        # 不回显非法请求体，防止密钥被错误当作字段提交后进入审计输出。
        return JSONResponse(
            {"error": {"code": "INVALID_REQUEST", "message": "请求参数或 JSON 格式无效"}}, status_code=422
        )

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(WEB / "index.html")

    @app.get("/app.js", include_in_schema=False)
    async def script():
        return FileResponse(WEB / "app.js", media_type="text/javascript")

    @app.get("/style.css", include_in_schema=False)
    async def style():
        return FileResponse(WEB / "style.css", media_type="text/css")

    @app.get("/api/health")
    async def health():
        service().store._db.execute("SELECT 1").fetchone()
        return {"ok": True, "provider": config.provider}

    @app.get("/api/models")
    async def models():
        return {
            "provider": config.provider,
            "baseUrl": config.base_url if config.provider == "aliyun" else None,
            "models": [p.model_dump() for p in MODEL_PROFILES],
        }

    def service() -> AgentGateway:
        return app.state.gateway

    @app.get("/api/storage")
    async def storage():
        return {**service().store.database_stats(), "agent": service().stats()}

    @app.get("/api/storage/tables/{table}")
    async def storage_table(
        table: str, offset: int = Query(default=0, ge=0), limit: int = Query(default=20, ge=1, le=100)
    ):
        return service().store.inspect_table(table, offset, limit)

    @app.get("/api/runs")
    async def runs():
        return service().store.list()

    @app.post("/api/runs", status_code=201)
    async def create_run(value: RunInput):
        validate_input(value)
        profile = next(p for p in MODEL_PROFILES if p.id == value.model)
        return service().create(
            value.model_copy(
                update={
                    "prompt": value.prompt.strip(),
                    "thinking": value.thinking or profile.thinking_mode == "required",
                }
            )
        )

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: UUID):
        return service().store.get(str(run_id))

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel_run(run_id: UUID):
        return service().cancel(str(run_id))

    @app.post("/api/runs/{run_id}/resume", status_code=201)
    async def resume_run(run_id: UUID, value: ResumeInput | None = None):
        return service().resume(str(run_id), value.checkpoint_id if value else None)

    @app.get("/api/runs/{run_id}/summary")
    async def summary(run_id: UUID):
        return service().store.run_summary(str(run_id))

    @app.get("/api/runs/{run_id}/checkpoints")
    async def checkpoint_history(run_id: UUID):
        return {"runId": str(run_id), "checkpoints": service().store.checkpoints(str(run_id))}

    @app.get("/api/runs/{run_id}/checkpoints/{checkpoint_id}")
    async def saved_checkpoint(run_id: UUID, checkpoint_id: UUID):
        return service().store.checkpoint(str(run_id), str(checkpoint_id))

    @app.get("/api/runs/{run_id}/checkpoint")
    async def checkpoint(run_id: UUID):
        return service().store.get(str(run_id))["checkpoint"]

    @app.get("/api/runs/{run_id}/trace")
    async def trace(run_id: UUID, after: str | None = None):
        store = service().store
        run = store.get(str(run_id))
        cursor = parse_cursor(after, run["lastSeq"])
        page = store.events(str(run_id), cursor)
        next_cursor = page[-1]["seq"] if page else cursor
        return {
            "run": run,
            "events": page,
            "nextCursor": next_cursor,
            "hasMore": next_cursor < run["lastSeq"],
        }

    @app.get("/api/runs/{run_id}/events")
    async def events(run_id: UUID, request: Request, after: str | None = None):
        store = service().store
        key = str(run_id)
        run = store.get(key)
        # 自动重连使用 Last-Event-ID，优先于首次打开页面的 query 参数。
        cursor = parse_cursor(request.headers.get("last-event-id", after), run["lastSeq"])

        async def stream():
            nonlocal cursor
            # 先注册再查 DB；通知只用于唤醒，事件内容始终从 SQLite 读取。
            with store.subscribe(key) as signal:
                yield b": connected\n\n"
                while not await request.is_disconnected():
                    signal.clear()
                    page = store.events(key, cursor)
                    for event in page:
                        cursor = event["seq"]
                        yield encode_sse(
                            SSEFrame(
                                json.dumps(event, ensure_ascii=False),
                                event["type"],
                                str(cursor),
                            )
                        )
                    latest = store.get(key)
                    if cursor < latest["lastSeq"]:
                        continue
                    if latest["status"] != RunStatus.RUNNING:
                        return
                    try:
                        await asyncio.wait_for(signal.wait(), timeout=15)
                    except TimeoutError:
                        yield b": heartbeat\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    return app
