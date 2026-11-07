import pytest

import store


@pytest.fixture(autouse=True)
def reset_store():
    # Refunds mutate module state; restore the seed so tests stay independent.
    seed = dict(store._CUSTOMERS)
    yield
    store._CUSTOMERS.clear()
    store._CUSTOMERS.update(seed)
