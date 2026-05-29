"""Auto paper-bet on whale signals + PnL on exits.

Hooked into whale-fast between persist_signals() and send_signal_alerts():

  - NEW or ADDED signal -> place_copy_bet(): record a paper_bet at the price
    observed when the signal fired, sized at `bankroll_usd * stake_pct *
    conviction_discount`. source='whale_copy', source_ref_id=signal_id.

  - EXIT or TRIM signal -> close_copy_bet(): find any open paper_bet with the
    same (wallet via source_ref_id linkage) + same asset_id, mark it settled
    at the new price, compute PnL = (exit_price - entry_price) * shares.
    closed_by_signal_id = the exiting signal so the alerter can fetch the PnL.

Sizing is fixed-$ times conviction (idea #10 from the strategy brainstorm).
Per-whale weighting (bigger size for proven WR) is Phase B once we have
real backtest data.

Limitation: if the bot wasn't running when the whale entered, there's no
open paper_bet to close — the EXIT alert just fires without a PnL line.
That's correct behavior.
"""

import logging
import sqlite3
import time

from polywhale.ai_advisor import (
    AIAdvice,
    build_context_from_signal,
    call_advisor,
)
from polywhale.friction_observer import (
    snapshot_entry_friction,
    snapshot_exit_friction,
)
from polywhale.whale_sizing import (
    check_portfolio_guards,
    compute_kelly_stake,
)

logger = logging.getLogger(__name__)

ENTRY_SIGNAL_TYPES = ("new_position", "added_size")
EXIT_SIGNAL_TYPES = ("closed_position", "reduced_size")


def current_deployed_usd(conn: sqlite3.Connection) -> float:
    """Sum of cost_usd across open whale_copy paper bets."""
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM poly_paper_bets "
        "WHERE source = 'whale_copy' AND settled_at IS NULL"
    ).fetchone()
    return float(row[0] or 0)


def process_copy_trades(
    conn: sqlite3.Connection,
    *,
    bankroll_usd: float,
    stake_pct: float,
    weight_by_conviction: bool = True,
    ai_api_key: str = "",
    ai_model: str = "",
    use_ai_advisor: bool = False,
) -> dict:
    """For each unalerted signal: open or close a paper copy bet.

    Idempotent: each signal is only processed once because we look for paper_bets
    already linked via source_ref_id or closed_by_signal_id.

    If use_ai_advisor is True, ENTRY signals also call the OpenRouter advisor to
    get a stake multiplier on top of the mechanical conviction-weighted base.
    """
    rows = conn.execute(
        "SELECT * FROM whale_signals WHERE alerted_at IS NULL"
    ).fetchall()
    opened = 0
    closed = 0
    skipped_bankroll = 0
    realized_pnl = 0.0
    ai_calls = 0
    for r in rows:
        sig = r["signal_type"]
        if sig in ENTRY_SIGNAL_TYPES:
            ai_advice = None
            if use_ai_advisor and ai_api_key:
                try:
                    ctx = build_context_from_signal(conn, r, bankroll=bankroll_usd)
                    ai_advice = call_advisor(
                        context=ctx, api_key=ai_api_key, model=ai_model,
                    )
                    ai_calls += 1
                except Exception as exc:
                    logger.warning(
                        "ai_advisor failed for signal %s: %s — falling back to 1.0x",
                        r["signal_id"], exc,
                    )
                    ai_advice = None
            deployed_before = current_deployed_usd(conn)
            if place_copy_bet(
                conn, r,
                bankroll_usd=bankroll_usd,
                stake_pct=stake_pct,
                weight_by_conviction=weight_by_conviction,
                ai_advice=ai_advice,
            ):
                opened += 1
            else:
                # If deployed >= bankroll, this skip was bankroll-driven.
                if deployed_before >= bankroll_usd * 0.99:
                    skipped_bankroll += 1
        elif sig in EXIT_SIGNAL_TYPES:
            pnl = close_copy_bet(conn, r)
            if pnl is not None:
                closed += 1
                realized_pnl += pnl
    return {
        "opened": opened,
        "closed": closed,
        "skipped_bankroll": skipped_bankroll,
        "realized_pnl": round(realized_pnl, 4),
        "ai_calls": ai_calls,
    }


