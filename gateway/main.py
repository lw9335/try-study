"""python -m gateway.main；内置入口始终使用一个 worker。"""

import uvicorn

from gateway.api import create_app
from gateway.config import Settings


def main():
    settings = Settings.from_env()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        workers=1,
        timeout_graceful_shutdown=5,
    )


if __name__ == "__main__":
    main()
