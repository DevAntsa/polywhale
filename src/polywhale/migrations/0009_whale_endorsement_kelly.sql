-- Two structural additions for Phase A + Phase B:
-- 1. Endorsement flag for officially-vetted wallets (Polymarket's smart-money list,
--    public profiles with traceable identity, etc). Endorsed wallets are exempt from
--    auto-drop regardless of in-app PnL pattern, because some genuine alpha
--    strategies (e.g. wokerjoesleeper's 81% WR low-prob-NOs algo) don't surface PnL
--    at our 60s polling cadence.
-- 2. Kelly sizing columns on whale_watchlist:
--    pnl_variance     — variance of per-trade PnL (USD²), required for Kelly formula
--    kelly_fraction   — computed 1/4 Kelly fraction (cached per refresh)
--    notes_endorsement — free-text reason for endorsement

ALTER TABLE whale_watchlist ADD COLUMN endorsed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE whale_watchlist ADD COLUMN endorsement_source TEXT;
ALTER TABLE whale_watchlist ADD COLUMN pnl_variance REAL;
ALTER TABLE whale_watchlist ADD COLUMN kelly_fraction REAL;
ALTER TABLE whale_watchlist ADD COLUMN risk_flags TEXT;
