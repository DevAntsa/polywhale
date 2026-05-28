"""Polymarket Gamma + CLOB API clients (read-only).

Gamma   = market metadata, prices, list of markets
CLOB    = order book snapshots, used for depth measurement

No auth required for any of the endpoints used here.
"""

import json
import logging
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
DATA_BASE = "https://data-api.polymarket.com"
LB_BASE = "https://lb-api.polymarket.com"


@dataclass(frozen=True)
class PolyMarket:
    slug: str
    question: str
    outcomes: list[str]
    outcome_prices: list[float]
    volume_24h: float
    volume_total: float
    category: str | None
    closed: bool
    end_date: str | None
    token_ids: list[str] = field(default_factory=list)
    best_bid: float | None = None
    best_ask: float | None = None
    spread: float | None = None
    liquidity: float | None = None
    neg_risk: bool = False
    accepting_orders: bool = False

    @property
    def price_sum(self) -> float:
        return sum(self.outcome_prices) if self.outcome_prices else 0.0


@dataclass(frozen=True)
class BookLevel:
    price: float
    size: float


@dataclass(frozen=True)
class PolyBook:
    market: str
    asset_id: str
    server_ts: int
    bids: list[BookLevel]
    asks: list[BookLevel]
    last_trade_price: float | None
    tick_size: float | None
    neg_risk: bool

    @property
    def best_bid(self) -> float | None:
        # Bids are sorted ascending by price; best is the last (highest) bid.
        return self.bids[-1].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        # Asks are sorted descending; best (lowest) ask is the last entry.
        return self.asks[-1].price if self.asks else None

    @property
    def spread(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    def depth_within(self, *, side: str, pct: float = 0.05) -> float:
        """Sum size of levels within `pct` of the best price on `side` ('bid' or 'ask')."""
        if side == "bid":
            top = self.best_bid
            if top is None:
                return 0.0
            cutoff = top * (1 - pct)
            return sum(b.size for b in self.bids if b.price >= cutoff)
        if side == "ask":
            top = self.best_ask
            if top is None:
                return 0.0
            cutoff = top * (1 + pct)
            return sum(a.size for a in self.asks if a.price <= cutoff)
        raise ValueError(f"side must be 'bid' or 'ask', got {side!r}")


@dataclass(frozen=True)
class LeaderboardRow:
    wallet: str
    pseudonym: str | None
    name: str | None
    amount: float


@dataclass(frozen=True)
class WhalePosition:
    wallet: str
    asset_id: str
    condition_id: str | None
    market_slug: str | None
    event_slug: str | None
    title: str | None
    outcome: str | None
    size: float
    avg_price: float | None
    current_price: float | None
    current_value: float | None
    initial_value: float | None
    cash_pnl: float | None
    realized_pnl: float | None
    percent_pnl: float | None
    end_date: str | None
    neg_risk: bool


class PolymarketClient:
    """Thin httpx wrapper around the Polymarket Gamma, CLOB, and data REST APIs."""

    def __init__(
        self,
        gamma_url: str = GAMMA_BASE,
        clob_url: str = CLOB_BASE,
        data_url: str = DATA_BASE,
        lb_url: str = LB_BASE,
        timeout: float = 10.0,
    ) -> None:
        self._gamma = httpx.Client(base_url=gamma_url, timeout=timeout)
        self._clob = httpx.Client(base_url=clob_url, timeout=timeout)
        self._data = httpx.Client(base_url=data_url, timeout=timeout)
        self._lb = httpx.Client(base_url=lb_url, timeout=timeout)

    def close(self) -> None:
        self._gamma.close()
        self._clob.close()
        self._data.close()
        self._lb.close()

    def __enter__(self) -> "PolymarketClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def list_markets(
        self,
        *,
        closed: bool = False,
        limit: int = 100,
        order: str = "volume24hr",
        ascending: bool = False,
    ) -> list[PolyMarket]:
        params = {
            "closed": "true" if closed else "false",
            "limit": str(limit),
            "order": order,
            "ascending": "true" if ascending else "false",
        }
        resp = self._gamma.get("/markets", params=params)
        resp.raise_for_status()
        return [parse_market(raw) for raw in resp.json()]

    def get_event(self, slug: str) -> dict | None:
        """Look up a Polymarket event (container for many markets) by slug.

        Events are the right unit for combinatorial detection: they contain all
        the mutually-exclusive markets in a neg-risk group.
        """
        try:
            resp = self._gamma.get("/events", params={"slug": slug})
        except httpx.HTTPError as exc:
            logger.warning("gamma /events failed for slug=%r: %s", slug, exc)
            return None
        if resp.status_code >= 400:
            logger.warning("gamma /events %d for slug=%r", resp.status_code, slug)
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        if not data:
            return None
        return data[0]

    def get_market(self, slug: str) -> PolyMarket | None:
        """Look up a single market by slug. Returns None if not found or on transient errors."""
        try:
            resp = self._gamma.get("/markets", params={"slug": slug})
        except httpx.HTTPError as exc:
            logger.warning("gamma /markets request failed for slug=%r: %s", slug, exc)
            return None
        if resp.status_code >= 500:
            logger.warning("gamma /markets %d for slug=%r", resp.status_code, slug)
            return None
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        try:
            data = resp.json()
        except ValueError:
            return None
        if not data:
            return None
        return parse_market(data[0])

    def get_book(self, token_id: str) -> PolyBook:
        resp = self._clob.get("/book", params={"token_id": token_id})
        resp.raise_for_status()
        return parse_book(resp.json())

    def get_leaderboard(
        self,
        metric: str = "profit",
        *,
        window: str | None = "30d",
    ) -> list[LeaderboardRow]:
        """Polymarket leaderboard. metric in {profit, volume}. window in {None, '1d', '30d'}."""
        params: dict[str, str] = {}
        if window:
            params["window"] = window
        resp = self._lb.get(f"/{metric}", params=params)
        resp.raise_for_status()
        return [parse_leaderboard_row(r) for r in resp.json()]

    def get_whale_positions(
        self,
        wallet: str,
        *,
        size_threshold: float = 10.0,
    ) -> list[WhalePosition]:
        """Pull current open positions for a wallet via the public data API.

        `size_threshold` filters out dust positions. Returns positions in
        descending order of current value (the API sorts this way).
        """
        resp = self._data.get(
            "/positions",
            params={"user": wallet, "sizeThreshold": str(size_threshold)},
        )
        resp.raise_for_status()
        data = resp.json()
        return [parse_position(raw) for raw in data]

    def get_activity(self, wallet: str, *, limit: int = 500) -> list[dict]:
        """Pull a wallet's recent activity (trades, redemptions, rebates).

        Returns raw dicts with keys including: type, timestamp, conditionId,
        price, side, outcome, size, slug, title. Caps at API max of ~500.
        """
        resp = self._data.get(
            "/activity", params={"user": wallet, "limit": str(limit)}
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []


def parse_market(raw: dict) -> PolyMarket:
    outcomes_raw = raw.get("outcomes") or "[]"
    prices_raw = raw.get("outcomePrices") or "[]"
    tokens_raw = raw.get("clobTokenIds") or "[]"
    outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else list(outcomes_raw)
    prices_list = json.loads(prices_raw) if isinstance(prices_raw, str) else list(prices_raw)
    tokens_list = json.loads(tokens_raw) if isinstance(tokens_raw, str) else list(tokens_raw)
    prices = [float(p) for p in prices_list]
    return PolyMarket(
        slug=raw["slug"],
        question=raw["question"],
        outcomes=outcomes,
        outcome_prices=prices,
        volume_24h=float(raw.get("volume24hr") or 0),
        volume_total=float(raw.get("volume") or 0),
        category=raw.get("category"),
        closed=bool(raw.get("closed")),
        end_date=raw.get("endDate"),
        token_ids=[str(t) for t in tokens_list],
        best_bid=_opt_float(raw.get("bestBid")),
        best_ask=_opt_float(raw.get("bestAsk")),
        spread=_opt_float(raw.get("spread")),
        liquidity=_opt_float(raw.get("liquidityNum") or raw.get("liquidity")),
        neg_risk=bool(raw.get("negRisk")),
        accepting_orders=bool(raw.get("acceptingOrders")),
    )


def parse_leaderboard_row(raw: dict) -> LeaderboardRow:
    return LeaderboardRow(
        wallet=str(raw.get("proxyWallet") or "").lower(),
        pseudonym=raw.get("pseudonym"),
        name=raw.get("name"),
        amount=float(raw.get("amount") or 0),
    )


def parse_position(raw: dict) -> WhalePosition:
    return WhalePosition(
        wallet=str(raw.get("proxyWallet") or ""),
        asset_id=str(raw.get("asset") or ""),
        condition_id=raw.get("conditionId"),
        market_slug=raw.get("slug"),
        event_slug=raw.get("eventSlug"),
        title=raw.get("title"),
        outcome=raw.get("outcome"),
        size=float(raw.get("size") or 0),
        avg_price=_opt_float(raw.get("avgPrice")),
        current_price=_opt_float(raw.get("curPrice")),
        current_value=_opt_float(raw.get("currentValue")),
        initial_value=_opt_float(raw.get("initialValue")),
        cash_pnl=_opt_float(raw.get("cashPnl")),
        realized_pnl=_opt_float(raw.get("realizedPnl")),
        percent_pnl=_opt_float(raw.get("percentPnl")),
        end_date=raw.get("endDate"),
        neg_risk=bool(raw.get("negativeRisk")),
    )


def parse_book(raw: dict) -> PolyBook:
    return PolyBook(
        market=str(raw.get("market") or ""),
        asset_id=str(raw.get("asset_id") or ""),
        server_ts=int(raw.get("timestamp") or 0),
        bids=[BookLevel(float(b["price"]), float(b["size"])) for b in raw.get("bids") or []],
        asks=[BookLevel(float(a["price"]), float(a["size"])) for a in raw.get("asks") or []],
        last_trade_price=_opt_float(raw.get("last_trade_price")),
        tick_size=_opt_float(raw.get("tick_size")),
        neg_risk=bool(raw.get("neg_risk")),
    )


def _opt_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
