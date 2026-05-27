from pathlib import Path

from polywhale.db import connect, run_migrations
from polywhale.poly_watch import WatchTarget, take_snapshot, watch_loop
from polywhale.polymarket import PolyBook


class _StubClient:
    def __init__(self, books: dict[str, PolyBook]) -> None:
        self._books = books
        self.calls = 0

    def get_book(self, token_id: str) -> PolyBook:
        self.calls += 1
        return self._books[token_id]


def _sample_book(token_id: str = "tok1") -> PolyBook:
    from polywhale.polymarket import BookLevel

    return PolyBook(
        market="0xabc",
        asset_id=token_id,
        server_ts=1779000000,
        bids=[
            BookLevel(0.40, 100),
            BookLevel(0.42, 200),
            BookLevel(0.45, 500),
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


def test_take_snapshot_persists_full_row(tmp_path: Path) -> None:
    db = tmp_path / "t.sqlite"
    conn = connect(db)
    try:
        run_migrations(conn)
        book = _sample_book("tok1")
        client = _StubClient({"tok1": book})
        target = WatchTarget(market_slug="nba-champion", token_id="tok1", outcome="OKC")
        snapshot_id = take_snapshot(conn, client, target)
        assert snapshot_id > 0
        row = conn.execute(
            "SELECT * FROM polymarket_books WHERE snapshot_id = ?", (snapshot_id,)
        ).fetchone()
        assert row["market_slug"] == "nba-champion"
        assert row["token_id"] == "tok1"
        assert row["outcome"] == "OKC"
        assert row["best_bid"] == 0.45
        assert row["best_ask"] == 0.48
        assert row["bid_depth_top5pc"] > 0
        assert row["ask_depth_top5pc"] > 0
        assert row["book_json"]  # full JSON stored
    finally:
        conn.close()


def test_watch_loop_runs_fixed_iterations(tmp_path: Path) -> None:
    db = tmp_path / "t.sqlite"
    conn = connect(db)
    try:
        run_migrations(conn)
        book = _sample_book("tok1")
        client = _StubClient({"tok1": book})
        targets = [
            WatchTarget(market_slug="m1", token_id="tok1", outcome="YES"),
        ]
        total = watch_loop(conn, client, targets, interval_s=0, max_iterations=3)
        assert total == 3
        n = conn.execute("SELECT COUNT(*) FROM polymarket_books").fetchone()[0]
        assert n == 3
    finally:
        conn.close()


def test_watch_loop_handles_per_target_errors(tmp_path: Path) -> None:
    db = tmp_path / "t.sqlite"
    conn = connect(db)
    try:
        run_migrations(conn)

        class _FlakyClient:
            def __init__(self) -> None:
                self.calls = 0

            def get_book(self, token_id):
                self.calls += 1
                if token_id == "bad":
                    raise RuntimeError("network blip")
                return _sample_book(token_id)

        client = _FlakyClient()
        targets = [
            WatchTarget(market_slug="m1", token_id="tok1", outcome="YES"),
            WatchTarget(market_slug="m1", token_id="bad", outcome="NO"),
        ]
        total = watch_loop(conn, client, targets, interval_s=0, max_iterations=2)
        # 2 iterations x 2 targets, but "bad" always fails -> 2 successful + 2 failed = 2 stored
        assert total == 2
        n = conn.execute("SELECT COUNT(*) FROM polymarket_books").fetchone()[0]
        assert n == 2
    finally:
        conn.close()
