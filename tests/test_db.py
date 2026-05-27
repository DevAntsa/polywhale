from pathlib import Path

from polywhale.db import connect, run_migrations

EXPECTED_TABLES = {
    "polymarket_books",
    "whale_positions",
    "whale_profiles",
    "whale_signals",
    "combo_arbs",
    "poly_paper_bets",
    "schema_migrations",
}


def test_migrations_apply_then_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "test.sqlite"
    conn = connect(db)
    try:
        first = run_migrations(conn)
        assert first, "expected at least one migration on first run"

        second = run_migrations(conn)
        assert second == [], "expected no migrations on second run"

        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert EXPECTED_TABLES.issubset(tables)
    finally:
        conn.close()


def test_wal_mode_enabled(tmp_path: Path) -> None:
    db = tmp_path / "test.sqlite"
    conn = connect(db)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        conn.close()
