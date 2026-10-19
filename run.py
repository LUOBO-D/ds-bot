"""启动入口：python run.py"""

from __future__ import annotations

import uvicorn

from qqbot.config import settings


def main() -> None:
    uvicorn.run(
        "qqbot.main:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
        reload=False,
    )


if __name__ == "__main__":
    main()
