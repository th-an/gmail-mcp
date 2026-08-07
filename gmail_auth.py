"""Google OAuth2 authorization-code authentication for the Gmail MCP server.

Google's OAuth2 *device* flow does not support Gmail scopes, so this uses the
authorization-code (web server) flow with a **loopback redirect**: the server
starts a short-lived HTTP listener on 127.0.0.1, opens the consent page in the
default browser, and captures the redirected authorization code. PKCE (S256)
is used so no client secret is required, though a secret is sent if provided.

Requires a Google OAuth client of type "Desktop app" (loopback redirects are
automatically allowed for that type — no redirect URI registration needed).

Flow:
  1. Build the authorization URL (code + PKCE verifier + state) and open it
  2. A local HTTP server catches the redirect on http://127.0.0.1:PORT/
  3. Exchange the code for a refresh token + access token
  4. Store the refresh token; refresh silently before each expiry thereafter
"""

import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from typing import Dict, List, Optional

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

LOOPBACK_HOST = "127.0.0.1"
DEFAULT_AUTH_PORT = 8765
AUTH_TIMEOUT_SECONDS = 300

DEFAULT_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.settings.basic",
]
FULL_ACCESS_SCOPE = "https://mail.google.com/"


def _load_dotenv(path: Optional[str] = None) -> None:
    """Populate os.environ from the .env next to this file (real env vars win)."""
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip("\"'").strip()
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


_load_dotenv()


def _token_dir() -> str:
    return os.getenv("GMAIL_TOKEN_DIR", os.path.expanduser("~/.gmail-mcp"))


def _cache_path() -> str:
    return os.path.join(_token_dir(), "token_cache.json")


def _client_id() -> str:
    cid = os.getenv("GMAIL_CLIENT_ID", "")
    if not cid:
        raise RuntimeError(
            "GMAIL_CLIENT_ID is not set. Create a Google Cloud OAuth client "
            "('Desktop app' type), enable the Gmail API, and set its client id, "
            "then retry."
        )
    return cid


def _client_secret() -> Optional[str]:
    return os.getenv("GMAIL_CLIENT_SECRET") or None


def _scopes() -> List[str]:
    scopes = list(DEFAULT_SCOPES)
    if os.getenv("GMAIL_FULL_ACCESS") == "1":
        scopes.append(FULL_ACCESS_SCOPE)
    return scopes


def _auth_port() -> int:
    return int(os.getenv("GMAIL_AUTH_PORT", str(DEFAULT_AUTH_PORT)))


def _redirect_uri() -> str:
    return f"http://{LOOPBACK_HOST}:{_auth_port()}/"


