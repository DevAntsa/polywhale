"""Optional AI advisor for paper-bet stake sizing via OpenRouter.

Toggle with POLYWHALE_USE_AI_ADVISOR=true. When ON, place_copy_bet() asks the
configured model for a stake multiplier in [0.0, 2.5]. The AI's choice and
reasoning are stored in poly_paper_bets.ai_* columns alongside the mechanical
baseline (mechanical_stake) so we can A/B-compare PnL later.

The mechanical conviction filter (McDonald-2019 overreaction) runs FIRST and
adjusts the base stake. The AI's multiplier compounds on top — they look at
different factors (chase risk vs whale-market fit / concentration).

Failure mode: any error (network, missing key, bad JSON) -> multiplier 1.0
(use mechanical baseline). Bot keeps working, the failure is logged.
"""

import json
import logging
import sqlite3
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "anthropic/claude-haiku-4.5"
DEFAULT_MAX_MULTIPLIER = 2.5

SYSTEM_PROMPT = """You are a position-sizing advisor for a Polymarket whale copy-trading bot.

The bot is in paper-trading mode — no real money at stake yet.
We are building a track record to validate decisions later.

Your job: given context about a whale's new position, output a stake
multiplier in [0.0, 2.5] to apply to our base stake.

A separate mechanical filter already handles chase risk (recent price
movement toward the whale's side). You don't need to factor that in.
Focus on things the mechanical rule can't see.

Calibration rules:
- We have very limited data on what actually works. When in doubt, return 1.0.
- Only deviate from 1.0 when you have a SPECIFIC reason in the context.
- Range expectations: 0.0 = skip, 0.5 = strong reason to reduce,
  1.0 = no strong signal either way, 1.5 = mild reason to lean in,
  2.0+ = rare; reserve for unusually strong alignment.
- Never recommend more than 2.5.

Be honest about uncertainty. Brief reasoning, no fluff."""


@dataclass
class AIAdvice:
    multiplier: float
    reason: str
    confidence: str
    raw_response: str | None = None
    error: str | None = None


def build_user_prompt(context: dict) -> str:
    margin = context.get("whale_margin_pct")
    lines = [
        "A Polymarket whale just opened a position. Recommend stake multiplier.",
        "",
        "WHALE",
        f"  pseudonym: {context.get('whale_label') or '(unnamed)'}",
    ]
    if margin is not None:
        lines.append(f"  margin: {margin}% (proven sharp threshold)")
    lines += [
        f"  signals last 30d: {context.get('whale_signals_30d', 0)}",
        "",
        "MARKET",
        f'  title: "{context.get("market_title", "?")}"',
        f"  whale's side: {context.get('outcome', '?')}",
        f"  current price: {context.get('current_price', 0):.3f}",
        f"  whale's size: {context.get('whale_size_shares', 0):,} shares",
        "",
        "OUR PORTFOLIO",
        f"  open copy positions: {context.get('open_positions', 0)}",
        (
            f"  capital deployed: ${context.get('capital_deployed', 0):.2f} "
            f"of ${context.get('bankroll', 2000):.0f}"
        ),
        f"  open positions in this same market: {context.get('open_in_market', 0)}",
        "",
        "Factors to weigh (broad, directional — we have limited data):",
        "- Does the whale's track record fit this market type?",
        "- Are we already concentrated in this market or category?",
        "- Anything else in the context that warrants more or less than base?",
        "",
        "Output STRICT JSON, no prose around it:",
        "{",
        '  "stake_multiplier": <float 0.0 to 2.5>,',
        '  "reason": "<one sentence, max 200 chars>",',
        '  "confidence": "<low|medium|high>"',
        "}",
    ]
    return "\n".join(lines)


