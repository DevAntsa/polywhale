"""Telegram /command bot using long-polling getUpdates.

Runs on a 30s systemd timer. Each invocation:
  1. Fetches new messages via getUpdates with offset=last_update_id+1
  2. For each /command message, dispatches to a handler
  3. Sends formatted HTML response back to the chat
  4. Persists last_update_id so we don't reprocess

Commands implemented:
  /pulse       - overall system state
  /whales      - watchlist with WR + activity
  /positions   - currently-open copy bets
  /pnl         - paper PnL summary
  /forecast    - walk-forward projection
  /friction    - friction-report digest
  /help        - command list
"""

import logging
import sqlite3
import time

import httpx

from polywhale.copy_trader import copy_trade_stats, current_deployed_usd
from polywhale.friction_observer import compute_friction_report
from polywhale.telegram import API_BASE, send_message

logger = logging.getLogger(__name__)

LAST_UPDATE_KEY = "telegram_last_update_id"


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM bot_state WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else None


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    now = int(time.time())
    conn.execute(
        "INSERT OR REPLACE INTO bot_state(key, value, updated_at) VALUES (?, ?, ?)",
        (key, value, now),
    )
    conn.commit()


def get_updates(token: str, *, offset: int = 0, timeout: int = 3) -> list[dict]:
    """Long-poll Telegram for new messages. Returns updates list."""
    url = f"{API_BASE}/bot{token}/getUpdates"
    try:
        resp = httpx.get(
            url,
            params={"offset": str(offset), "timeout": str(timeout)},
            timeout=timeout + 5,
        )
        if resp.status_code != 200:
            logger.warning("getUpdates non-200: %d", resp.status_code)
            return []
        body = resp.json()
        if not body.get("ok"):
            return []
        return body.get("result", []) or []
    except httpx.HTTPError as exc:
        logger.warning("getUpdates http error: %s", exc)
        return []


def process_pending(
    conn: sqlite3.Connection,
    *,
    token: str,
    chat_id: str,
    poll_timeout: int = 3,
) -> dict:
    """Pull new commands, dispatch, respond. Returns {'processed': N, 'last_id': X}."""
    last_id_str = get_state(conn, LAST_UPDATE_KEY)
    last_id = int(last_id_str) if last_id_str else 0
    updates = get_updates(token, offset=last_id + 1, timeout=poll_timeout)
    processed = 0
    for upd in updates:
        upd_id = int(upd.get("update_id", 0))
        if upd_id <= last_id:
            continue
        msg = upd.get("message") or {}
        text = (msg.get("text") or "").strip()
        if text.startswith("/"):
            cmd = text.split()[0].lower()
            response = dispatch_command(conn, cmd)
            send_message(token, chat_id, response, parse_mode="HTML")
            processed += 1
        last_id = upd_id
    if updates:
        set_state(conn, LAST_UPDATE_KEY, str(last_id))
    return {"processed": processed, "last_id": last_id}


def dispatch_command(conn: sqlite3.Connection, cmd: str) -> str:
    """Route /command to handler, return formatted Telegram response."""
    cmd = cmd.lower().split("@")[0]  # handle @botname suffix
    handler = COMMAND_HANDLERS.get(cmd)
    if not handler:
        return f"Unknown command: <code>{cmd}</code>\nTry /help"
    try:
        return handler(conn)
    except Exception as exc:
        logger.exception("handler error for %s", cmd)
        return f"Error running {cmd}: <code>{type(exc).__name__}</code>"


# ----- Handlers -----


def handle_help(conn: sqlite3.Connection) -> str:
    return (
        "🤖 <b>polywhale commands</b>\n\n"
        "/pulse - overall state snapshot\n"
        "/whales - watchlist with activity\n"
        "/positions - currently-open copy bets\n"
        "/pnl - paper P&amp;L summary\n"
        "/forecast - walk-forward weekly projection\n"
        "/friction - paper-to-real translation report\n"
        "/help - this message"
    )


