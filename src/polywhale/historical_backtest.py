"""Reconstruct historical positions from raw activity events and backtest.

Pipeline:
  1. Pull all TRADE/REDEEM events for a wallet from whale_activity_history
  2. Group by conditionId (= one market = one position trajectory)
  3. Within each market, group by asset (= specific outcome side)
  4. Sum signed shares (BUY = +, SELL = -) chronologically
  5. A "position episode" starts when net shares goes from 0 → positive
  6. The episode ends when net shares returns to 0 (closed via sell) OR when
     we see a REDEEM event for the conditionId (closed at resolution)
  7. Entry vwap = volume-weighted avg buy price within the episode
  8. Exit vwap = volume-weighted avg sell price OR (1.0 if REDEEMed)
  9. Episode PnL = (exit_vwap - entry_vwap) * shares - fees

Each episode is one historical "trade" we could have copied. The backtest
sums PnL across episodes per wallet to produce an empirical edge estimate
with statistical depth dramatically beyond our 86 live trades.

Caveats:
  - We're simulating "what if we'd entered at the whale's vwap entry price"
    which is optimistic vs reality (we'd fill at next available ask, possibly
    worse).
  - Fees not yet modeled (Polymarket taker 0.75-1.8%). Pass fee_pct to apply.
  - REDEEM at $1.0 vs 0 dichotomy ignores multi-outcome markets — for those,
    we look up the resolution_outcome_index from market_resolutions.
"""

import logging
import sqlite3
from collections import defaultdict
from dataclasses import dataclass

from polywhale.historical_backfill import get_or_fetch_resolution
from polywhale.polymarket import PolymarketClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PositionEpisode:
    wallet: str
    condition_id: str
    asset: str
    outcome_index: int | None
    market_slug: str | None
    title: str | None
    entry_ts: int
    exit_ts: int | None             # None if still open
    shares: float                   # peak shares held during episode
    entry_vwap: float
    exit_vwap: float | None         # None if still open
    resolution_status: str          # 'won' | 'lost' | 'sold' | 'open'
    pnl_usd: float | None           # None if still open
    fee_paid: float


@dataclass(frozen=True)
class BacktestSummary:
    wallets: int
    episodes_total: int
    episodes_resolved: int
    episodes_won: int
    episodes_lost: int
    episodes_open: int
    realized_pnl: float
    avg_pnl: float
    by_wallet: dict


def reconstruct_positions_for_wallet(
    conn: sqlite3.Connection,
    client: PolymarketClient,
    wallet: str,
    *,
    fee_pct: float = 0.0,
) -> list[PositionEpisode]:
    """Walk a wallet's TRADE+REDEEM events chronologically; emit position episodes."""
    rows = list(
        conn.execute(
            """
            SELECT timestamp, type, condition_id, asset, side, price, size,
                   usdc_size, outcome, outcome_index, market_slug, title
            FROM whale_activity_history
            WHERE wallet = ?
            ORDER BY condition_id, asset, timestamp
            """,
            (wallet.lower(),),
        )
    )
    if not rows:
        return []

    # Group by (condition_id, asset) — one outcome side on one market
    groups: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        key = (r["condition_id"] or "", r["asset"] or "")
        if key[0] and key[1]:
            groups[key].append(r)

    episodes: list[PositionEpisode] = []
    for (cond_id, asset), events in groups.items():
        episodes.extend(
            _episodes_from_event_stream(
                conn, client, wallet, cond_id, asset, events, fee_pct
            )
        )
    return episodes


