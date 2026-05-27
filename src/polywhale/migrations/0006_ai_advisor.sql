-- Logged-advisor pattern: record what the AI said alongside the mechanical
-- baseline so we can backtest "AI on vs AI off" once we have settled trades.

ALTER TABLE poly_paper_bets ADD COLUMN mechanical_stake REAL;
ALTER TABLE poly_paper_bets ADD COLUMN ai_multiplier REAL;
ALTER TABLE poly_paper_bets ADD COLUMN ai_reason TEXT;
ALTER TABLE poly_paper_bets ADD COLUMN ai_confidence TEXT;