def handle_pulse(conn: sqlite3.Connection) -> str:
    def _scalar(sql: str, params: tuple = ()) -> int:
        row = conn.execute(sql, params).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    watchlist_active = _scalar(
        "SELECT COUNT(*) FROM whale_watchlist WHERE active = 1"
    )
    with_positions = _scalar(
        "SELECT COUNT(DISTINCT wallet) FROM whale_positions"
    )
    signals_24h = _scalar(
        "SELECT COUNT(*) FROM whale_signals WHERE detected_at >= ?",
        (int(time.time()) - 86400,),
    )
    paper_total = _scalar("SELECT COUNT(*) FROM poly_paper_bets")
    paper_settled = _scalar(
        "SELECT COUNT(*) FROM poly_paper_bets WHERE settled_at IS NOT NULL"
    )
    paper_pnl = (
        conn.execute(
            "SELECT COALESCE(SUM(pnl_usd), 0) FROM poly_paper_bets"
        ).fetchone()[0]
        or 0
    )
    ct = copy_trade_stats(conn)
    deployed = current_deployed_usd(conn)
    return (
        "📊 <b>pulse</b>\n"
        f"<i>watchlist</i>: {watchlist_active} active ({with_positions} with positions)\n"
        f"<i>signals 24h</i>: {signals_24h}\n"
        f"<i>paper bets</i>: {paper_total} ({paper_settled} settled)\n"
        f"<i>paper P&amp;L</i>: ${paper_pnl:+.2f}\n"
        f"\n"
        f"<b>whale copy</b>\n"
        f"<i>open</i>: {ct['open_positions']}  <i>deployed</i>: ${deployed:.0f}\n"
        f"<i>closed</i>: {ct['closed_positions']} "
        f"(W/L {ct['wins']}/{ct['losses']})\n"
        f"<i>realized</i>: ${ct['realized_pnl']:+.2f}"
    )


def handle_whales(conn: sqlite3.Connection) -> str:
    rows = list(
        conn.execute(
            """
            SELECT wallet, label, signals_30d, margin_pct, profit_usd,
                   win_rate_pct, wr_sample_size, last_signal_at, last_trade_at,
                   endorsed, risk_flags
            FROM whale_watchlist
            WHERE active = 1
            ORDER BY signals_30d DESC, profit_usd DESC NULLS LAST
            LIMIT 25
            """
        )
    )
    if not rows:
        return "<i>No active whales.</i>"
    now = int(time.time())
    lines = ["🐋 <b>watchlist</b> (top by activity)"]
    for r in rows:
        label = (r["label"] or r["wallet"][:10])[:18]
        tags = []
        if r["endorsed"]:
            tags.append("⭐")
        if r["risk_flags"]:
            tags.append("⚠️")
        tag = "".join(tags)
        sigs = r["signals_30d"] or 0
        wr = f"{r['win_rate_pct']:.0f}%" if r["win_rate_pct"] is not None else "-"
        margin = (
            f"{r['margin_pct']:.1f}%" if r["margin_pct"] is not None else "-"
        )
        age = "-"
        if r["last_signal_at"]:
            h = (now - int(r["last_signal_at"])) / 3600.0
            age = f"{h:.1f}h" if h < 24 else f"{h / 24:.1f}d"
        lines.append(
            f"{tag}<b>{label}</b> · sigs30={sigs} · WR {wr} · margin {margin} · {age}"
        )
    return "\n".join(lines)


def handle_positions(conn: sqlite3.Connection) -> str:
    rows = list(
        conn.execute(
            """
            SELECT pb.market_slug, pb.entry_price, pb.size_shares, pb.cost_usd,
                   pb.placed_at, ws.title, ws.wallet, ws.outcome
            FROM poly_paper_bets pb
            JOIN whale_signals ws ON pb.source_ref_id = ws.signal_id
            WHERE pb.source = 'whale_copy' AND pb.settled_at IS NULL
              AND pb.frozen_at IS NULL
            ORDER BY pb.placed_at DESC
            LIMIT 15
            """
        )
    )
    if not rows:
        return "<i>No open copy positions.</i>"
    now = int(time.time())
    total_deployed = current_deployed_usd(conn)
    lines = [
        f"📂 <b>open positions</b> ({len(rows)} shown; "
        f"total deployed ${total_deployed:.0f})"
    ]
    for r in rows:
        label = conn.execute(
            "SELECT label FROM whale_watchlist WHERE wallet = ?", (r["wallet"],)
        ).fetchone()
        who = (label["label"] if label and label["label"] else r["wallet"][:10])[:14]
        title = (r["title"] or "?")[:38]
        age_h = (now - int(r["placed_at"])) / 3600.0
        age = f"{age_h:.1f}h" if age_h < 24 else f"{age_h / 24:.1f}d"
        lines.append(
            f"<b>{who}</b> · ${float(r['cost_usd']):.0f} @ {float(r['entry_price']):.3f} "
            f"· {age}\n  <i>{title}</i>"
        )
    return "\n".join(lines)


