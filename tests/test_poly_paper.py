from pathlib import Path

from polywhale.db import connect, run_migrations
from polywhale.poly_arb import ComboArb, ComboLeg
from polywhale.poly_paper import (
    paper_pnl_summary,
    record_combo_arb_legs,
    record_single_leg,
    settle_paper_bets,
)
from polywhale.polymarket import PolyMarket


def _arb(sum_ask: float, n_legs: int = 3) -> ComboArb:
    legs = []
    per_ask = sum_ask / n_legs
    for i in range(n_legs):
        legs.append(
            ComboLeg(
                market_slug=f"m{i}",
                question=f"Will outcome {i} win?",
                outcome_title=f"Outcome {i}",
                token_id=f"tok{i}",
                best_ask=per_ask,
                ask_depth=1000.0,
            )
        )
    return ComboArb(
        event_slug="ev",
        event_title="Test Event",
        captured_at=12345,
        outcomes_count=n_legs,
        sum_best_ask=sum_ask,
        edge_pct=(1 - sum_ask) * 100,
        legs=legs,
    )


def test_record_combo_arb_legs_sizing(tmp_path: Path) -> None:
    db = tmp_path / "t.sqlite"
    conn = connect(db)
    try:
        run_migrations(conn)
        arb = _arb(sum_ask=0.90, n_legs=3)
        summary = record_combo_arb_legs(conn, arb, total_stake_usd=90.0)
        assert summary.placed == 3
        assert abs(summary.total_cost_usd - 90.0) < 0.01
        # Each leg ask=0.3; shares = 90/0.9 = 100; cost per leg = 30
        rows = list(conn.execute("SELECT * FROM poly_paper_bets ORDER BY bet_id"))
        assert len(rows) == 3
        for r in rows:
            assert abs(r["size_shares"] - 100.0) < 1e-6
            assert abs(r["cost_usd"] - 30.0) < 1e-6
            assert r["side"] == "YES"
            assert r["source"] == "combo_arb"
    finally:
        conn.close()


def test_record_single_leg(tmp_path: Path) -> None:
    db = tmp_path / "t.sqlite"
    conn = connect(db)
    try:
        run_migrations(conn)
        bet_id = record_single_leg(
            conn,
            market_slug="some-market",
            event_slug=None,
            token_id="tok1",
            side="NO",
            outcome_title="Loss",
            entry_price=0.60,
            size_shares=50.0,
            source="manual",
            notes="test note",
        )
        assert bet_id > 0
        row = conn.execute("SELECT * FROM poly_paper_bets WHERE bet_id = ?", (bet_id,)).fetchone()
        assert row["side"] == "NO"
        assert row["entry_price"] == 0.60
        assert row["size_shares"] == 50.0
        assert abs(row["cost_usd"] - 30.0) < 1e-6
        assert row["notes"] == "test note"
    finally:
        conn.close()


class _StubClient:
    def __init__(self, markets: dict[str, PolyMarket]) -> None:
        self._markets = markets

    def get_market(self, slug: str) -> PolyMarket | None:
        return self._markets.get(slug)


def _resolved_market(slug: str, *, yes_wins: bool) -> PolyMarket:
    return PolyMarket(
        slug=slug,
        question="?",
        outcomes=["Yes", "No"],
        outcome_prices=[1.0, 0.0] if yes_wins else [0.0, 1.0],
        volume_24h=0,
        volume_total=0,
        category=None,
        closed=True,
        end_date=None,
    )


def _open_market(slug: str) -> PolyMarket:
    return PolyMarket(
        slug=slug,
        question="?",
        outcomes=["Yes", "No"],
        outcome_prices=[0.5, 0.5],
        volume_24h=0,
        volume_total=0,
        category=None,
        closed=False,
        end_date=None,
    )


