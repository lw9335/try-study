"""进程级数据库租约：避免第二个服务误把第一个服务的 Run 判为中断。"""

import os
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def database_lease(database: str):
    if database == ":memory:" or database.startswith(("postgresql://", "postgres://")):
        yield
        return
    path = Path(database + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as file:
        if path.stat().st_size == 0:
            file.write(b"0")
            file.flush()
        file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("该数据库已有服务在运行；请使用单个 worker 或不同 DATABASE_PATH") from None
        try:
            yield
        finally:
            file.seek(0)
            if os.name == "nt":
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(file, fcntl.LOCK_UN)
