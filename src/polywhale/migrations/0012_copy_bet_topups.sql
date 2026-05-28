-- Track top-ups on existing copy positions. When a whale ADDS to a position
-- we already have an open bet for, we increase our position rather than
-- opening a new row. These columns track how many times that happened.

ALTER TABLE poly_paper_bets ADD COLUMN add_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE poly_paper_bets ADD COLUMN additions_total_usd REAL NOT NULL DEFAULT 0;
ALTER TABLE poly_paper_bets ADD COLUMN last_topup_at INTEGER;
