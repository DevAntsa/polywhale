import time
from pathlib import Path

from polywhale.db import connect, run_migrations
from polywhale.whale_classify import SHAPE_ARB_OP, SHAPE_SHARP, WhaleProfile
from polywhale.whale_refresh import (
    SOURCE_AUTO_MARGIN,
    SOURCE_MANUAL,
    deactivate,
    load_active_watchlist,
    mark_dormant_auto,
    mark_endorsed,
    refresh_watchlist,
    seed_from_static,
    upsert_manual,
)


class _StubClient:
    def __init__(
        self,
        profiles: list[WhaleProfile],
        activity_events: list[dict] | None = None,
    ) -> None:
        self._profiles = profiles
        # Default to high-quality activity so test candidates pass the new filter
        if activity_events is None:
            activity_events = []
            for i in range(25):
                activity_events.append(
                    {"type": "TRADE", "conditionId": f"m{i}", "timestamp": int(time.time())}
                )
            for i in range(18):
                activity_events.append(
                    {"type": "REDEEM", "conditionId": f"m{i}", "timestamp": int(time.time())}
                )
        self._activity = activity_events

    def get_leaderboard(self, metric: str, *, window: str = "30d"):
        # Build LeaderboardRow-shaped objects from the profile list
        from polywhale.polymarket import LeaderboardRow
        if metric == "profit":
            return [
                LeaderboardRow(wallet=p.wallet, pseudonym=p.pseudonym, name=p.name, amount=p.profit)
                for p in self._profiles
            ]
        return [
            LeaderboardRow(wallet=p.wallet, pseudonym=p.pseudonym, name=p.name, amount=p.volume)
            for p in self._profiles
        ]

    def get_activity(self, wallet: str, *, limit: int = 500) -> list[dict]:
        return self._activity


def _profile(wallet, profit, volume, pseudonym=None):
    margin = (profit / volume * 100.0) if volume > 0 else 0.0
    if volume < 1_000_000:
        shape = "unknown"
    elif margin >= 3.0:
        shape = SHAPE_SHARP
    elif margin < 2.0:
        shape = SHAPE_ARB_OP
    else:
        shape = "hybrid"
    return WhaleProfile(
        wallet=wallet.lower(), pseudonym=pseudonym, name=None,
        window="30d", profit=profit, volume=volume,
        margin_pct=margin, shape=shape, captured_at=int(time.time()),
    )