def _episodes_from_event_stream(
    conn: sqlite3.Connection,
    client: PolymarketClient,
    wallet: str,
    condition_id: str,
    asset: str,
    events: list[sqlite3.Row],
    fee_pct: float,
) -> list[PositionEpisode]:
    """Process the ordered TRADE/REDEEM stream for one (condition_id, asset) pair."""
    out: list[PositionEpisode] = []
    cur_shares = 0.0
    buy_cost = 0.0
    buy_shares = 0.0
    sell_proceeds = 0.0
    sell_shares = 0.0
    entry_ts: int | None = None
    market_slug: str | None = None
    title: str | None = None
    outcome_idx: int | None = None
    redeem_payout = 0.0
    redeem_observed = False

    for ev in events:
        ev_type = ev["type"]
        size = float(ev["size"] or 0)
        price = float(ev["price"] or 0)
        ts = int(ev["timestamp"] or 0)
        if market_slug is None:
            market_slug = ev["market_slug"]
            title = ev["title"]
            outcome_idx = ev["outcome_index"]

        if ev_type == "TRADE":
            side = (ev["side"] or "").upper()
            if side == "BUY":
                if cur_shares == 0:
                    # Starting a new episode
                    buy_cost = 0.0
                    buy_shares = 0.0
                    sell_proceeds = 0.0
                    sell_shares = 0.0
                    redeem_payout = 0.0
                    redeem_observed = False
                    entry_ts = ts
                buy_cost += size * price
                buy_shares += size
                cur_shares += size
            elif side == "SELL":
                sell_proceeds += size * price
                sell_shares += size
                cur_shares -= size
                if cur_shares <= 1e-6 and entry_ts is not None and buy_shares > 0:
                    out.append(
                        _build_episode(
                            wallet, condition_id, asset, outcome_idx,
                            market_slug, title, entry_ts, ts, buy_shares,
                            buy_cost, sell_proceeds + redeem_payout,
                            sell_shares, redeem_observed, fee_pct,
                        )
                    )
                    cur_shares = 0.0
                    entry_ts = None
        elif ev_type == "REDEEM":
            # REDEEM USDC payout means this side won. usdc_size = total received.
            payout = ev["usdc_size"]
            if payout is not None:
                redeem_payout += float(payout)
            redeem_observed = True
            if entry_ts is not None and buy_shares > 0:
                out.append(
                    _build_episode(
                        wallet, condition_id, asset, outcome_idx,
                        market_slug, title, entry_ts, ts, buy_shares,
                        buy_cost, sell_proceeds + redeem_payout,
                        sell_shares, redeem_observed, fee_pct,
                    )
                )
                cur_shares = 0.0
                entry_ts = None

    # Position still open at end of data → emit "open" episode
    if entry_ts is not None and buy_shares > 0:
        resolution = get_or_fetch_resolution(
            conn, client, condition_id=condition_id, market_slug=market_slug
        )
        if resolution and resolution["closed"]:
            try:
                idx = resolution["token_ids"].index(asset)
                final_price = float(resolution["outcome_prices"][idx])
            except (ValueError, IndexError):
                final_price = 0.5
            implied_exit = (sell_proceeds + cur_shares * final_price)
            entry_vwap = buy_cost / buy_shares
            exit_vwap = implied_exit / buy_shares
            gross_pnl = implied_exit - buy_cost
            fee = (buy_cost + abs(sell_proceeds)) * fee_pct
            out.append(
                PositionEpisode(
                    wallet=wallet.lower(),
                    condition_id=condition_id,
                    asset=asset,
                    outcome_index=outcome_idx,
                    market_slug=market_slug,
                    title=title,
                    entry_ts=entry_ts,
                    exit_ts=None,
                    shares=buy_shares,
                    entry_vwap=round(entry_vwap, 6),
                    exit_vwap=round(exit_vwap, 6),
                    resolution_status="won" if final_price > 0.5 else "lost",
                    pnl_usd=round(gross_pnl - fee, 4),
                    fee_paid=round(fee, 4),
                )
            )
        else:
            entry_vwap = buy_cost / buy_shares if buy_shares else 0.0
            out.append(
                PositionEpisode(
                    wallet=wallet.lower(),
                    condition_id=condition_id,
                    asset=asset,
                    outcome_index=outcome_idx,
                    market_slug=market_slug,
                    title=title,
                    entry_ts=entry_ts,
                    exit_ts=None,
                    shares=buy_shares,
                    entry_vwap=round(entry_vwap, 6),
                    exit_vwap=None,
                    resolution_status="open",
                    pnl_usd=None,
                    fee_paid=0.0,
                )
            )
    return out


