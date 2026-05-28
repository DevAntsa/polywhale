-- Raw activity history pulled from data-api/activity, used for
-- historical backtest + walk-forward validation.

CREATE TABLE whale_activity_history (
    event_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet            TEXT NOT NULL,
    timestamp         INTEGER NOT NULL,
    type              TEXT NOT NULL,          -- 'TRADE', 'REDEEM', 'MAKER_REBATE', etc.
    condition_id      TEXT,                   -- market identifier
    asset             TEXT,                   -- token id (CTF token)
    side              TEXT,                   -- 'BUY' | 'SELL'
    price             REAL,                   -- per-share price at trade
    size              REAL,                   -- shares
    usdc_size         REAL,                   -- dollar amount
    outcome           TEXT,
    outcome_index     INTEGER,
    market_slug       TEXT,
    title             TEXT,
    transaction_hash  TEXT,
    fetched_at        INTEGER NOT NULL,
    UNIQUE(wallet, transaction_hash, type, outcome_index)
);

CREATE INDEX idx_whale_activity_wallet_ts ON whale_activity_history (wallet, timestamp);
CREATE INDEX idx_whale_activity_condition ON whale_activity_history (condition_id);
CREATE INDEX idx_whale_activity_type ON whale_activity_history (type);

-- Cached market resolutions from gamma so position reconstruction doesn't
-- hit the API every backtest run.
CREATE TABLE market_resolutions (
    condition_id     TEXT PRIMARY KEY,
    market_slug      TEXT,
    title            TEXT,
    closed           INTEGER NOT NULL,
    outcome_prices   TEXT,                    -- JSON list
    token_ids        TEXT,                    -- JSON list
    end_date         TEXT,
    fetched_at       INTEGER NOT NULL,
    last_refreshed   INTEGER NOT NULL
);

CREATE INDEX idx_market_resolutions_closed ON market_resolutions (closed);