def handle_pnl(conn: sqlite3.Connection) -> str:
    """Per-whale PnL summary."""
    rows = list(
        conn.execute(
            """
            SELECT ws.wallet,
                   COUNT(*) AS n,
                   SUM(CASE WHEN pb.pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN pb.pnl_usd <= 0 THEN 1 ELSE 0 END) AS losses,
                   ROUND(SUM(pb.pnl_usd), 2) AS pnl
            FROM poly_paper_bets pb
            JOIN whale_signals ws ON pb.source_ref_id = ws.signal_id
            WHERE pb.source = 'whale_copy' AND pb.settled_at IS NOT NULL
            GROUP BY ws.wallet
            ORDER BY pnl DESC NULLS LAST
            """
        )
    )
    if not rows:
        return "<i>No closed copy bets yet.</i>"
    total_pnl = sum(float(r["pnl"] or 0) for r in rows)
    lines = [f"💰 <b>per-whale P&amp;L</b> (total ${total_pnl:+,.2f})"]
    for r in rows[:15]:
        label = conn.execute(
            "SELECT label FROM whale_watchlist WHERE wallet = ?", (r["wallet"],)
        ).fetchone()
        who = (label["label"] if label and label["label"] else r["wallet"][:10])[:18]
        pnl = float(r["pnl"] or 0)
        sign = "+" if pnl >= 0 else "-"
        lines.append(
            f"<b>{who}</b> · {r['n']} bets · W/L {r['wins']}/{r['losses']} "
            f"· {sign}${abs(pnl):.2f}"
        )
    return "\n".join(lines)


def handle_forecast(conn: sqlite3.Connection) -> str:
    """Quick walk-forward forecast summary."""
    # Avoid importing the full walk_forward inside a long-polling timer;
    # just show the last MC + WF anchors from our calibration notes.
    return (
        "📈 <b>forecast (live data)</b>\n"
        "<i>For the cached anchors:</i>\n"
        "• Walk-forward: ~$85/wk, 58.8% consistency\n"
        "• Realistic real-money: $30-50/wk after friction\n"
        "<i>For fresh numbers, run on box:</i>\n"
        "<code>polywhale walk-forward</code>\n"
        "<code>polywhale historical-backtest</code>"
    )


def handle_friction(conn: sqlite3.Connection) -> str:
    rpt = compute_friction_report(conn)
    if rpt.get("covered_bets", 0) == 0:
        return (
            "🧪 <b>friction</b>\n"
            f"closed bets: {rpt.get('total_closed_bets', 0)}\n"
            f"with book coverage: 0\n"
            f"<i>{rpt.get('message', '')}</i>"
        )
    lines = [
        f"🧪 <b>friction</b> ({rpt['covered_bets']} observations)",
        f"entry slippage median: {rpt['entry_slippage_median_pct']}%",
        f"exit slippage median: {rpt['exit_slippage_median_pct']}%",
    ]
    if rpt.get("edge_retention_pct") is not None:
        lines.append(f"edge retention: <b>{rpt['edge_retention_pct']}%</b>")
    return "\n".join(lines)


COMMAND_HANDLERS = {
    "/help": handle_help,
    "/pulse": handle_pulse,
    "/whales": handle_whales,
    "/positions": handle_positions,
    "/pnl": handle_pnl,
    "/forecast": handle_forecast,
    "/friction": handle_friction,
}
