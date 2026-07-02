"""video2listener 入口。启动 Web 服务。"""

import logging
from src.web.server import app
from src.config import get_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

if __name__ == "__main__":
    import uvicorn

    cfg = get_config()
    port = cfg["web"]["port"]
    log_level = cfg["app"].get("log_level", "info").lower()

    uvicorn.run(
        "src.web.server:app",
        host="127.0.0.1",
        port=port,
        log_level=log_level,
        reload=False,
    )
