-- Maker-side routing shadow mode. We stay in paper for the actual bet bookkeeping
-- (entry_price, cost_usd, pnl_usd) but annotate each bet with what real-money
-- execution would have looked like under maker-first routing with taker fallback.
--
-- entry_route / exit_route:
--   'maker'                — limit order filled inside the wait window
--   'taker'                — taker placed immediately (signal was time-critical)
--   'maker_fallback_taker' — limit didn't fill, fell back to market
--
-- entry_price_routed / exit_price_routed:
--   the actual fill price real-money would have hit
-- entry_fee_usd / exit_fee_usd:
--   signed: negative = fee paid (taker), positive = rebate captured (maker)

ALTER TABLE poly_paper_bets ADD COLUMN entry_route TEXT;
ALTER TABLE poly_paper_bets ADD COLUMN entry_price_routed REAL;
ALTER TABLE poly_paper_bets ADD COLUMN entry_fee_usd REAL;
ALTER TABLE poly_paper_bets ADD COLUMN exit_route TEXT;
ALTER TABLE poly_paper_bets ADD COLUMN exit_price_routed REAL;
ALTER TABLE poly_paper_bets ADD COLUMN exit_fee_usd REAL;
ALTER TABLE poly_paper_bets ADD COLUMN net_pnl_routed_usd REAL;
