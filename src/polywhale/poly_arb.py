"""Combinatorial arbitrage detection on Polymarket negative-risk groups.

For an event with N mutually-exclusive outcomes (e.g., FIFA World Cup winner),
the YES tokens should sum to $1.00. When sum(best_ask) < 1.00 (minus fees), we
can buy the entire YES set and lock in guaranteed profit because exactly one
outcome will resolve YES at $1.00.
"""

import json
import logging
import sqlite3
import time
from dataclasses import dataclass

from polywhale.polymarket import PolymarketClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ComboLeg:
    market_slug: str
    question: str | None
    outcome_title: str | None
    token_id: str
    best_ask: float | None
    ask_depth: float | None


@dataclass(frozen=True)
class ComboArb:
    event_slug: str
    event_title: str | None
    captured_at: int
    outcomes_count: int
    sum_best_ask: float
    edge_pct: float
    legs: list[ComboLeg]


def detect_combo_arb(
    client: PolymarketClient,
    event_slug: str,
    *,
    fee_pct: float = 0.75,
    min_edge_pct: float = 0.5,
) -> ComboArb | None:
    """Detect a combinatorial arb on a single neg-risk event.

    Returns the ComboArb if edge after fees >= min_edge_pct, else None.
    `fee_pct`: Polymarket taker fee for the category (sports 0.75, politics 1.0,
        crypto 1.8). The break-even sum is 1.0 - 2*fee_pct/100 because both buy
        legs incur taker fees on the smaller side of the 50c mark.
    """
    event = client.get_event(event_slug)
    if event is None:
        logger.warning("event not found: %s", event_slug)
        return None
    markets = event.get("markets") or []
    if not markets:
        logger.warning("event %s has no markets", event_slug)
        return None

    legs: list[ComboLeg] = []
    sum_ask = 0.0
    for raw in markets:
        if not raw.get("negRisk"):
            # Skip non-neg-risk markets included in the event (e.g., season-long props)
            continue
        token_ids_raw = raw.get("clobTokenIds") or "[]"
        token_ids = json.loads(token_ids_raw) if isinstance(token_ids_raw, str) else token_ids_raw
        if not token_ids:
            continue
        yes_token = str(token_ids[0])
        try:
            book = client.get_book(yes_token)
        except Exception as exc:
            logger.warning("get_book failed for %s: %s", yes_token, exc)
            continue
        best_ask = book.best_ask
        if best_ask is None:
            continue
        depth = book.depth_within(side="ask", pct=0.05)
        sum_ask += best_ask
        legs.append(
            ComboLeg(
                market_slug=raw.get("slug", ""),
                question=raw.get("question"),
                outcome_title=raw.get("groupItemTitle"),
                token_id=yes_token,
                best_ask=best_ask,
                ask_depth=depth,
            )
        )
        time.sleep(0.05)  # rate-limit politeness

    if not legs:
        return None
    # Edge per $1 of guaranteed return.
    raw_edge = 1.0 - sum_ask
    edge_after_fees = raw_edge - (fee_pct / 100.0)  # one side of the spread pays fee
    edge_pct = edge_after_fees * 100.0
    if edge_pct < min_edge_pct:
        return None
    return ComboArb(
        event_slug=event_slug,
        event_title=event.get("title"),
        captured_at=int(time.time()),
        outcomes_count=len(legs),
        sum_best_ask=round(sum_ask, 6),
        edge_pct=round(edge_pct, 4),
        legs=legs,
    )


def persist_combo_arb(conn: sqlite3.Connection, arb: ComboArb) -> int:
    cur = conn.execute(
        """
        INSERT INTO combo_arbs (
            event_slug, event_title, captured_at, outcomes_count,
            sum_best_ask, edge_pct, legs_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            arb.event_slug,
            arb.event_title,
            arb.captured_at,
            arb.outcomes_count,
            arb.sum_best_ask,
            arb.edge_pct,
            json.dumps(
                [
                    {
                        "market_slug": leg.market_slug,
                        "question": leg.question,
                        "outcome_title": leg.outcome_title,
                        "token_id": leg.token_id,
                        "best_ask": leg.best_ask,
                        "ask_depth": leg.ask_depth,
                    }
                    for leg in arb.legs
                ]
            ),
        ),
    )
    conn.commit()
    return cur.lastrowid or 0


def inspect_event(client: PolymarketClient, event_slug: str) -> tuple[float, int, str | None]:
    """Return (sum_best_ask, leg_count, event_title) without persisting or filtering.

    Useful for diagnostic scans of multiple events to find candidate arb targets.
    """
    event = client.get_event(event_slug)
    if event is None:
        return (0.0, 0, None)
    markets = event.get("markets") or []
    sum_ask = 0.0
    legs = 0
    for raw in markets:
        if not raw.get("negRisk"):
            continue
        token_ids_raw = raw.get("clobTokenIds") or "[]"
        token_ids = json.loads(token_ids_raw) if isinstance(token_ids_raw, str) else token_ids_raw
        if not token_ids:
            continue
        try:
            book = client.get_book(str(token_ids[0]))
        except Exception:
            continue
        if book.best_ask is None:
            continue
        sum_ask += book.best_ask
        legs += 1
        time.sleep(0.05)
    return (round(sum_ask, 6), legs, event.get("title"))
