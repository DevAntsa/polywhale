"""Tests for walk-forward validation."""

from pathlib import Path

from polywhale.historical_backtest import PositionEpisode
from polywhale.walk_forward import _evaluate_window


def _ep(wallet, entry_ts, exit_ts, pnl, shares=100, entry_vwap=0.40) -> PositionEpisode:
    return PositionEpisode(
        wallet=wallet, condition_id="c", asset="a", outcome_index=0,
        market_slug="m", title="T",
        entry_ts=entry_ts, exit_ts=exit_ts,
        shares=shares, entry_vwap=entry_vwap,
        exit_vwap=entry_vwap + (pnl / shares) if shares else 0.5,
        resolution_status="sold", pnl_usd=pnl, fee_paid=0.0,
    )


def test_evaluate_window_selects_top_k_by_train_pnl() -> None:
    # Setup: 3 whales with different train PnL
    train_lo, train_hi = 100, 200
    test_lo, test_hi = 200, 300

    eps = [
        # Whale A: +50 in train, +5 per-episode in test (3 test episodes)
        _ep("0xa", entry_ts=110, exit_ts=120, pnl=50),
        _ep("0xa", entry_ts=210, exit_ts=220, pnl=5),
        _ep("0xa", entry_ts=230, exit_ts=240, pnl=5),
        _ep("0xa", entry_ts=250, exit_ts=260, pnl=5),
        # Whale B: +20 in train, -10 per episode in test
        _ep("0xb", entry_ts=130, exit_ts=140, pnl=20),
        _ep("0xb", entry_ts=210, exit_ts=220, pnl=-10),
        # Whale C: -5 in train (excluded from positive ranking)
        _ep("0xc", entry_ts=150, exit_ts=160, pnl=-5),
        _ep("0xc", entry_ts=240, exit_ts=250, pnl=100),  # not followed
    ]
    result = _evaluate_window(
        eps, train_lo, train_hi, test_lo, test_hi, top_k=2, stake_usd=40.0
    )
    # Both A and B are positive in train; both selected
    assert set(result.train_whales_top_k) == {"0xa", "0xb"}
    # Test episodes: 3 from A + 1 from B = 4
    assert result.test_episodes == 4


def test_evaluate_window_excludes_negative_train_whales() -> None:
    train_lo, train_hi = 100, 200
    test_lo, test_hi = 200, 300

    eps = [
        _ep("0xloser", entry_ts=110, exit_ts=120, pnl=-50),
        _ep("0xloser", entry_ts=210, exit_ts=220, pnl=100),  # not followed
        _ep("0xwinner", entry_ts=130, exit_ts=140, pnl=30),
        _ep("0xwinner", entry_ts=220, exit_ts=230, pnl=10),
    ]
    result = _evaluate_window(
        eps, train_lo, train_hi, test_lo, test_hi, top_k=5, stake_usd=40.0
    )
    assert "0xloser" not in result.train_whales_top_k
    assert "0xwinner" in result.train_whales_top_k
    # Only winner's test episode counted
    assert result.test_episodes == 1


def test_evaluate_window_no_qualifying_whales_returns_zero() -> None:
    """If no whale had positive PnL in training, test_pnl should be 0."""
    train_lo, train_hi = 100, 200
    test_lo, test_hi = 200, 300

    eps = [_ep("0xa", entry_ts=110, exit_ts=120, pnl=-10)]
    result = _evaluate_window(
        eps, train_lo, train_hi, test_lo, test_hi, top_k=5, stake_usd=40.0
    )
    assert result.train_whales_top_k == []
    assert result.test_episodes == 0
    assert result.test_pnl == 0.0


def test_evaluate_window_scales_pnl_by_stake(tmp_path: Path) -> None:
    """A whale with 100 shares x $0.40 = $40 notional and +$10 PnL scales to
    our $40 stake → we'd get the same +$10 if our stake == notional."""
    train_lo, train_hi = 100, 200
    test_lo, test_hi = 200, 300
    eps = [
        _ep("0xa", entry_ts=110, exit_ts=120, pnl=10),
        # Whale's test trade: 100 shares x $0.40 = $40 notional, pnl=$20
        _ep("0xa", entry_ts=210, exit_ts=220, pnl=20, shares=100, entry_vwap=0.40),
    ]
    result = _evaluate_window(
        eps, train_lo, train_hi, test_lo, test_hi, top_k=5, stake_usd=40.0
    )
    # Our stake $40 == whale's notional $40 → scale 1.0 → we get same $20
    assert abs(result.test_pnl - 20.0) < 0.01

    # Now with stake of $20 (half the notional) → we should get half the PnL
    result_half = _evaluate_window(
        eps, train_lo, train_hi, test_lo, test_hi, top_k=5, stake_usd=20.0
    )
    assert abs(result_half.test_pnl - 10.0) < 0.01
