"""Command-line entry point for polywhale.

All commands target Polymarket. Designed to be wired into systemd timers
for unattended operation on a small VPS (see deploy/).
"""

import logging
import sys

import click

from polywhale.backtest import (
    collect_signals,
    resolve_bets,
    summarize,
    synthesize_bets,
)
from polywhale.config import Settings
from polywhale.copy_trader import copy_trade_stats, process_copy_trades
from polywhale.db import connect, run_migrations
from polywhale.historical_backfill import backfill_all_watchlist, backfill_wallet_activity
from polywhale.historical_backtest import backtest_all_wallets
from polywhale.logging_setup import configure as configure_logging
from polywhale.monte_carlo import simulate_aggregated, simulate_per_whale
from polywhale.poly_arb import detect_combo_arb, inspect_event, persist_combo_arb
from polywhale.poly_paper import (
    freeze_paper_bet,
    paper_pnl_summary,
    record_combo_arb_legs,
    record_single_leg,
    settle_paper_bets,
    unfreeze_paper_bet,
)
from polywhale.poly_watch import WatchTarget, watch_loop
from polywhale.polymarket import PolymarketClient
from polywhale.whale_alerter import _wallet_labels, send_signal_alerts
from polywhale.whale_classify import fetch_and_classify, persist_profiles, top_arb_ops, top_sharps
from polywhale.whale_diff import detect_for_wallets, persist_signals
from polywhale.whale_refresh import (
    deactivate as watchlist_deactivate,
)
from polywhale.whale_refresh import (
    load_active_watchlist,
    refresh_watchlist,
    update_activity_stats,
    upsert_manual,
)
from polywhale.whale_review import (
    auto_drop,
    evaluate_all_active,
    review_and_autodrop,
)
from polywhale.whale_watch import prune_old_snapshots, snapshot_wallet, watch_wallets

logger = logging.getLogger("polywhale")


@click.group()
@click.option("--log-level", default=None, help="Override POLYWHALE_LOG_LEVEL (e.g. DEBUG, INFO).")
@click.pass_context
def cli(ctx: click.Context, log_level: str | None) -> None:
    settings = Settings.load()
    configure_logging(log_level or settings.log_level)
    ctx.obj = settings


@cli.command()
@click.pass_obj
def migrate(settings: Settings) -> None:
    """Apply any unapplied SQL migrations."""
    conn = connect(settings.db_path)
    try:
        applied = run_migrations(conn)
    finally:
        conn.close()
    if applied:
        click.echo(f"Applied {len(applied)} migration(s): {applied}")
    else:
        click.echo("Database is up to date.")


# ----- Polymarket market data -----


@cli.command(name="poly-markets")
@click.option("--limit", type=int, default=20, show_default=True)
@click.option(
    "--show-skew",
    is_flag=True,
    default=False,
    help="Only show markets whose outcome prices don't sum to ~1.00.",
)
@click.pass_obj
def poly_markets(settings: Settings, limit: int, show_skew: bool) -> None:
    """List active Polymarket markets by 24h volume."""
    _ = settings
    with PolymarketClient() as client:
        markets = client.list_markets(closed=False, limit=max(limit * 3, 50))
    markets = sorted(markets, key=lambda m: m.volume_24h, reverse=True)
    if show_skew:
        markets = [m for m in markets if abs(m.price_sum - 1.0) > 0.005]
    markets = markets[:limit]
    if not markets:
        click.echo("No matching markets.")
        return
    click.echo(f"{len(markets)} market(s):")
    for m in markets:
        sum_tag = f"  SUM={m.price_sum:.3f}" if abs(m.price_sum - 1.0) > 0.005 else ""
        if len(m.outcomes) <= 4:
            prices = " / ".join(
                f"{o} {p:.3f}" for o, p in zip(m.outcomes, m.outcome_prices, strict=False)
            )
        else:
            prices = f"{len(m.outcomes)} outcomes"
        question = m.question[:65] + ("..." if len(m.question) > 65 else "")
        click.echo(f"  ${m.volume_24h:>11,.0f}/24h  [{prices}]{sum_tag}  {question}")


@cli.command(name="poly-book")
@click.option("--slug", required=True, help="Polymarket market slug.")
@click.pass_obj
def poly_book(settings: Settings, slug: str) -> None:
    """Print the current order book for a Polymarket market by slug."""
    _ = settings
    with PolymarketClient() as client:
        market = client.get_market(slug)
        if market is None:
            click.echo(f"No market found for slug {slug!r}.")
            return
        click.echo(f"\n{market.question}")
        click.echo(
            f"  outcomes={market.outcomes}  prices={market.outcome_prices}  "
            f"vol24h=${market.volume_24h:,.0f}  neg_risk={market.neg_risk}"
        )
        for token_id, outcome in zip(market.token_ids, market.outcomes, strict=False):
            book = client.get_book(token_id)
            bid5 = book.depth_within(side="bid", pct=0.05)
            ask5 = book.depth_within(side="ask", pct=0.05)
            click.echo(f"\n  Outcome [{outcome}] token={token_id[:12]}...{token_id[-6:]}")
            click.echo(
                f"    best_bid={book.best_bid}  best_ask={book.best_ask}  "
                f"spread={book.spread}  last={book.last_trade_price}"
            )
            click.echo(f"    depth within 5% of inside: bid={bid5:,.0f}  ask={ask5:,.0f}")
            click.echo(f"    bid levels={len(book.bids)}  ask levels={len(book.asks)}")