def test_seed_from_static_inserts_known_wallets(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        n = seed_from_static(conn)
        assert n > 0
        rows = list(conn.execute(
            "SELECT wallet, source FROM whale_watchlist WHERE source = ?", (SOURCE_MANUAL,)
        ))
        assert len(rows) == n
        # Idempotent
        assert seed_from_static(conn) == 0
    finally:
        conn.close()


def test_refresh_adds_qualifying_sharp(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        # Seed empty so first run only inserts from leaderboard
        client = _StubClient([
            _profile("0xsharp1", profit=200_000, volume=2_000_000, pseudonym="sharpguy"),
            _profile("0xarb1", profit=20_000, volume=2_000_000),       # arb_op
            _profile("0xsmall", profit=10_000, volume=100_000),         # unknown
        ])
        result = refresh_watchlist(
            conn, client, min_margin_pct=3.0, min_profit_usd=50_000,
            min_volume_usd=1_000_000, max_dormant_days=14,
        )
        assert result.added >= 1
        row = conn.execute(
            "SELECT * FROM whale_watchlist WHERE wallet = '0xsharp1'"
        ).fetchone()
        assert row["source"] == SOURCE_AUTO_MARGIN
        assert row["label"] == "sharpguy"
        assert row["active"] == 1
        # arb_op and small NOT added
        arb_row = conn.execute(
            "SELECT * FROM whale_watchlist WHERE wallet = '0xarb1'"
        ).fetchone()
        assert arb_row is None
    finally:
        conn.close()


def test_refresh_does_not_deactivate_manual_entries(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        upsert_manual(conn, wallet="0xmanual1", label="user-picked")
        # Empty leaderboard, no whale_positions activity
        client = _StubClient([])
        result = refresh_watchlist(conn, client, max_dormant_days=14)
        assert result.deactivated == 0  # manual is exempt
        row = conn.execute(
            "SELECT * FROM whale_watchlist WHERE wallet = '0xmanual1'"
        ).fetchone()
        assert row["active"] == 1
    finally:
        conn.close()


def test_mark_dormant_auto_skips_active_wallets(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        # auto entry with recent activity
        now = int(time.time())
        conn.execute(
            "INSERT INTO whale_watchlist(wallet, source, added_at, active) VALUES (?, ?, ?, 1)",
            ("0xactive", SOURCE_AUTO_MARGIN, now),
        )
        conn.execute(
            "INSERT INTO whale_positions(wallet, asset_id, size, captured_at) VALUES (?, ?, ?, ?)",
            ("0xactive", "tA", 100.0, now - 60),
        )
        # auto entry with old activity
        conn.execute(
            "INSERT INTO whale_watchlist(wallet, source, added_at, active) VALUES (?, ?, ?, 1)",
            ("0xstale", SOURCE_AUTO_MARGIN, now - 60 * 86400),
        )
        conn.execute(
            "INSERT INTO whale_positions(wallet, asset_id, size, captured_at) VALUES (?, ?, ?, ?)",
            ("0xstale", "tS", 100.0, now - 60 * 86400),
        )
        conn.commit()
        deactivated = mark_dormant_auto(conn, days=14)
        assert deactivated == 1
        active_row = conn.execute(
            "SELECT active FROM whale_watchlist WHERE wallet = '0xactive'"
        ).fetchone()
        assert active_row["active"] == 1
        stale_row = conn.execute(
            "SELECT active, deactivated_reason FROM whale_watchlist WHERE wallet = '0xstale'"
        ).fetchone()
        assert stale_row["active"] == 0
        assert "dormant" in stale_row["deactivated_reason"]
    finally:
        conn.close()


def test_load_active_watchlist_returns_db_rows(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        upsert_manual(conn, wallet="0xa")
        upsert_manual(conn, wallet="0xb")
        wallets = load_active_watchlist(conn)
        assert set(wallets) == {"0xa", "0xb"}
    finally:
        conn.close()


def test_load_active_watchlist_falls_back_to_static_when_empty(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        wallets = load_active_watchlist(conn)
        # Static union from watchlist.py — should be non-empty
        assert len(wallets) > 0
    finally:
        conn.close()


def test_deactivate_marks_wallet_inactive(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        upsert_manual(conn, wallet="0xtarget")
        assert deactivate(conn, "0xtarget", reason="dropped")
        row = conn.execute(
            "SELECT active, deactivated_reason FROM whale_watchlist WHERE wallet = '0xtarget'"
        ).fetchone()
        assert row["active"] == 0
        assert row["deactivated_reason"] == "dropped"
        # Second deactivate is no-op
        assert not deactivate(conn, "0xtarget", reason="x")
    finally:
        conn.close()


def test_update_activity_stats_counts_signals(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        upsert_manual(conn, wallet="0xactive")
        upsert_manual(conn, wallet="0xquiet")
        now = int(time.time())
        # 3 signals for 0xactive, 0 for 0xquiet
        for i, ts in enumerate([now - 3600, now - 7200, now - 86400]):
            conn.execute(
                "INSERT INTO whale_signals(wallet, signal_type, asset_id, market_slug, "
                "latest_captured_at, detected_at) VALUES (?, 'new_position', ?, 'm', 1, ?)",
                ("0xactive", f"t{i}", ts),
            )
        conn.commit()
        from polywhale.whale_refresh import update_activity_stats
        updated = update_activity_stats(conn)
        assert updated == 1  # only 0xactive matched
        active_row = conn.execute(
            "SELECT signals_30d, last_signal_at FROM whale_watchlist WHERE wallet = '0xactive'"
        ).fetchone()
        assert active_row["signals_30d"] == 3
        assert active_row["last_signal_at"] == now - 3600
        quiet_row = conn.execute(
            "SELECT signals_30d, last_signal_at FROM whale_watchlist WHERE wallet = '0xquiet'"
        ).fetchone()
        assert quiet_row["signals_30d"] == 0
        assert quiet_row["last_signal_at"] is None
    finally:
        conn.close()


def test_load_active_watchlist_sorts_by_activity(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        upsert_manual(conn, wallet="0xlow")
        upsert_manual(conn, wallet="0xhigh")
        conn.execute(
            "UPDATE whale_watchlist SET signals_30d = 1 WHERE wallet = '0xlow'"
        )
        conn.execute(
            "UPDATE whale_watchlist SET signals_30d = 10 WHERE wallet = '0xhigh'"
        )
        conn.commit()
        wallets = load_active_watchlist(conn)
        assert wallets.index("0xhigh") < wallets.index("0xlow")
    finally:
        conn.close()


def test_upsert_manual_reactivates_existing(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        upsert_manual(conn, wallet="0xz", label="first")
        deactivate(conn, "0xz", reason="test")
        upsert_manual(conn, wallet="0xz")  # reactivate
        row = conn.execute(
            "SELECT active, label FROM whale_watchlist WHERE wallet = '0xz'"
        ).fetchone()
        assert row["active"] == 1
        assert row["label"] == "first"  # preserved
    finally:
        conn.close()


def test_mark_endorsed_records_specialty(tmp_path: Path) -> None:
    """Cycle 1 (identity research): polymarket-26-list endorsement carries
    the wallet's named specialty (Politics / Sports / Weather / etc.) so we
    can detect lane drift later. mark_endorsed should persist it."""
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        upsert_manual(conn, wallet="0xpolitics", label="cowcat")
        ok = mark_endorsed(
            conn,
            "0xpolitics",
            source="polymarket-26-list",
            specialty="Politics",
        )
        assert ok is True
        row = conn.execute(
            "SELECT endorsed, endorsement_source, polymarket_specialty "
            "FROM whale_watchlist WHERE wallet = '0xpolitics'"
        ).fetchone()
        assert row["endorsed"] == 1
        assert row["endorsement_source"] == "polymarket-26-list"
        assert row["polymarket_specialty"] == "Politics"
    finally:
        conn.close()


def test_mark_endorsed_specialty_none_keeps_existing(tmp_path: Path) -> None:
    """Re-endorsing without specifying specialty should NOT clear it (COALESCE)."""
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        upsert_manual(conn, wallet="0xkeep")
        mark_endorsed(
            conn, "0xkeep", source="polymarket-26-list", specialty="Weather",
        )
        # Re-endorse with a different source but no specialty
        mark_endorsed(conn, "0xkeep", source="verified-handle")
        row = conn.execute(
            "SELECT endorsement_source, polymarket_specialty "
            "FROM whale_watchlist WHERE wallet = '0xkeep'"
        ).fetchone()
        assert row["endorsement_source"] == "verified-handle"
        assert row["polymarket_specialty"] == "Weather"  # preserved
    finally:
        conn.close()
