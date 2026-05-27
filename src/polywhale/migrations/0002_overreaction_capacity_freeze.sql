-- Additions from 2026-05-27 research session:
--   1. whale_signals  : record recent price move so we can fade chased trades
--   2. poly_paper_bets: cap arb leg sizes by real book depth + freeze on dispute

ALTER TABLE whale_signals ADD COLUMN recent_move_pct REAL;
ALTER TABLE whale_signals ADD COLUMN conviction_discount REAL;

ALTER TABLE poly_paper_bets ADD COLUMN intended_shares REAL;
ALTER TABLE poly_paper_bets ADD COLUMN capacity_capped INTEGER NOT NULL DEFAULT 0;
ALTER TABLE poly_paper_bets ADD COLUMN frozen_at INTEGER;
ALTER TABLE poly_paper_bets ADD COLUMN frozen_reason TEXT;

CREATE INDEX idx_poly_paper_bets_frozen ON poly_paper_bets (frozen_at);
