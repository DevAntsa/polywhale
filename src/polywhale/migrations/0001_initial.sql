-- Initial schema for polywhale. Polymarket-only; no sportsbook tables.

-- Order book snapshots from CLOB /book endpoint.
CREATE TABLE polymarket_books (
    snapshot_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    market_slug      TEXT NOT NULL,
    token_id         TEXT NOT NULL,
    outcome          TEXT,
    captured_at      INTEGER NOT NULL,
    server_ts        INTEGER,
    best_bid         REAL,
    best_ask         REAL,
    spread           REAL,
    bid_depth_top5pc REAL,
    ask_depth_top5pc REAL,
    last_trade_price REAL,
    tick_size        REAL,
    neg_risk         INTEGER,
    book_json        TEXT NOT NULL
);
CREATE INDEX idx_polymarket_books_market_time ON polymarket_books (market_slug, captured_at);
CREATE INDEX idx_polymarket_books_token_time  ON polymarket_books (token_id, captured_at);


-- Whale open positions, polled from data-api /positions.
-- History-preserving: each call appends, doesn't update.
CREATE TABLE whale_positions (
    snapshot_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet           TEXT NOT NULL,
    asset_id         TEXT NOT NULL,
    condition_id     TEXT,
    market_slug      TEXT,
    event_slug       TEXT,
    title            TEXT,
    outcome          TEXT,
    size             REAL,
    avg_price        REAL,
    current_price    REAL,
    current_value    REAL,
    initial_value    REAL,
    cash_pnl         REAL,
    realized_pnl     REAL,
    percent_pnl      REAL,
    end_date         TEXT,
    neg_risk         INTEGER,
    captured_at      INTEGER NOT NULL,
    raw_json         TEXT
);
CREATE INDEX idx_whale_positions_wallet_time  ON whale_positions (wallet, captured_at);
CREATE INDEX idx_whale_positions_asset        ON whale_positions (asset_id);
CREATE INDEX idx_whale_positions_market_slug  ON whale_positions (market_slug);


-- Whale leaderboard classifications: who is a 'sharp' vs 'arb_op'.
CREATE TABLE whale_profiles (
    profile_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet       TEXT NOT NULL,
    pseudonym    TEXT,
    name         TEXT,
    window       TEXT NOT NULL,
    profit       REAL,
    volume       REAL,
    margin_pct   REAL,
    shape        TEXT NOT NULL,
    captured_at  INTEGER NOT NULL
);
CREATE INDEX idx_whale_profiles_wallet ON whale_profiles (wallet);
CREATE INDEX idx_whale_profiles_shape  ON whale_profiles (shape, captured_at);


-- Detected position changes (diff between consecutive whale snapshots).
CREATE TABLE whale_signals (
    signal_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet              TEXT NOT NULL,
    signal_type         TEXT NOT NULL,
    asset_id            TEXT,
    market_slug         TEXT,
    title               TEXT,
    outcome             TEXT,
    old_size            REAL,
    new_size            REAL,
    current_price       REAL,
    prev_captured_at    INTEGER,
    latest_captured_at  INTEGER NOT NULL,
    detected_at         INTEGER NOT NULL,
    alerted_at          INTEGER
);
CREATE INDEX idx_whale_signals_wallet_time ON whale_signals (wallet, detected_at);
CREATE INDEX idx_whale_signals_alerted     ON whale_signals (alerted_at);
CREATE INDEX idx_whale_signals_type        ON whale_signals (signal_type, detected_at);


-- Combinatorial arbs detected on neg-risk event groups.
CREATE TABLE combo_arbs (
    arb_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_slug       TEXT NOT NULL,
    event_title      TEXT,
    captured_at      INTEGER NOT NULL,
    outcomes_count   INTEGER,
    sum_best_ask     REAL NOT NULL,
    edge_pct         REAL NOT NULL,
    legs_json        TEXT NOT NULL,
    alerted_at       INTEGER
);
CREATE INDEX idx_combo_arbs_event_time ON combo_arbs (event_slug, captured_at);
CREATE INDEX idx_combo_arbs_edge       ON combo_arbs (edge_pct DESC);


-- Paper bets recorded against real Polymarket prices.
CREATE TABLE poly_paper_bets (
    bet_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source            TEXT NOT NULL,
    source_ref_id     INTEGER,
    market_slug       TEXT NOT NULL,
    event_slug        TEXT,
    token_id          TEXT NOT NULL,
    side              TEXT NOT NULL,
    outcome_title     TEXT,
    entry_price       REAL NOT NULL,
    size_shares       REAL NOT NULL,
    cost_usd          REAL NOT NULL,
    placed_at         INTEGER NOT NULL,
    settled_at        INTEGER,
    resolved_outcome  TEXT,
    payout_per_share  REAL,
    pnl_usd           REAL,
    notes             TEXT,
    alerted_at        INTEGER
);
CREATE INDEX idx_poly_paper_bets_settled ON poly_paper_bets (settled_at);
CREATE INDEX idx_poly_paper_bets_market  ON poly_paper_bets (market_slug);
CREATE INDEX idx_poly_paper_bets_event   ON poly_paper_bets (event_slug);
CREATE INDEX idx_poly_paper_bets_source  ON poly_paper_bets (source, placed_at);
