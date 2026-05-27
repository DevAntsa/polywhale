import logging

from polywhale.logging_setup import ApiKeyRedactor


def _record(msg: str, args: tuple | None = None) -> logging.LogRecord:
    return logging.LogRecord(
        name="t",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg=msg,
        args=args,
        exc_info=None,
    )


def test_redactor_strips_apikey_in_msg() -> None:
    redactor = ApiKeyRedactor()
    record = _record("GET https://api.example.com?apiKey=secret123&x=1 HTTP/1.1")
    redactor.filter(record)
    assert "secret123" not in record.msg
    assert "apiKey=***" in record.msg


def test_redactor_strips_apikey_in_args() -> None:
    redactor = ApiKeyRedactor()
    record = _record(
        "HTTP Request: %s %s",
        args=("GET", "https://api.example.com?apiKey=secret456 HTTP/1.1 200"),
    )
    redactor.filter(record)
    formatted = record.getMessage()
    assert "secret456" not in formatted
    assert "apiKey=***" in formatted


def test_redactor_leaves_plain_records_alone() -> None:
    redactor = ApiKeyRedactor()
    record = _record("plain message")
    assert redactor.filter(record) is True
    assert record.msg == "plain message"


def test_redactor_handles_quoted_apikey() -> None:
    redactor = ApiKeyRedactor()
    record = _record('URL is "apiKey=abc"')
    redactor.filter(record)
    assert "abc" not in record.msg


def test_redactor_passes_through_dict_args() -> None:
    redactor = ApiKeyRedactor()
    record = _record("summary: %s", args={"a": 1, "b": "no-key-here"})
    redactor.filter(record)
    formatted = record.getMessage()
    assert "no-key-here" in formatted


def test_redactor_redacts_dict_string_values() -> None:
    # Wrap in tuple so LogRecord's single-Mapping unwrap path mirrors real usage
    # (logger.info("...", {"key": "..."}) gets args=({"key": "..."},) at makeRecord).
    redactor = ApiKeyRedactor()
    record = _record("config: %s", args=({"url": "https://x?apiKey=topsecret"},))
    redactor.filter(record)
    formatted = record.getMessage()
    assert "topsecret" not in formatted
    assert "apiKey=***" in formatted
