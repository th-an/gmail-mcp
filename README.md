# Gmail MCP Server

A Model Context Protocol (MCP) server that lets AI assistants (Claude Desktop, Claude Code, etc.) read, send and manage email in a Gmail mailbox via the **Gmail REST API**. It mirrors the tool surface of the [Outlook MCP server](https://github.com/th-an/outlook-mcp) and the [iCloud MCP server](https://github.com/th-an/icloud-mcp-multiaccount) using the Gmail API.

- **Personal Google accounts** — any Gmail / Google Workspace account
- **Auth**: OAuth2 **authorization-code flow** with a local loopback redirect + PKCE (Desktop-app client)
- **Transport**: stdio (works with Claude Desktop / Claude Code)
- Cross-platform (macOS / Windows / Linux / Docker)

## Features / Tools

The server exposes **60 MCP tools** across eight categories:

| Category | Tools |
|---|---|
| Connection | `get_server_info`, `test_email_connection`, `get_email_profile` |
| Read | `read_emails`, `read_full_email`, `get_unread_emails`, `search_emails`, `advanced_search_emails`, `get_email_thread`, `get_message_metadata`, `export_email_mime` |
| Send | `send_email`, `send_email_with_attachments`, `send_email_from_template` |
| Compose | `send_reply`, `forward_email`, `create_draft`, `edit_draft`, `send_draft`, `schedule_send`, `list_scheduled_sends`, `cancel_scheduled_send`, `flush_scheduled_sends`, `save_email_template`, `list_email_templates`, `delete_email_template` |
| Manage | `get_email_folders`, `mark_email_read`, `mark_email_unread`, `move_email`, `move_emails`, `copy_email`, `delete_email`, `permanent_delete_email`, `create_folder`, `rename_folder`, `delete_folder`, `list_message_ids`, `clear_folder`, `mark_folder_read`, `flag_email`, `follow_up_email`, `clear_email_flag`, `pin_email` |
| Attachments | `list_email_attachments`, `download_attachment`, `add_attachment_to_email`, `remove_attachment_from_email` |
| Automation | `triage_inbox`, `auto_organize`, `send_email_digest`, `email_analytics`, `dedupe_emails`, `batch_request` |
| Gmail extras | `star_email`, `unstar_email`, `mark_important`, `unmark_important`, `list_email_aliases`, `set_vacation_responder` |

Reading emails never marks them as read (Gmail `messages.get` does not change labels).

> **Folders vs labels**: Gmail organizes mail with **labels**, not folders. A message can carry several labels at once (`INBOX`, `SENT`, `DRAFT`, `TRASH`, `SPAM`, `STARRED`, `IMPORTANT`, `UNREAD`, custom labels). The tools accept a label name, path, or id wherever a "folder" is expected.

### Tool reference

#### Connection

**`get_server_info()`** → `dict`
Server name, signed-in account (`emailAddress`), mailbox totals, Gmail endpoint, token dir, and whether full access is enabled.

**`test_email_connection()`** → `dict`
Verifies connectivity and auth by fetching the current user profile.

**`get_email_profile()`** → `dict`
Signed-in account email, total messages/threads, and history id.

#### Read

**`read_emails(folder="INBOX", limit=5, full_content=False)`** → `list[dict]`
Reads emails from a label, newest first. `full_content=False` returns the Gmail snippet preview; `True` returns the full body. Never marks emails as read.

**`read_full_email(email_id, folder="INBOX")`** → `dict`
Reads a single email completely: full body (`body` + `body_html`), recipients (to/cc/bcc), date, labels, and attachment metadata.

**`get_unread_emails(folder="INBOX", limit=10)`** → `list[dict]`
Only unread emails (preview mode).

**`search_emails(query, folder="INBOX", limit=10)`** → `list[dict]`
Keyword search using native Gmail query syntax, e.g. `from:user is:unread`.

**`advanced_search_emails(query=None, folder="INBOX", limit=20, from_address=None, to_address=None, subject=None, after_date=None, before_date=None, unread_only=False, has_attachments=None)`** → `list[dict]`
Combines free-text `query` with structured filters AND-combined. Dates accept ISO 8601 or relative strings (`2h`, `3d`). Translated to Gmail `from:`/`to:`/`subject:`/`after:`/`before:`/`newer_than:`/`older_than:`/`is:unread`/`has:attachment`.

**`get_email_thread(email_id, limit=50)`** → `dict`
Returns the full conversation thread (`threadId`) with message summaries.

**`get_message_metadata(email_id)`** → `dict`
Metadata only (headers + labels), no body.

**`export_email_mime(email_id, folder="INBOX")`** → `dict`
Raw MIME message (`format=raw`) as a base64 string plus byte count.

#### Send

**`send_email(to, subject, body, cc=None, bcc=None, importance=None, read_receipt=None)`** → `dict`
Sends a plain email. Separate multiple recipients with commas. `importance`: `low|normal|high`. `read_receipt`: an address to request a read receipt from.

**`send_email_with_attachments(to, subject, body, cc=None, bcc=None, attachments=None, importance=None, read_receipt=None)`** → `dict`
Sends with base64 attachments — list of `{"content": <base64>, "filename": <name>, "content_type": <MIME, optional>}`. Max ~25MB.

**`send_email_from_template(template_name, to, cc=None, bcc=None, subject_override=None, body_replace=None)`** → `dict`
Sends using a saved template. `body_replace`: list of `{"from": ..., "to": ...}` replacements.

#### Compose

**`send_reply(email_id, body, reply_all=False, recipients=None)`** → `dict`
Replies to an email (sets `In-Reply-To`/`References`). `reply_all=True` replies to sender + all recipients.

**`forward_email(email_id, to, comment=None)`** → `dict`
Forwards an email to comma-separated recipients with an optional comment.

**`create_draft(to, subject, body, cc=None, bcc=None, importance=None, read_receipt=None)`** → `dict`
Creates an unsent draft; returns `draft_id` and `message_id`. Send later with `send_draft`.

**`edit_draft(draft_id, to=None, subject=None, body=None, cc=None, bcc=None)`** → `dict`
Updates only the fields provided. Pass an empty string to clear a field.

**`send_draft(draft_id)`** → `dict`
Sends a previously created draft.

**`schedule_send(to, subject, body, send_at, cc=None, bcc=None, importance=None)`** → `dict`
Schedules an email. `send_at` accepts ISO 8601 or relative (`30m`, `2h`, `1d`). Sent by an in-process worker while the server is running; the queue is persisted to disk so `flush_scheduled_sends()` can catch up overdue items after a restart.
> **Multi-instance caveat:** the queue lives in a per-instance local file (`$GMAIL_TOKEN_DIR/scheduled_sends.json`). Deploy with `--max-instances 1` for deterministic scheduling.

**`list_scheduled_sends()`** → `list[dict]` — pending scheduled emails.
**`cancel_scheduled_send(schedule_id)`** → `dict` — cancels a pending scheduled email.
**`flush_scheduled_sends()`** → `dict` — sends any now-due scheduled emails.

**`save_email_template(name, subject, body)`** → `dict` — saves a reusable template.
**`list_email_templates()`** → `list[dict]` — lists saved templates.
**`delete_email_template(name)`** → `dict` — deletes a template.

#### Manage

**`get_email_folders()`** → `list[dict]`
Lists all labels with `id`, `name`, `type` (system/user), `total`, `unread`, `threads_total`, `hidden`.

**`mark_email_read(email_id, folder="INBOX")`** → `dict` — removes the `UNREAD` label.
**`mark_email_unread(email_id, folder="INBOX")`** → `dict` — adds the `UNREAD` label.

**`move_email(email_id, source_folder, destination_folder)`** → `dict`
Moves between labels. Destination `Archive` removes `INBOX`/`SPAM`/`TRASH`. **Gmail message ids are stable across moves** (unlike Outlook).

**`move_emails(email_ids, source_folder, destination_folder)`** → `dict`
Batch-moves multiple emails in one call (Gmail `batchModify`).

**`copy_email(email_id, source_folder, destination_folder)`** → `dict`
Applies the destination label (Gmail has no physical copies).

**`delete_email(email_id, folder="INBOX")`** → `dict` — moves to Trash.
**`permanent_delete_email(email_id, folder="INBOX")`** → `dict` — hard-deletes; requires `GMAIL_FULL_ACCESS=1`.

**`create_folder(folder_name, parent_folder=None)`** → `dict` — creates a label (Gmail labels are flat; `parent_folder` is ignored).
**`rename_folder(folder_name, new_name)`** → `dict` — renames a label.
**`delete_folder(folder_name)`** → `dict` — deletes a label (messages are NOT deleted).
**`list_message_ids(folder="INBOX", limit=100)`** → `list[str]` — cheap id-only listing.
**`clear_folder(folder="INBOX", permanent=False)`** → `dict` — trashes all messages in a label (`permanent=True` hard-deletes, full-access scope).
**`mark_folder_read(folder="INBOX", limit=500)`** → `dict` — marks unread messages in a label as read.

**`flag_email(email_id, flag_status="flagged", due_date=None, start_date=None)`** → `dict`
Gmail has no follow-up flags; this stars/unstars. `flag_status`: `flagged|notFlagged`.
**`follow_up_email(email_id, due_date, start_date=None)`** → `dict` — convenience star.
**`clear_email_flag(email_id)`** → `dict` — removes the star.

**`pin_email(email_id, pinned=True)`** → `dict`
Pins/unpins via the `IMPORTANT` label.

#### Attachments

**`list_email_attachments(email_id, folder="INBOX")`** → `list[dict]` — attachment id, part id, filename, content type, size.
**`download_attachment(email_id, attachment_id, folder="INBOX")`** → `dict` — attachment content as base64.
**`add_attachment_to_email(email_id, content_base64, name, content_type=None, folder="INBOX", draft_id=None)`** → `dict` — adds an attachment to a **draft** (Gmail cannot edit sent messages).
**`remove_attachment_from_email(email_id, attachment_id, folder="INBOX", draft_id=None)`** → `dict` — removes an attachment from a **draft** (pass the filename/Content-ID from `list_email_attachments`).

> **Draft id churn:** Gmail regenerates a draft's *message id* on every update (`edit_draft`, `add_attachment_to_email`, `remove_attachment_from_email`), so a `message_id` goes stale after an edit. All draft operations return the fresh `draft_id` and `message_id` — pass the `draft_id` directly to the next draft operation (or rely on the built-in message-id → thread-id lookup).

#### Automation

**`triage_inbox(limit=50)`** → `dict` — classifies recent inbox emails into `reply_needed`, `priority`, `review`, `newsletter`, `read_later` buckets.
**`auto_organize(folder="INBOX", limit=100, move_to=None, mark_newsletters_read=False, archive_older_than=None)`** → `dict` — applies simple rules (move all, mark newsletters read, archive older than a relative age like `30d`).
**`send_email_digest(folder="INBOX", limit=20, to=None, subject=None)`** → `dict` — emails a digest of recent messages (defaults to yourself).
**`email_analytics(folder="INBOX", limit=500)`** → `dict` — totals, unread/attachment counts, top senders, messages per day, thread count.
**`dedupe_emails(folder="INBOX", limit=500, dry_run=True)`** → `dict` — finds duplicates (same subject + sender + received day). `dry_run=False` trashes the extras.
**`batch_request(requests)`** → `dict` — runs up to 100 Gmail API requests in one batch call. Each item: `{"id": 1, "method": "GET", "url": "/gmail/v1/users/me/messages", ...}`.

#### Gmail extras

**`star_email(email_id)`** / **`unstar_email(email_id)`** → `dict` — star management.
**`mark_important(email_id)`** / **`unmark_important(email_id)`** → `dict` — IMPORTANT label management.
**`list_email_aliases()`** → `list[dict]` — send-as aliases (`displayName`, `isDefault`, `isVerified`).
**`set_vacation_responder(enabled, subject=None, body=None)`** → `dict` — enables/disables the vacation auto-reply.

## Prerequisites

1. A Gmail / Google Workspace account.
2. A Google Cloud OAuth client (one-time, free):
   - [console.cloud.google.com](https://console.cloud.google.com) → create a project → enable the **Gmail API**
   - OAuth consent screen → **External**, add your own address as a test user
   - Credentials → Create OAuth client ID → **Desktop app**
   - Copy the **client ID** and **client secret**

## Quick Start

```bash
git clone <this-repo> && cd Gmail
cp .env.example .env   # edit GMAIL_CLIENT_ID if you made your own OAuth client
uv sync                # installs deps into .venv
uv run python verify_setup.py   # optional: checks your .env; add --live to test the client id against Google
uv run python server.py
```

On the **first API call**, the server opens the Google consent page in your default browser (and prints the URL to stderr). Sign in and approve the requested scopes; the redirect back to `http://127.0.0.1:8765/` is caught automatically by a local listener. Tokens are cached in `~/.gmail-mcp/token_cache.json` and refresh silently afterwards.

> **Why not device flow?** Google's OAuth2 device flow (TVs and Limited Input devices clients) does not support Gmail scopes — it returns `invalid_scope`. The Desktop-app client type allows loopback redirects without registering a redirect URI.

## Claude Desktop configuration

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "gmail-mail": {
      "command": "/absolute/path/to/Gmail/.venv/bin/python",
      "args": ["/absolute/path/to/Gmail/server.py"],
      "env": {
        "GMAIL_CLIENT_ID": "your-oauth-client-id.apps.googleusercontent.com"
      }
    }
  }
}
```

With Claude Code (CLI):

```bash
claude mcp add gmail-mail -- python /absolute/path/to/Gmail/server.py
```

## HTTP mode

The server can also run as a remote MCP endpoint (streamable-http):

```bash
uv run python server.py --transport streamable-http --host 127.0.0.1 --port 8000 --path /mcp
# or via env: GMAIL_TRANSPORT=streamable-http
```

## Cloud Run (remote deployment)

The server deploys to Google Cloud Run as a public streamable-http endpoint:

```bash
export GMAIL_CLIENT_ID=your-oauth-client-id.apps.googleusercontent.com
./deploy.sh   # uses gcloud run deploy --source .
```

- The Dockerfile bakes in a token cache at `/root/.gmail-mcp/token_cache.json` (see `Dockerfile`), so the deployed service authenticates to your mailbox without interactive sign-in. Re-seed that file (`token_cache.json`) with a real refresh token before rebuilding if the token is rotated.
- `--max-instances 1` is required for deterministic `schedule_send` (see caveat below).
- To seed `token_cache.json`: run the server locally, complete the loopback sign-in, then `cp ~/.gmail-mcp/token_cache.json ./token_cache.json` before building.

## Docker

```bash
docker build -t gmail-mcp-server .
mkdir -p ~/.gmail-mcp   # token cache lives here
docker run -i --rm \
  -v ~/.gmail-mcp:/root/.gmail-mcp \
  -e GMAIL_CLIENT_ID=your-oauth-client-id.apps.googleusercontent.com \
  gmail-mcp-server
