-- Link paper bets to the EXIT/TRIM signal that closed them, so the alerter
-- can show "we closed this on the back of signal N → PnL +$X".

ALTER TABLE poly_paper_bets ADD COLUMN closed_by_signal_id INTEGER;
CREATE INDEX idx_poly_paper_bets_closed_by ON poly_paper_bets (closed_by_signal_id);
