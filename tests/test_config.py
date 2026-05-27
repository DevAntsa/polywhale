import os
from pathlib import Path

import pytest

from polywhale.config import Settings, _load_dotenv


def test_load_dotenv_reads_simple(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / ".env"
    env.write_text(
        '# a comment\nFOO=bar\n\nBAZ=qux quux\nQUOTED="with spaces"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("FOO", raising=False)
    monkeypatch.delenv("BAZ", raising=False)
    monkeypatch.delenv("QUOTED", raising=False)
    _load_dotenv(env)
    assert os.environ["FOO"] == "bar"
    assert os.environ["BAZ"] == "qux quux"
    assert os.environ["QUOTED"] == "with spaces"


def test_load_dotenv_does_not_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / ".env"
    env.write_text("FOO=fromfile\n", encoding="utf-8")
    monkeypatch.setenv("FOO", "fromenv")
    _load_dotenv(env)
    assert os.environ["FOO"] == "fromenv"


def test_settings_load_with_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for k in (
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "POLYWHALE_DB_PATH",
        "POLYWHALE_LOG_LEVEL", "POLYWHALE_PAPER_BANKROLL_USD",
        "POLYWHALE_PAPER_STAKE_PCT",
    ):
        monkeypatch.delenv(k, raising=False)
    empty = tmp_path / ".env"
    empty.write_text("", encoding="utf-8")
    settings = Settings.load(dotenv_path=empty)
    assert settings.telegram_bot_token == ""
    assert settings.telegram_chat_id == ""
    assert settings.db_path == Path("data/polywhale.sqlite")
    assert settings.log_level == "INFO"
    assert settings.paper_bankroll_usd == 2000.0
    assert settings.paper_stake_pct == 0.02


def test_settings_load_populates_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "POLYWHALE_DB_PATH"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "TELEGRAM_BOT_TOKEN=t\nTELEGRAM_CHAT_ID=c\nPOLYWHALE_DB_PATH=/tmp/pw.sqlite\n",
        encoding="utf-8",
    )
    settings = Settings.load(dotenv_path=env)
    assert settings.telegram_bot_token == "t"
    assert settings.telegram_chat_id == "c"
    assert settings.db_path == Path("/tmp/pw.sqlite")
