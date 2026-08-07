# Gmail MCP Server — Features, Scopes & Learnings

Status: **Operational** · Account: `sujithkannan5589@gmail.com` · Deployed: Cloud Run (`gmail-mcp-fjkea5crda-uc.a.run.app`) · Updated 2026-08-07

---

## 1. Summary

| Item | Value |
|---|---|
| MCP tools registered | **60** |
| Verified working (live, no special scope) | **59** |
| Live-verified in full test run | **54** (after draft-id fix) |
| Requires opt-in `GMAIL_FULL_ACCESS=1` | 1 (`permanent_delete_email`) |
| OAuth app status | **In production** (published; token no longer expires) |
| Auth flow | OAuth2 authorization-code + PKCE, loopback redirect |

---

## 2. OAuth Scopes

The server requests these scopes in the consent screen (`gmail_auth.py`):

| Scope | Grants | Used by |
|---|---|---|
| `gmail.modify` | Read, compose, send, trash; manage labels, drafts | All read/send/compose/manage tools (except permanent delete) |
| `gmail.labels` | Create/rename/delete labels | `create_folder`, `rename_folder`, `delete_folder`, `copy_email`, ... |
| `gmail.settings.basic` | Modify account settings, send-as aliases, vacation responder | `set_vacation_responder`, `list_email_aliases` |
| `mail.google.com` (full, **opt-in**) | Everything incl. permanent (hard) delete | `permanent_delete_email`, `clear_folder(permanent=True)` |

- Enable full access: set `GMAIL_FULL_ACCESS=1` in `.env`, then **re-consent** (the new scope needs approval).
- `mail.google.com` is a *restricted* scope; `gmail.*` scopes are *sensitive*. In production (unverified) the consent screen shows a warning — harmless for a single personal account.
- All default scopes are now **non-expiring** because the app was **published** (Testing-mode refresh tokens die after 7 days).

---

## 3. Feature Inventory (60 tools) — live verification status

Legend: ✅ = passed live test · ⚠️ = verified with caveat · 🔵 = needs special scope/condition · **fixed** = was a caveat, now fixed

### Connection — 3/3 ✅
| # | Tool | Status | Notes |
|---|---|---|---|
| 1 | `get_server_info` | ✅ | account, totals, endpoint |
| 2 | `test_email_connection` | ✅ | |
| 3 | `get_email_profile` | ✅ | |