def _load_cache() -> Dict[str, str]:
    try:
        if os.path.exists(_cache_path()):
            with open(_cache_path(), "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        sys.stderr.write(f"WARNING: could not load token cache: {e}\n")
        sys.stderr.flush()
    return {}


def _save_cache(data: Dict[str, str]) -> None:
    try:
        os.makedirs(_token_dir(), exist_ok=True)
        with open(_cache_path(), "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception as e:
        sys.stderr.write(f"WARNING: could not save token cache: {e}\n")
        sys.stderr.flush()


def _post_form(url: str, data: Dict[str, str]) -> Dict[str, str]:
    req = urllib.request.Request(url, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    body = urllib.parse.urlencode(data).encode("utf-8")
    with urllib.request.urlopen(req, data=body, timeout=30) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw else {}


def _refresh(refresh_token: str) -> Dict[str, str]:
    data = {
        "client_id": _client_id(),
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    secret = _client_secret()
    if secret:
        data["client_secret"] = secret
    return _post_form(TOKEN_URL, data)


def _new_verifier() -> str:
    return secrets.token_urlsafe(64)


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")


def build_auth_url(port: int, verifier: str, state: str) -> str:
    params = {
        "client_id": _client_id(),
        "redirect_uri": f"http://{LOOPBACK_HOST}:{port}/",
        "response_type": "code",
        "scope": " ".join(_scopes()),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": _pkce_challenge(verifier),
        "code_challenge_method": "S256",
    }
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)


def _make_callback_handler(result: Dict[str, Optional[str]], state: str):
    """Return a BaseHTTPRequestHandler that records the OAuth callback."""

    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        def _respond(self, heading: str, failed: bool) -> None:
            body = (
                "<html><body><h2>Gmail MCP: " + heading + "</h2>"
                "<p>You can close this window and return to the terminal.</p></body></html>"
                if not failed
                else "<html><body><h2>Gmail MCP: authentication failed</h2><p>" + heading + "</p></body></html>"
            )
            payload = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            code = qs.get("code", [None])[0]
            error = qs.get("error", [None])[0]
            state_ok = qs.get("state", [None])[0] == state

            if result["done"]:
                self._respond("authentication already complete.", failed=False)
                return

            if error:
                result["error"] = error
                result["done"] = True
                self._respond("error: " + str(error), failed=True)
                return

            if not state_ok:
                sys.stderr.write(
                    f"[Gmail MCP] ignoring callback with state mismatch "
                    f"(expected {state!r}, got {qs.get('state', [None])[0]!r})\n"
                )
                sys.stderr.flush()
                self._respond("state mismatch — ignore this stale tab.", failed=True)
                return

            if not code:
                self._respond("missing authorization code.", failed=True)
                return

            result["code"] = code
            result["error"] = None
            result["state_mismatch"] = False
            result["done"] = True
            self._respond("authentication complete.", failed=False)

        def log_message(self, *args) -> None:
            pass

    return CallbackHandler


def _run_loopback(port: int, auth_url: str, state: str) -> str:
    """Open the browser, wait for the redirect, return the authorization code."""
    result: Dict[str, Optional[str]] = {"code": None, "error": None, "state_mismatch": False, "done": False}
    handler = _make_callback_handler(result, state)
    server = http.server.ThreadingHTTPServer((LOOPBACK_HOST, port), handler)
    server.daemon_threads = True
    server.block_on_close = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        sys.stderr.write(
            f"\n[Gmail MCP] Open this URL in a browser and sign in:\n  {auth_url}\n"
            f"(waiting for the callback on http://{LOOPBACK_HOST}:{port}/ ...)\n\n"
        )
        sys.stderr.flush()
        try:
            webbrowser.open(auth_url)
        except Exception:
            pass

        deadline = time.time() + AUTH_TIMEOUT_SECONDS
        while not result["done"] and time.time() < deadline:
            time.sleep(0.2)

        if not result["done"]:
            raise RuntimeError(
                f"Authentication timed out after {AUTH_TIMEOUT_SECONDS}s "
                f"(no callback on http://{LOOPBACK_HOST}:{port}/)."
            )
        if result["error"]:
            raise RuntimeError(f"Authentication failed: {result['error']}")
        if result["state_mismatch"]:
            raise RuntimeError("Authentication failed: OAuth state mismatch.")
        if not result["code"]:
            raise RuntimeError("Authentication failed: no authorization code returned.")
        return result["code"]
    finally:
        server.shutdown()
        server.server_close()


def _exchange_code(code: str, redirect_uri: str, verifier: str) -> Dict[str, str]:
    data = {
        "client_id": _client_id(),
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }
    secret = _client_secret()
    if secret:
        data["client_secret"] = secret
    return _post_form(TOKEN_URL, data)


def _authenticate() -> str:
    """Run the loopback authorization flow and return a fresh access token."""
    port = _auth_port()
    redirect_uri = _redirect_uri()
    verifier = _new_verifier()
    state = secrets.token_urlsafe(16)
    auth_url = build_auth_url(port, verifier, state)
    code = _run_loopback(port, auth_url, state)
    result = _exchange_code(code, redirect_uri, verifier)
    if "access_token" not in result:
        raise RuntimeError(f"Token exchange failed: {result}")
    _save_cache(
        {
            "refresh_token": result.get("refresh_token", ""),
            "client_id": _client_id(),
            "client_secret": _client_secret() or "",
        }
    )
    return result["access_token"]


def acquire_access_token() -> str:
    """Return a valid Gmail API access token, refreshing or re-authenticating if needed."""
    cache = _load_cache()
    refresh_token = cache.get("refresh_token", "")
    if refresh_token:
        result = _refresh(refresh_token)
        if "access_token" in result:
            if result.get("refresh_token"):
                cache["refresh_token"] = result["refresh_token"]
                _save_cache(cache)
            return result["access_token"]
        sys.stderr.write(f"WARNING: token refresh failed, re-authenticating: {result}\n")
        sys.stderr.flush()

    return _authenticate()