@cli.command(name="poly-watch")
@click.option("--slug", "slugs", multiple=True, help="Market slug(s) to watch.")
@click.option(
    "--default",
    "use_default",
    is_flag=True,
    default=False,
    help="Add top-volume markets to the watch set (good for arb-scan context).",
)
@click.option(
    "--from-positions",
    "use_positions",
    is_flag=True,
    default=False,
    help="Add markets where we currently hold open paper bets (enables intraday "
         "price tracking on our own positions).",
)
@click.option("--interval", type=int, default=60, show_default=True)
@click.option("--iterations", type=int, default=1, show_default=True, help="0 = loop forever.")
@click.pass_obj
def poly_watch(
    settings: Settings,
    slugs: tuple[str, ...],
    use_default: bool,
    use_positions: bool,
    interval: int,
    iterations: int,
) -> None:
    """Poll Polymarket order books for given slugs; persist depth snapshots."""
    from polywhale.watchlist import (
        fetch_default_market_slugs,
        fetch_open_position_market_slugs,
    )

    conn = connect(settings.db_path)
    try:
        run_migrations(conn)
        with PolymarketClient() as client:
            collected: list[str] = list(slugs)
            if use_default:
                collected += fetch_default_market_slugs(client, top_n=10)
            if use_positions:
                collected += fetch_open_position_market_slugs(conn)
            # Dedup while preserving order
            seen: set[str] = set()
            resolved = tuple(s for s in collected if s and not (s in seen or seen.add(s)))
            if not resolved:
                click.echo("No slugs given. Use --slug, --default, or --from-positions.")
                return
            targets: list[WatchTarget] = []
            for slug in resolved:
                market = client.get_market(slug)
                if market is None:
                    click.echo(f"  skip: no market for slug {slug!r}")
                    continue
                for token_id, outcome in zip(market.token_ids, market.outcomes, strict=False):
                    targets.append(
                        WatchTarget(market_slug=market.slug, token_id=token_id, outcome=outcome)
                    )
            if not targets:
                click.echo("No targets resolved. Aborting.")
                return
            click.echo(
                f"Watching {len(targets)} outcome(s) across {len(resolved)} market(s) "
                f"every {interval}s..."
            )
            max_iter = None if iterations == 0 else iterations
            total = watch_loop(conn, client, targets, interval_s=interval, max_iterations=max_iter)
            click.echo(f"Done. {total} snapshot(s) stored.")
    finally:
        conn.close()


# ----- Combinatorial arbitrage -----


@cli.command(name="poly-arbs")
@click.option("--event-slug", "event_slugs", multiple=True, required=True)
@click.option("--fee-pct", type=float, default=0.75, show_default=True)
@click.option("--min-edge", type=float, default=0.5, show_default=True)
@click.option("--inspect-only", is_flag=True, default=False)
@click.pass_obj
def poly_arbs(
    settings: Settings,
    event_slugs: tuple[str, ...],
    fee_pct: float,
    min_edge: float,
    inspect_only: bool,
) -> None:
    """Detect combinatorial arbitrage on Polymarket neg-risk event groups."""
    conn = connect(settings.db_path)
    try:
        run_migrations(conn)
        with PolymarketClient() as client:
            for slug in event_slugs:
                click.echo(f"\nEvent: {slug}")
                if inspect_only:
                    sum_ask, legs_n, title = inspect_event(client, slug)
                    if legs_n == 0:
                        click.echo("  (no neg-risk legs found)")
                        continue
                    raw_edge_pct = (1.0 - sum_ask) * 100
                    net_edge_pct = ((1.0 - sum_ask) - fee_pct / 100) * 100
                    click.echo(
                        f"  title          : {title}\n"
                        f"  neg-risk legs  : {legs_n}\n"
                        f"  sum(best_ask)  : {sum_ask:.4f}\n"
                        f"  raw edge       : {raw_edge_pct:.2f}%\n"
                        f"  after {fee_pct:.2f}% fee: {net_edge_pct:.2f}%"
                    )
                    continue
                arb = detect_combo_arb(client, slug, fee_pct=fee_pct, min_edge_pct=min_edge)
                if not arb:
                    click.echo(f"  no arb (sum >= {1.0 - min_edge / 100.0:.4f} after fees)")
                    continue
                arb_id = persist_combo_arb(conn, arb)
                click.echo(
                    f"  ARB FOUND #{arb_id}: sum_ask={arb.sum_best_ask:.4f}  "
                    f"edge={arb.edge_pct:.2f}%  legs={arb.outcomes_count}"
                )
                click.echo(f"  title={arb.event_title}")
                cheapest = sorted(arb.legs, key=lambda x: x.best_ask or 1.0)[:5]
                click.echo("  Cheapest legs:")
                for leg in cheapest:
                    name = leg.outcome_title or leg.question or leg.market_slug[:30]
                    click.echo(
                        f"    {name[:35]:<35} ask={leg.best_ask:.4f}  depth={leg.ask_depth:.0f}"
                    )
    finally:
        conn.close()


# ----- Paper trading -----


@cli.command(name="poly-paper-combo")
@click.option("--event-slug", required=True)
@click.option("--total-stake", type=float, default=100.0, show_default=True)
@click.option("--allow-overround", is_flag=True, default=False)
@click.pass_obj
def poly_paper_combo(
    settings: Settings,
    event_slug: str,
    total_stake: float,
    allow_overround: bool,
) -> None:
    """Detect combo arb on an event; record one paper bet per leg at current ask."""
    conn = connect(settings.db_path)
    try:
        run_migrations(conn)
        with PolymarketClient() as client:
            min_edge = -1000.0 if allow_overround else -100.0
            arb = detect_combo_arb(client, event_slug, min_edge_pct=min_edge)
            if not arb:
                click.echo("No legs found.")
                return
            arb_id = None
            if arb.edge_pct >= 0.5:
                arb_id = persist_combo_arb(conn, arb)
            summary = record_combo_arb_legs(
                conn, arb, arb_id=arb_id, total_stake_usd=total_stake, event_slug=event_slug
            )
        click.echo(
            f"Paper-bet placed: {summary.placed} leg(s), total cost ${summary.total_cost_usd:.2f}."
        )
        click.echo(f"  event_slug={event_slug}")
        click.echo(f"  sum_best_ask={arb.sum_best_ask:.4f}  edge_pct={arb.edge_pct:.2f}%")
        if arb.sum_best_ask > 1.0:
            click.echo("  NOTE: sum > 1, this is a known paper loss (pipeline validation only).")
    finally:
        conn.close()


