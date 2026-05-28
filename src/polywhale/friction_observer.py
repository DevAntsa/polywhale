"""Pre-real-money friction instrumentation.

When a paper bet opens, we record what the REAL-MONEY entry price would have
been (best_ask from polymarket_books snapshot closest to signal time). Same
for exits (best_bid). The delta between paper price (data-api current_price)
and real fill price = slippage we'd actually pay.

After enough trades, `polywhale friction-report` computes:
  - entry slippage distribution (mean, median, 95th pct)
  - exit slippage distribution
  - total round-trip friction per category
  - what real-money PnL would have been

This is the prerequisite for a real-money pilot. We cannot decide go/no-go
without measured friction; the research established realistic friction is
2.6% which exceeds our 1.6% gross paper edge.

Limitation: requires `polymarket_books` coverage on the asset around signal
time. Without it, friction columns stay NULL. Our poly-watch timer uses
`--from-positions` so coverage should be ~100% within ~30 minutes.
"""

import logging
import sqlite3
import time

logger = logging.getLogger(__name__)

# How far back to look for a book snapshot (1 hour matches our 30-min poll cadence + slack)
BOOK_TOLERANCE_S = 3600


def get_book_at(
    conn: sqlite3.Connection,
    asset_id: str,
    target_ts: int,
    *,
    tolerance_s: int = BOOK_TOLERANCE_S,
) -> sqlite3.Row | None:
    """Closest book snapshot for `asset_id` within `tolerance_s` of `target_ts`."""
    return conn.execute(
        """
        SELECT best_bid, best_ask, captured_at
        FROM polymarket_books
        WHERE token_id = ?
          AND ABS(captured_at - ?) <= ?
        ORDER BY ABS(captured_at - ?) ASC
        LIMIT 1
        """,
        (asset_id, target_ts, tolerance_s, target_ts),
    ).fetchone()


def snapshot_entry_friction(
    conn: sqlite3.Connection,
    *,
    bet_id: int,
    asset_id: str,
    signal_ts: int,
    paper_entry_price: float,
) -> dict:
    """Record hypothetical real entry price + slippage for an open bet.

    Returns the friction dict; logs warning if book coverage missing.
    """
    if not asset_id or paper_entry_price <= 0:
        return {"recorded": False, "reason": "missing_inputs"}
    book = get_book_at(conn, asset_id, signal_ts)
    if not book or book["best_ask"] is None:
        return {"recorded": False, "reason": "no_book_coverage"}
    real_entry = float(book["best_ask"])
    age_s = abs(int(time.time()) - int(book["captured_at"]))
    slippage_pct = (real_entry - paper_entry_price) / paper_entry_price
    conn.execute(
        "UPDATE poly_paper_bets SET hypothetical_real_entry = ?, "
        "entry_slippage_pct = ?, entry_book_age_s = ? WHERE bet_id = ?",
        (real_entry, round(slippage_pct, 6), age_s, bet_id),
    )
    conn.commit()
    return {
        "recorded": True,
        "real_entry": real_entry,
        "paper_entry": paper_entry_price,
        "slippage_pct": round(slippage_pct, 6),
        "book_age_s": age_s,
    }


def snapshot_exit_friction(
    conn: sqlite3.Connection,
    *,
    bet_id: int,
    asset_id: str,
    signal_ts: int,
    paper_exit_price: float,
) -> dict:
    """Record hypothetical real exit price + slippage on close."""
    if not asset_id or paper_exit_price <= 0:
        return {"recorded": False, "reason": "missing_inputs"}
    book = get_book_at(conn, asset_id, signal_ts)
    if not book or book["best_bid"] is None:
        return {"recorded": False, "reason": "no_book_coverage"}
    real_exit = float(book["best_bid"])
    age_s = abs(int(time.time()) - int(book["captured_at"]))
    # On exit we sell into the bid → we get LESS than paper if real_exit < paper.
    # Slippage_pct here represents the % we'd be SHORT relative to paper.
    slippage_pct = (paper_exit_price - real_exit) / paper_exit_price
    conn.execute(
        "UPDATE poly_paper_bets SET hypothetical_real_exit = ?, "
        "exit_slippage_pct = ?, exit_book_age_s = ? WHERE bet_id = ?",
        (real_exit, round(slippage_pct, 6), age_s, bet_id),
    )
    conn.commit()
    return {
        "recorded": True,
        "real_exit": real_exit,
        "paper_exit": paper_exit_price,
        "slippage_pct": round(slippage_pct, 6),
        "book_age_s": age_s,
    }


