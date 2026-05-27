"""Command-line entry point for polywhale.

All commands target Polymarket. Designed to be wired into systemd timers
for unattended operation on a small VPS (see deploy/).
"""

import logging
import sys

import click

from polywhale.config import Settings
from polywhale.db import connect, run_migrations
from polywhale.logging_setup import configure as configure_logging
from polywhale.poly_arb import detect_combo_arb, inspect_event, persist_combo_arb
from polywhale.poly_paper import (
    paper_pnl_summary,
    record_combo_arb_legs,
    record_single_leg,
    settle_paper_bets,
)
from polywhale.poly_watch import WatchTarget, watch_loop
from polywhale.polymarket import PolymarketClient
from polywhale.whale_alerter import send_signal_alerts
from polywhale.whale_classify import fetch_and_classify, persist_profiles, top_arb_ops, top_sharps
from polywhale.whale_diff import detect_for_wallets, persist_signals
from polywhale.whale_watch import snapshot_wallet, watch_wallets

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
    help="Use the curated default Polymarket watchlist.",
)
@click.option("--interval", type=int, default=60, show_default=True)
@click.option("--iterations", type=int, default=1, show_default=True, help="0 = loop forever.")
@click.pass_obj
def poly_watch(
    settings: Settings,
    slugs: tuple[str, ...],
    use_default: bool,
    interval: int,
    iterations: int,
) -> None:
    """Poll Polymarket order books for given slugs; persist depth snapshots."""
    from polywhale.watchlist import fetch_default_market_slugs

    conn = connect(settings.db_path)
    try:
        run_migrations(conn)
        with PolymarketClient() as client:
            resolved: tuple[str, ...] = slugs
            if use_default:
                fetched = fetch_default_market_slugs(client, top_n=10)
                resolved = tuple(list(slugs) + fetched)
            if not resolved:
                click.echo("No slugs given. Use --slug or --default.")
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
@click.pass_obj
def poly_paper_settle(settings: Settings) -> None:
    """Check Gamma for resolution of open paper bets; mark P&L on settled ones."""
    conn = connect(settings.db_path)
    try:
        run_migrations(conn)
        with PolymarketClient() as client:
            summary = settle_paper_bets(conn, client)
        click.echo(
            f"Checked {summary['checked']} market(s). "
            f"Settled {summary['settled']}. Still open: {summary['still_open']}."
        )
    finally:
        conn.close()


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
    from polywhale.watchlist import DEFAULT_WHALE_WALLETS

    resolved: tuple[str, ...] = wallets
    if use_default and not wallets:
        resolved = tuple(DEFAULT_WHALE_WALLETS)
    elif use_default and wallets:
        resolved = tuple(list(wallets) + DEFAULT_WHALE_WALLETS)
    if not resolved:
        click.echo("No wallets given. Use --wallet or --default.")
        return
    conn = connect(settings.db_path)
    try:
        run_migrations(conn)
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
    from polywhale.watchlist import DEFAULT_WHALE_WALLETS

    targets: tuple[str, ...] = wallets
    if use_default:
        targets = tuple(list(wallets) + DEFAULT_WHALE_WALLETS)
    if not targets:
        targets = tuple(DEFAULT_WHALE_WALLETS)
    if not targets:
        click.echo("No wallets.")
        return
    conn = connect(settings.db_path)
    try:
        run_migrations(conn)
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

        whales = _scalar("SELECT COUNT(DISTINCT wallet) FROM whale_positions")
        click.echo(f"  whales tracked       : {whales}")
        click.echo(f"  whale snapshots      : {_scalar('SELECT COUNT(*) FROM whale_positions')}")
        click.echo(f"  whale signals        : {_scalar('SELECT COUNT(*) FROM whale_signals')}")
        click.echo(f"  book snapshots       : {_scalar('SELECT COUNT(*) FROM polymarket_books')}")
        click.echo(f"  combo arbs detected  : {_scalar('SELECT COUNT(*) FROM combo_arbs')}")
        paper_total = _scalar("SELECT COUNT(*) FROM poly_paper_bets")
        paper_settled = _scalar("SELECT COUNT(*) FROM poly_paper_bets WHERE settled_at IS NOT NULL")
        paper_pnl = conn.execute(
            "SELECT COALESCE(SUM(pnl_usd), 0) FROM poly_paper_bets"
        ).fetchone()[0]
        click.echo(f"  paper bets total     : {paper_total}  (settled: {paper_settled})")
        click.echo(f"  paper P&L            : ${paper_pnl:+.2f}")
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