@cli.command(name="poly-paper-bet")
@click.option("--slug", required=True)
@click.option("--side", required=True, type=click.Choice(["YES", "NO"]))
@click.option("--shares", type=float, required=True)
@click.pass_obj
def poly_paper_bet(settings: Settings, slug: str, side: str, shares: float) -> None:
    """Record one directional paper bet on a single Polymarket market."""
    conn = connect(settings.db_path)
    try:
        run_migrations(conn)
        with PolymarketClient() as client:
            market = client.get_market(slug)
            if market is None:
                click.echo(f"Market not found: {slug}")
                return
            if not market.token_ids:
                click.echo("No clobTokenIds on market.")
                return
            token_idx = 0 if side == "YES" else 1
            token_id = market.token_ids[token_idx]
            book = client.get_book(token_id)
            ask = book.best_ask
            if ask is None:
                click.echo("No ask available; cannot price the entry.")
                return
        bet_id = record_single_leg(
            conn,
            market_slug=slug,
            event_slug=None,
            token_id=token_id,
            side=side,
            outcome_title=market.outcomes[token_idx] if token_idx < len(market.outcomes) else None,
            entry_price=ask,
            size_shares=shares,
        )
        click.echo(
            f"Paper bet #{bet_id}: {side} on {slug} @ {ask:.4f}, "
            f"shares={shares}, cost=${ask * shares:.2f}"
        )
    finally:
        conn.close()


@cli.command(name="poly-paper-settle")
@click.option(
    "--prune-days",
    type=int,
    default=30,
    show_default=True,
    help="Also prune whale_positions and polymarket_books older than N days. 0 disables.",
)
@click.pass_obj
def poly_paper_settle(settings: Settings, prune_days: int) -> None:
    """Settle resolved paper bets and prune old snapshots (daily housekeeping)."""
    conn = connect(settings.db_path)
    try:
        run_migrations(conn)
        with PolymarketClient() as client:
            summary = settle_paper_bets(conn, client)
        click.echo(
            f"Checked {summary['checked']} market(s). "
            f"Settled {summary['settled']}. Still open: {summary['still_open']}. "
            f"Frozen: {summary.get('frozen', 0)}."
        )
        if prune_days > 0:
            pruned = prune_old_snapshots(conn, days=prune_days)
            click.echo(
                f"Pruned (>{prune_days}d): whale_positions={pruned['whale_positions']}, "
                f"polymarket_books={pruned['polymarket_books']}"
            )
    finally:
        conn.close()


@cli.command(name="poly-paper-freeze")
@click.option("--bet-id", type=int, required=True)
@click.option(
    "--reason",
    required=True,
    help="Why the bet is frozen (e.g., 'UMA dispute on market X').",
)
@click.pass_obj
def poly_paper_freeze(settings: Settings, bet_id: int, reason: str) -> None:
    """Freeze a paper bet so settlement skips it until unfrozen."""
    conn = connect(settings.db_path)
    try:
        run_migrations(conn)
        ok = freeze_paper_bet(conn, bet_id, reason=reason)
    finally:
        conn.close()
    if ok:
        click.echo(f"Froze bet {bet_id}: {reason}")
    else:
        click.echo(f"Bet {bet_id} not frozen (already settled, missing, or already frozen).")


@cli.command(name="poly-paper-unfreeze")
@click.option("--bet-id", type=int, required=True)
@click.pass_obj
def poly_paper_unfreeze(settings: Settings, bet_id: int) -> None:
    """Clear the frozen flag on a paper bet so settlement can resume."""
    conn = connect(settings.db_path)
    try:
        run_migrations(conn)
        ok = unfreeze_paper_bet(conn, bet_id)
    finally:
        conn.close()
    if ok:
        click.echo(f"Unfroze bet {bet_id}.")
    else:
        click.echo(f"Bet {bet_id} was not frozen.")


@cli.command(name="poly-paper-pulse")
@click.pass_obj
def poly_paper_pulse(settings: Settings) -> None:
    """Show paper-bet stats by source: count, cost, P&L, wins/losses."""
    conn = connect(settings.db_path)
    try:
        run_migrations(conn)
        stats = paper_pnl_summary(conn)
        if not stats:
            click.echo("No paper bets yet.")
            return
        click.echo("Polymarket paper P&L:")
        for source, s in stats.items():
            click.echo(
                f"  {source:<12} bets={s['bets']:>4}  settled={s['settled_bets']:>4}  "
                f"wins={s['wins']:>3}  losses={s['losses']:>3}  "
                f"cost=${s['total_cost']:>9.2f}  P&L=${s['total_pnl']:>+9.2f}"
            )
    finally:
        conn.close()


# ----- Whales -----


@cli.command(name="poly-whales")
@click.option("--window", default="30d", show_default=True)
@click.option("--top", "top_n", type=int, default=50, show_default=True)
@click.option("--min-volume", type=float, default=1_000_000.0, show_default=True)
@click.pass_obj
def poly_whales(settings: Settings, window: str, top_n: int, min_volume: float) -> None:
    """Fetch + classify Polymarket leaderboard wallets as sharp / arb_op / hybrid."""
    from collections import Counter

    conn = connect(settings.db_path)
    try:
        run_migrations(conn)
        with PolymarketClient() as client:
            profiles = fetch_and_classify(client, window=window, top_n=top_n, min_volume=min_volume)
            stored = persist_profiles(conn, profiles)
        click.echo(f"Classified {stored} wallet(s) over window={window}.")
        shapes = Counter(p.shape for p in profiles)
        click.echo(
            f"  sharps={shapes['sharp']}  arb_ops={shapes['arb_op']}  "
            f"hybrids={shapes['hybrid']}  unknown={shapes['unknown']}"
        )
        sharps = top_sharps(profiles, n=10)
        if sharps:
            click.echo("\nTop sharps (worth copying):")
            for p in sharps:
                name = (p.pseudonym or p.name or p.wallet[:10])[:25]
                click.echo(
                    f"  {name:<25} {p.wallet}  margin={p.margin_pct:>5.1f}%  "
                    f"profit=${p.profit:>10,.0f}  vol=${p.volume:>13,.0f}"
                )
        arb_ops = top_arb_ops(profiles, n=5)
        if arb_ops:
            click.echo("\nTop arb operators (DO NOT copy):")
            for p in arb_ops:
                name = (p.pseudonym or p.name or p.wallet[:10])[:25]
                click.echo(
                    f"  {name:<25} {p.wallet}  margin={p.margin_pct:>5.1f}%  "
                    f"profit=${p.profit:>10,.0f}  vol=${p.volume:>13,.0f}"
                )
    finally:
        conn.close()


