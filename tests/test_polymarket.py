import httpx

from polywhale.polymarket import (
    BookLevel,
    PolyBook,
    PolyMarket,
    PolymarketClient,
    parse_book,
    parse_market,
)


def test_parse_market_extracts_fields() -> None:
    raw = {
        "slug": "btc-150k",
        "question": "Will Bitcoin hit $150k by June 30, 2026?",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.0135", "0.9865"]',
        "volume24hr": 5821653.0,
        "volume": 15734008.0,
        "category": "Crypto",
        "closed": False,
        "endDate": "2026-06-30T00:00:00Z",
    }
    m = parse_market(raw)
    assert m.slug == "btc-150k"
    assert m.outcomes == ["Yes", "No"]
    assert m.outcome_prices == [0.0135, 0.9865]
    assert abs(m.price_sum - 1.0) < 1e-9
    assert m.volume_24h == 5821653.0
    assert m.category == "Crypto"
    assert m.closed is False


def test_parse_market_handles_already_decoded_lists() -> None:
    raw = {
        "slug": "x",
        "question": "Q",
        "outcomes": ["Yes", "No"],
        "outcomePrices": [0.5, 0.5],
        "volume24hr": 0,
        "volume": 0,
    }
    m = parse_market(raw)
    assert m.outcomes == ["Yes", "No"]
    assert m.outcome_prices == [0.5, 0.5]


def test_price_sum_property() -> None:
    m = PolyMarket(
        slug="x",
        question="Q",
        outcomes=["A", "B", "C"],
        outcome_prices=[0.45, 0.29, 0.27],
        volume_24h=0,
        volume_total=0,
        category=None,
        closed=False,
        end_date=None,
    )
    assert abs(m.price_sum - 1.01) < 1e-9


def test_parse_book_basic() -> None:
    raw = {
        "market": "0xabc",
        "asset_id": "12345",
        "timestamp": "1779000000",
        "bids": [
            {"price": "0.40", "size": "100"},
            {"price": "0.45", "size": "500"},
        ],
        "asks": [
            {"price": "0.60", "size": "200"},
            {"price": "0.48", "size": "400"},
        ],
        "last_trade_price": "0.46",
        "tick_size": "0.001",
        "neg_risk": False,
    }
    b = parse_book(raw)
    assert b.market == "0xabc"
    assert b.asset_id == "12345"
    assert b.server_ts == 1779000000
    # Bids ascending; best = last = 0.45
    assert b.best_bid == 0.45
    # Asks descending; best (lowest) = last = 0.48
    assert b.best_ask == 0.48
    assert b.spread is not None
    assert abs(b.spread - 0.03) < 1e-9


def test_book_depth_within_pct() -> None:
    book = PolyBook(
        market="m",
        asset_id="a",
        server_ts=0,
        bids=[
            BookLevel(0.40, 100),
            BookLevel(0.45, 500),
            BookLevel(0.46, 200),
        ],
        asks=[
            BookLevel(0.60, 100),
            BookLevel(0.55, 300),
            BookLevel(0.48, 400),
        ],
        last_trade_price=0.46,
        tick_size=0.001,
        neg_risk=False,
    )
    # best bid = 0.46; 5% below = 0.437. Bids >= 0.437: 0.45 + 0.46 = 700
    assert book.depth_within(side="bid", pct=0.05) == 700
    # best ask = 0.48; 5% above = 0.504. Asks <= 0.504: only 0.48 = 400
    assert book.depth_within(side="ask", pct=0.05) == 400


def test_list_markets_hits_expected_endpoint(monkeypatch) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json=[
                {
                    "slug": "test",
                    "question": "Test?",
                    "outcomes": '["Yes", "No"]',
                    "outcomePrices": '["0.6", "0.4"]',
                    "volume24hr": 1000,
                    "volume": 5000,
                    "closed": False,
                }
            ],
        )

    client = PolymarketClient()
    client.close()
    transport = httpx.MockTransport(handler)
    client._gamma = httpx.Client(base_url="https://gamma-api.polymarket.com", transport=transport)
    client._clob = httpx.Client(base_url="https://clob.polymarket.com", transport=transport)
    try:
        markets = client.list_markets(closed=False, limit=10)
    finally:
        client.close()
    assert "/markets" in captured["url"]
    assert "closed=false" in captured["url"]
    assert "limit=10" in captured["url"]
    assert len(markets) == 1
    assert markets[0].slug == "test"


def test_get_book_hits_clob_endpoint() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "market": "0xabc",
                "asset_id": "12345",
                "timestamp": "1779000000",
                "bids": [{"price": "0.45", "size": "100"}],
                "asks": [{"price": "0.55", "size": "100"}],
                "last_trade_price": "0.50",
                "tick_size": "0.001",
                "neg_risk": False,
            },
        )

    client = PolymarketClient()
    client.close()
    transport = httpx.MockTransport(handler)
    client._gamma = httpx.Client(base_url="https://gamma-api.polymarket.com", transport=transport)
    client._clob = httpx.Client(base_url="https://clob.polymarket.com", transport=transport)
    try:
        book = client.get_book("12345")
    finally:
        client.close()
    assert "/book" in captured["url"]
    assert "token_id=12345" in captured["url"]
    assert book.asset_id == "12345"
    assert book.best_bid == 0.45
    assert book.best_ask == 0.55
