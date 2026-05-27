import httpx

from polywhale.telegram import send_message


def _client_with_handler(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_send_message_returns_true_on_ok(monkeypatch) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.read().decode("utf-8")
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    transport = httpx.MockTransport(handler)

    def fake_post(url, **kwargs):
        with httpx.Client(transport=transport) as c:
            return c.post(url, **kwargs)

    monkeypatch.setattr(httpx, "post", fake_post)
    ok = send_message("TOKEN", "12345", "hello")
    assert ok is True
    assert "/botTOKEN/" in captured["url"]
    assert '"chat_id"' in captured["body"] and "12345" in captured["body"]
    assert '"text"' in captured["body"] and "hello" in captured["body"]


def test_send_message_returns_false_on_api_error(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "description": "chat not found"})

    transport = httpx.MockTransport(handler)

    def fake_post(url, **kwargs):
        with httpx.Client(transport=transport) as c:
            return c.post(url, **kwargs)

    monkeypatch.setattr(httpx, "post", fake_post)
    assert send_message("TOKEN", "12345", "hi") is False


def test_send_message_returns_false_on_http_error(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    transport = httpx.MockTransport(handler)

    def fake_post(url, **kwargs):
        with httpx.Client(transport=transport) as c:
            return c.post(url, **kwargs)

    monkeypatch.setattr(httpx, "post", fake_post)
    assert send_message("TOKEN", "12345", "hi") is False


def test_send_message_returns_false_on_network_error(monkeypatch) -> None:
    def fake_post(url, **kwargs):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(httpx, "post", fake_post)
    assert send_message("TOKEN", "12345", "hi") is False