@cli.command(name="whale-snapshot")
@click.option("--wallet", required=True)
@click.option("--size-threshold", type=float, default=10.0, show_default=True)
@click.pass_obj
def whale_snapshot(settings: Settings, wallet: str, size_threshold: float) -> None:
    """Pull current open positions for a wallet; persist to whale_positions."""
    conn = connect(settings.db_path)
    try:
        run_migrations(conn)
        with PolymarketClient() as client:
            count = snapshot_wallet(conn, client, wallet, size_threshold=size_threshold)
        click.echo(f"Stored {count} positions for {wallet}.")
        rows = list(
            conn.execute(
                """
                SELECT title, outcome, size, avg_price, current_price, cash_pnl, percent_pnl
                FROM whale_positions
                WHERE wallet = ?
                  AND captured_at = (SELECT MAX(captured_at) FROM whale_positions WHERE wallet = ?)
                ORDER BY current_value DESC NULLS LAST
                LIMIT 10
                """,
                (wallet, wallet),
            )
        )
        if rows:
            click.echo("\nTop 10 positions by current value:")
            for r in rows:
                title = (r["title"] or "")[:55]
                pnl = r["cash_pnl"] or 0
                pct = r["percent_pnl"] or 0
                click.echo(
                    f"  {title:<55} {r['outcome']:<10} size={r['size']:>10,.0f}  "
                    f"avg={r['avg_price']:.3f}  cur={r['current_price']:.3f}  "
                    f"pnl={pnl:>10,.2f} ({pct:>+.1f}%)"
                )
    finally:
        conn.close()


@cli.command(name="whale-watch")
@click.option("--wallet", "wallets", multiple=True)
@click.option("--default", "use_default", is_flag=True, default=False)
@click.option("--interval", type=int, default=300, show_default=True)
@click.option("--iterations", type=int, default=1, show_default=True, help="0 = loop forever.")
@click.pass_obj
def whale_watch_cmd(
    settings: Settings,
    wallets: tuple[str, ...],
    use_default: bool,
    interval: int,
    iterations: int,
) -> None:
    """Poll multiple whale wallets' open positions on an interval."""
    resolved: tuple[str, ...] = wallets
    conn = connect(settings.db_path)
    try:
        run_migrations(conn)
        if use_default and not wallets:
            resolved = tuple(load_active_watchlist(conn))
        elif use_default and wallets:
            resolved = tuple(list(wallets) + load_active_watchlist(conn))
        if not resolved:
            click.echo("No wallets given. Use --wallet or --default.")
            return
        with PolymarketClient() as client:
            max_iter = None if iterations == 0 else iterations
            total = watch_wallets(
                conn, client, resolved, interval_s=interval, max_iterations=max_iter
            )
        click.echo(f"Done. {total} position-row(s) stored across {len(resolved)} wallet(s).")
    finally:
        conn.close()


@cli.command(name="whale-signals")
@click.option("--wallet", "wallets", multiple=True)
@click.option("--default", "use_default", is_flag=True, default=False)
@click.option("--alert", is_flag=True, default=False, help="Push Telegram alert for new signals.")
@click.pass_obj
def whale_signals_cmd(
    settings: Settings,
    wallets: tuple[str, ...],
    use_default: bool,
    alert: bool,
) -> None:
    """Diff the last two whale snapshots; emit new/added/closed/reduced signals."""
    targets: tuple[str, ...] = wallets
    conn = connect(settings.db_path)
    try:
        run_migrations(conn)
        if use_default:
            targets = tuple(list(wallets) + load_active_watchlist(conn))
        if not targets:
            targets = tuple(load_active_watchlist(conn))
        if not targets:
            click.echo("No wallets.")
            return
        signals = detect_for_wallets(conn, targets)
        stored = persist_signals(conn, signals)
        click.echo(
            f"Detected {len(signals)} signal(s) across {len(targets)} wallet(s); stored {stored}."
        )
        from collections import Counter

        by_type = Counter(s.signal_type for s in signals)
        for t, c in by_type.most_common():
            click.echo(f"  {t}: {c}")
        if signals:
            click.echo("\nRecent signals:")
            for s in signals[:10]:
                title = (s.title or "(?)")[:45]
                old_str = f" (was {s.old_size:,.0f})" if s.old_size else ""
                click.echo(
                    f"  {s.signal_type:<16} {s.wallet[:10]} "
                    f"{s.outcome or '?':<10} size={s.new_size or 0:>8,.0f}{old_str}  {title}"
                )
        if alert:
            if not settings.telegram_bot_token or not settings.telegram_chat_id:
                click.echo("\n[alert skipped: Telegram not configured]")
            else:
                summary = send_signal_alerts(
                    conn,
                    token=settings.telegram_bot_token,
                    chat_id=settings.telegram_chat_id,
                )
                if summary["sent"]:
                    click.echo(f"\nTelegram alert sent for {summary['signals']} signal(s).")
                else:
                    click.echo(f"\nNo alert sent: {summary['reason']}")
    finally:
        conn.close()


def _send_autodrop_alert(reviews, *, token: str, chat_id: str) -> None:
    """Notify Telegram when whale-fast auto-drops one or more whales."""
    import html as _html

    from polywhale.telegram import send_message
    lines = [f"🚫 <b>Auto-dropped {len(reviews)} whale(s)</b>"]
    for r in reviews:
        who = _html.escape(r.label or r.wallet[:14])
        reason = _html.escape(r.reason)[:140]
        lines.append(f"• <b>{who}</b> [tier {r.tier}] — {reason}")
    send_message(token, chat_id, "\n".join(lines))


