from pathlib import Path

from polywhale.db import connect, run_migrations
from polywhale.whale_alerter import (
    _format_multi,
    _format_single,
    _wallet_labels,
    format_signal_alert,
    send_signal_alerts,
)


class _FakeSender:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, token: str, chat_id: str, text: str) -> bool:
        self.calls.append((token, chat_id, text))
        return self.ok


def _ins_sig(
    conn, *, asset_id: str, outcome: str, signal_type: str = "new_position",
    title: str = "t",
) -> int:
    cur = conn.execute(
        "INSERT INTO whale_signals(wallet, signal_type, asset_id, market_slug, "
        "title, outcome, new_size, current_price, prev_captured_at, "
        "latest_captured_at, detected_at) "
        "VALUES ('0xw', ?, ?, 'm', ?, ?, 100000, 0.5, 100, 200, 200)",
        (signal_type, asset_id, title, outcome),
    )
    conn.commit()
    return cur.lastrowid or 0


def _ins_copy_open(conn, signal_id: int) -> None:
    conn.execute(
        "INSERT INTO poly_paper_bets(source, source_ref_id, market_slug, token_id, "
        "side, entry_price, size_shares, cost_usd, placed_at) "
        "VALUES ('whale_copy', ?, 'm', 't', 'YES', 0.5, 100, 50, 1)",
        (signal_id,),
    )
    conn.commit()


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
        assert "NEW" in out
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


def test_format_single_conviction_warning_dropped_for_compactness(tmp_path: Path) -> None:
    """New 2-line compact format drops chase warnings; user queries via /pnl."""
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        _insert_signal(conn, recent_move_pct=0.12, conviction_discount=0.5)
        rows = list(conn.execute("SELECT * FROM whale_signals"))
        out = _format_single(rows[0], {})
        # Compact format: just two lines, no chase warning bloat
        assert out.count("\n") <= 1
    finally:
        conn.close()


def test_format_single_exit_shows_was_size(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        _insert_signal(
            conn,
            signal_type="closed_position",
            old_size=250_000,
            new_size=0,
        )
        rows = list(conn.execute("SELECT * FROM whale_signals"))
        out = _format_single(rows[0], {})
        assert "🔴" in out
        assert "EXIT" in out
        assert "was" in out
        assert "250.0K" in out
    finally:
        conn.close()


def test_format_single_trim_shows_size_ratio(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        _insert_signal(
            conn,
            signal_type="reduced_size",
            old_size=200_000,
            new_size=50_000,
        )
        rows = list(conn.execute("SELECT * FROM whale_signals"))
        out = _format_single(rows[0], {})
        assert "➖" in out  # noqa: RUF001
        assert "TRIM" in out
        assert "50.0K" in out
        assert "200.0K" in out
    finally:
        conn.close()


def test_format_single_added_shows_was(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        _insert_signal(
            conn,
            signal_type="added_size",
            old_size=50_000,
            new_size=200_000,
        )
        rows = list(conn.execute("SELECT * FROM whale_signals"))
        out = _format_single(rows[0], {})
        assert "➕" in out  # noqa: RUF001
        assert "ADDED" in out
        assert "was" in out
        assert "200.0K" in out
        assert "50.0K" in out
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


# copy-only alert mode (notifications ≈ our trades, not the whale firehose).

def test_send_signal_alerts_copy_only_sends_only_copy_trades(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        copied = _ins_sig(conn, asset_id="t1", outcome="Yes", title="COPIEDTITLE")
        _ins_copy_open(conn, copied)            # this one became our bet
        _ins_sig(conn, asset_id="t2", outcome="No", title="FIREHOSETITLE")  # not copied
        fake = _FakeSender()
        res = send_signal_alerts(conn, token="x", chat_id="y", sender=fake, copy_only=True)
        assert res["sent"] is True
        assert res["signals"] == 1
        assert len(fake.calls) == 1
        text = fake.calls[0][2]
        assert "COPIEDTITLE" in text
        assert "FIREHOSETITLE" not in text
        # Both signals marked alerted (firehose one is consumed, just not sent).
        unalerted = conn.execute(
            "SELECT COUNT(*) FROM whale_signals WHERE alerted_at IS NULL"
        ).fetchone()[0]
        assert unalerted == 0
    finally:
        conn.close()


def test_send_signal_alerts_copy_only_suppresses_when_none_copied(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        _ins_sig(conn, asset_id="t1", outcome="FIREHOSE")  # no copy bet
        fake = _FakeSender()
        res = send_signal_alerts(conn, token="x", chat_id="y", sender=fake, copy_only=True)
        assert res["sent"] is False
        assert len(fake.calls) == 0
        unalerted = conn.execute(
            "SELECT COUNT(*) FROM whale_signals WHERE alerted_at IS NULL"
        ).fetchone()[0]
        assert unalerted == 0  # still consumed so copy-processing stays idempotent
    finally:
        conn.close()


def test_send_signal_alerts_default_sends_firehose(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        _ins_sig(conn, asset_id="t1", outcome="A")
        _ins_sig(conn, asset_id="t2", outcome="B")
        fake = _FakeSender()
        # copy_only defaults to False → full firehose.
        res = send_signal_alerts(conn, token="x", chat_id="y", sender=fake)
        assert res["sent"] is True
        assert res["signals"] == 2
        assert len(fake.calls) == 1
    finally:
        conn.close()