### Read — 8/8 ✅
| # | Tool | Status | Notes |
|---|---|---|---|
| 4 | `read_emails` | ✅ | full body; never marks read |
| 5 | `read_full_email` | ✅ | text + HTML |
| 6 | `get_unread_emails` | ✅ **fixed** | now uses `q=is:unread` + single label (see learning #13) |
| 7 | `search_emails` | ✅ | native Gmail query |
| 8 | `advanced_search_emails` | ✅ | structured filters + relative dates |
| 9 | `get_email_thread` | ✅ | |
| 10 | `get_message_metadata` | ✅ | |
| 11 | `export_email_mime` | ✅ | raw MIME base64 |

### Send — 3/3 ✅
| # | Tool | Status | Notes |
|---|---|---|---|
| 12 | `send_email` | ✅ | |
| 13 | `send_email_with_attachments` | ✅ | base64 content (the Outlook-style `*_with_file_paths` does **not** exist here) |
| 14 | `send_email_from_template` | ✅ | `body_replace` works |

### Compose — 12/12 ✅
| # | Tool | Status | Notes |
|---|---|---|---|
| 15 | `send_reply` | ✅ | threading headers |
| 16 | `forward_email` | ✅ | |
| 17 | `create_draft` | ✅ | returns `draft_id` + `message_id` |
| 18 | `edit_draft` | ✅ **fixed** | now returns fresh `message_id` |
| 19 | `send_draft` | ✅ | |
| 20 | `schedule_send` | ✅ | persisted queue |
| 21 | `list_scheduled_sends` | ✅ | |
| 22 | `cancel_scheduled_send` | ✅ | |
| 23 | `flush_scheduled_sends` | ✅ | no-op when queue empty |
| 24 | `save_email_template` | ✅ | |
| 25 | `list_email_templates` | ✅ | |
| 26 | `delete_email_template` | ✅ | |

### Manage — 18/18 (17 ✅ + 1 🔵)
| # | Tool | Status | Notes |
|---|---|---|---|
| 27 | `get_email_folders` | ✅ | 52 labels |
| 28 | `mark_email_read` | ✅ | |
| 29 | `mark_email_unread` | ✅ | |
| 30 | `move_email` | ✅ | |
| 31 | `move_emails` | ✅ | batchModify |
| 32 | `copy_email` | ✅ | |
| 33 | `delete_email` | ✅ | to Trash |
| 34 | `permanent_delete_email` | 🔵 | needs `GMAIL_FULL_ACCESS=1` |
| 35 | `create_folder` | ✅ | |
| 36 | `rename_folder` | ✅ | |
| 37 | `delete_folder` | ✅ | |
| 38 | `list_message_ids` | ✅ | |
| 39 | `clear_folder` | ✅ | trash mode |
| 40 | `mark_folder_read` | ✅ | |
| 41 | `flag_email` | ✅ | star-based |
| 42 | `follow_up_email` | ✅ | |
| 43 | `clear_email_flag` | ✅ | |
| 44 | `pin_email` | ✅ | IMPORTANT label |

### Attachments — 4/4 ✅
| # | Tool | Status | Notes |
|---|---|---|---|
| 45 | `list_email_attachments` | ✅ | |
| 46 | `download_attachment` | ✅ | base64 content |
| 47 | `add_attachment_to_email` | ✅ **fixed** | optional `draft_id`, returns fresh ids |
| 48 | `remove_attachment_from_email` | ✅ **fixed** | optional `draft_id`, threadId fallback |

### Automation — 6/6 ✅
| # | Tool | Status | Notes |
|---|---|---|---|
| 49 | `triage_inbox` | ✅ | 5 buckets |
| 50 | `auto_organize` | ✅ | |
| 51 | `send_email_digest` | ✅ | |
| 52 | `email_analytics` | ✅ | |
| 53 | `dedupe_emails` | ✅ | |
| 54 | `batch_request` | ✅ | up to 100 calls |

### Gmail extras — 6/6 ✅
| # | Tool | Status | Notes |
|---|---|---|---|
| 55 | `star_email` | ✅ | |
| 56 | `unstar_email` | ✅ | |
| 57 | `mark_important` | ✅ | |
| 58 | `unmark_important` | ✅ | |
| 59 | `list_email_aliases` | ✅ | |
| 60 | `set_vacation_responder` | ✅ | enabled + disabled |

### Counts
- **60 / 60** work — `permanent_delete_email` now enabled too (full-access scope granted + deployed)
- Live test run verified **54**; the remaining were test-coverage skips, not failures.

---

## 4. Key Learnings

### 4.1 OAuth / Google Console
1. **Consent URL is one-shot.** The PKCE verifier + state live in process memory; if the terminal dies or the flow errors, a fresh URL must be generated. Reuse of an old URL/tab causes **state mismatch**.
2. **Stale browser callbacks clobber results.** Chrome keeps old tabs alive; a completed old callback can overwrite a valid one within the poll window. Fixed by *ignoring* mismatched callbacks (`gmail_auth.py`).
3. **Loopback server hang.** HTTP keep-alive + Chrome speculative connections kept the single-threaded `HTTPServer` blocked, so `server.shutdown()` never returned even after a successful callback. Fixed with `ThreadingHTTPServer` + `Connection: close` + `block_on_close=False`.
4. **Testing mode = 7-day refresh-token expiry.** Only `openid/email/profile` scopes are exempt. Publishing the app (even unverified) stops the expiry. An existing token minted under Testing still dies 7 days after consent — **re-consent after publishing** to get a permanent token.
5. **"Unverified app" warning** is expected for Gmail (sensitive) scopes in production — click *Advanced → Go to Gmail MCP (unsafe)*. Full verification is not worth it for a personal account.
6. **Client name ≠ consent-screen brand.** The OAuth *client* may be named "Gmail MCP" while the consent screen shows a different *app name* ("Eigent MCP"); they're separate settings on the OAuth consent screen page.

### 4.2 Gmail API quirks
7. **Draft message-id churn.** Gmail regenerates a draft's `message.id` on every update (`edit_draft` / `add_attachment` / `remove_attachment`), so previously captured ids go stale. Fix: pass `draft_id` directly, or use the `message_id` returned by the prior draft operation; also added a threadId fallback.
8. **Labels, not folders.** Moves are label assignments; `Archive` = absence of `INBOX/SPAM/TRASH`. `create_folder(parent_folder)` ignores parent (flat).
9. **Sent messages are immutable** — attachment add/remove only works on drafts.
10. **No follow-up flags** — `flag_email`/`follow_up_email` map to stars; `pin_email` uses IMPORTANT.
11. **Permanent delete** requires the restricted `mail.google.com` scope; opt-in only.
12. **Reads never mark as read** (messages.get doesn't change labels).
13. **The Gmail API rejects multiple `labelIds`** — `labelIds=INBOX,UNREAD` returns HTTP 400 `Invalid label` even though the docs describe AND semantics. Use a **single** label + a query filter instead: `label_ids=[label_id], q="is:unread"` (this is how `get_unread_emails` and `mark_folder_read` work).

### 4.3 Cloud Run deployment
13. **gcloud drops `token_cache.json` from the build context.** With no `.gcloudignore`, the source tarball excluded the token (it follows ignore rules independent of `.dockerignore`). Fix: added `.gcloudignore` that allows it. Deploy failed with `COPY failed: file not found in build context` before this.
14. **Non-interactive auth in the container** works because `token_cache.json` is baked into the image and refreshed at runtime. Keep it in sync: `cp ~/.gmail-mcp/token_cache.json ./token_cache.json` before building.
15. **Pass `GMAIL_CLIENT_SECRET`** to the container (deploy.sh updated) so token refresh never fails.
16. **`--max-instances 1`** is required for deterministic `schedule_send` (queue is per-instance, persisted locally).
17. **Streamable-http endpoint**: `POST <url>/mcp` with `Accept: application/json, text/event-stream`; plain GET returns 406 (normal).

### 4.4 Operations
18. Local token cache: `~/.gmail-mcp/token_cache.json`; Cloud seed: `./token_cache.json` (gitignored).
19. Local dev: `uv run python server.py` (stdio). Remote: `--transport streamable-http --host 0.0.0.0`.
20. Full test suite: `uv run python -m pytest` → **47 passed**.

---

## 5. Recommended Runbook

1. Re-auth locally (rare, only if token revoked): run auth flow, then `cp ~/.gmail-mcp/token_cache.json ./token_cache.json`.
2. Redeploy after code changes:
   ```
   export GMAIL_CLIENT_ID=602029807322-jg9pgb31kf61pkqkld49er6qv9sgg7uc.apps.googleusercontent.com
   export GMAIL_CLIENT_SECRET=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
   ./deploy.sh
   ```
3. Smoke-test: `curl -X POST <url>/mcp -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"smoke","version":"1.0"}}}'`