@cli.command(name="whale-fast")
@click.option("--wallet", "wallets", multiple=True)
@click.option("--default", "use_default", is_flag=True, default=False)
@click.option("--alert", is_flag=True, default=False, help="Push Telegram alert for new signals.")
@click.option("--size-threshold", type=float, default=10.0, show_default=True)
@click.pass_obj
def whale_fast_cmd(
    settings: Settings,
    wallets: tuple[str, ...],
    use_default: bool,
    alert: bool,
    size_threshold: float,
) -> None:
    """One-shot snapshot + diff + alert for whale wallets. Designed for 60s timer cadence."""
    targets: tuple[str, ...] = wallets
    conn = connect(settings.db_path)
    try:
        run_migrations(conn)
        if use_default and not wallets:
            targets = tuple(load_active_watchlist(conn))
        elif use_default and wallets:
            targets = tuple(list(wallets) + load_active_watchlist(conn))
        if not targets:
            click.echo("No wallets. Use --wallet or --default.")
            return
        with PolymarketClient() as client:
            snap_count = 0
            for wallet in targets:
                try:
                    snap_count += snapshot_wallet(
                        conn, client, wallet, size_threshold=size_threshold
                    )
                except Exception as exc:
                    logger.warning("snapshot failed for %s: %s", wallet, exc)
        signals = detect_for_wallets(conn, targets)
        stored = persist_signals(conn, signals)
        copy = {"opened": 0, "closed": 0, "realized_pnl": 0.0}
        if stored > 0:
            copy = process_copy_trades(
                conn,
                bankroll_usd=settings.paper_bankroll_usd,
                stake_pct=settings.paper_stake_pct,
                ai_api_key=settings.openrouter_api_key,
                ai_model=settings.ai_model,
                use_ai_advisor=settings.use_ai_advisor,
            )
        # Continuous auto-drop: re-tier every cycle. Manual entries are exempt
        # (handled inside evaluate_whale). When trades close, tier D/E becomes
        # actionable immediately instead of waiting for Sunday's refresh.
        dropped_reviews = review_and_autodrop(conn)
        ai_tag = f" ai_calls={copy.get('ai_calls', 0)}" if settings.use_ai_advisor else ""
        sk = copy.get("skipped_bankroll", 0)
        skip_tag = f" skipped_bankroll={sk}" if sk else ""
        drop_tag = f" dropped={len(dropped_reviews)}" if dropped_reviews else ""
        click.echo(
            f"whale-fast: wallets={len(targets)} positions={snap_count} "
            f"signals_detected={len(signals)} stored={stored} "
            f"copy_opened={copy['opened']} copy_closed={copy['closed']}{skip_tag} "
            f"copy_pnl=${copy['realized_pnl']:+.2f}{ai_tag}{drop_tag}"
        )
        if dropped_reviews and settings.telegram_bot_token and settings.telegram_chat_id:
            _send_autodrop_alert(
                dropped_reviews,
                token=settings.telegram_bot_token,
                chat_id=settings.telegram_chat_id,
            )
        if alert and stored > 0:
            if not settings.telegram_bot_token or not settings.telegram_chat_id:
                click.echo("[alert skipped: Telegram not configured]")
            else:
                summary = send_signal_alerts(
                    conn,
                    token=settings.telegram_bot_token,
                    chat_id=settings.telegram_chat_id,
                )
                if summary["sent"]:
                    click.echo(f"Telegram alert sent for {summary['signals']} signal(s).")
    finally:
        conn.close()


@cli.command(name="whale-refresh")
@click.option("--min-margin", type=float, default=3.0, show_default=True,
              help="Minimum margin %% (profit/volume) to qualify as a sharp.")
@click.option("--min-profit", type=float, default=50_000.0, show_default=True,
              help="Minimum total profit (USD) to qualify.")
@click.option("--min-volume", type=float, default=1_000_000.0, show_default=True,
              help="Minimum volume (USD) to qualify (filters lucky small samples).")
@click.option("--max-dormant-days", type=int, default=14, show_default=True,
              help="Reject candidates with no trades in N days. Also deactivates "
                   "auto-discovered watchlist entries that went silent for N days.")
@click.option("--min-wr", type=float, default=60.0, show_default=True,
              help="Minimum approx win rate %% (computed from REDEEMs per market).")
@click.option("--min-wr-sample", type=int, default=20, show_default=True,
              help="Minimum unique markets traded for WR to be considered.")
@click.option("--top", "top_n", type=int, default=100, show_default=True,
              help="How deep to scan the leaderboard.")
@click.pass_obj
def whale_refresh_cmd(
    settings: Settings,
    min_margin: float,
    min_profit: float,
    min_volume: float,
    max_dormant_days: int,
    min_wr: float,
    min_wr_sample: int,
    top_n: int,
) -> None:
    """Auto-discover new sharps from the leaderboard; deactivate dormant auto entries."""
    conn = connect(settings.db_path)
    try:
        run_migrations(conn)
        with PolymarketClient() as client:
            result = refresh_watchlist(
                conn, client,
                min_margin_pct=min_margin,
                min_profit_usd=min_profit,
                min_volume_usd=min_volume,
                max_dormant_days=max_dormant_days,
                min_wr_pct=min_wr,
                min_wr_sample=min_wr_sample,
                top_n=top_n,
            )
        click.echo("whale-refresh:")
        click.echo(f"  seeded (first run)        : {result.seeded}")
        click.echo(f"  new auto-sharps added     : {result.added}")
        click.echo(f"  existing entries updated  : {result.updated}")
        click.echo(f"  rejected by activity/WR   : {result.rejected_activity}")
        click.echo(f"  backfilled stats          : {result.backfilled_stats}")
        click.echo(f"  dormant auto deactivated  : {result.deactivated}")
        click.echo(f"  review auto-dropped       : {result.review_dropped}")
        click.echo(f"  active watchlist total    : {result.active_total}")
    finally:
        conn.close()


@cli.command(name="watchlist")
@click.option("--all", "show_all", is_flag=True, default=False,
              help="Include deactivated entries in output.")
@click.option("--no-refresh-stats", is_flag=True, default=False,
              help="Skip recomputing signals_30d before display (faster).")