def _build_episode(
    wallet: str, condition_id: str, asset: str, outcome_idx: int | None,
    market_slug: str | None, title: str | None, entry_ts: int, exit_ts: int,
    buy_shares: float, buy_cost: float, gross_exit: float, sell_shares: float,
    redeem_observed: bool, fee_pct: float,
) -> PositionEpisode:
    entry_vwap = buy_cost / buy_shares if buy_shares else 0.0
    exit_vwap = gross_exit / buy_shares if buy_shares else 0.0
    gross_pnl = gross_exit - buy_cost
    fee_base = buy_cost + abs(gross_exit)
    fee = fee_base * fee_pct
    net_pnl = gross_pnl - fee
    if redeem_observed:
        status = "won"
    elif sell_shares > 0:
        status = "sold"
    else:
        status = "open"
    return PositionEpisode(
        wallet=wallet.lower(),
        condition_id=condition_id,
        asset=asset,
        outcome_index=outcome_idx,
        market_slug=market_slug,
        title=title,
        entry_ts=entry_ts,
        exit_ts=exit_ts,
        shares=round(buy_shares, 6),
        entry_vwap=round(entry_vwap, 6),
        exit_vwap=round(exit_vwap, 6),
        resolution_status=status,
        pnl_usd=round(net_pnl, 4),
        fee_paid=round(fee, 4),
    )


def backtest_all_wallets(
    conn: sqlite3.Connection,
    client: PolymarketClient,
    *,
    fee_pct: float = 0.0,
    only_active: bool = True,
) -> BacktestSummary:
    """Reconstruct episodes for all watchlist wallets + aggregate stats."""
    sql = "SELECT wallet FROM whale_watchlist"
    if only_active:
        sql += " WHERE active = 1"
    wallets = [r["wallet"] for r in conn.execute(sql)]
    all_episodes: list[PositionEpisode] = []
    by_wallet: dict[str, dict] = {}
    for w in wallets:
        episodes = reconstruct_positions_for_wallet(conn, client, w, fee_pct=fee_pct)
        all_episodes.extend(episodes)
        resolved = [e for e in episodes if e.pnl_usd is not None]
        wins = sum(1 for e in resolved if (e.pnl_usd or 0) > 0)
        losses = sum(1 for e in resolved if (e.pnl_usd or 0) <= 0)
        pnl = sum(e.pnl_usd or 0 for e in resolved)
        by_wallet[w] = {
            "episodes": len(episodes),
            "resolved": len(resolved),
            "wins": wins,
            "losses": losses,
            "pnl": round(pnl, 2),
            "wr_pct": round(wins / (wins + losses) * 100, 1) if (wins + losses) else None,
        }
    resolved_all = [e for e in all_episodes if e.pnl_usd is not None]
    won_all = sum(1 for e in resolved_all if (e.pnl_usd or 0) > 0)
    lost_all = sum(1 for e in resolved_all if (e.pnl_usd or 0) <= 0)
    pnl_total = sum(e.pnl_usd or 0 for e in resolved_all)
    avg = pnl_total / len(resolved_all) if resolved_all else 0.0
    return BacktestSummary(
        wallets=len(wallets),
        episodes_total=len(all_episodes),
        episodes_resolved=len(resolved_all),
        episodes_won=won_all,
        episodes_lost=lost_all,
        episodes_open=len(all_episodes) - len(resolved_all),
        realized_pnl=round(pnl_total, 2),
        avg_pnl=round(avg, 4),
        by_wallet=by_wallet,
    )