def place_copy_bet(
    conn: sqlite3.Connection,
    signal_row: sqlite3.Row,
    *,
    bankroll_usd: float,
    stake_pct: float,
    weight_by_conviction: bool = True,
    ai_advice: AIAdvice | None = None,
) -> bool:
    """Open a paper bet on the whale's outcome. Returns True if recorded.

    Mechanical stake = bankroll * stake_pct * conviction_discount.
    If ai_advice is provided, final stake = mechanical * ai_advice.multiplier.
    Both are stored so we can A/B-compare PnL later.
    """
    existing = conn.execute(
        "SELECT 1 FROM poly_paper_bets "
        "WHERE source = 'whale_copy' AND source_ref_id = ?",
        (signal_row["signal_id"],),
    ).fetchone()
    if existing:
        return False

    # Cycle 2 archetype filter: skip wallets whose playbook is structurally
    # uncopyable by retail (news-arb, oracle-edge, insider, market-making,
    # cross-platform hedging). Default retail_copyable=1 so unclassified
    # wallets still trade.
    copyable_row = conn.execute(
        "SELECT retail_copyable, playbook_archetype FROM whale_watchlist "
        "WHERE wallet = ?",
        (signal_row["wallet"],),
    ).fetchone()
    if copyable_row is not None and copyable_row["retail_copyable"] == 0:
        logger.info(
            "place_copy_bet skipped by archetype filter: wallet=%s playbook=%s",
            signal_row["wallet"][:14],
            copyable_row["playbook_archetype"] or "unspecified",
        )
        return False

    price = signal_row["current_price"]
    if price is None or price <= 0 or price >= 1:
        return False
    market_slug = signal_row["market_slug"]
    asset_id = signal_row["asset_id"]
    if not market_slug or not asset_id:
        return False
    conviction = signal_row["conviction_discount"]
    conviction_f = float(conviction) if conviction is not None else 1.0
    conv_mult = conviction_f if weight_by_conviction else 1.0

    # Kelly-fractional sizing replaces the flat $40 stake. See whale_sizing.py
    # for math and references (Kelly 1956, Thorp 1969, MacLean/Thorp/Ziemba 2010).
    sizing = compute_kelly_stake(
        conn,
        signal_row["wallet"],
        bankroll_usd=bankroll_usd,
        conviction_multiplier=conv_mult,
    )
    if sizing.skipped:
        logger.info(
            "place_copy_bet skipped by kelly: wallet=%s %s",
            signal_row["wallet"][:14], sizing.reason,
        )
        return False
    mechanical_stake = sizing.stake_usd
    ai_mult = ai_advice.multiplier if ai_advice is not None else 1.0
    final_stake = mechanical_stake * ai_mult
    if final_stake <= 0:
        return False

    # ADDED top-up: if signal is "added_size" AND we have an open bet for this
    # whale on this asset, add to that position instead of opening a new row
    # (which the same-market dedup would reject).
    existing_for_topup = None
    if signal_row["signal_type"] == "added_size":
        existing_for_topup = find_open_bet_for_wallet_asset(
            conn, signal_row["wallet"], asset_id
        )

    if existing_for_topup is not None:
        # Top-up flow: still apply deployment cap (not same-market dedup)
        deployed = current_deployed_usd(conn)
        if deployed + final_stake > bankroll_usd:
            logger.info(
                "topup skipped (bankroll cap): deployed=$%.2f + new=$%.2f > $%.2f",
                deployed, final_stake, bankroll_usd,
            )
            return False
        return _apply_topup(
            conn, existing_for_topup, signal_row,
            additional_stake=final_stake,
            current_price=float(price),
            ai_advice=ai_advice,
        )

    # New position flow: portfolio guards (max positions, deploy cap, dedup)
    allowed, guard_reason = check_portfolio_guards(
        conn,
        proposed_stake=final_stake,
        bankroll_usd=bankroll_usd,
        market_slug=market_slug,
        outcome=signal_row["outcome"],
    )
    if not allowed:
        logger.info(
            "place_copy_bet skipped by portfolio guard: %s", guard_reason,
        )
        return False

    shares = final_stake / float(price)
    now = int(time.time())
    cur = conn.execute(
        """
        INSERT INTO poly_paper_bets(
            source, source_ref_id, market_slug, event_slug, token_id,
            side, outcome_title, entry_price, size_shares, cost_usd, placed_at,
            intended_shares, capacity_capped, notes,
            mechanical_stake, ai_multiplier, ai_reason, ai_confidence
        ) VALUES ('whale_copy', ?, ?, NULL, ?, 'YES', ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
        """,
        (
            signal_row["signal_id"],
            market_slug,
            asset_id,
            signal_row["outcome"],
            float(price),
            round(shares, 6),
            round(final_stake, 4),
            now,
            round(shares, 6),
            f"copy_of_{signal_row['wallet'][:10]}",
            round(mechanical_stake, 4),
            ai_advice.multiplier if ai_advice else None,
            ai_advice.reason if ai_advice else None,
            ai_advice.confidence if ai_advice else None,
        ),
    )
    bet_id = cur.lastrowid
    conn.commit()

    # Friction instrumentation: best-effort, never blocks the bet
    if bet_id:
        try:
            snapshot_entry_friction(
                conn, bet_id=bet_id, asset_id=asset_id,
                signal_ts=int(signal_row["detected_at"] or now),
                paper_entry_price=float(price),
            )
        except Exception as exc:
            logger.warning("friction entry snapshot failed: %s", exc)

        # Maker-routing shadow observer: records what real-money execution
        # would have looked like under maker-first routing with taker fallback.
        try:
            from polywhale.maker_router import shadow_observe_entry
            shadow_observe_entry(
                conn,
                bet_id=bet_id,
                signal_row=signal_row,
                paper_entry_price=float(price),
                stake_usd=float(final_stake),
                market_slug=market_slug,
            )
        except Exception as exc:
            logger.warning("maker shadow entry failed: %s", exc)

    logger.info(
        "place_copy_bet signal=%d wallet=%s stake=$%.2f (mech=$%.2f x ai=%.2f) "
        "shares=%.1f @ %.4f",
        signal_row["signal_id"], signal_row["wallet"][:10], final_stake,
        mechanical_stake, ai_mult, shares, price,
    )
    return True


