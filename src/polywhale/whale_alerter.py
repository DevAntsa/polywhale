"""Push Telegram alerts for whale signals (typically `new_position` from sharps)."""

import logging
import sqlite3
import time
from collections.abc import Callable

from polywhale.telegram import send_message

logger = logging.getLogger(__name__)

Sender = Callable[[str, str, str], bool]


def find_unalerted_signals(
    conn: sqlite3.Connection,
    *,
    signal_types: tuple[str, ...] = ("new_position", "added_size"),
    wallets: tuple[str, ...] | None = None,
) -> list[sqlite3.Row]:
    placeholders_t = ",".join("?" for _ in signal_types)
    sql = (
        f"SELECT * FROM whale_signals "
        f"WHERE alerted_at IS NULL AND signal_type IN ({placeholders_t}) "
    )
    params: list[object] = list(signal_types)
    if wallets:
        placeholders_w = ",".join("?" for _ in wallets)
        sql += f"AND wallet IN ({placeholders_w}) "
        params.extend(wallets)
    sql += "ORDER BY detected_at ASC"
    return conn.execute(sql, params).fetchall()


def format_signal_alert(signals: list[sqlite3.Row]) -> str:
    if not signals:
        return ""
    if len(signals) == 1:
        return _format_single(signals[0])
    lines = [f"{len(signals)} new whale signals:"]
    for r in signals[:10]:
        title = (r["title"] or "(unknown)")[:50]
        size = r["new_size"] or 0
        old_size = r["old_size"]
        old_str = f" (was {old_size:,.0f})" if old_size else ""
        lines.append(
            f"- {r['signal_type'][:14]:<14} {r['wallet'][:10]} "
            f"{r['outcome'] or '?':<10} size={size:>8,.0f}{old_str}  {title}"
        )
    if len(signals) > 10:
        lines.append(f"... and {len(signals) - 10} more")
    return "\n".join(lines)


def _format_single(r: sqlite3.Row) -> str:
    title = r["title"] or "(unknown)"
    size = r["new_size"] or 0
    old_size = r["old_size"]
    price = r["current_price"]
    label = {
        "new_position": "NEW BET",
        "added_size": "ADDED",
        "closed_position": "CLOSED",
        "reduced_size": "REDUCED",
    }.get(r["signal_type"], r["signal_type"].upper())
    lines = [
        f"{label} by whale {r['wallet'][:14]}...",
        f"  {title}",
        f"  outcome: {r['outcome'] or '?'}",
        f"  size: {size:,.0f}" + (f" (was {old_size:,.0f})" if old_size else ""),
    ]
    if price is not None:
        lines.append(f"  current price: {price:.3f}")
    return "\n".join(lines)


def send_signal_alerts(
    conn: sqlite3.Connection,
    *,
    token: str,
    chat_id: str,
    signal_types: tuple[str, ...] = ("new_position", "added_size"),
    wallets: tuple[str, ...] | None = None,
    sender: Sender | None = None,
) -> dict:
    """Send one Telegram message for unalerted signals; mark them alerted on success."""
    send = sender if sender is not None else send_message
    rows = find_unalerted_signals(conn, signal_types=signal_types, wallets=wallets)
    if not rows:
        return {"sent": False, "signals": 0, "reason": "no unalerted signals"}
    text = format_signal_alert(rows)
    ok = send(token, chat_id, text)
    if not ok:
        return {"sent": False, "signals": len(rows), "reason": "telegram api failed"}
    now = int(time.time())
    conn.executemany(
        "UPDATE whale_signals SET alerted_at = ? WHERE signal_id = ?",
        [(now, r["signal_id"]) for r in rows],
    )
    conn.commit()
    logger.info("sent %d whale signal alert(s)", len(rows))
    return {"sent": True, "signals": len(rows), "reason": "delivered"}
