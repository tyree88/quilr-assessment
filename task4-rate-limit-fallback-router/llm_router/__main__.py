"""Run the gateway: `uv run python -m llm_router`.

PRIMARY_URL + SECONDARY_URL set -> real HTTP providers. Otherwise two fake providers;
FAKE_PRIMARY_MODE (ok | 429 | 500 | 400 | conn | boom | hang) shows the fallback.
"""

import logging
import os
import sys
from pathlib import Path

import httpx
import uvicorn

from llm_router.app import create_app
from llm_router.config import Settings
from llm_router.providers import FakeProvider, HttpProvider


def main() -> None:
    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    settings = Settings(db_path=Path(os.environ.get("GATEWAY_DB", "gateway.db")))
    primary_url, secondary_url = os.environ.get("PRIMARY_URL"), os.environ.get("SECONDARY_URL")
    if primary_url and secondary_url:
        client = httpx.AsyncClient()
        primary = HttpProvider("primary", primary_url, client)
        secondary = HttpProvider("secondary", secondary_url, client)
    else:
        mode = os.environ.get("FAKE_PRIMARY_MODE", "ok")
        primary = FakeProvider("primary", "ok" if mode == "hang" else mode,
                               delay_s=60.0 if mode == "hang" else 0.0)
        secondary = FakeProvider("secondary")
    uvicorn.run(create_app(settings, primary, secondary), host="127.0.0.1",
                port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    main()
