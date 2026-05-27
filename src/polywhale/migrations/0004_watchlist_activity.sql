-- Activity ranking on the watchlist: signals_30d + last_signal_at so we can
-- sort by who's actually firing, not just who's on the leaderboard.

ALTER TABLE whale_watchlist ADD COLUMN signals_30d INTEGER NOT NULL DEFAULT 0;
ALTER TABLE whale_watchlist ADD COLUMN last_signal_at INTEGER;

CREATE INDEX idx_whale_watchlist_activity ON whale_watchlist (signals_30d DESC);
