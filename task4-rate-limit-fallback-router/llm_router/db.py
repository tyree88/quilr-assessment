from pathlib import Path

import aiosqlite

from llm_router.config import Settings

_SCHEMA = (Path(__file__).parent / "schema.sql").read_text()


def connect(settings: Settings) -> aiosqlite.Connection:
    """One short-lived connection per transaction (decision 1). Use as `async with connect(s)`."""
    # isolation_level=None: no implicit BEGIN; we issue BEGIN IMMEDIATE ourselves.
    # timeout=: sqlite3's busy handler, how long to wait for another connection's write lock.
    # Not awaited here: aiosqlite's Connection starts its thread in __aenter__; awaiting
    # first and then entering starts it twice (RuntimeError, leaked non-daemon thread).
    return aiosqlite.connect(
        settings.db_path,
        isolation_level=None,
        timeout=settings.busy_timeout_ms / 1000,
    )


async def init_db(settings: Settings) -> None:
    """Run once at startup: WAL mode (persists in the file) + schema."""
    async with connect(settings) as conn:
        await conn.execute("PRAGMA journal_mode = WAL")
        await conn.executescript(_SCHEMA)
