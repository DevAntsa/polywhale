"""Manual preview: render a sample alert and send to Telegram to confirm formatting.

Run with the venv: python tests/_preview_alert.py
Not part of the pytest suite (filename starts with _).
"""

import sqlite3

from polywhale.config import Settings
from polywhale.telegram import send_message
from polywhale.whale_alerter import format_signal_alert


def _row(**kw) -> sqlite3.Row:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cols = list(kw.keys())
    conn.execute(f"CREATE TABLE r ({', '.join(c + ' TEXT' for c in cols)})")
    conn.execute(
        f"INSERT INTO r({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
        tuple(kw.values()),
    )
    return conn.execute("SELECT * FROM r").fetchone()


def main() -> None:
    s = Settings.load()
    if not s.telegram_bot_token or not s.telegram_chat_id:
        raise SystemExit("set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env first")

    single = _row(
        wallet="0xa5ea13a81d2b7e8e424b182bdc1db08e756bd96a",
        signal_type="new_position",
        asset_id="t1",
        market_slug="m",
        title="Will Trump win the 2028 Republican presidential nomination?",
        outcome="Yes",
        old_size=None,
        new_size=250_000,
        current_price=0.42,
        recent_move_pct=0.12,
        conviction_discount=0.5,
    )
    ok1 = send_message(s.telegram_bot_token, s.telegram_chat_id, format_signal_alert([single]))
    print("single:", ok1)

    multi = [
        _row(
            wallet="0xa5ea13a81d2b7e8e424b182bdc1db08e756bd96a",
            signal_type="new_position",
            asset_id="t1",
            market_slug="m",
            title="Yankees vs Red Sox",
            outcome="Yankees",
            old_size=None,
            new_size=100_000,
            current_price=0.43,
            recent_move_pct=None,
            conviction_discount=None,
        ),
        _row(
            wallet="0x2c335066fe58fe9237c3d3dc7b275c2a034a0563",
            signal_type="added_size",
            asset_id="t2",
            market_slug="m",
            title="Will Bitcoin close >$120K on May 31?",
            outcome="No",
            old_size=25_000,
            new_size=80_000,
            current_price=0.31,
            recent_move_pct=None,
            conviction_discount=None,
        ),
        _row(
            wallet="0xf284ad6d607f777f34bc643cea587c33a886b9f9",
            signal_type="new_position",
            asset_id="t3",
            market_slug="m",
            title="Will France win Euro 2028?",
            outcome="Yes",
            old_size=None,
            new_size=550_000,
            current_price=0.18,
            recent_move_pct=0.09,
            conviction_discount=0.5,
        ),
    ]
    ok2 = send_message(s.telegram_bot_token, s.telegram_chat_id, format_signal_alert(multi))
    print("multi:", ok2)


if __name__ == "__main__":
    main()
