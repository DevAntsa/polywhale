"""Push Telegram alerts for whale signals (typically `new_position` from sharps).

Alerts use Telegram HTML parse mode: <b>bold</b>, <code>mono</code>, <pre>block</pre>.
All dynamic text must be HTML-escaped because Telegram refuses to render messages
with unbalanced angle brackets.
"""

import html
import logging
import sqlite3
import time
from collections.abc import Callable

from polywhale.telegram import send_message

logger = logging.getLogger(__name__)

Sender = Callable[[str, str, str], bool]

SIGNAL_EMOJI = {
    "new_position": "🟢",
    "added_size": "➕",  # noqa: RUF001
    "closed_position": "🔴",
    "reduced_size": "➖",  # noqa: RUF001
}

SIGNAL_LABEL = {
    "new_position": "NEW",
    "added_size": "ADDED",
    "closed_position": "CLOSED",
    "reduced_size": "REDUCED",
}


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


def _wallet_labels(
    conn: sqlite3.Connection, wallets: list[str]
) -> dict[str, str]:
    """Return a wallet -> friendly-name map. Uses whale_profiles.pseudonym if known,
    otherwise shortens the hex to 0xABCD..WXYZ."""
    if not wallets:
        return {}
    placeholders = ",".join("?" for _ in wallets)
    rows = conn.execute(
        f"SELECT wallet, pseudonym, name FROM whale_profiles "
        f"WHERE wallet IN ({placeholders}) "
        f"ORDER BY captured_at DESC",
        wallets,
    ).fetchall()
    seen: dict[str, str] = {}
    for r in rows:
        wallet = r["wallet"]
        if wallet in seen:
            continue
        label = r["pseudonym"] or r["name"]
        if label:
            seen[wallet] = label
    return {w: seen.get(w) or _short_addr(w) for w in wallets}


def _short_addr(wallet: str) -> str:
    if not wallet or len(wallet) < 10:
        return wallet or "?"
    return f"{wallet[:6]}…{wallet[-4:]}"


def _fmt_size(n: float | None) -> str:
    if n is None:
        return "?"
    n = float(n)
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if abs(n) >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{n:,.0f}"


def _fmt_price(p: float | None) -> str:
    return f"{float(p):.3f}" if p is not None else "?"


def _conviction_warning(r: sqlite3.Row) -> str | None:
    """If the market already moved meaningfully toward the whale's side, flag it."""
    if "conviction_discount" not in r.keys() or "recent_move_pct" not in r.keys():
        return None
    discount = r["conviction_discount"]
    move = r["recent_move_pct"]
    if discount is None or move is None:
        return None
    try:
        discount_f = float(discount)
        move_f = float(move)
    except (TypeError, ValueError):
        return None
    if discount_f >= 0.99:
        return None
    direction = "+" if move_f >= 0 else "-"
    pp = abs(move_f) * 100
    return f"⚠️ market moved {direction}{pp:.1f}pp last 24h — likely chase"


def format_signal_alert(
    signals: list[sqlite3.Row],
    *,
    labels: dict[str, str] | None = None,
) -> str:
    labels = labels or {}
    if not signals:
        return ""
    if len(signals) == 1:
        return _format_single(signals[0], labels)
    return _format_multi(signals, labels)


def _format_single(r: sqlite3.Row, labels: dict[str, str]) -> str:
    emoji = SIGNAL_EMOJI.get(r["signal_type"], "🐋")
    label = SIGNAL_LABEL.get(r["signal_type"], r["signal_type"].upper())
    wallet_name = labels.get(r["wallet"]) or _short_addr(r["wallet"])
    title = html.escape((r["title"] or "(unknown market)")[:80])
    outcome = html.escape(r["outcome"] or "?")
    size_str = _fmt_size(r["new_size"])
    old_size = r["old_size"]
    if old_size:
        size_str = f"{size_str} (was {_fmt_size(old_size)})"
    price = r["current_price"]
    lines = [
        f"{emoji} <b>{label}</b> · 🐋 <b>{html.escape(wallet_name)}</b>",
        f"📊 {title}",
        f"   <b>{outcome}</b> · size <b>{size_str}</b> · @ {_fmt_price(price)}",
    ]
    warn = _conviction_warning(r)
    if warn:
        lines.append(warn)
    return "\n".join(lines)


def _format_multi(rows: list[sqlite3.Row], labels: dict[str, str]) -> str:
    header = f"🐋 <b>{len(rows)} whale moves</b>"
    table_lines = []
    for r in rows[:10]:
        emoji = SIGNAL_EMOJI.get(r["signal_type"], "🐋")
        label = SIGNAL_LABEL.get(r["signal_type"], r["signal_type"][:6])
        who = (labels.get(r["wallet"]) or _short_addr(r["wallet"]))[:12]
        outcome = (r["outcome"] or "?")[:10]
        size = _fmt_size(r["new_size"])
        price = _fmt_price(r["current_price"])
        title = (r["title"] or "")[:38]
        table_lines.append(
            f"{emoji} {label:<7} {who:<12} {outcome:<10} {size:>7} @ {price}  {title}"
        )
    body = html.escape("\n".join(table_lines))
    if len(rows) > 10:
        body += html.escape(f"\n… and {len(rows) - 10} more")
    chases = [r for r in rows if _conviction_warning(r) is not None]
    footer = ""
    if chases:
        footer = f"\n⚠️ {len(chases)} flagged as possible chase (market already moved)"
    return f"{header}\n<pre>{body}</pre>{footer}"


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
    labels = _wallet_labels(conn, list({r["wallet"] for r in rows}))
    text = format_signal_alert(rows, labels=labels)
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
