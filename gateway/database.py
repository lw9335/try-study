"""数据库连接层：开发使用 SQLite，Compose 使用 PostgreSQL，Store 共用事务语义。"""

import sqlite3
from pathlib import Path


class SQLDatabase:
    def __init__(self, location: str):
        self.postgres = location.startswith(("postgresql://", "postgres://"))
        self.location = location
        if self.postgres:
            import psycopg

            self.connection = psycopg.connect(location, autocommit=True, connect_timeout=10)
            # 一个 schema 只允许一个 Agent worker；锁随进程/连接关闭自动释放。
            locked = self.execute(
                "SELECT pg_try_advisory_lock(hashtext(current_database()), hashtext(current_schema()))"
            ).fetchone()[0]
            if not locked:
                self.connection.close()
                raise RuntimeError("该 PostgreSQL Run Store 已有 worker 在运行")
        else:
            if location != ":memory:":
                Path(location).parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(location, isolation_level=None)
            self.execute("PRAGMA journal_mode=WAL")
            self.execute("PRAGMA foreign_keys=ON")
            self.execute("PRAGMA busy_timeout=5000")

    def execute(self, sql: str, values=()):
        if self.postgres:
            sql = sql.replace("?", "%s").replace("BEGIN IMMEDIATE", "BEGIN")
        return self.connection.execute(sql, values)

    def executescript(self, script: str):
        for sql in script.split(";"):
            if sql.strip():
                self.execute(sql)

    def columns(self, table: str) -> list[dict]:
        if table not in {"runs", "events", "checkpoints"}:
            raise ValueError("Unknown table")
        if self.postgres:
            return [
                {"name": row[0], "type": row[1]}
                for row in self.execute(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema=current_schema() AND table_name=? ORDER BY ordinal_position",
                    (table,),
                ).fetchall()
            ]
        return [{"name": row[1], "type": row[2]} for row in self.execute(f"PRAGMA table_info({table})")]

    def close(self):
        self.connection.close()
