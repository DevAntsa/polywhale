"""Classify Polymarket leaderboard wallets by behaviour shape.

- `sharp`   : margin >= 3% on meaningful volume = real predictive edge.
              These are the ones whose new positions are worth copying.
- `arb_op`  : margin < 2% on meaningful volume = automated arb / market-making.
              Copying just one leg of their behaviour is *negative* EV.
- `hybrid`  : 2% <= margin < 3% = ambiguous; treat with care.
- `unknown` : volume below threshold; not enough signal to classify.

Sources: lb-api.polymarket.com /profit and /volume endpoints, joined by
proxy-wallet address. Persisted in whale_profiles for historical tracking.
"""

import logging
import sqlite3
import time
from dataclasses import dataclass

from polywhale.polymarket import PolymarketClient

logger = logging.getLogger(__name__)

SHAPE_SHARP = "sharp"
SHAPE_ARB_OP = "arb_op"
SHAPE_HYBRID = "hybrid"
SHAPE_UNKNOWN = "unknown"


@dataclass(frozen=True)
class WhaleProfile:
    wallet: str
    pseudonym: str | None
    name: str | None
    window: str
    profit: float
    volume: float
    margin_pct: float
    shape: str
    captured_at: int


def classify_shape(
    profit: float,
    volume: float,
    *,
    min_volume: float = 1_000_000.0,
    sharp_margin: float = 3.0,
    arb_margin: float = 2.0,
) -> str:
    """Return shape label from profit/volume ratio."""
    if volume < min_volume:
        return SHAPE_UNKNOWN
    margin_pct = (profit / volume) * 100.0
    if margin_pct >= sharp_margin:
        return SHAPE_SHARP
    if margin_pct < arb_margin:
        return SHAPE_ARB_OP
    return SHAPE_HYBRID


def fetch_and_classify(
    client: PolymarketClient,
    *,
    window: str = "30d",
    top_n: int = 50,
    min_volume: float = 1_000_000.0,
) -> list[WhaleProfile]:
    """Pull leaderboard profit + volume, join by wallet, compute margin & shape."""
    profits = client.get_leaderboard("profit", window=window)
    volumes = client.get_leaderboard("volume", window=window)
    vol_by_wallet = {v.wallet: v for v in volumes}
    now = int(time.time())

    profiles: list[WhaleProfile] = []
    for p in profits[:top_n]:
        v = vol_by_wallet.get(p.wallet)
        volume = v.amount if v else 0.0
        margin = (p.amount / volume * 100.0) if volume > 0 else 0.0
        shape = classify_shape(p.amount, volume, min_volume=min_volume)
        profiles.append(
            WhaleProfile(
                wallet=p.wallet,
                pseudonym=p.pseudonym,
                name=p.name,
                window=window,
                profit=p.amount,
                volume=volume,
                margin_pct=margin,
                shape=shape,
                captured_at=now,
            )
        )
    return profiles


def persist_profiles(conn: sqlite3.Connection, profiles: list[WhaleProfile]) -> int:
    if not profiles:
        return 0
    conn.executemany(
        """
        INSERT INTO whale_profiles (
            wallet, pseudonym, name, window, profit, volume,
            margin_pct, shape, captured_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                p.wallet,
                p.pseudonym,
                p.name,
                p.window,
                p.profit,
                p.volume,
                p.margin_pct,
                p.shape,
                p.captured_at,
            )
            for p in profiles
        ],
    )
    conn.commit()
    return len(profiles)


def top_sharps(profiles: list[WhaleProfile], *, n: int = 10) -> list[WhaleProfile]:
    return sorted(
        (p for p in profiles if p.shape == SHAPE_SHARP),
        key=lambda x: x.profit,
        reverse=True,
    )[:n]


def top_arb_ops(profiles: list[WhaleProfile], *, n: int = 5) -> list[WhaleProfile]:
    return sorted(
        (p for p in profiles if p.shape == SHAPE_ARB_OP),
        key=lambda x: x.volume,
        reverse=True,
    )[:n]
