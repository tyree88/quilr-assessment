CREATE TABLE IF NOT EXISTS usage (
    id      INTEGER PRIMARY KEY,                    -- rowid alias = reservation id
    tenant  TEXT    NOT NULL,                       -- sha256(api key), never the raw key
    ts_ms   INTEGER NOT NULL,                       -- wall clock, integer ms (decision 2)
    tokens  INTEGER NOT NULL CHECK (tokens >= 0)
) STRICT;

CREATE INDEX IF NOT EXISTS usage_tenant_ts ON usage (tenant, ts_ms);
