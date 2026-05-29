-- Cycle 3 (longevity research, 2026-05-29). Akey 2026 + Yang 2026: 44% OOS
-- skill persistence. Top survival features ranked by evidence:
--   1) sample size (n>=50)
--   2) maker-vs-taker share        — not yet collected, placeholder weight
--   3) market breadth with focus
--   4) recovery factor
--   5) return skew
--
-- survival_score is 0-100, recomputed periodically by whale_review. Wallets
-- with score < 35 AND n_resolved >= 30 are candidates for auto-drop; 35-50
-- are size-cut to 50%; >= 50 get full Kelly stake.

ALTER TABLE whale_watchlist ADD COLUMN survival_score REAL;
ALTER TABLE whale_watchlist ADD COLUMN survival_score_at INTEGER;
ALTER TABLE whale_watchlist ADD COLUMN survival_n_resolved INTEGER NOT NULL DEFAULT 0;
ALTER TABLE whale_watchlist ADD COLUMN survival_maker_share REAL;
