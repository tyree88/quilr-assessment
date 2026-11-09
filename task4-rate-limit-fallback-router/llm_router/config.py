from pathlib import Path

from pydantic import BaseModel, ConfigDict, PositiveFloat, PositiveInt


class Settings(BaseModel):
    """Gateway knobs. Defaults are the brief's numbers; tests override per case."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    limit_tokens: PositiveInt = 50_000      # per tenant per window
    window_ms: PositiveInt = 60_000         # integer ms, same unit as DB rows
    upstream_timeout_s: PositiveFloat = 3.0  # brief: "times out after 3000ms"; per provider
    busy_timeout_ms: PositiveInt = 5_000    # SQLite waits this long for the write lock
    db_path: Path = Path("gateway.db")      # on disk, per brief
