# Changelog

All notable changes to the Gmail MCP Server are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-08-08

First tagged release. The server exposes **60 MCP tools** covering read, send, compose,
manage, attachments, automation, and Gmail-specific extras via the Gmail REST API.

### Added

- 60 MCP tools across eight categories (connection, read, send, compose, manage,
  attachments, automation, Gmail extras). Full tool reference in `README.md`.
- OAuth2 **authorization-code flow** with loopback redirect + PKCE (`gmail_auth.py`),
  implemented on the standard library — no Google client library.
- `GMAIL_FULL_ACCESS=1` opt-in for the sensitive `https://mail.google.com/` scope,
  enabling `permanent_delete_email` and permanent `clear_folder`.
- Draft lifecycle tools: `create_draft`, `edit_draft`, `send_draft`, plus
  `add_attachment_to_email` / `remove_attachment_from_email`.
- Schedule-send suite: `schedule_send`, `list_scheduled_sends`,
  `cancel_scheduled_send`, `flush_scheduled_sends` (single-instance worker queue).
- Template, triage, auto-organize, digest, analytics, and dedupe automation tools.
- `GMAIL_TRANSPORT` env var + `--transport` / `--port` flags (stdio, http, sse,
  streamable-http) for local or Cloud Run deployments.
- Dockerfile, `deploy.sh` (Cloud Run), `.gcloudignore`, and seeded `token_cache.json`
  support for non-interactive containerized auth.
- 47 pytest tests under `tests/`.

### Fixed

- **`get_unread_emails`**: Gmail API rejects multiple `labelIds` (`INBOX,UNREAD` →
  HTTP 400). Now uses `q="is:unread"` with a single label.
- **Draft attachment churn**: Gmail regenerates a draft's *message id* on every
  update, so previously captured ids went stale and chained draft operations broke.
  `add_attachment_to_email` / `remove_attachment_from_email` now accept an optional
  `draft_id`, return the fresh `message_id`, and resolve drafts via `threadId`
  fallback; `edit_draft` returns both `draft_id` and `message_id`.
- **Non-ASCII body edits**: editing a draft body containing non-ASCII characters
  (em dash, accents, symbols) raised `UnicodeEncodeError` from a stale
  `Content-Transfer-Encoding: base64` header. Body replacement now re-encodes the
  part as `charset=utf-8` 8-bit MIME.
- **OAuth loopback keep-alive**: the local callback server hung after redirect;
  switched to `ThreadingHTTPServer` with `Connection: close`.
- **Stale OAuth callbacks**: an old browser tab completing auth could overwrite a
  newer valid token; mismatched state callbacks are now ignored and logged.

### Security

- Tokens cached under `~/.gmail-mcp` (git-ignored); no passwords stored.
- Default scopes `gmail.modify`, `gmail.labels`, `gmail.settings.basic`; full-access
  scope is strictly opt-in.
- Reading emails never marks them as read.

[1.0.0]: https://github.com/th-an/gmail-mcp/releases/tag/v1.0.0
