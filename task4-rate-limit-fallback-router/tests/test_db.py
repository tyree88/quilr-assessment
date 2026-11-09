import sqlite3

import pytest

from llm_router.config import Settings
from llm_router.db import connect, init_db

pytestmark = pytest.mark.anyio


@pytest.fixture
async def settings(tmp_path):
    s = Settings(db_path=tmp_path / "gw.db")
    await init_db(s)
    return s


async def test_file_on_disk_in_wal_mode(settings):
    assert settings.db_path.exists()
    async with connect(settings) as conn:  # fresh connection: WAL must persist
        assert (await (await conn.execute("PRAGMA journal_mode")).fetchone())[0] == "wal"
        assert (await (await conn.execute("PRAGMA busy_timeout")).fetchone())[0] == 5000


def test_sqlite_supports_strict_tables():
    assert sqlite3.sqlite_version_info >= (3, 37)


@pytest.mark.parametrize(("ts_ms", "tokens"), [(1.5, 1), (1, -1)])
async def test_schema_rejects_float_time_and_negative_tokens(settings, ts_ms, tokens):
    async with connect(settings) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            await conn.execute(
                "INSERT INTO usage (tenant, ts_ms, tokens) VALUES ('t', ?, ?)", (ts_ms, tokens)
            )
