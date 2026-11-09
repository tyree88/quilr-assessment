import pytest


@pytest.fixture
def anyio_backend() -> str:
    # The anyio pytest plugin runs each @pytest.mark.anyio test once per backend
    # this fixture returns. Only asyncio: aiosqlite and Starlette target it, and
    # asyncio.timeout() is part of the code under test.
    return "asyncio"
