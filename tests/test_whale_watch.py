from pathlib import Path

from polywhale.db import connect, run_migrations
from polywhale.polymarket import WhalePosition
from polywhale.whale_watch import (
    prune_old_snapshots,
    snapshot_wallet,
    snapshot_wallets_parallel,
    watch_wallets,
)


class _StubClient:
    def __init__(self, positions: list[WhalePosition]) -> None:
        self._positions = positions
        self.calls = 0

    def get_whale_positions(self, wallet: str, *, size_threshold: float = 10.0):
        self.calls += 1
        return [p for p in self._positions if p.wallet == wallet]


def _pos(wallet: str, asset_id: str, title: str = "Test", size: float = 100.0) -> WhalePosition:
    return WhalePosition(
        wallet=wallet,
        asset_id=asset_id,
        condition_id="0xcond",
        market_slug="m",
        event_slug="e",
        title=title,
        outcome="Yes",
        size=size,
        avg_price=0.5,
        current_price=0.55,
        current_value=size * 0.55,
        initial_value=size * 0.5,
        cash_pnl=size * 0.05,
        realized_pnl=0,
        percent_pnl=10.0,
        end_date="2026-12-31",
        neg_risk=False,
    )


def test_snapshot_wallet_persists_rows(tmp_path: Path) -> None:
    db = tmp_path / "t.sqlite"
    conn = connect(db)
    try:
        run_migrations(conn)
        positions = [
            _pos("0xwhale", "tok1", title="Match A", size=1000),
            _pos("0xwhale", "tok2", title="Match B", size=500),
        ]
        client = _StubClient(positions)
        count = snapshot_wallet(conn, client, "0xwhale")
        assert count == 2
        rows = list(
            conn.execute(
                "SELECT title, outcome, size, avg_price FROM whale_positions ORDER BY size DESC"
            )
        )
        assert len(rows) == 2
        assert rows[0]["title"] == "Match A"
        assert rows[0]["size"] == 1000
        assert rows[1]["title"] == "Match B"
    finally:
        conn.close()


def test_snapshot_wallet_empty_when_no_positions(tmp_path: Path) -> None:
    db = tmp_path / "t.sqlite"
    conn = connect(db)
    try:
        run_migrations(conn)
        client = _StubClient([])
        count = snapshot_wallet(conn, client, "0xwhale")
        assert count == 0
        n = conn.execute("SELECT COUNT(*) FROM whale_positions").fetchone()[0]
        assert n == 0
    finally:
        conn.close()


def test_watch_wallets_iterates_each_wallet(tmp_path: Path) -> None:
    db = tmp_path / "t.sqlite"
    conn = connect(db)
    try:
        run_migrations(conn)
        positions = [
            _pos("0xw1", "tok1", title="W1 pos1"),
            _pos("0xw2", "tok2", title="W2 pos1"),
            _pos("0xw2", "tok3", title="W2 pos2"),
        ]
        client = _StubClient(positions)
        total = watch_wallets(conn, client, ["0xw1", "0xw2"], interval_s=0, max_iterations=2)
        # 2 iterations: first writes 3 rows, second is unchanged and writes 0 (change-only insert).
        assert total == 3
        n = conn.execute("SELECT COUNT(*) FROM whale_positions").fetchone()[0]
        assert n == 3
    finally:
        conn.close()


def test_snapshot_wallet_skips_when_unchanged(tmp_path: Path) -> None:
    db = tmp_path / "t.sqlite"
    conn = connect(db)
    try:
        run_migrations(conn)
        positions = [_pos("0xwhale", "tok1", title="A", size=1000)]
        client = _StubClient(positions)
        assert snapshot_wallet(conn, client, "0xwhale") == 1
        # Second call with identical positions writes nothing.
        assert snapshot_wallet(conn, client, "0xwhale") == 0
        n = conn.execute("SELECT COUNT(*) FROM whale_positions").fetchone()[0]
        assert n == 1
    finally:
        conn.close()