def _apply_topup(
    conn: sqlite3.Connection,
    existing_bet: sqlite3.Row,
    signal_row: sqlite3.Row,
    *,
    additional_stake: float,
    current_price: float,
    ai_advice: "AIAdvice | None" = None,
) -> bool:
    """Add to an existing copy position. VWAP the entry, increment add_count.

    Existing entry_price is replaced by the volume-weighted average across the
    original entry + this addition, so the eventual exit PnL math just works.
    Notes column gets an appended "topup from sig_N" entry.
    """
    if additional_stake <= 0 or current_price <= 0:
        return False
    old_cost = float(existing_bet["cost_usd"])
    old_shares = float(existing_bet["size_shares"])
    additional_shares = additional_stake / current_price
    new_cost = old_cost + additional_stake
    new_shares = old_shares + additional_shares
    new_vwap = new_cost / new_shares if new_shares > 0 else current_price
    add_count = int(existing_bet["add_count"] or 0) + 1
    additions_total = float(existing_bet["additions_total_usd"] or 0) + additional_stake
    now = int(time.time())

    # Append top-up note so we can audit
    prev_notes = existing_bet["notes"] or ""
    sep = "; " if prev_notes else ""
    topup_note = (
        f"topup_sig{signal_row['signal_id']}_+${additional_stake:.2f}"
        f"@{current_price:.4f}"
    )
    new_notes = f"{prev_notes}{sep}{topup_note}"

    conn.execute(
        """
        UPDATE poly_paper_bets SET
            cost_usd = ?,
            size_shares = ?,
            entry_price = ?,
            add_count = ?,
            additions_total_usd = ?,
            last_topup_at = ?,
            notes = ?,
            ai_multiplier = COALESCE(?, ai_multiplier),
            ai_reason = COALESCE(?, ai_reason),
            ai_confidence = COALESCE(?, ai_confidence)
        WHERE bet_id = ?
        """,
        (
            round(new_cost, 4),
            round(new_shares, 6),
            round(new_vwap, 6),
            add_count,
            round(additions_total, 4),
            now,
            new_notes[:500],
            ai_advice.multiplier if ai_advice else None,
            ai_advice.reason if ai_advice else None,
            ai_advice.confidence if ai_advice else None,
            existing_bet["bet_id"],
        ),
    )
    conn.commit()
    logger.info(
        "topup_copy_bet bet=%d wallet=%s added=$%.2f shares=%.1f "
        "new_vwap=%.4f total_cost=$%.2f adds=%d",
        existing_bet["bet_id"], signal_row["wallet"][:10], additional_stake,
        additional_shares, new_vwap, new_cost, add_count,
    )
    return True


