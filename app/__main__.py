"""Arranque: python -m app"""

import logging

import uvicorn

from .config import load


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    cfg = load()
    uvicorn.run("app.main:create_app", factory=True, host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
