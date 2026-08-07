import base64
import hashlib
import json
import os
import urllib.parse
import urllib.request

import gmail_auth


class FakeResponse:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode()

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_default_scopes(monkeypatch):
    monkeypatch.delenv("GMAIL_FULL_ACCESS", raising=False)
    scopes = gmail_auth._scopes()
    assert "https://www.googleapis.com/auth/gmail.modify" in scopes
    assert "https://mail.google.com/" not in scopes


def test_full_access_scope(monkeypatch):
    monkeypatch.setenv("GMAIL_FULL_ACCESS", "1")
    assert "https://mail.google.com/" in gmail_auth._scopes()


def test_client_id_required(monkeypatch):
    monkeypatch.delenv("GMAIL_CLIENT_ID", raising=False)
    try:
        gmail_auth._client_id()
        assert False, "should raise"
    except RuntimeError as e:
        assert "GMAIL_CLIENT_ID" in str(e)


def test_refresh(monkeypatch):
    monkeypatch.setenv("GMAIL_CLIENT_ID", "cid")
    seen = {}

    def fake_urlopen(req, data=None, timeout=None, **kwargs):
        seen["url"] = req.full_url
        assert req.get_method() == "POST"
        return FakeResponse({"access_token": "new-token", "expires_in": 3600})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    result = gmail_auth._refresh("rt")
    assert result["access_token"] == "new-token"
    assert gmail_auth.TOKEN_URL in seen["url"]


def test_acquire_refreshes_from_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("GMAIL_TOKEN_DIR", str(tmp_path))
    monkeypatch.setenv("GMAIL_CLIENT_ID", "cid")
    cache = tmp_path / "token_cache.json"
    cache.write_text(json.dumps({"refresh_token": "rt"}))

    def fake_urlopen(req, data=None, timeout=None, **kwargs):
        return FakeResponse({"access_token": "fresh-token", "expires_in": 3600})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    token = gmail_auth.acquire_access_token()
    assert token == "fresh-token"


def test_pkce_challenge():
    verifier = "aBc123-_~x"
    challenge = gmail_auth._pkce_challenge(verifier)
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert challenge == expected
    assert "/" not in challenge and "+" not in challenge


def test_build_auth_url(monkeypatch):
    monkeypatch.setenv("GMAIL_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("GMAIL_FULL_ACCESS", "0")
    url = gmail_auth.build_auth_url(8765, "verifier123", "state-abc")
    parsed = urllib.parse.urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "accounts.google.com"
    qs = urllib.parse.parse_qs(parsed.query)
    assert qs["client_id"] == ["cid.apps.googleusercontent.com"]
    assert qs["redirect_uri"] == ["http://127.0.0.1:8765/"]
    assert qs["response_type"] == ["code"]
    assert qs["access_type"] == ["offline"]
    assert qs["prompt"] == ["consent"]
    assert qs["state"] == ["state-abc"]
    assert qs["code_challenge_method"] == ["S256"]
    assert qs["code_challenge"] == [gmail_auth._pkce_challenge("verifier123")]
    assert "https://www.googleapis.com/auth/gmail.modify" in qs["scope"][0]
    assert "https://mail.google.com/" not in qs["scope"][0]


def test_exchange_code(monkeypatch):
    monkeypatch.setenv("GMAIL_CLIENT_ID", "cid")
    seen = {}

    def fake_urlopen(req, data=None, timeout=None, **kwargs):
        seen["body"] = urllib.parse.parse_qs(data.decode())
        return FakeResponse({"access_token": "tok", "refresh_token": "rt"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    result = gmail_auth._exchange_code("the-code", "http://127.0.0.1:8765/", "verifier")
    assert result["access_token"] == "tok"
    assert seen["body"]["grant_type"] == ["authorization_code"]
    assert seen["body"]["code"] == ["the-code"]
    assert seen["body"]["code_verifier"] == ["verifier"]
    assert seen["body"]["redirect_uri"] == ["http://127.0.0.1:8765/"]


def test_authenticate_saves_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("GMAIL_TOKEN_DIR", str(tmp_path))
    monkeypatch.setenv("GMAIL_CLIENT_ID", "cid")

    def fake_run_loopback(port, auth_url, state):
        assert port == gmail_auth.DEFAULT_AUTH_PORT
        return "auth-code"

    def fake_urlopen(req, data=None, timeout=None, **kwargs):
        body = urllib.parse.parse_qs(data.decode())
        assert body["grant_type"] == ["authorization_code"]
        return FakeResponse({"access_token": "fresh", "refresh_token": "rt", "expires_in": 3600})

    monkeypatch.setattr(gmail_auth, "_run_loopback", fake_run_loopback)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    token = gmail_auth._authenticate()
    assert token == "fresh"
    cache = json.loads((tmp_path / "token_cache.json").read_text())
    assert cache["refresh_token"] == "rt"
    assert cache["client_id"] == "cid"


def test_acquire_authenticates_when_no_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("GMAIL_TOKEN_DIR", str(tmp_path))
    monkeypatch.setenv("GMAIL_CLIENT_ID", "cid")
    monkeypatch.setattr(gmail_auth, "_authenticate", lambda: "loopback-token")
    assert gmail_auth.acquire_access_token() == "loopback-token"
