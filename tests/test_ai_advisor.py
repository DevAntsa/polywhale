import json
from pathlib import Path

import httpx
import pytest

from polywhale.ai_advisor import (
    AIAdvice,
    build_context_from_signal,
    build_user_prompt,
    call_advisor,
)
from polywhale.db import connect, run_migrations


def _mock_openrouter(content_dict: dict, *, status: int = 200):
    """Return a monkeypatch helper that makes httpx.post return content_dict as
    the message content (JSON-encoded)."""
    def handler(request: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(status, text="oops")
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps(content_dict)}}],
        })
    transport = httpx.MockTransport(handler)

    def fake_post(url, **kwargs):
        with httpx.Client(transport=transport) as c:
            return c.post(url, **kwargs)
    return fake_post


def test_build_user_prompt_includes_key_fields() -> None:
    prompt = build_user_prompt({
        "whale_label": "bossoskil1",
        "whale_margin_pct": 4.6,
        "whale_signals_30d": 9,
        "market_title": "Yankees vs Red Sox",
        "outcome": "Yankees",
        "current_price": 0.43,
        "whale_size_shares": 100_000,
        "open_positions": 3,
        "capital_deployed": 115.0,
        "bankroll": 2000.0,
        "open_in_market": 0,
    })
    assert "bossoskil1" in prompt
    assert "4.6%" in prompt
    assert "Yankees vs Red Sox" in prompt
    assert "100,000 shares" in prompt
    assert "$2000" in prompt
    assert "stake_multiplier" in prompt


def test_build_user_prompt_handles_missing_margin() -> None:
    prompt = build_user_prompt({
        "whale_label": None,
        "market_title": "?",
        "outcome": "?",
        "current_price": 0.5,
    })
    assert "(unnamed)" in prompt
    assert "margin:" not in prompt  # omitted when unknown


def test_call_advisor_returns_default_without_api_key() -> None:
    advice = call_advisor(context={}, api_key="")
    assert advice.multiplier == 1.0
    assert advice.error == "missing_api_key"


def test_call_advisor_parses_valid_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "post", _mock_openrouter({
        "stake_multiplier": 1.4,
        "reason": "MLB specialist firing on MLB",
        "confidence": "high",
    }))
    advice = call_advisor(context={"market_title": "t"}, api_key="k")
    assert advice.multiplier == 1.4
    assert advice.reason == "MLB specialist firing on MLB"
    assert advice.confidence == "high"
    assert advice.error is None


def test_call_advisor_clamps_overlarge_multiplier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "post", _mock_openrouter({
        "stake_multiplier": 5.0,  # over 2.5 cap
        "reason": "huge",
        "confidence": "high",
    }))
    advice = call_advisor(context={}, api_key="k")
    assert advice.multiplier == 2.5


def test_call_advisor_clamps_negative(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "post", _mock_openrouter({
        "stake_multiplier": -1.0,
        "reason": "bug",
        "confidence": "low",
    }))
    advice = call_advisor(context={}, api_key="k")
    assert advice.multiplier == 0.0


def test_call_advisor_defaults_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "post", _mock_openrouter({}, status=500))
    advice = call_advisor(context={}, api_key="k")
    assert advice.multiplier == 1.0
    assert "status_500" in (advice.error or "")


def test_call_advisor_defaults_on_bad_json(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "not-json"}}],
        })
    transport = httpx.MockTransport(handler)

    def fake_post(url, **kwargs):
        with httpx.Client(transport=transport) as c:
            return c.post(url, **kwargs)
    monkeypatch.setattr(httpx, "post", fake_post)
    advice = call_advisor(context={}, api_key="k")
    assert advice.multiplier == 1.0
    assert "parse_content" in (advice.error or "")


def test_call_advisor_strips_markdown_fences(monkeypatch: pytest.MonkeyPatch) -> None:
    """Some models wrap JSON in ```json ... ``` despite response_format=json_object."""
    fenced = '```json\n{"stake_multiplier": 1.3, "reason": "ok", "confidence": "high"}\n```'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": fenced}}],
        })
    transport = httpx.MockTransport(handler)

    def fake_post(url, **kwargs):
        with httpx.Client(transport=transport) as c:
            return c.post(url, **kwargs)
    monkeypatch.setattr(httpx, "post", fake_post)
    advice = call_advisor(context={}, api_key="k")
    assert advice.multiplier == 1.3
    assert advice.reason == "ok"
    assert advice.error is None


def test_call_advisor_handles_invalid_confidence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "post", _mock_openrouter({
        "stake_multiplier": 1.2,
        "reason": "ok",
        "confidence": "moderate",  # not in low|medium|high
    }))
    advice = call_advisor(context={}, api_key="k")
    assert advice.confidence == "medium"  # fallback


def test_build_context_from_signal_pulls_db(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        from polywhale.whale_refresh import upsert_manual
        upsert_manual(conn, wallet="0xabc", label="bossoskil1")
        conn.execute(
            "UPDATE whale_watchlist SET margin_pct = 4.6, signals_30d = 9 "
            "WHERE wallet = '0xabc'"
        )
        conn.execute(
            "INSERT INTO whale_signals(wallet, signal_type, asset_id, market_slug, "
            "title, outcome, new_size, current_price, prev_captured_at, "
            "latest_captured_at, detected_at) "
            "VALUES (?, 'new_position', 't1', 'm1', 'Yankees', 'Yes', 100000, 0.43, 1, 2, 3)",
            ("0xabc",),
        )
        conn.commit()
        sig = conn.execute("SELECT * FROM whale_signals").fetchone()
        ctx = build_context_from_signal(conn, sig, bankroll=2000.0)
        assert ctx["whale_label"] == "bossoskil1"
        assert ctx["whale_margin_pct"] == 4.6
        assert ctx["whale_signals_30d"] == 9
        assert ctx["market_title"] == "Yankees"
        assert ctx["bankroll"] == 2000.0
    finally:
        conn.close()


def test_advice_stored_with_paper_bet(tmp_path: Path) -> None:
    """Integration: place_copy_bet stores both mechanical and AI fields."""
    from polywhale.copy_trader import place_copy_bet
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        conn.execute(
            "INSERT INTO whale_signals(wallet, signal_type, asset_id, market_slug, "
            "title, outcome, new_size, current_price, prev_captured_at, "
            "latest_captured_at, detected_at, conviction_discount) "
            "VALUES ('0xw', 'new_position', 't1', 'm1', 'T', 'Yes', 100, 0.40, 1, 2, 3, 1.0)"
        )
        conn.commit()
        sig = conn.execute("SELECT * FROM whale_signals").fetchone()
        advice = AIAdvice(multiplier=1.5, reason="specialist match", confidence="high")
        assert place_copy_bet(
            conn, sig,
            bankroll_usd=2000.0, stake_pct=0.02, ai_advice=advice,
        )
        bet = conn.execute("SELECT * FROM poly_paper_bets").fetchone()
        # Kelly exploration stake (no prior trades for this whale) = 0.5% x $2000 = $10
        # AI multiplier 1.5 x $10 = $15
        assert abs(float(bet["mechanical_stake"]) - 10.0) < 0.5
        assert abs(float(bet["cost_usd"]) - 15.0) < 0.5
        assert bet["ai_multiplier"] == 1.5
        assert bet["ai_reason"] == "specialist match"
        assert bet["ai_confidence"] == "high"
    finally:
        conn.close()
