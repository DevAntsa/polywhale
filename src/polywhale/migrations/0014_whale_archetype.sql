-- Cycle 2 (archetype research, 2026-05-29). Five strategy archetypes are
-- structurally uncopyable by a retail Finnish operator (news-arb, oracle-edge,
-- insider, market-making, cross-platform hedging). Two are sizing-discounted
-- (narrative trading, resolution/endgame). Rest are full-Kelly.
--
-- retail_copyable defaults to 1 — we only mark 0 when we have evidence of an
-- uncopyable playbook (manual classification + future fingerprint detection).

ALTER TABLE whale_watchlist ADD COLUMN playbook_archetype TEXT;
ALTER TABLE whale_watchlist ADD COLUMN retail_copyable INTEGER NOT NULL DEFAULT 1;