@click.pass_obj
def watchlist_cmd(settings: Settings, show_all: bool, no_refresh_stats: bool) -> None:
    """Print the current whale watchlist sorted by recent activity."""
    import time as _time

    conn = connect(settings.db_path)
    try:
        run_migrations(conn)
        if not no_refresh_stats:
            update_activity_stats(conn)
        sql = (
            "SELECT wallet, label, source, margin_pct, profit_usd, "
            "signals_30d, last_signal_at, win_rate_pct, wr_sample_size, "
            "last_trade_at, active, deactivated_reason "
            "FROM whale_watchlist"
        )
        if not show_all:
            sql += " WHERE active = 1"
        sql += (
            " ORDER BY active DESC, signals_30d DESC, "
            "profit_usd DESC NULLS LAST"
        )
        rows = list(conn.execute(sql))
        if not rows:
            click.echo("Watchlist is empty. Run `polywhale whale-refresh` first.")
            return
        now = int(_time.time())

        def _age(ts: int | None) -> str:
            if not ts:
                return "-"
            h = (now - int(ts)) / 3600.0
            return f"{h:.1f}h" if h < 24 else f"{h / 24:.1f}d"

        click.echo(
            f"{'wallet':<14} {'label':<18} {'margin%':>7} "
            f"{'profit':>11} {'sigs30':>6} {'WR':>5} {'n':>4} "
            f"{'last_trade':<10} {'last_sig':<10}"
        )
        for r in rows:
            label = (r["label"] or "")[:18]
            margin = f"{r['margin_pct']:.1f}" if r["margin_pct"] is not None else "-"
            profit = f"${r['profit_usd']:,.0f}" if r["profit_usd"] is not None else "-"
            sigs = r["signals_30d"] or 0
            wr_val = r["win_rate_pct"]
            wr = f"{wr_val:.0f}%" if wr_val is not None else "-"
            wr_n = r["wr_sample_size"] or 0
            click.echo(
                f"{r['wallet'][:14]:<14} {label:<18} {margin:>7} {profit:>11} "
                f"{sigs:>6} {wr:>5} {wr_n:>4} "
                f"{_age(r['last_trade_at']):<10} {_age(r['last_signal_at']):<10}"
            )
    finally:
        conn.close()


@cli.command(name="whale-review")
@click.option("--auto-drop", "do_drop", is_flag=True, default=False,
              help="Actually deactivate whales recommended for drop. Default: preview only.")
@click.option("--min-trades", type=int, default=25, show_default=True,
              help="Minimum closed paper-copy trades before tier D/A is even considered.")
@click.option("--loss-threshold", type=float, default=-30.0, show_default=True,
              help="Realized PnL <= this triggers tier D (drop).")
@click.option("--zero-epsilon", type=float, default=0.20, show_default=True,
              help="|avg pnl/trade| <= this with enough samples triggers tier D.")
@click.option("--max-quiet-days", type=int, default=21, show_default=True,
              help="No signals in N days + insufficient samples = tier E (drop dormant).")
@click.option("--boost-threshold", type=float, default=200.0, show_default=True,
              help="Realized PnL >= this with >= min-trades triggers tier A (boost candidate).")
@click.pass_obj
def whale_review_cmd(
    settings: Settings, do_drop: bool, min_trades: int, loss_threshold: float,
    zero_epsilon: float, max_quiet_days: int, boost_threshold: float,
) -> None:
    """Score every active whale and recommend keep/drop based on observed paper PnL."""
    conn = connect(settings.db_path)
    try:
        run_migrations(conn)
        reviews = evaluate_all_active(
            conn,
            min_trades_to_judge=min_trades,
            loss_threshold=loss_threshold,
            zero_pnl_epsilon=zero_epsilon,
            max_quiet_days=max_quiet_days,
            boost_threshold=boost_threshold,
            boost_min_trades=min_trades,
        )
        # Sort by tier (A best, E worst), then by PnL within tier
        tier_order = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4}
        reviews.sort(key=lambda r: (tier_order.get(r.tier, 9), -r.realized_pnl_all))
        click.echo(
            f"=== Whale Review ({len(reviews)} active) ===\n"
            f"  min-trades={min_trades}  loss<={loss_threshold}  "
            f"zero-eps={zero_epsilon}  dormant>{max_quiet_days}d\n"
        )
        click.echo(
            f"  {'tier':<4} {'whale':<22} {'trades':>6} {'W/L':>7} {'PnL':>10} "
            f"{'avg':>8} {'rec':<14}  reason"
        )
        counts: dict[str, int] = {}
        for r in reviews:
            counts[r.tier] = counts.get(r.tier, 0) + 1
            who = (r.label or r.wallet[:14])[:22]
            wl_str = f"{r.wins}/{r.losses}"
            avg = (
                f"${r.avg_pnl_per_trade:+.2f}"
                if r.avg_pnl_per_trade is not None
                else "-"
            )
            click.echo(
                f"  {r.tier:<4} {who:<22} {r.closed_trades_all:>6} {wl_str:>7} "
                f"${r.realized_pnl_all:+8.2f} {avg:>8} {r.recommendation:<14}  "
                f"{r.reason[:80]}"
            )
        click.echo(
            "\n  Tier counts: "
            + ", ".join(f"{t}={counts.get(t, 0)}" for t in ("A", "B", "C", "D", "E"))
        )
        droppable = [r for r in reviews if r.recommendation in ("drop", "drop_dormant")]
        if do_drop and droppable:
            dropped = auto_drop(conn, droppable)
            click.echo(f"\n  AUTO-DROPPED {len(dropped)} whale(s).")
        elif droppable and not do_drop:
            click.echo(
                f"\n  {len(droppable)} whale(s) recommended for drop. "
                "Re-run with --auto-drop to apply."
            )
    finally:
        conn.close()


@cli.command(name="watchlist-add")
@click.option("--wallet", required=True)
@click.option("--label", default=None, help="Optional pseudonym/note for this wallet.")
@click.option("--notes", default=None)
@click.pass_obj
def watchlist_add_cmd(
    settings: Settings, wallet: str, label: str | None, notes: str | None
) -> None:
    """Manually add (or re-activate) a wallet on the watchlist."""
    conn = connect(settings.db_path)
    try:
        run_migrations(conn)
        ok = upsert_manual(conn, wallet=wallet, label=label, notes=notes)
        click.echo(f"watchlist-add {wallet}: {'ok' if ok else 'no change'}")
    finally:
        conn.close()


@cli.command(name="watchlist-remove")
@click.option("--wallet", required=True)
@click.option("--reason", default="manual", show_default=True)
@click.pass_obj
def watchlist_remove_cmd(settings: Settings, wallet: str, reason: str) -> None:
    """Deactivate a wallet (does not delete; can be re-activated with watchlist-add)."""
    conn = connect(settings.db_path)
    try:
        run_migrations(conn)
        ok = watchlist_deactivate(conn, wallet, reason=reason)
        click.echo(f"watchlist-remove {wallet}: {'ok' if ok else 'not active'}")
    finally:
        conn.close()


