from pathlib import Path

from polywhale.db import connect, run_migrations
from polywhale.poly_arb import ComboArb, ComboLeg, detect_combo_arb, persist_combo_arb
from polywhale.polymarket import BookLevel, PolyBook


class _StubClient:
    def __init__(self, event: dict, books: dict[str, PolyBook]) -> None:
        self._event = event
        self._books = books

    def get_event(self, slug: str) -> dict | None:
        if self._event.get("slug") == slug:
            return self._event
        return None

    def get_book(self, token_id: str) -> PolyBook:
        return self._books[token_id]


def _book(ask_price: float, ask_size: float = 1000.0) -> PolyBook:
    return PolyBook(
        market="m",
        asset_id="a",
        server_ts=0,
        bids=[BookLevel(ask_price - 0.01, 100)],
        asks=[BookLevel(ask_price + 0.10, 100), BookLevel(ask_price, ask_size)],
        last_trade_price=ask_price,
        tick_size=0.001,
        neg_risk=True,
    )


def _market(slug: str, token_id: str, *, neg_risk: bool = True) -> dict:
    return {
        "slug": slug,
        "question": f"Will {slug} win?",
        "groupItemTitle": slug.upper(),
        "negRisk": neg_risk,
        "clobTokenIds": f'["{token_id}", "tok-no-{token_id}"]',
    }


def test_detect_combo_arb_returns_none_when_sum_above_threshold() -> None:
    event = {
        "slug": "test-event",
        "title": "Test",
        "markets": [
            _market("a", "tok_a"),
            _market("b", "tok_b"),
            _market("c", "tok_c"),
        ],
    }
    books = {
        "tok_a": _book(0.35),
        "tok_b": _book(0.35),
        "tok_c": _book(0.35),
    }
    client = _StubClient(event, books)
    arb = detect_combo_arb(client, "test-event", fee_pct=0.75, min_edge_pct=0.5)
    # Sum = 1.05 > 1, no arb
    assert arb is None


def test_detect_combo_arb_returns_arb_when_underpriced() -> None:
    event = {
        "slug": "test-event",
        "title": "Test",
        "markets": [
            _market("a", "tok_a"),
            _market("b", "tok_b"),
            _market("c", "tok_c"),
        ],
    }
    books = {
        "tok_a": _book(0.30),
        "tok_b": _book(0.30),
        "tok_c": _book(0.30),
    }
    client = _StubClient(event, books)
    arb = detect_combo_arb(client, "test-event", fee_pct=0.75, min_edge_pct=0.5)
    # Sum = 0.90; raw edge 10%; after 0.75% fee -> 9.25% edge
    assert arb is not None
    assert abs(arb.sum_best_ask - 0.90) < 1e-6
    assert 9.0 < arb.edge_pct < 9.5
    assert arb.outcomes_count == 3


def test_detect_combo_arb_skips_non_negrisk_markets() -> None:
    event = {
        "slug": "test-event",
        "title": "Mixed",
        "markets": [
            _market("a", "tok_a"),
            _market("season-prop", "tok_prop", neg_risk=False),
            _market("b", "tok_b"),
        ],
    }
    books = {
        "tok_a": _book(0.30),
        "tok_b": _book(0.30),
        "tok_prop": _book(0.50),  # not neg-risk, should be skipped
    }
    client = _StubClient(event, books)
    arb = detect_combo_arb(client, "test-event", fee_pct=0.75, min_edge_pct=0.0)
    assert arb is not None
    assert arb.outcomes_count == 2  # prop skipped
    assert abs(arb.sum_best_ask - 0.60) < 1e-6


def test_persist_combo_arb_writes_row(tmp_path: Path) -> None:
    db = tmp_path / "t.sqlite"
    conn = connect(db)
    try:
        run_migrations(conn)
        arb = ComboArb(
            event_slug="test-event",
            event_title="Test",
            captured_at=12345,
            outcomes_count=3,
            sum_best_ask=0.90,
            edge_pct=9.25,
            legs=[
                ComboLeg("a", "Will A win?", "A", "tok_a", 0.30, 1000.0),
                ComboLeg("b", "Will B win?", "B", "tok_b", 0.30, 1000.0),
            ],
        )
        arb_id = persist_combo_arb(conn, arb)
        assert arb_id > 0
        row = conn.execute("SELECT * FROM combo_arbs WHERE arb_id = ?", (arb_id,)).fetchone()
        assert row["event_slug"] == "test-event"
        assert row["outcomes_count"] == 3
        assert abs(row["edge_pct"] - 9.25) < 1e-6
    finally:
        conn.close()


def test_detect_returns_none_when_event_missing() -> None:
    client = _StubClient({"slug": "other", "markets": []}, {})
    assert detect_combo_arb(client, "missing-event") is None