def close_copy_bet(
    conn: sqlite3.Connection,
    signal_row: sqlite3.Row,
) -> float | None:
    """Find an open paper bet matching this whale + asset and close it. Returns PnL or None."""
    asset_id = signal_row["asset_id"]
    wallet = signal_row["wallet"]
    if not asset_id or not wallet:
        return None
    open_bet = conn.execute(
        """
        SELECT pb.* FROM poly_paper_bets pb
        JOIN whale_signals ws ON pb.source_ref_id = ws.signal_id
        WHERE pb.source = 'whale_copy'
          AND pb.settled_at IS NULL
          AND pb.frozen_at IS NULL
          AND ws.wallet = ?
          AND ws.asset_id = ?
        ORDER BY pb.placed_at DESC
        LIMIT 1
        """,
        (wallet, asset_id),
    ).fetchone()
    if not open_bet:
        return None
    exit_price = signal_row["current_price"]
    if exit_price is None or exit_price <= 0:
        return None
    entry_price = float(open_bet["entry_price"])
    shares = float(open_bet["size_shares"])
    pnl = (float(exit_price) - entry_price) * shares
    now = int(time.time())
    conn.execute(
        """
        UPDATE poly_paper_bets SET
            settled_at = ?,
            resolved_outcome = 'closed_early',
            payout_per_share = ?,
            pnl_usd = ?,
            closed_by_signal_id = ?
        WHERE bet_id = ?
        """,
        (
            now,
            float(exit_price),
            round(pnl, 4),
            signal_row["signal_id"],
            open_bet["bet_id"],
        ),
    )
    conn.commit()

    # Friction instrumentation on exit: best-effort
    try:
        snapshot_exit_friction(
            conn, bet_id=int(open_bet["bet_id"]), asset_id=asset_id,
            signal_ts=int(signal_row["detected_at"] or now),
            paper_exit_price=float(exit_price),
        )
    except Exception as exc:
        logger.warning("friction exit snapshot failed: %s", exc)

    # Maker-routing shadow observer on exit.
    try:
        from polywhale.maker_router import shadow_observe_exit
        shadow_observe_exit(
            conn,
            bet_id=int(open_bet["bet_id"]),
            exit_signal_row=signal_row,
            paper_exit_price=float(exit_price),
            shares=shares,
            market_slug=str(open_bet["market_slug"] or ""),
        )
    except Exception as exc:
        logger.warning("maker shadow exit failed: %s", exc)

    logger.info(
        "close_copy_bet signal=%d wallet=%s entry=%.4f exit=%.4f shares=%.1f pnl=$%.2f",
        signal_row["signal_id"], wallet[:10], entry_price, exit_price, shares, pnl,
    )
    return pnl


def find_open_copy_bet_for_signal(
    conn: sqlite3.Connection, signal_id: int
) -> sqlite3.Row | None:
    """Look up the paper bet a NEW/ADDED signal opened (for alert enrichment)."""
    return conn.execute(
        "SELECT * FROM poly_paper_bets "
        "WHERE source = 'whale_copy' AND source_ref_id = ?",
        (signal_id,),
    ).fetchone()


def find_open_bet_for_wallet_asset(
    conn: sqlite3.Connection, wallet: str, asset_id: str
) -> sqlite3.Row | None:
    """Find our open copy bet (if any) following the given whale on the given asset.

    Used by the top-up flow on ADDED signals: when bossoskil1 adds to their
    Yankees position, we should add to OUR Yankees position rather than open
    a new row that the same-market dedup would reject anyway.
    """
    return conn.execute(
        """
        SELECT pb.* FROM poly_paper_bets pb
        JOIN whale_signals ws ON pb.source_ref_id = ws.signal_id
        WHERE pb.source = 'whale_copy'
          AND pb.settled_at IS NULL
          AND pb.frozen_at IS NULL
          AND ws.wallet = ?
          AND ws.asset_id = ?
        ORDER BY pb.placed_at DESC
        LIMIT 1
        """,
        (wallet, asset_id),
    ).fetchone()


def find_closed_copy_bet_by_exit_signal(
    conn: sqlite3.Connection, signal_id: int
) -> sqlite3.Row | None:
    """Look up the paper bet an EXIT/TRIM signal closed (for alert enrichment)."""
    return conn.execute(
        "SELECT * FROM poly_paper_bets WHERE closed_by_signal_id = ?",
        (signal_id,),
    ).fetchone()


def copy_trade_stats(conn: sqlite3.Connection) -> dict:
    """Open + realized stats across all whale_copy paper bets."""
    open_row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd), 0) AS deployed "
        "FROM poly_paper_bets WHERE source = 'whale_copy' AND settled_at IS NULL"
    ).fetchone()
    closed_row = conn.execute(
        "SELECT COUNT(*) AS n, "
        "       COALESCE(SUM(pnl_usd), 0) AS pnl, "
        "       SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins, "
        "       SUM(CASE WHEN pnl_usd <= 0 THEN 1 ELSE 0 END) AS losses "
        "FROM poly_paper_bets WHERE source = 'whale_copy' AND settled_at IS NOT NULL"
    ).fetchone()
    wins = int(closed_row["wins"] or 0)
    losses = int(closed_row["losses"] or 0)
    wr = (wins / (wins + losses) * 100.0) if (wins + losses) > 0 else None
    return {
        "open_positions": int(open_row["n"] or 0),
        "capital_deployed": round(float(open_row["deployed"] or 0), 2),
        "closed_positions": int(closed_row["n"] or 0),
        "realized_pnl": round(float(closed_row["pnl"] or 0), 2),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(wr, 1) if wr is not None else None,
    }
