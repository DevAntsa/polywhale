from pathlib import Path

from polywhale.db import connect, run_migrations
from polywhale.whale_alerter import (
    _format_multi,
    _format_single,
    _wallet_labels,
    format_signal_alert,
)


def _insert_signal(
    conn,
    *,
    wallet: str = "0xabcdef0123456789aaaaaaaaaaaaaaaaaaaaaaaa",
    signal_type: str = "new_position",
    asset_id: str = "tok1",
    title: str = "Will the Yankees beat the Red Sox?",
    outcome: str = "Yankees",
    old_size: float | None = None,
    new_size: float | None = 100_000,
    current_price: float | None = 0.43,
    recent_move_pct: float | None = None,
    conviction_discount: float | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO whale_signals(
            wallet, signal_type, asset_id, market_slug, title, outcome,
            old_size, new_size, current_price, prev_captured_at,
            latest_captured_at, detected_at,
            recent_move_pct, conviction_discount
        ) VALUES (?, ?, ?, 'm', ?, ?, ?, ?, ?, 100, 200, 200, ?, ?)
        """,
        (
            wallet,
            signal_type,
            asset_id,
            title,
            outcome,
            old_size,
            new_size,
            current_price,
            recent_move_pct,
            conviction_discount,
        ),
    )
    conn.commit()


def test_wallet_labels_uses_pseudonym_when_present(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        conn.execute(
            "INSERT INTO whale_profiles(wallet, pseudonym, window, shape, captured_at) "
            "VALUES (?, ?, '30d', 'sharp', 100)",
            ("0xabc1234567890abcdef0000000000000000000000", "surfandturf"),
        )
        conn.commit()
        labels = _wallet_labels(conn, ["0xabc1234567890abcdef0000000000000000000000", "0xunknown"])
        assert labels["0xabc1234567890abcdef0000000000000000000000"] == "surfandturf"
        assert "…" in labels["0xunknown"] or labels["0xunknown"] == "0xunknown"
    finally:
        conn.close()


def test_format_single_signal_has_emoji_and_html(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        _insert_signal(conn)
        rows = list(conn.execute("SELECT * FROM whale_signals"))
        out = _format_single(rows[0], {})
        assert "🟢" in out  # new_position emoji
        assert "🐋" in out
        assert "<b>NEW</b>" in out
        assert "Yankees" in out
        # Size formatted with K suffix
        assert "100.0K" in out
    finally:
        conn.close()


def test_format_single_html_escapes_market_title(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        _insert_signal(conn, title="Will <script> happen & finish?")
        rows = list(conn.execute("SELECT * FROM whale_signals"))
        out = _format_single(rows[0], {})
        assert "<script>" not in out  # raw not allowed
        assert "&lt;script&gt;" in out
        assert "&amp;" in out
    finally:
        conn.close()


def test_format_single_conviction_warning_when_market_moved(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        # Market moved +12pp in last 24h, whale just bought -> chase flag
        _insert_signal(conn, recent_move_pct=0.12, conviction_discount=0.5)
        rows = list(conn.execute("SELECT * FROM whale_signals"))
        out = _format_single(rows[0], {})
        assert "⚠️" in out
        assert "chase" in out.lower()
    finally:
        conn.close()


def test_format_multi_uses_pre_block(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        _insert_signal(conn, asset_id="t1", outcome="Yes")
        _insert_signal(conn, asset_id="t2", outcome="No", signal_type="added_size",
                       old_size=25_000, new_size=50_000)
        rows = list(conn.execute("SELECT * FROM whale_signals ORDER BY signal_id"))
        out = _format_multi(rows, {})
        assert "🐋" in out
        assert "<pre>" in out and "</pre>" in out
        assert "2 whale moves" in out
        assert "🟢" in out
        assert "➕" in out  # noqa: RUF001
    finally:
        conn.close()


def test_format_signal_alert_dispatches_by_count(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        _insert_signal(conn)
        rows = list(conn.execute("SELECT * FROM whale_signals"))
        single = format_signal_alert(rows)
        assert "<pre>" not in single  # single uses inline format, not pre block
        _insert_signal(conn, asset_id="t2", outcome="No")
        rows = list(conn.execute("SELECT * FROM whale_signals ORDER BY signal_id"))
        multi = format_signal_alert(rows)
        assert "<pre>" in multi
    finally:
        conn.close()