def call_advisor(
    *,
    context: dict,
    api_key: str,
    model: str = DEFAULT_MODEL,
    max_multiplier: float = DEFAULT_MAX_MULTIPLIER,
    timeout_s: float = 15.0,
) -> AIAdvice:
    """POST to OpenRouter, parse JSON multiplier, clamp to safety bounds."""
    if not api_key:
        return AIAdvice(
            multiplier=1.0, reason="no API key configured",
            confidence="low", error="missing_api_key",
        )
    user_prompt = build_user_prompt(context)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.3,
        "max_tokens": 300,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/DevAntsa/polywhale",
        "X-Title": "polywhale",
    }
    try:
        resp = httpx.post(
            OPENROUTER_URL, json=payload, headers=headers, timeout=timeout_s
        )
    except httpx.HTTPError as exc:
        logger.warning("openrouter request failed: %s", exc)
        return AIAdvice(
            multiplier=1.0, reason="api error", confidence="low",
            error=f"http_error: {exc}",
        )
    if resp.status_code != 200:
        logger.warning(
            "openrouter non-200: %d %s", resp.status_code, resp.text[:200]
        )
        return AIAdvice(
            multiplier=1.0, reason="api error", confidence="low",
            error=f"status_{resp.status_code}",
        )
    try:
        body = resp.json()
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError) as exc:
        return AIAdvice(
            multiplier=1.0, reason="api error", confidence="low",
            error=f"parse_envelope: {exc}", raw_response=resp.text[:500],
        )
    try:
        parsed = json.loads(_strip_markdown_fences(content))
        mult = float(parsed["stake_multiplier"])
        reason = str(parsed.get("reason", ""))[:200]
        conf = str(parsed.get("confidence", "medium"))
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        return AIAdvice(
            multiplier=1.0, reason="parse failed", confidence="low",
            error=f"parse_content: {exc}", raw_response=content[:500],
        )
    mult = max(0.0, min(max_multiplier, mult))
    if conf not in ("low", "medium", "high"):
        conf = "medium"
    return AIAdvice(
        multiplier=mult, reason=reason, confidence=conf, raw_response=content[:500]
    )


def _strip_markdown_fences(s: str) -> str:
    """Strip ```json ... ``` wrappers some models add despite response_format=json_object."""
    s = s.strip()
    if s.startswith("```"):
        # Drop the opening fence and optional language tag
        first_newline = s.find("\n")
        if first_newline != -1:
            s = s[first_newline + 1:]
        else:
            s = s[3:]
    if s.endswith("```"):
        s = s[:-3]
    return s.strip()


def build_context_from_signal(
    conn: sqlite3.Connection,
    signal_row: sqlite3.Row,
    *,
    bankroll: float = 2000.0,
) -> dict:
    """Gather context for the advisor from the live DB."""
    wallet = signal_row["wallet"]
    wl = conn.execute(
        "SELECT label, margin_pct, signals_30d FROM whale_watchlist WHERE wallet = ?",
        (wallet,),
    ).fetchone()
    open_row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd), 0) AS deployed "
        "FROM poly_paper_bets WHERE source = 'whale_copy' AND settled_at IS NULL"
    ).fetchone()
    open_market = conn.execute(
        "SELECT COUNT(*) FROM poly_paper_bets "
        "WHERE source = 'whale_copy' AND settled_at IS NULL AND market_slug = ?",
        (signal_row["market_slug"],),
    ).fetchone()[0]
    margin = None
    if wl and wl["margin_pct"] is not None:
        margin = round(float(wl["margin_pct"]), 1)
    return {
        "whale_label": wl["label"] if wl else None,
        "whale_margin_pct": margin,
        "whale_signals_30d": int(wl["signals_30d"]) if wl else 0,
        "market_title": signal_row["title"] or "(unknown market)",
        "outcome": signal_row["outcome"] or "?",
        "current_price": float(signal_row["current_price"] or 0),
        "whale_size_shares": int(signal_row["new_size"] or 0),
        "open_positions": int(open_row["n"] or 0),
        "capital_deployed": round(float(open_row["deployed"] or 0), 2),
        "bankroll": bankroll,
        "open_in_market": int(open_market or 0),
    }
