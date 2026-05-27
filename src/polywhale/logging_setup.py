"""Logging configuration + apiKey redaction."""

import logging
import re

_APIKEY_RE = re.compile(r"apiKey=[^&\s\"']+")


class ApiKeyRedactor(logging.Filter):
    """Replace apiKey=<value> with apiKey=*** in log records (including args)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _APIKEY_RE.sub("apiKey=***", record.msg)
        args = record.args
        if not args:
            return True
        if isinstance(args, tuple):
            record.args = tuple(
                _APIKEY_RE.sub("apiKey=***", a) if isinstance(a, str) else a for a in args
            )
        elif isinstance(args, dict):
            record.args = {
                k: (_APIKEY_RE.sub("apiKey=***", v) if isinstance(v, str) else v)
                for k, v in args.items()
            }
        # Other arg types (rare) pass through unchanged.
        return True


def configure(level: str) -> None:
    """Configure root logging: stderr handler, apiKey redaction, quieter HTTP loggers."""
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    redactor = ApiKeyRedactor()
    for handler in logging.getLogger().handlers:
        handler.addFilter(redactor)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
