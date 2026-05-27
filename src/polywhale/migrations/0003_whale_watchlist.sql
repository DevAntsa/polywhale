-- DB-backed whale watchlist so we can auto-discover new sharps weekly
-- instead of editing watchlist.py by hand.

CREATE TABLE whale_watchlist (
    wallet              TEXT PRIMARY KEY,
    label               TEXT,           -- pseudonym from lb-api
    source              TEXT NOT NULL,  -- 'manual' | 'auto-margin' | 'auto-wr'
    added_at            INTEGER NOT NULL,
    last_classified_at  INTEGER,
    margin_pct          REAL,
    profit_usd          REAL,
    volume_usd          REAL,
    shape               TEXT,           -- 'sharp' | 'arb_op' | 'hybrid' | 'unknown'
    active              INTEGER NOT NULL DEFAULT 1,
    deactivated_at      INTEGER,
    deactivated_reason  TEXT,
    notes               TEXT
);
CREATE INDEX idx_whale_watchlist_active ON whale_watchlist (active, profit_usd DESC);
CREATE INDEX idx_whale_watchlist_source ON whale_watchlist (source, active);
