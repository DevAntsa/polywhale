"""Configuration loaded from environment variables (.env at project root)."""

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader. Skips blank lines and comments. Does not override existing env."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_chat_id: str
    db_path: Path
    log_level: str
    paper_bankroll_usd: float
    paper_stake_pct: float
    openrouter_api_key: str
    ai_model: str
    use_ai_advisor: bool
    alert_copy_only: bool

    @classmethod
    def load(cls, dotenv_path: Path | None = None) -> "Settings":
        env_path = dotenv_path or _project_root() / ".env"
        _load_dotenv(env_path)
        return cls(
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
            db_path=Path(os.environ.get("POLYWHALE_DB_PATH", "data/polywhale.sqlite")),
            log_level=os.environ.get("POLYWHALE_LOG_LEVEL", "INFO"),
            paper_bankroll_usd=float(os.environ.get("POLYWHALE_PAPER_BANKROLL_USD", "2000")),
            paper_stake_pct=float(os.environ.get("POLYWHALE_PAPER_STAKE_PCT", "0.02")),
            openrouter_api_key=os.environ.get("OPENROUTER_API_KEY", ""),
            ai_model=os.environ.get("POLYWHALE_AI_MODEL", "anthropic/claude-haiku-4.5"),
            use_ai_advisor=os.environ.get("POLYWHALE_USE_AI_ADVISOR", "false").lower()
            in ("true", "1", "yes"),
            # Default: only notify on OUR copy trades, not the whale-activity firehose.
            alert_copy_only=os.environ.get("POLYWHALE_ALERT_COPY_ONLY", "true").lower()
            in ("true", "1", "yes"),
        )


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]
