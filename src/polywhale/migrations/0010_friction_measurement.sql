-- Friction measurement on copy bets — pre-real-money instrumentation.
-- Records what a real fill would have looked like at signal time, so we can
-- compute slippage + latency drag and validate paper-to-real translation.

ALTER TABLE poly_paper_bets ADD COLUMN hypothetical_real_entry REAL;
ALTER TABLE poly_paper_bets ADD COLUMN hypothetical_real_exit REAL;
ALTER TABLE poly_paper_bets ADD COLUMN entry_slippage_pct REAL;
ALTER TABLE poly_paper_bets ADD COLUMN exit_slippage_pct REAL;
ALTER TABLE poly_paper_bets ADD COLUMN entry_book_age_s INTEGER;
ALTER TABLE poly_paper_bets ADD COLUMN exit_book_age_s INTEGER;
