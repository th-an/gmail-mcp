import io
import json
import time
import urllib.request
from unittest.mock import Mock

import pytest

from gmail_client import GmailClient, b64url_decode, b64url_encode


class FakeResponse:
    def __init__(self, payload, status=200):
        self._data = json.dumps(payload).encode()
        self.status = status

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _patched_client(monkeypatch, payloads, statuses=None):
    responses = list(payloads)
    codes = list(statuses or [200] * len(payloads))

    def fake_urlopen(req, timeout=None, data=None, **kwargs):
        payload = responses.pop(0)
        code = codes.pop(0)
        if code != 200:
            raise urllib.request.HTTPError(
                req.full_url, code, "Error", None,
                io.BytesIO(json.dumps({"error": {"message": "boom"}}).encode()),
            )
        return FakeResponse(payload)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = GmailClient()
    client._token = "fake-token"
    client._token_expiry = time.time() + 3000
    return client


def test_list_messages(monkeypatch):
    client = _patched_client(monkeypatch, [{"messages": [{"id": "1"}, {"id": "2"}]}])
    resp = client.list_messages(q="is:unread", label_ids=["INBOX"], max_results=10)
    assert len(resp["messages"]) == 2


def test_get_message_raw(monkeypatch):
    client = _patched_client(monkeypatch, [{"raw": b64url_encode(b"subject: x")}])
    msg = client.get_message("abc", "raw")
    assert b64url_decode(msg["raw"]).startswith(b"subject: x")


def test_send_message(monkeypatch):
    client = _patched_client(monkeypatch, [{"id": "sent1", "threadId": "t1"}])
    result = client.send_message(b64url_encode(b"raw"))
    assert result["id"] == "sent1"


def test_modify_message(monkeypatch):
    client = _patched_client(monkeypatch, [{"id": "m1", "labelIds": ["INBOX"]}])
    result = client.modify_message("m1", add_labels=["STARRED"], remove_labels=["UNREAD"])
    assert result["id"] == "m1"


def test_batch_modify_and_delete(monkeypatch):
    client = _patched_client(monkeypatch, [{}, {}])
    client.batch_modify(["1", "2"], remove_labels=["UNREAD"])
    client.batch_delete(["1", "2"])


def test_label_cache(monkeypatch):
    payload = {"labels": [{"id": "INBOX", "name": "INBOX"}, {"id": "L1", "name": "Projects"}]}
    client = _patched_client(monkeypatch, [payload, payload])
    assert client.resolve_label_id("inbox") == "INBOX"
    assert client.resolve_label_id("projects") == "L1"
    assert client.resolve_label_id("L1") == "L1"
    assert client.resolve_label_id("missing") is None


def test_transient_retry_then_success(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None, data=None, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.request.HTTPError(
                req.full_url, 503, "Unavailable", None,
                io.BytesIO(json.dumps({"error": {"message": "busy"}}).encode()),
            )
        return FakeResponse({"labels": []})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = GmailClient()
    client._token = "fake-token"
    client._token_expiry = time.time() + 3000
    assert client.list_labels() == []
    assert calls["n"] == 3


def test_error_raises_gmail_error(monkeypatch):
    def fake_urlopen(req, timeout=None, data=None, **kwargs):
        raise urllib.request.HTTPError(
            req.full_url, 400, "Bad Request", None,
            io.BytesIO(json.dumps({"error": {"message": "invalid"}}).encode()),
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = GmailClient()
    client._token = "fake-token"
    client._token_expiry = time.time() + 3000
    with pytest.raises(Exception) as exc_info:
        client.get_message("x", "full")
    assert "invalid" in str(exc_info.value)