def test_settle_paper_bets_yes_winner(tmp_path: Path) -> None:
    db = tmp_path / "t.sqlite"
    conn = connect(db)
    try:
        run_migrations(conn)
        record_single_leg(
            conn,
            market_slug="m1",
            event_slug=None,
            token_id="tok1",
            side="YES",
            outcome_title="A",
            entry_price=0.30,
            size_shares=100.0,
        )
        client = _StubClient({"m1": _resolved_market("m1", yes_wins=True)})
        summary = settle_paper_bets(conn, client)
        assert summary == {"checked": 1, "settled": 1, "still_open": 0}
        row = conn.execute("SELECT * FROM poly_paper_bets WHERE bet_id = 1").fetchone()
        assert row["resolved_outcome"] == "won"
        # cost was 30, paid out 100 -> pnl = 70
        assert abs(row["pnl_usd"] - 70.0) < 1e-4
    finally:
        conn.close()


def test_settle_paper_bets_yes_loser(tmp_path: Path) -> None:
    db = tmp_path / "t.sqlite"
    conn = connect(db)
    try:
        run_migrations(conn)
        record_single_leg(
            conn,
            market_slug="m1",
            event_slug=None,
            token_id="tok1",
            side="YES",
            outcome_title="A",
            entry_price=0.40,
            size_shares=100.0,
        )
        client = _StubClient({"m1": _resolved_market("m1", yes_wins=False)})
        settle_paper_bets(conn, client)
        row = conn.execute("SELECT * FROM poly_paper_bets WHERE bet_id = 1").fetchone()
        assert row["resolved_outcome"] == "lost"
        # cost was 40, payout 0 -> pnl = -40
        assert abs(row["pnl_usd"] + 40.0) < 1e-4
    finally:
        conn.close()


def test_settle_paper_bets_no_winner(tmp_path: Path) -> None:
    db = tmp_path / "t.sqlite"
    conn = connect(db)
    try:
        run_migrations(conn)
        record_single_leg(
            conn,
            market_slug="m1",
            event_slug=None,
            token_id="tok2",
            side="NO",
            outcome_title="B",
            entry_price=0.55,
            size_shares=100.0,
        )
        # Yes lost so No won
        client = _StubClient({"m1": _resolved_market("m1", yes_wins=False)})
        settle_paper_bets(conn, client)
        row = conn.execute("SELECT * FROM poly_paper_bets WHERE bet_id = 1").fetchone()
        assert row["resolved_outcome"] == "won"
        # cost 55, payout 100 -> pnl 45
        assert abs(row["pnl_usd"] - 45.0) < 1e-4
    finally:
        conn.close()


def test_settle_paper_bets_still_open(tmp_path: Path) -> None:
    db = tmp_path / "t.sqlite"
    conn = connect(db)
    try:
        run_migrations(conn)
        record_single_leg(
            conn,
            market_slug="m1",
            event_slug=None,
            token_id="tok1",
            side="YES",
            outcome_title="A",
            entry_price=0.5,
            size_shares=100.0,
        )
        client = _StubClient({"m1": _open_market("m1")})
        summary = settle_paper_bets(conn, client)
        assert summary["settled"] == 0
        assert summary["still_open"] == 1
    finally:
        conn.close()


def test_paper_pnl_summary_groups_by_source(tmp_path: Path) -> None:
    db = tmp_path / "t.sqlite"
    conn = connect(db)
    try:
        run_migrations(conn)
        arb = _arb(0.90, n_legs=2)
        record_combo_arb_legs(conn, arb, total_stake_usd=100.0)
        record_single_leg(
            conn,
            market_slug="m99",
            event_slug=None,
            token_id="tok99",
            side="YES",
            outcome_title="X",
            entry_price=0.20,
            size_shares=50.0,
            source="manual",
        )
        summary = paper_pnl_summary(conn)
        assert "combo_arb" in summary
        assert "manual" in summary
        assert summary["combo_arb"]["bets"] == 2
        assert summary["manual"]["bets"] == 1
    finally:
        conn.close()