```

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `GMAIL_CLIENT_ID` | — | Google OAuth client id (required) |
| `GMAIL_CLIENT_SECRET` | — | Client secret from the console (recommended for Desktop-app clients) |
| `GMAIL_TOKEN_DIR` | `~/.gmail-mcp` | Where the token cache is stored |
| `GMAIL_AUTH_PORT` | `8765` | Local port for the OAuth loopback callback |
| `GMAIL_FULL_ACCESS` | `0` | Set to `1` to add the `mail.google.com` scope, enabling `permanent_delete_email` and permanent `clear_folder` |
| `GMAIL_TRANSPORT` | `stdio` | Transport when `--transport` is not passed (`stdio`, `http`, `sse`, `streamable-http`) |
| `PORT` | `8000` | HTTP bind port for `--port` (Cloud Run injects this) |

## Security Notes

- Uses OAuth2 authorization-code flow with PKCE — no passwords are stored.
- Tokens are cached locally in `~/.gmail-mcp` (keep it private; it's in `.gitignore`).
- Default scopes: `gmail.modify`, `gmail.labels`, `gmail.settings.basic`. `GMAIL_FULL_ACCESS=1` adds the sensitive `https://mail.google.com/` scope (permanent delete).
- All traffic to the Gmail API is TLS-encrypted.
- `read_emails` uses no-read semantics: reading does not change labels.

