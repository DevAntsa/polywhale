"""Default watchlists for Polymarket measurement.

For markets: we resolve the default list *dynamically* via the Gamma API
(top N by 24h volume). This keeps the watchlist self-healing as markets
resolve and new ones open. Run `polywhale poly-watch --default` to use it.

For wallets: the list is static because wallet addresses are stable
across weeks (unlike market slugs).
"""

import sqlite3

from polywhale.polymarket import PolymarketClient


def fetch_default_market_slugs(client: PolymarketClient, *, top_n: int = 10) -> list[str]:
    """Return slugs of top N active Polymarket markets by 24h volume.

    Self-healing: stale markets (resolved, low-volume) get filtered as new
    ones rise to the top. Good default for an ongoing measurement loop.
    """
    markets = client.list_markets(
        closed=False, limit=max(top_n * 2, 30), order="volume24hr", ascending=False
    )
    return [m.slug for m in markets[:top_n]]


def fetch_open_position_market_slugs(
    conn: sqlite3.Connection, *, max_markets: int = 200
) -> list[str]:
    """Return distinct market slugs of markets where we currently hold open paper bets.

    Used by `poly-watch --from-positions` so the book-snapshot timer covers the
    exact markets we have skin in — enables intraday price tracking for
    profit-take and momentum-exit rules.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT market_slug
        FROM poly_paper_bets
        WHERE source = 'whale_copy'
          AND settled_at IS NULL
          AND market_slug IS NOT NULL
        ORDER BY market_slug
        LIMIT ?
        """,
        (max_markets,),
    ).fetchall()
    return [r[0] for r in rows]


# Margin-ranked sharps from our own classifier (polywhale poly-whales).
# Selection: margin >= 3% on >= $1M 30-day volume, 2026-05-27.
MARGIN_RANKED_SHARPS: list[str] = [
    "0x9f2fe025f84839ca81dd8e0338892605702d2ca8",  # surfandturf  13.9% margin
    "0xa5ea13a81d2b7e8e424b182bdc1db08e756bd96a",  # bossoskil1   4.9% margin (MLB specialist)
    "0x2c335066fe58fe9237c3d3dc7b275c2a034a0563",  # (unnamed)    6.0% margin
    "0x2a2c53bd278c04da9962fcf96490e17f3dfb9bc1",  # (unnamed)    3.6% margin
    "0xfbf3d501e88815464642d0e913f15379c3eeb218",  # VPenguin     6.0% margin
]

# Polywhaler.com top sharps by win-rate weighted ranking.
# All tagged "Smart Whale" / "Unlikely Insider" by Polywhaler. Filter: WR > 70%, big PnL.
# Stats refreshed 2026-05-27 from screenshots. (DORMANT) = no trades in 14+ days,
# so they won't fire signals; kept in the list in case they reactivate.
POLYWHALER_SHARPS: list[str] = [
    "0x2974bd0059e48f215c391882976e0f1b4c8c9c23",  # 65765757    94.0% wr, $93k PnL, mod risk
    "0xf284ad6d607f777f34bc643cea587c33a886b9f9",  # strike123   92.2% wr, $935k PnL, mod risk
    "0x73e35ce0b7e36ef3ce29ebd12b30b28007383239",  # ID4         97.1% wr, $2.58M PnL, high risk
    "0xeb6789ca6b1425ff908a69a2a5469c38532cd696",  # ExitLiquidty 84.0% wr, $301k PnL  (DORMANT 27d)
    "0x1e3b6822abfb39331b863eb729cdc251f607c850",  # saintQ      93.0% wr, $36k PnL, low risk
    "0xc6587b11a2209e46dfe3928b31c5514a8e33b784",  # Erasmus.    74.5% wr, $496k PnL, high risk
    "0x13414a77a4be48988851c73dfd824d0168e70853",  # PineBluff   84.1% wr, $389k PnL  (DORMANT 14d)
    "0x7f9e2d1df78614564a70becc7fa14aa9a6623a0e",  # nojnn       76.8% wr, $253k PnL, mod risk
    "0x63d43bbb87f85af03b8f2f9e2fad7b54334fa2f1",  # wokerjoesleeper 90% wr, $120k PnL, high risk
    "0x5d0f03cf1243a3e21262d6cf844795afd9fff0ad",  # EB99999     88.0% wr, $440k PnL, mod risk
    "0xde7be6d489bce070a959e0cb813128ae659b5f4b",  # wan123      72.2% wr, $805k PnL, high risk
    "0x8c80d213c0cbad777d06ee3f58f6ca4bc03102c3",  # SecondWindCap 82.4% wr, $3.5M (DORMANT 19d)
]

# Union of both ranking methods - what whale-watch and whale-signals use by default.
DEFAULT_WHALE_WALLETS: list[str] = list(dict.fromkeys(MARGIN_RANKED_SHARPS + POLYWHALER_SHARPS))

# Known arb operators - tracked for comparison and combinatorial arb signal,
# but NOT for copy-trading (margin too thin, taking both sides of mispricings).
KNOWN_ARB_OPERATORS: list[str] = [
    "0xbddf61af533ff524d27154e589d2d7a81510c684",  # Countryside    1.4%
    "0x204f72f35326db932158cba6adff0b9a1da95e14",  # swisstony      1.7%
    "0x2005d16a84ceefa912d4e380cd32e7ff827875ea",  # RN1            1.0%
    "0xfe787d2da716d60e8acff57fb87eb13cd4d10319",  # ferrariChampions2026  1.6%
    "0x5268527977f700f9bf9b6d5cd843859e4e70135d",  # HomeRunHazard  1.1%
]
