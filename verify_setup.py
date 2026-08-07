#!/usr/bin/env python3
"""Verify the Gmail MCP .env / OAuth setup before running the server.

Checks:
  1. .env exists and is readable
  2. GMAIL_CLIENT_ID is present and well-formed
  3. Optional GMAIL_CLIENT_SECRET is well-formed if set
  4. GMAIL_FULL_ACCESS / GMAIL_TRANSPORT / GMAIL_TOKEN_DIR are valid if set
  5. The uv environment + server module import cleanly

Optional live check (--live): performs a real token-endpoint request to Google
to confirm the client ID (and secret, if set) are valid. It uses a bogus refresh
token: Google answers `invalid_grant` for a well-formed client (expected) and
`invalid_client` for a wrong client id/secret.

Exit code 0 = all checks passed, 1 = any check failed.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

TOKEN_URL = "https://oauth2.googleapis.com/token"

CLIENT_ID_RE = re.compile(r"^\d{12,}-[A-Za-z0-9_\-]+\.apps\.googleusercontent\.com$")
SECRET_RE = re.compile(r"^[A-Za-z0-9_\-]{20,}$")

GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
BOLD = "\033[1m"
RESET = "\033[0m"


def load_env(path: str | None = None) -> dict:
    """Minimal .env parser (KEY=VALUE, # comments, optional quotes)."""
    env: dict = {}
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return {"__missing__": path}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("\"'").strip()
            env[key] = value
    return env


def ok(msg: str) -> None:
    print(f"  {GREEN}[OK]{RESET} {msg}")


def warn(msg: str) -> None:
    print(f"  {YELLOW}[WARN]{RESET} {msg}")


def fail(msg: str) -> None:
    print(f"  {RED}[FAIL]{RESET} {msg}")


def live_client_check(client_id: str, client_secret: str | None) -> bool:
    print()
    print(f"{BOLD}Live client check{RESET} (POST {TOKEN_URL})")
    data: dict = {
        "client_id": client_id,
        "refresh_token": "verify_setup_bogus_token_for_check",
        "grant_type": "refresh_token",
    }
    if client_secret:
        data["client_secret"] = client_secret
    req = urllib.request.Request(TOKEN_URL, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        body = urllib.parse.urlencode(data).encode("utf-8")
        with urllib.request.urlopen(req, data=body, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode("utf-8", errors="replace"))
        except Exception:
            err = {}
        code = err.get("error", "")
        desc = err.get("error_description", str(e))
        if code == "invalid_grant":
            ok("Client ID (and secret) accepted by Google — invalid_grant is expected here (bogus refresh token).")
            return True
        fail(f"Client rejected by Google: {code} — {desc}")
        if code == "invalid_client":
            fail("Hint: wrong client id/secret, or the client type is not 'Desktop app'.")
            if not client_secret:
                fail("Hint: this client type has a secret — set GMAIL_CLIENT_SECRET in .env.")
        return False
    except Exception as e:
        fail(f"Could not reach Google: {e}")
        return False

    ok(f"Token endpoint responded (unexpected: {payload}).")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify Gmail MCP .env setup")
    parser.add_argument("--live", action="store_true", help="run a live OAuth client-credentials check")
    args = parser.parse_args()

    passed = True
    env = load_env()

    print(f"{BOLD}1. .env file{RESET}")
    missing = env.pop("__missing__", None)
    if missing:
        fail(f"Not found at {missing}. Run: cp .env.example .env and edit GMAIL_CLIENT_ID")
        passed = False
    else:
        ok(f".env found with {len(env)} entries")

    print(f"{BOLD}2. GMAIL_CLIENT_ID{RESET}")
    client_id = env.get("GMAIL_CLIENT_ID", "")
    if not client_id:
        fail("Not set. Add GMAIL_CLIENT_ID=\"...apps.googleusercontent.com\" to .env")
        passed = False
    elif not CLIENT_ID_RE.match(client_id):
        fail(f"Malformed client id: {client_id!r}. Expected format: 1234....apps.googleusercontent.com")
        passed = False
    else:
        ok(f"{client_id}")

    print(f"{BOLD}3. GMAIL_CLIENT_SECRET (optional){RESET}")
    secret = env.get("GMAIL_CLIENT_SECRET", "")
    if secret and not SECRET_RE.match(secret):
        fail("Looks malformed (Google client secrets are ~24 alphanumeric chars).")
        passed = False
    elif secret:
        ok("Set (will be used in token requests).")
    else:
        warn("Not set — a Desktop-app client has a secret; recommended in .env.")

    print(f"{BOLD}4. Other settings{RESET}")
    full = env.get("GMAIL_FULL_ACCESS", "")
    if full not in ("", "0", "1"):
        fail(f"GMAIL_FULL_ACCESS={full!r} must be 0 or 1")
        passed = False
    else:
        if full == "1":
            warn("GMAIL_FULL_ACCESS=1 — adds mail.google.com scope (permanent delete).")
        ok("GMAIL_FULL_ACCESS valid")

    transport = env.get("GMAIL_TRANSPORT", "")
    if transport and transport not in ("stdio", "http", "sse", "streamable-http"):
        fail(f"GMAIL_TRANSPORT={transport!r} not a valid transport")
        passed = False
    else:
        ok("GMAIL_TRANSPORT valid" if transport else "GMAIL_TRANSPORT unset (defaults to stdio)")

    token_dir = env.get("GMAIL_TOKEN_DIR", "")
    if token_dir and not token_dir.startswith(("~", "/")):
        warn("GMAIL_TOKEN_DIR is relative; ~/.gmail-mcp default is recommended.")

    print(f"{BOLD}5. Environment & server import{RESET}")
    py = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv", "bin", "python")
    if not os.path.exists(py):
        warn("No .venv found — run: uv sync")
    else:
        try:
            subprocess.run(
                [py, "-c", "import server, gmail_client, gmail_auth; print('imports OK')"],
                cwd=os.path.dirname(os.path.abspath(__file__)),
                check=True, capture_output=True, text=True,
            )
            ok("server.py, gmail_client.py, gmail_auth.py import cleanly")
        except subprocess.CalledProcessError as e:
            fail(f"Import failed: {e.stderr.strip() or e.stdout.strip()}")
            passed = False

    if args.live and passed:
        passed = live_client_check(client_id, secret) and passed
    elif args.live:
        fail("Skipping live check because local checks failed.")

    print()
    if passed:
        print(f"{GREEN}{BOLD}All checks passed.{RESET} You can now run: uv run python server.py")
        return 0
    print(f"{RED}{BOLD}Some checks failed.{RESET} Fix the items above and re-run.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
