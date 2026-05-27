"""Thin Telegram Bot API wrapper for outbound messages."""

import logging

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"


def send_message(
    token: str,
    chat_id: str,
    text: str,
    *,
    base_url: str = API_BASE,
    timeout: float = 10.0,
) -> bool:
    """Send a message via Telegram Bot API. Returns True on success, False on any failure."""
    url = f"{base_url}/bot{token}/sendMessage"
    try:
        resp = httpx.post(
            url,
            json={"chat_id": chat_id, "text": text},
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        logger.warning("telegram send failed: %s", exc)
        return False
    if resp.status_code != 200:
        logger.warning("telegram non-200: %d %s", resp.status_code, resp.text[:200])
        return False
    try:
        body = resp.json()
    except ValueError:
        logger.warning("telegram response not JSON: %s", resp.text[:200])
        return False
    if not body.get("ok"):
        logger.warning("telegram api error: %s", body.get("description"))
        return False
    return True