@cli.command(name="backtest")
@click.option("--since-days", type=int, default=30, show_default=True)
@click.option("--stake", type=float, default=100.0, show_default=True,
              help="Base stake per signal (USD).")
@click.option("--min-conviction", type=float, default=0.0, show_default=True,
              help="Drop signals whose conviction_discount is below this.")
@click.option("--no-conviction-weighting", is_flag=True, default=False,
              help="Treat all signals as full stake regardless of conviction discount.")
@click.option("--top", "top_n", type=int, default=20, show_default=True)
@click.pass_obj
def backtest_cmd(
    settings: Settings,
    since_days: int,
    stake: float,
    min_conviction: float,
    no_conviction_weighting: bool,
    top_n: int,
) -> None:
    """Replay historical whale signals into synthetic paper bets; show per-wallet PnL."""
    conn = connect(settings.db_path)
    try:
        run_migrations(conn)
        rows = collect_signals(
            conn, since_days=since_days, min_conviction=min_conviction
        )
        bets = synthesize_bets(
            rows,
            stake_per_signal=stake,
            weight_by_conviction=not no_conviction_weighting,
        )
        with PolymarketClient() as client:
            resolved = resolve_bets(bets, client)
        s = summarize(len(rows), resolved)
        click.echo(f"=== Backtest: last {since_days} days ===")
        click.echo(f"  signals collected : {s.signals_total}")
        click.echo(f"  synthetic bets    : {s.bets_placed}")
        click.echo(f"  resolved          : {s.bets_resolved}")
        click.echo(f"  unresolved (skip) : {s.bets_unresolved}")
        click.echo(f"  total PnL         : ${s.total_pnl:+,.2f}")
        if not s.by_wallet:
            click.echo("\nNo bets to score yet. Run the live bot for a few days, then retry.")
            return
        labels = _wallet_labels(conn, list(s.by_wallet.keys()))
        click.echo("\nPer-whale attribution (sorted by settled PnL):")
        ranked = sorted(
            s.by_wallet.items(),
            key=lambda kv: -kv[1]["pnl"],
        )[:top_n]
        for wallet, stats in ranked:
            label = labels.get(wallet) or wallet[:14] + "..."
            click.echo(
                f"  {label:<25} bets={stats['bets']:>3}  "
                f"settled={stats['settled']:>3}  "
                f"W/L={stats['wins']}/{stats['losses']:<3}  "
                f"PnL=${stats['pnl']:+,.2f}"
            )
        click.echo("\nConviction bucket (does overreaction filter help?):")
        for bucket in ("full", "discount-light", "discount-heavy", "discount-floor"):
            stats = s.by_bucket.get(bucket)
            if not stats:
                continue
            click.echo(
                f"  {bucket:<18} bets={stats['bets']:>3}  "
                f"settled={stats['settled']:>3}  "
                f"W/L={stats['wins']}/{stats['losses']:<3}  "
                f"PnL=${stats['pnl']:+,.2f}"
            )
    finally:
        conn.close()


@cli.command(name="historical-backfill")
@click.option("--wallet", default=None,
              help="Single wallet to backfill. Omit to do entire watchlist.")
@click.option("--max-offset", type=int, default=5000, show_default=True,
              help="Max pagination depth (api errors past ~5K).")
@click.option("--page-size", type=int, default=500, show_default=True)
@click.pass_obj
def historical_backfill_cmd(
    settings: Settings, wallet: str | None, max_offset: int, page_size: int,
) -> None:
    """Paginate data-api/activity for watchlist wallets; store raw events."""
    conn = connect(settings.db_path)
    try:
        run_migrations(conn)
        with PolymarketClient() as client:
            if wallet:
                r = backfill_wallet_activity(
                    conn, client, wallet,
                    max_offset=max_offset, page_size=page_size,
                )
                click.echo(
                    f"backfill {wallet[:14]}: pages={r['pages']} "
                    f"inserted={r['inserted']} dup={r['skipped_dup']} "
                    f"oldest_ts={r['oldest_ts']}"
                )
            else:
                s = backfill_all_watchlist(
                    conn, client, max_offset=max_offset, page_size=page_size,
                )
                click.echo(
                    f"backfill all: wallets={s['wallets']} "
                    f"total_pages={s['total_pages']} "
                    f"total_inserted={s['total_inserted']}"
                )
    finally:
        conn.close()


@cli.command(name="historical-backtest")
@click.option(
    "--fee-pct", type=float, default=0.01, show_default=True,
    help="Per-side fee (0.01 = 1%). PM: 0.0075 sports, 0.01 politics, 0.018 crypto.",
)
@click.option("--top", "top_n", type=int, default=20, show_default=True)
@click.pass_obj
def historical_backtest_cmd(settings: Settings, fee_pct: float, top_n: int) -> None:
    """Reconstruct historical position episodes per whale + aggregate edge stats."""
    conn = connect(settings.db_path)
    try:
        run_migrations(conn)
        with PolymarketClient() as client:
            s = backtest_all_wallets(conn, client, fee_pct=fee_pct)
        click.echo(
            f"=== Historical Backtest (fee {fee_pct*100:.2f}%) ===\n"
            f"  wallets evaluated     : {s.wallets}\n"
            f"  position episodes     : {s.episodes_total}\n"
            f"  resolved              : {s.episodes_resolved}\n"
            f"  still open            : {s.episodes_open}\n"
            f"  wins / losses         : {s.episodes_won} / {s.episodes_lost}\n"
            f"  realized PnL          : ${s.realized_pnl:+,.2f}\n"
            f"  avg PnL per episode   : ${s.avg_pnl:+,.4f}\n"
        )
        if s.by_wallet:
            click.echo("Per-whale edge (sorted by PnL):")
            ranked = sorted(
                s.by_wallet.items(), key=lambda kv: -(kv[1]["pnl"] or 0)
            )[:top_n]
            for w, stats in ranked:
                label = conn.execute(
                    "SELECT label FROM whale_watchlist WHERE wallet = ?", (w,)
                ).fetchone()
                name = (label["label"] if label and label["label"] else w[:14])[:25]
                wr = f"{stats['wr_pct']}%" if stats["wr_pct"] is not None else "-"
                click.echo(
                    f"  {name:<25} ep={stats['episodes']:>4} "
                    f"resolved={stats['resolved']:>4} "
                    f"W/L={stats['wins']}/{stats['losses']:<4} WR={wr:>5}  "
                    f"PnL=${stats['pnl']:+10,.2f}"
                )
    finally:
        conn.close()


