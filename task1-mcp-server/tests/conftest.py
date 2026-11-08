import pytest

import store


@pytest.fixture
def anyio_backend():
    # anyio ships with mcp and has a pytest plugin, so no pytest-asyncio dependency.
    return "asyncio"


@pytest.fixture(autouse=True)
def reset_store():
    # Refunds mutate module state; restore the seed so tests stay independent.
    seed = dict(store._CUSTOMERS)
    yield
    store._CUSTOMERS.clear()
    store._CUSTOMERS.update(seed)