def compute_friction_report(conn: sqlite3.Connection) -> dict:
    """Aggregate friction stats across all closed copy bets that have measurements."""
    rows = list(
        conn.execute(
            """
            SELECT entry_price, payout_per_share, size_shares, cost_usd, pnl_usd,
                   hypothetical_real_entry, hypothetical_real_exit,
                   entry_slippage_pct, exit_slippage_pct,
                   entry_book_age_s, exit_book_age_s
            FROM poly_paper_bets
            WHERE source = 'whale_copy'
              AND settled_at IS NOT NULL
              AND (hypothetical_real_entry IS NOT NULL
                   OR hypothetical_real_exit IS NOT NULL)
            """
        )
    )
    if not rows:
        return {
            "covered_bets": 0,
            "total_closed_bets": _scalar(
                conn,
                "SELECT COUNT(*) FROM poly_paper_bets WHERE source = 'whale_copy' "
                "AND settled_at IS NOT NULL",
            ),
            "message": "no friction observations yet (need book coverage on closed bets)",
        }

    entry_slip = [
        float(r["entry_slippage_pct"]) for r in rows
        if r["entry_slippage_pct"] is not None
    ]
    exit_slip = [
        float(r["exit_slippage_pct"]) for r in rows
        if r["exit_slippage_pct"] is not None
    ]

    paper_pnl_total = 0.0
    real_pnl_total = 0.0
    real_pnl_count = 0
    for r in rows:
        paper_pnl = float(r["pnl_usd"] or 0)
        paper_pnl_total += paper_pnl
        # Reconstruct hypothetical real PnL
        if (
            r["hypothetical_real_entry"] is not None
            and r["hypothetical_real_exit"] is not None
            and r["size_shares"] is not None
        ):
            real_entry = float(r["hypothetical_real_entry"])
            real_exit = float(r["hypothetical_real_exit"])
            shares = float(r["size_shares"])
            real_pnl_total += (real_exit - real_entry) * shares
            real_pnl_count += 1

    def _pct(arr, p):
        if not arr:
            return None
        arr = sorted(arr)
        idx = max(0, min(int(len(arr) * p / 100.0), len(arr) - 1))
        return arr[idx]

    return {
        "covered_bets": len(rows),
        "total_closed_bets": _scalar(
            conn,
            "SELECT COUNT(*) FROM poly_paper_bets WHERE source = 'whale_copy' "
            "AND settled_at IS NOT NULL",
        ),
        "entry_slippage_observations": len(entry_slip),
        "entry_slippage_mean_pct": (
            round(sum(entry_slip) / len(entry_slip) * 100, 3) if entry_slip else None
        ),
        "entry_slippage_median_pct": (
            round(_pct(entry_slip, 50) * 100, 3) if entry_slip else None
        ),
        "entry_slippage_p95_pct": (
            round(_pct(entry_slip, 95) * 100, 3) if entry_slip else None
        ),
        "exit_slippage_observations": len(exit_slip),
        "exit_slippage_mean_pct": (
            round(sum(exit_slip) / len(exit_slip) * 100, 3) if exit_slip else None
        ),
        "exit_slippage_median_pct": (
            round(_pct(exit_slip, 50) * 100, 3) if exit_slip else None
        ),
        "exit_slippage_p95_pct": (
            round(_pct(exit_slip, 95) * 100, 3) if exit_slip else None
        ),
        "paper_pnl_total": round(paper_pnl_total, 2),
        "hypothetical_real_pnl_total": round(real_pnl_total, 2),
        "real_pnl_observations": real_pnl_count,
        "edge_retention_pct": (
            round(real_pnl_total / paper_pnl_total * 100, 1)
            if abs(paper_pnl_total) > 0.01 else None
        ),
    }


def _scalar(conn: sqlite3.Connection, sql: str) -> int:
    row = conn.execute(sql).fetchone()
    return int(row[0]) if row and row[0] is not None else 0
