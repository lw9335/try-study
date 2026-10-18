"""第 5、10 步：SQLite Run Store + 持久化事件日志 + 安全步骤 checkpoint。"""

from __future__ import annotations

import asyncio
import json
import math
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from gateway.database import SQLDatabase
from gateway.events import EventType, GatewayError, RunEvent, RunStatus, utc_now
from gateway.model import RunInput


def clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


class RunStore:
    """同步短事务只在单个 asyncio 事件循环调用；支持 SQLite / PostgreSQL。"""

    def __init__(self, database: str = ":memory:"):
        self._db = SQLDatabase(database)
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, doc TEXT NOT NULL, created_at TEXT, status TEXT,
                model TEXT, trace_id TEXT, parent_run_id TEXT, last_seq INTEGER
            );
            CREATE TABLE IF NOT EXISTS events (
                run_id TEXT NOT NULL REFERENCES runs(id),
                seq INTEGER NOT NULL, doc TEXT NOT NULL, type TEXT, created_at TEXT,
                PRIMARY KEY(run_id, seq)
            );
            CREATE TABLE IF NOT EXISTS checkpoints (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL, seq INTEGER NOT NULL,
                step INTEGER NOT NULL, kind TEXT NOT NULL, created_at TEXT NOT NULL,
                snapshot TEXT NOT NULL, source_checkpoint_id TEXT,
                UNIQUE(run_id, seq), FOREIGN KEY(run_id, seq) REFERENCES events(run_id, seq)
            );
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS store_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        self._migrate_legacy()
        self._listeners: dict[str, set[asyncio.Event]] = {}

    def _migrate_legacy(self):
        if self._db.execute("SELECT version FROM schema_migrations WHERE version=2").fetchone():
            return
        with self._transaction():
            for table, definitions in {
                "runs": {
                    "created_at": "TEXT",
                    "status": "TEXT",
                    "model": "TEXT",
                    "trace_id": "TEXT",
                    "parent_run_id": "TEXT",
                    "last_seq": "INTEGER",
                },
                "events": {"type": "TEXT", "created_at": "TEXT"},
            }.items():
                existing = {column["name"] for column in self._db.columns(table)}
                for name, datatype in definitions.items():
                    if name not in existing:
                        self._db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {datatype}")
            for (doc,) in self._db.execute("SELECT doc FROM runs").fetchall():
                run = json.loads(doc)
                self._update_run(run)
                if (
                    run["lastSeq"]
                    and not self._db.execute(
                        "SELECT id FROM checkpoints WHERE run_id=?", (run["id"],)
                    ).fetchone()
                ):
                    # 旧版仅保存最新快照：明确标记 legacy，不伪造缺失的历史。
                    self._save_checkpoint(run, "legacy")
            for run_id, seq, doc in self._db.execute("SELECT run_id,seq,doc FROM events").fetchall():
                event = json.loads(doc)
                self._db.execute(
                    "UPDATE events SET type=?,created_at=? WHERE run_id=? AND seq=?",
                    (event["type"], event["timestamp"], run_id, seq),
                )
            self._db.executescript("""
                CREATE INDEX IF NOT EXISTS runs_created_idx ON runs(created_at);
                CREATE INDEX IF NOT EXISTS events_type_idx ON events(type);
                CREATE INDEX IF NOT EXISTS checkpoints_run_idx ON checkpoints(run_id,seq);
            """)
            self._db.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(2,?)", (utc_now(),))

    @contextmanager
    def _transaction(self):
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    def get(self, run_id: str) -> dict:
        row = self._db.execute("SELECT doc FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise GatewayError("RUN_NOT_FOUND", "Run 不存在")
        return json.loads(row[0])

    def list(self, limit: int = 50) -> list[dict]:
        return [
            json.loads(row[0])
            for row in self._db.execute(
                "SELECT doc FROM runs ORDER BY created_at DESC,id DESC LIMIT ?", (limit,)
            )
        ]

    def events(self, run_id: str, after: int = 0, limit: int = 256) -> list[dict]:
        return [
            json.loads(row[0])
            for row in self._db.execute(
                "SELECT doc FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?",
                (run_id, after, limit),
            )
        ]

    def create(self, request: RunInput, parent: dict | None = None) -> dict:
        now = utc_now()
        checkpoint = (
            clone(parent["checkpoint"])
            if parent
            else {
                "step": 0,
                "messages": [
                    {"role": "system", "content": "你是一个有帮助的助手。需要加法时使用提供的 add 工具。"},
                    {"role": "user", "content": request.prompt},
                ],
            }
        )
        run = {
            "id": str(uuid4()),
            "traceId": str(uuid4()),
            "parentRunId": parent["id"] if parent else None,
            "sourceCheckpointId": parent.get("resumeCheckpointId") if parent else None,
            "status": RunStatus.RUNNING,
            "input": request.model_dump(),
            "createdAt": now,
            "updatedAt": now,
            "lastSeq": 0,
            "ttftMs": None,
            "firstDeltaMs": None,
            "checkpoint": checkpoint,
        }
        with self._transaction():
            self._db.execute("INSERT INTO runs(id,doc) VALUES (?,?)", (run["id"], json.dumps(run)))
            self._write(
                run,
                EventType.RUN_CREATED,
                {
                    "model": request.model,
                    "parentRunId": run["parentRunId"],
                    "checkpointStep": checkpoint["step"],
                },
                checkpoint_kind="initial",
            )
        self._notify(run["id"])
        return self.get(run["id"])

    def _write(self, run: dict, kind: EventType, data: dict, checkpoint_kind: str | None = None) -> dict:
        run["lastSeq"] += 1
        run["updatedAt"] = utc_now()
        elapsed = max(
            0,
            int(
                (
                    datetime.fromisoformat(run["updatedAt"]) - datetime.fromisoformat(run["createdAt"])
                ).total_seconds()
                * 1000
            ),
        )
        if kind in (EventType.MODEL_DELTA, EventType.MODEL_REASONING) and run["firstDeltaMs"] is None:
            run["firstDeltaMs"] = elapsed
        # TTFT = Run 创建到首个非空答案片段的时间，排除 created / 心跳 / 思考。
        if kind == EventType.MODEL_DELTA and run["ttftMs"] is None:
            run["ttftMs"] = elapsed
        event = RunEvent(
            run_id=run["id"],
            trace_id=run["traceId"],
            seq=run["lastSeq"],
            type=kind,
            timestamp=run["updatedAt"],
            data=data,
        ).model_dump(mode="json", by_alias=True)
        self._db.execute(
            "INSERT INTO events(run_id,seq,doc,type,created_at) VALUES (?,?,?,?,?)",
            (
                run["id"],
                run["lastSeq"],
                json.dumps(event, ensure_ascii=False, allow_nan=False),
                str(kind),
                run["updatedAt"],
            ),
        )
        self._update_run(run)
        if checkpoint_kind:
            self._save_checkpoint(run, checkpoint_kind)
        return event

    def _update_run(self, run: dict):
        self._db.execute(
            "UPDATE runs SET doc=?,created_at=?,status=?,model=?,trace_id=?,parent_run_id=?,last_seq=? "
            "WHERE id=?",
            (
                json.dumps(run, ensure_ascii=False, allow_nan=False),
                run["createdAt"],
                run["status"],
                run["input"]["model"],
                run["traceId"],
                run.get("parentRunId"),
                run["lastSeq"],
                run["id"],
            ),
        )

    def _save_checkpoint(self, run: dict, kind: str):
        self._db.execute(
            "INSERT INTO checkpoints(id,run_id,seq,step,kind,created_at,snapshot,source_checkpoint_id) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                str(uuid4()),
                run["id"],
                run["lastSeq"],
                run["checkpoint"]["step"],
                kind,
                run["updatedAt"],
                json.dumps(run["checkpoint"], ensure_ascii=False, allow_nan=False),
                run.get("sourceCheckpointId") if kind == "initial" else None,
            ),
        )

    def checkpoints(self, run_id: str) -> list[dict]:
        self.get(run_id)
        rows = self._db.execute(
            "SELECT id,seq,step,kind,created_at,snapshot,source_checkpoint_id FROM checkpoints "
            "WHERE run_id=? ORDER BY seq DESC",
            (run_id,),
        ).fetchall()
        return [
            {
                "id": row[0],
                "runId": run_id,
                "seq": row[1],
                "step": row[2],
                "kind": row[3],
                "timestamp": row[4],
                "messageCount": len(json.loads(row[5])["messages"]),
                "sourceCheckpointId": row[6],
            }
            for row in rows
        ]

    def checkpoint(self, run_id: str, checkpoint_id: str) -> dict:
        row = self._db.execute(
            "SELECT snapshot FROM checkpoints WHERE run_id=? AND id=?", (run_id, checkpoint_id)
        ).fetchone()
        if not row:
            raise GatewayError("CHECKPOINT_NOT_FOUND", "Checkpoint 不属于此 Run 或不存在")
        info = next(c for c in self.checkpoints(run_id) if c["id"] == checkpoint_id)
        return {**info, "snapshot": json.loads(row[0])}

    def append(
        self, run_id: str, kind: EventType, data: dict | None = None, checkpoint: dict | None = None
    ) -> dict | None:
        if kind in (EventType.RUN_COMPLETED, EventType.RUN_FAILED, EventType.RUN_CANCELLED):
            raise ValueError("终态事件必须经 finish 写入")
        return self._commit(run_id, kind, data or {}, checkpoint=checkpoint)

    def finish(
        self, run_id: str, status: RunStatus, data: dict, checkpoint: dict | None = None
    ) -> dict | None:
        if status == RunStatus.RUNNING:
            raise ValueError("finish 必须提供终态")
        return self._commit(run_id, EventType(f"run.{status}"), data, status, checkpoint)

    def _commit(
        self,
        run_id: str,
        kind: EventType,
        data: dict,
        status: RunStatus | None = None,
        checkpoint: dict | None = None,
    ) -> dict | None:
        event = None
        with self._transaction():
            run = self.get(run_id)
            # compare-and-set：完成、取消和失败竞态只有一个终态胜出。
            if run["status"] == RunStatus.RUNNING:
                if status:
                    run["status"] = status
                if checkpoint is not None:
                    run["checkpoint"] = clone(checkpoint)
                checkpoint_kind = (
                    ("final" if status == RunStatus.COMPLETED else "step") if checkpoint else None
                )
                event = self._write(run, kind, data, checkpoint_kind)
        if event:
            self._notify(run_id)
        return event

    @contextmanager
    def subscribe(self, run_id: str) -> Iterator[asyncio.Event]:
        signal = asyncio.Event()
        self._listeners.setdefault(run_id, set()).add(signal)
        try:
            yield signal
        finally:
            listeners = self._listeners[run_id]
            listeners.discard(signal)
            if not listeners:
                del self._listeners[run_id]

    def _notify(self, run_id: str):
        for signal in self._listeners.get(run_id, ()):
            signal.set()

    def recover_interrupted(self):
        # 启动时只标记失败，不重跑不确定的上游请求。
        ids = self._db.execute("SELECT id FROM runs").fetchall()
        for (run_id,) in ids:
            if self.get(run_id)["status"] == RunStatus.RUNNING:
                self.finish(
                    run_id,
                    RunStatus.FAILED,
                    {
                        "code": "PROCESS_INTERRUPTED",
                        "message": "进程中断。可回放事件，或显式从 checkpoint 新建 Run。",
                        "retryable": True,
                    },
                )

    def close(self):
        self._db.close()

    def import_sqlite(self, source: str) -> dict:
        """仅向空目标库导入既有本地历史，源库只读。每次导入均为整体事务。"""
        if self._db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] or not Path(source).is_file():
            return {"imported": False}
        with sqlite3.connect(Path(source).resolve().as_uri() + "?mode=ro", uri=True) as legacy:
            tables = {row[0] for row in legacy.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"runs", "events"} <= tables:
                raise RuntimeError("历史 SQLite 缺少 runs/events 表")
            runs = [json.loads(row[0]) for row in legacy.execute("SELECT doc FROM runs")]
            events = [
                json.loads(row[0]) for row in legacy.execute("SELECT doc FROM events ORDER BY run_id,seq")
            ]
            checkpoints = (
                legacy.execute(
                    "SELECT id,run_id,seq,step,kind,created_at,snapshot,source_checkpoint_id FROM checkpoints"
                ).fetchall()
                if "checkpoints" in tables
                else []
            )
        with self._transaction():
            for run in runs:
                self._db.execute("INSERT INTO runs(id,doc) VALUES(?,?)", (run["id"], json.dumps(run)))
                self._update_run(run)
            for event in events:
                self._db.execute(
                    "INSERT INTO events(run_id,seq,doc,type,created_at) VALUES(?,?,?,?,?)",
                    (
                        event["runId"],
                        event["seq"],
                        json.dumps(event, ensure_ascii=False),
                        event["type"],
                        event["timestamp"],
                    ),
                )
            for row in checkpoints:
                self._db.execute(
                    "INSERT INTO checkpoints"
                    "(id,run_id,seq,step,kind,created_at,snapshot,source_checkpoint_id) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    row,
                )
            checkpoint_run_ids = {row[1] for row in checkpoints}
            for run in runs:
                if run["lastSeq"] and run["id"] not in checkpoint_run_ids:
                    self._save_checkpoint(run, "legacy")
            report = {"imported": True, "runs": len(runs), "events": len(events), "timestamp": utc_now()}
            self._db.execute(
                "INSERT INTO store_metadata(key,value) VALUES('legacy_import',?)", (json.dumps(report),)
            )
        return report

    def database_stats(self) -> dict:
        self._db.execute("SELECT 1").fetchone()
        tables = [
            {
                "name": table,
                "count": self._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                "columns": self._db.columns(table),
            }
            for table in ("runs", "events", "checkpoints")
        ]
        statuses = dict(self._db.execute("SELECT status,COUNT(*) FROM runs GROUP BY status").fetchall())
        size = (
            self._db.execute("SELECT pg_database_size(current_database())").fetchone()[0]
            if self._db.postgres
            else self._db.execute("PRAGMA page_count").fetchone()[0]
            * self._db.execute("PRAGMA page_size").fetchone()[0]
        )
        imported = self._db.execute("SELECT value FROM store_metadata WHERE key='legacy_import'").fetchone()
        expression = (
            "(doc::jsonb->>'ttftMs')::double precision"
            if self._db.postgres
            else "json_extract(doc,'$.ttftMs')"
        )
        samples = [
            row[0]
            for row in self._db.execute(
                f"SELECT {expression} FROM runs WHERE status='completed' ORDER BY created_at DESC LIMIT 500"
            ).fetchall()
            if row[0] is not None
        ]
        samples.sort()
        latency = {
            "sampleCount": len(samples),
            "scope": "当前库最近 500 个已完成 Run 的正文 TTFT（含历史演示数据，不作为真实模型测速）",
            "meanMs": round(sum(samples) / len(samples)) if samples else None,
            "p50Ms": samples[math.ceil(len(samples) * 0.5) - 1] if samples else None,
            "p95Ms": samples[math.ceil(len(samples) * 0.95) - 1] if samples else None,
        }
        return {
            "engine": "PostgreSQL" if self._db.postgres else "SQLite",
            "connected": True,
            "schemaVersion": 2,
            "sizeBytes": size,
            "tables": tables,
            "runStatuses": statuses,
            "subscribers": sum(map(len, self._listeners.values())),
            "workerMode": "single",
            "durability": "Docker PostgreSQL volume" if self._db.postgres else "SQLite WAL",
            "legacyImport": json.loads(imported[0]) if imported else None,
            "ttft": latency,
        }

    def inspect_table(self, table: str, offset: int = 0, limit: int = 20) -> dict:
        columns = {
            "runs": ["id", "status", "model", "last_seq", "parent_run_id", "trace_id", "created_at"],
            "events": ["run_id", "seq", "type", "created_at"],
            "checkpoints": ["id", "run_id", "seq", "step", "kind", "source_checkpoint_id", "created_at"],
        }.get(table)
        if not columns:
            raise GatewayError("INVALID_TABLE", "仅允许浏览 runs、events、checkpoints")
        order = "created_at DESC,id DESC" if table == "runs" else "created_at DESC,run_id,seq DESC"
        rows = self._db.execute(
            f"SELECT {','.join(columns)} FROM {table} ORDER BY {order} LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        total = self._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return {
            "table": table,
            "columns": columns,
            "rows": [dict(zip(columns, row, strict=True)) for row in rows],
            "offset": offset,
            "limit": limit,
            "total": total,
            "hasMore": offset + len(rows) < total,
        }

    def run_summary(self, run_id: str) -> dict:
        run = self.get(run_id)
        counts = dict(
            self._db.execute(
                "SELECT type,COUNT(*) FROM events WHERE run_id=? GROUP BY type", (run_id,)
            ).fetchall()
        )
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        for (doc,) in self._db.execute(
            "SELECT doc FROM events WHERE run_id=? AND type='model.usage'", (run_id,)
        ).fetchall():
            data = json.loads(doc)["data"]
            for key in usage:
                usage[key] += data.get(key, 0)
        return {
            "runId": run_id,
            "parentRunId": run.get("parentRunId"),
            "sourceCheckpointId": run.get("sourceCheckpointId"),
            "eventCounts": counts,
            "modelCalls": counts.get("model.started", 0),
            "toolCalls": counts.get("tool.completed", 0),
            "checkpointCount": len(self.checkpoints(run_id)),
            "usage": usage,
            "terminalCount": sum(
                counts.get(f"run.{status}", 0) for status in ("completed", "cancelled", "failed")
            ),
            "subscribers": len(self._listeners.get(run_id, ())),
            "durationMs": max(
                0,
                int(
                    (
                        datetime.fromisoformat(run["updatedAt"]) - datetime.fromisoformat(run["createdAt"])
                    ).total_seconds()
                    * 1000
                ),
            ),
        }
