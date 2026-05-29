-- Cycle 1 (identity research, 2026-05-29): track the Polymarket-26-list
-- specialty category for each endorsed wallet. Lets us check whether a whale
-- is operating inside or outside their named lane.

ALTER TABLE whale_watchlist ADD COLUMN polymarket_specialty TEXT;