def test_snapshot_wallet_writes_when_size_changes(tmp_path: Path) -> None:
    db = tmp_path / "t.sqlite"
    conn = connect(db)
    try:
        run_migrations(conn)
        client = _StubClient([_pos("0xw", "tok1", size=1000)])
        snapshot_wallet(conn, client, "0xw")
        # Mutate the stub's position size and re-snapshot.
        client._positions = [_pos("0xw", "tok1", size=2000)]
        assert snapshot_wallet(conn, client, "0xw") == 1
        n = conn.execute("SELECT COUNT(*) FROM whale_positions").fetchone()[0]
        assert n == 2
    finally:
        conn.close()


def test_prune_old_snapshots_deletes_stale_rows(tmp_path: Path) -> None:
    import time as _time

    db = tmp_path / "t.sqlite"
    conn = connect(db)
    try:
        run_migrations(conn)
        ancient = int(_time.time()) - 60 * 24 * 60 * 60
        recent = int(_time.time()) - 60
        conn.execute(
            "INSERT INTO whale_positions(wallet, asset_id, size, captured_at) VALUES (?, ?, ?, ?)",
            ("0xw", "A", 100, ancient),
        )
        conn.execute(
            "INSERT INTO whale_positions(wallet, asset_id, size, captured_at) VALUES (?, ?, ?, ?)",
            ("0xw", "A", 100, recent),
        )
        conn.execute(
            "INSERT INTO polymarket_books(market_slug, token_id, captured_at, book_json) "
            "VALUES (?, ?, ?, ?)",
            ("m", "A", ancient, "{}"),
        )
        conn.commit()
        out = prune_old_snapshots(conn, days=30)
        assert out["whale_positions"] == 1
        assert out["polymarket_books"] == 1
        # Recent row survives.
        n = conn.execute("SELECT COUNT(*) FROM whale_positions").fetchone()[0]
        assert n == 1
    finally:
        conn.close()


def test_watch_wallets_handles_per_wallet_error(tmp_path: Path) -> None:
    db = tmp_path / "t.sqlite"
    conn = connect(db)
    try:
        run_migrations(conn)

        class _FlakyClient:
            def get_whale_positions(self, wallet: str, *, size_threshold: float = 10.0):
                if wallet == "0xbroken":
                    raise RuntimeError("rate limit")
                return [_pos(wallet, "tok1")]

        client = _FlakyClient()
        total = watch_wallets(conn, client, ["0xgood", "0xbroken"], interval_s=0, max_iterations=1)
        # Only 0xgood contributes
        assert total == 1
    finally:
        conn.close()


def test_snapshot_wallets_parallel_collects_all(tmp_path: Path) -> None:
    """Parallel fetch + serial write: all wallets' positions land in the DB."""
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        positions = [
            _pos("0xa", "tok1", title="A1"),
            _pos("0xb", "tok2", title="B1"),
            _pos("0xc", "tok3", title="C1"),
        ]
        client = _StubClient(positions)
        count = snapshot_wallets_parallel(
            conn, client, ["0xa", "0xb", "0xc"], max_workers=3,
        )
        assert count == 3
        rows = list(conn.execute(
            "SELECT wallet, title FROM whale_positions ORDER BY wallet"
        ))
        assert {r["wallet"] for r in rows} == {"0xa", "0xb", "0xc"}
    finally:
        conn.close()


def test_snapshot_wallets_parallel_tolerates_one_fetch_failure(tmp_path: Path) -> None:
    """If one wallet's fetch throws, the others still complete."""
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)

        class _PartiallyBrokenClient:
            def get_whale_positions(self, wallet, *, size_threshold=10.0):
                if wallet == "0xbroken":
                    raise RuntimeError("simulated fetch failure")
                return [_pos(wallet, f"tok-{wallet}", title=f"OK-{wallet}")]

        client = _PartiallyBrokenClient()
        count = snapshot_wallets_parallel(
            conn, client, ["0xa", "0xbroken", "0xc"], max_workers=3,
        )
        # Two wallets succeeded → 2 stored
        assert count == 2
    finally:
        conn.close()


def test_snapshot_wallets_parallel_empty_input_returns_zero(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        client = _StubClient([])
        assert snapshot_wallets_parallel(conn, client, []) == 0
    finally:
        conn.close()
