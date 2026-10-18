"""环境配置；.env 中的密钥只在服务端读取。"""

import os
from dataclasses import dataclass, field
from urllib.parse import urlparse

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    provider: str = "demo"
    api_key: str = field(default="", repr=False)
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    database: str = "./data/gateway.sqlite"
    database_url: str = field(default="", repr=False)
    legacy_sqlite: str = ""
    host: str = "127.0.0.1"
    port: int = 3000
    timeout: float = 120

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        settings = cls(
            provider=os.getenv("MODEL_PROVIDER", "demo"),
            api_key=os.getenv("DASHSCOPE_API_KEY", ""),
            base_url=os.getenv("DASHSCOPE_BASE_URL", cls.base_url),
            database=os.getenv("DATABASE_PATH", "./data/gateway.sqlite"),
            database_url=os.getenv("DATABASE_URL", ""),
            legacy_sqlite=os.getenv("LEGACY_SQLITE_PATH", ""),
            host=os.getenv("HOST", "127.0.0.1"),
            port=int(os.getenv("PORT", "3000")),
            timeout=float(os.getenv("RUN_TIMEOUT_MS", "120000")) / 1000,
        )
        if settings.provider not in ("demo", "aliyun"):
            raise ValueError("MODEL_PROVIDER 必须是 demo 或 aliyun")
        if not 1 <= settings.port <= 65535 or not 0.1 <= settings.timeout <= 3600:
            raise ValueError("PORT 或 RUN_TIMEOUT_MS 超出范围")
        if urlparse(settings.base_url).scheme != "https":
            raise ValueError("DASHSCOPE_BASE_URL 必须使用 HTTPS")
        return settings