@cli.command(name="monte-carlo")
@click.option("--horizon-days", type=int, default=7, show_default=True,
              help="Future-window length in days.")
@click.option("--samples", type=int, default=10_000, show_default=True,
              help="Number of bootstrap simulations.")
@click.option("--per-whale", "per_whale", is_flag=True, default=False,
              help="Resample each whale independently (preserves correlation).")
@click.option("--seed", type=int, default=None,
              help="Random seed for reproducible runs.")
@click.pass_obj
def monte_carlo_cmd(
    settings: Settings,
    horizon_days: int,
    samples: int,
    per_whale: bool,
    seed: int | None,
) -> None:
    """Bootstrap-resample historical paper PnL to project future-period distribution."""
    conn = connect(settings.db_path)
    try:
        run_migrations(conn)
        if per_whale:
            result = simulate_per_whale(
                conn, horizon_days=horizon_days, samples=samples, seed=seed
            )
        else:
            result = simulate_aggregated(
                conn, horizon_days=horizon_days, samples=samples, seed=seed
            )
        click.echo(
            f"=== Monte Carlo ({result.mode}) ===\n"
            f"  samples         : {result.samples:,}\n"
            f"  horizon         : {result.horizon_days} days\n"
            f"  trades/sample   : {result.trades_per_sample}\n"
            f"  historical n    : {result.historical_trades}\n"
            f"  hist mean/trade : ${result.historical_mean:+.2f}\n"
            f"  hist stdev      : ${result.historical_stdev:.2f}\n"
            f"\n"
            f"Future PnL distribution over {result.horizon_days}d "
            f"({result.trades_per_sample} trades/sample):\n"
            f"   5th percentile : ${result.p5:+10,.2f}\n"
            f"  25th percentile : ${result.p25:+10,.2f}\n"
            f"  50th (median)   : ${result.median_pnl:+10,.2f}\n"
            f"  75th percentile : ${result.p75:+10,.2f}\n"
            f"  95th percentile : ${result.p95:+10,.2f}\n"
            f"  mean            : ${result.mean_pnl:+10,.2f}\n"
            f"\n"
            f"  P(positive PnL)            : {result.prob_positive * 100:.1f}%\n"
            f"  median max-drawdown        : ${result.median_drawdown:,.2f}\n"
            f"  95th-pct (bad-case) DD     : ${result.p95_drawdown:,.2f}\n"
        )
        if per_whale and result.per_whale_breakdown:
            click.echo("Per-whale expected contribution over horizon:")
            ranked = sorted(
                result.per_whale_breakdown.items(), key=lambda kv: -kv[1]
            )
            for wallet, expected in ranked[:20]:
                label = conn.execute(
                    "SELECT label FROM whale_watchlist WHERE wallet = ?",
                    (wallet,),
                ).fetchone()
                name = (label["label"] if label and label["label"] else wallet[:14])[:25]
                click.echo(f"  {name:<25} ${expected:+10,.2f}")
    finally:
        conn.close()


# ----- Pulse (overall status) -----


@cli.command()
@click.pass_obj
def pulse(settings: Settings) -> None:
    """At-a-glance status: whales tracked, snapshots collected, paper P&L."""
    conn = connect(settings.db_path)
    try:
        run_migrations(conn)
        click.echo("=== polywhale pulse ===")

        def _scalar(sql: str) -> int:
            row = conn.execute(sql).fetchone()
            return int(row[0]) if row and row[0] is not None else 0

        watchlist_active = _scalar("SELECT COUNT(*) FROM whale_watchlist WHERE active = 1")
        whales_with_positions = _scalar("SELECT COUNT(DISTINCT wallet) FROM whale_positions")
        click.echo(f"  watchlist (active)   : {watchlist_active}")
        click.echo(f"  with open positions  : {whales_with_positions}")
        click.echo(f"  whale snapshots      : {_scalar('SELECT COUNT(*) FROM whale_positions')}")
        click.echo(f"  whale signals        : {_scalar('SELECT COUNT(*) FROM whale_signals')}")
        click.echo(f"  book snapshots       : {_scalar('SELECT COUNT(*) FROM polymarket_books')}")
        click.echo(f"  combo arbs detected  : {_scalar('SELECT COUNT(*) FROM combo_arbs')}")
        paper_total = _scalar("SELECT COUNT(*) FROM poly_paper_bets")
        paper_settled = _scalar("SELECT COUNT(*) FROM poly_paper_bets WHERE settled_at IS NOT NULL")
        paper_frozen = _scalar(
            "SELECT COUNT(*) FROM poly_paper_bets "
            "WHERE frozen_at IS NOT NULL AND settled_at IS NULL"
        )
        paper_pnl = conn.execute(
            "SELECT COALESCE(SUM(pnl_usd), 0) FROM poly_paper_bets"
        ).fetchone()[0]
        click.echo(
            f"  paper bets total     : {paper_total}  "
            f"(settled: {paper_settled}, frozen: {paper_frozen})"
        )
        click.echo(f"  paper P&L            : ${paper_pnl:+.2f}")
        ct = copy_trade_stats(conn)
        click.echo("  --- whale copy ---")
        click.echo(
            f"  open copy positions  : {ct['open_positions']}  "
            f"(${ct['capital_deployed']:.2f} deployed of ${settings.paper_bankroll_usd:.0f})"
        )
        click.echo(
            f"  closed copy trades   : {ct['closed_positions']}  "
            f"(W/L: {ct['wins']}/{ct['losses']}, "
            f"WR: {ct['win_rate_pct']}%)" if ct["win_rate_pct"] is not None
            else f"  closed copy trades   : {ct['closed_positions']}"
        )
        click.echo(f"  realized copy P&L    : ${ct['realized_pnl']:+.2f}")
    finally:
        conn.close()


def main() -> int:
    try:
        cli(standalone_mode=False)
    except click.ClickException as exc:
        exc.show()
        return exc.exit_code
    except click.exceptions.Abort:
        return 130
    except Exception:
        logger.exception("polywhale failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
