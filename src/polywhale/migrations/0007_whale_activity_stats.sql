-- WR + recency stats on whale_watchlist computed from data-api/activity.
-- Used in whale-refresh to filter dormant or low-WR candidates BEFORE adding.

ALTER TABLE whale_watchlist ADD COLUMN win_rate_pct REAL;
ALTER TABLE whale_watchlist ADD COLUMN wr_sample_size INTEGER;
ALTER TABLE whale_watchlist ADD COLUMN last_trade_at INTEGER;

CREATE INDEX idx_whale_watchlist_wr ON whale_watchlist (win_rate_pct DESC);
CREATE INDEX idx_whale_watchlist_last_trade ON whale_watchlist (last_trade_at DESC);