## Architecture

- **FastMCP** for the MCP protocol
- **Custom OAuth2 authorization-code flow** (`gmail_auth.py`) — loopback redirect + PKCE, stdlib urllib, no Google client library
- **Gmail REST API** (`gmail_client.py`) — `gmail.googleapis.com/gmail/v1` for all mail operations, base64url RFC-2822 MIME
- **Transports**: stdio (default), `streamable-http`/`http`/`sse` for remote use

### Known limitations

- **Labels not folders**: moving "into" a folder is a label assignment; messages can live under several labels at once.
- **Permanent delete** requires the sensitive `mail.google.com` scope (opt in via `GMAIL_FULL_ACCESS=1`).
- **Refresh-token expiry**: Google "Testing" OAuth apps revoke refresh tokens after ~7 days; either publish the app or re-authenticate. Re-seed `token_cache.json` for containerized deploys.
- **Sent messages can't be edited**: `add_attachment_to_email` / `remove_attachment_from_email` operate on drafts only.
- **Schedule send** is per-instance (in-process worker + local queue); deterministic only on single-instance deployments.
- **No follow-up flags**: Gmail has stars and IMPORTANT instead; `flag_email` maps to starring.
- **Archive** is the absence of `INBOX`/`SPAM`/`TRASH`; `move_email`/`move_emails` handle it specially.

## License

MIT. Independent open-source project; not affiliated with Google.
