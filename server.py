#!/usr/bin/env python3
"""Gmail MCP Server.

A Model Context Protocol (MCP) server that lets AI assistants (Claude Desktop,
Claude Code, etc.) interact with a Gmail mailbox through the Gmail REST API.
Mirrors the tool surface of the Outlook MCP server (th-an/outlook-mcp) using the
Gmail API instead of Microsoft Graph.

Auth: OAuth2 authorization-code flow (Google) with a loopback redirect. The
first call opens the consent page in the browser and captures the callback on
127.0.0.1; afterwards tokens refresh silently from a cached refresh token.
"""

import argparse
import base64
import email
import json
import mimetypes
import os
import re
import sys
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from email.encoders import encode_base64
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from typing import Any, Dict, List, Optional

from fastmcp import FastMCP

from gmail_client import GmailClient, GmailError, b64url_decode, b64url_encode

DEFAULT_FOLDER = "INBOX"
PREVIEW_LIMIT = 200

__version__ = "1.0.0"

mcp = FastMCP("Gmail MCP Server (Gmail REST API)", version=__version__)

_client_singleton: Optional[GmailClient] = None
_state_lock = threading.Lock()


def _client() -> GmailClient:
    global _client_singleton
    if _client_singleton is None:
        _client_singleton = GmailClient()
    return _client_singleton


# --------------------------------------------------------------------------
# MIME helpers
# --------------------------------------------------------------------------

_HTML_RE = re.compile(r"<(p|div|br|span|table|ul|ol|li|a|h[1-6]|b|i|u|strong|em)[ >]", re.I)


def _looks_html(body: str) -> bool:
    return bool(_HTML_RE.search(body)) or body.strip().lower().startswith("<html")


def _header(headers: List[Dict[str, str]], name: str) -> str:
    for h in headers or []:
        if (h.get("name") or "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _walk_parts(
    payload: Dict[str, Any],
    text_parts: List[str],
    html_parts: List[str],
    attachments: List[Dict[str, Any]],
) -> None:
    body = payload.get("body") or {}
    data = body.get("data")
    if data:
        decoded = b64url_decode(data).decode("utf-8", errors="replace")
        mime_type = (payload.get("mimeType") or "").lower()
        if mime_type == "text/plain":
            text_parts.append(decoded)
        elif mime_type == "text/html":
            html_parts.append(decoded)
    if payload.get("filename"):
        attachments.append(_attachment_info(payload))
    for part in payload.get("parts") or []:
        _walk_parts(part, text_parts, html_parts, attachments)


def _attachment_info(part: Dict[str, Any]) -> Dict[str, Any]:
    body = part.get("body") or {}
    return {
        "attachment_id": body.get("attachmentId", ""),
        "part_id": part.get("partId", ""),
        "filename": part.get("filename", ""),
        "content_type": part.get("mimeType", ""),
        "size": body.get("size", 0),
    }


def _extract_text(message: email.message.Message) -> str:
    if message.is_multipart():
        text: List[str] = []
        for part in message.walk():
            if part.get_content_type() == "text/plain":
                try:
                    text.append(part.get_payload(decode=True).decode("utf-8", errors="replace"))
                except Exception:
                    text.append(str(part.get_payload()))
        return "\n".join(text) if text else ""
    try:
        return message.get_payload(decode=True).decode("utf-8", errors="replace")
    except Exception:
        return str(message.get_payload())


def _quote_original(raw: bytes, body: str) -> str:
    msg = email.message_from_bytes(raw)
    frm = msg.get("From", "")
    date = msg.get("Date", "")
    original = _extract_text(msg).strip()
    quote = f"On {date}, {frm} wrote:\n{original}" if original else ""
    return f"{body}\n\n----------\n{quote}".rstrip()


def _build_attachment(att: Dict[str, Any]) -> MIMEBase:
    content = att.get("content", "")
    name = att.get("filename", "")
    content_type = att.get("content_type") or mimetypes.guess_type(name)[0] or "application/octet-stream"
    main, sub = (content_type.split("/", 1) + ["octet-stream"])[:2]
    part = MIMEBase(main, sub)
    try:
        part.set_payload(base64.b64decode(content))
    except Exception:
        part.set_payload(content.encode("utf-8"))
    part.add_header("Content-Disposition", "attachment", filename=name)
    encode_base64(part)
    return part


def _build_raw_message(
    to: str,
    subject: str,
    body: str,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    html: bool = False,
    importance: Optional[str] = None,
    read_receipt: Optional[str] = None,
    extra_headers: Optional[Dict[str, str]] = None,
    attachments: Optional[List[Dict[str, Any]]] = None,
    from_addr: Optional[str] = None,
) -> bytes:
    if attachments:
        root = MIMEMultipart("mixed")
        alt = MIMEMultipart("alternative")
        root.attach(alt)
        alt.attach(MIMEText(body, "html" if html else "plain", "utf-8"))
        for att in attachments:
            root.attach(_build_attachment(att))
        msg = root
    else:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(body, "html" if html else "plain", "utf-8"))

    if from_addr:
        msg["From"] = from_addr
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    if bcc:
        msg["Bcc"] = bcc
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    if importance:
        msg["Importance"] = importance
        msg["X-Priority"] = {"high": "1", "normal": "3", "low": "5"}.get(importance, "3")
    if read_receipt:
        msg["Disposition-Notification-To"] = read_receipt
    for k, v in (extra_headers or {}).items():
        if v:
            msg[k] = v
    return msg.as_bytes()


def _replace_body(msg: email.message.Message, body: str) -> None:
    html = _looks_html(body)
    target = "html" if html else "plain"
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == f"text/{target}":
                part.set_payload(body, charset="utf-8")
                part.replace_header("Content-Type", f"text/{target}; charset=utf-8")
                if "Content-Transfer-Encoding" in part:
                    del part["Content-Transfer-Encoding"]
                return
        msg.attach(MIMEText(body, target, "utf-8"))
    else:
        msg.set_payload(body, charset="utf-8")
        if "Content-Transfer-Encoding" in msg:
            del msg["Content-Transfer-Encoding"]
        msg.set_type(f"text/{target}")


def _apply_draft_edits(
    raw_b64: str,
    to: Optional[str] = None,
    subject: Optional[str] = None,
    body: Optional[str] = None,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
) -> str:
    msg = email.message_from_bytes(b64url_decode(raw_b64))
    if subject is not None:
        del msg["Subject"]
        msg["Subject"] = subject
    for header, value in (("To", to), ("Cc", cc), ("Bcc", bcc)):
        if value is not None:
            del msg[header]
            if value:
                msg[header] = value
    if body is not None:
        _replace_body(msg, body)
    return b64url_encode(msg.as_bytes())


def _mime_attach(raw_b64: str, content_base64: str, name: str, content_type: Optional[str]) -> str:
    msg = email.message_from_bytes(b64url_decode(raw_b64))
    att = _build_attachment(
        {"content": content_base64, "filename": name, "content_type": content_type}
    )
    if msg.is_multipart():
        msg.attach(att)
    else:
        root = MIMEMultipart("mixed")
        for header, value in msg.items():
            if header.lower() not in ("content-type", "content-transfer-encoding", "mime-version"):
                root[header] = value
        root.attach(msg)
        root.attach(att)
        msg = root
    return b64url_encode(msg.as_bytes())


def _mime_detach(raw_b64: str, attachment_ref: str) -> str:
    msg = email.message_from_bytes(b64url_decode(raw_b64))
    removed = False
    if msg.is_multipart():
        kept = []
        for part in msg.get_payload():
            if isinstance(part, str):
                kept.append(part)
                continue
            filename = part.get_filename() or ""
            content_id = part.get("Content-ID") or ""
            if attachment_ref and attachment_ref in (filename, content_id.strip("<>")):
                removed = True
                continue
            kept.append(part)
        if removed:
            msg.set_payload(kept)
    if not removed:
        raise GmailError(
            "Attachment not found in draft. Pass the filename (or Content-ID) from "
            "list_email_attachments."
        )
    return b64url_encode(msg.as_bytes())


# --------------------------------------------------------------------------
# date / folder helpers
# --------------------------------------------------------------------------

def _parse_datetime(value: str) -> Optional[datetime]:
    value = value.strip()
    m = re.fullmatch(r"(\d+)\s*([smhdw])", value, flags=re.IGNORECASE)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        td = {
            "s": timedelta(seconds=n),
            "m": timedelta(minutes=n),
            "h": timedelta(hours=n),
            "d": timedelta(days=n),
            "w": timedelta(weeks=n),
        }[unit]
        return datetime.now(timezone.utc) + td
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%Y/%m/%d",
    ):
        try:
            dt = datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def _parse_gmail_relative(value: str) -> Optional[str]:
    m = re.fullmatch(r"(\d+)\s*([smhdw])", value.strip(), flags=re.IGNORECASE)
    if m:
        return f"{m.group(1)}{m.group(2).lower()}"
    return None


WELL_KNOWN_LABELS = {
    "INBOX": "INBOX",
    "SENT": "SENT",
    "SENT ITEMS": "SENT",
    "DRAFT": "DRAFT",
    "DRAFTS": "DRAFT",
    "TRASH": "TRASH",
    "DELETED": "TRASH",
    "DELETED ITEMS": "TRASH",
    "SPAM": "SPAM",
    "JUNK": "SPAM",
    "JUNK EMAIL": "SPAM",
    "STARRED": "STARRED",
    "IMPORTANT": "IMPORTANT",
    "UNREAD": "UNREAD",
    "ALL MAIL": "ALL_MAIL",
    "ALL": "ALL_MAIL",
}


def _resolve_label(client: GmailClient, folder: str) -> str:
    key = folder.strip().upper()
    if key in WELL_KNOWN_LABELS:
        return WELL_KNOWN_LABELS[key]
    label_id = client.resolve_label_id(folder)
    if label_id is None:
        raise GmailError(f"Label/folder '{folder}' not found")
    return label_id


def _is_archive(folder: str) -> bool:
    return folder.strip().upper() in ("ARCHIVE", "ALL_MAIL", "ALL MAIL")


# --------------------------------------------------------------------------
# message summaries / state
# --------------------------------------------------------------------------

def _message_summary(message: Dict[str, Any], full: bool = False) -> Dict[str, Any]:
    payload = message.get("payload") or {}
    labels = message.get("labelIds") or []
    internal = message.get("internalDate") or ""
    date_iso = ""
    if internal:
        try:
            date_iso = datetime.fromtimestamp(int(internal) / 1000, tz=timezone.utc).isoformat()
        except Exception:
            date_iso = str(internal)
    text_parts: List[str] = []
    html_parts: List[str] = []
    attachments: List[Dict[str, Any]] = []
    _walk_parts(payload, text_parts, html_parts, attachments)
    body = "".join(text_parts) or "".join(html_parts)
    preview = message.get("snippet") or body
    return {
        "id": message.get("id", ""),
        "thread_id": message.get("threadId", ""),
        "from": _header(payload.get("headers"), "From"),
        "to": _header(payload.get("headers"), "To"),
        "cc": _header(payload.get("headers"), "Cc"),
        "bcc": _header(payload.get("headers"), "Bcc"),
        "subject": _header(payload.get("headers"), "Subject") or "No Subject",
        "date": date_iso,
        "unread": "UNREAD" in labels,
        "labels": labels,
        "has_attachments": bool(attachments),
        "body": body if full else (preview[:PREVIEW_LIMIT] + "..." if len(preview) > PREVIEW_LIMIT else preview),
    }


def _state_dir() -> str:
    return os.getenv("GMAIL_TOKEN_DIR", os.path.expanduser("~/.gmail-mcp"))


def _scheduled_path() -> str:
    return os.path.join(_state_dir(), "scheduled_sends.json")


def _load_scheduled() -> List[Dict[str, Any]]:
    try:
        if os.path.exists(_scheduled_path()):
            with open(_scheduled_path(), "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        sys.stderr.write(f"WARNING: could not load scheduled sends: {e}\n")
        sys.stderr.flush()
    return []


def _save_scheduled(items: List[Dict[str, Any]]) -> None:
    with _state_lock:
        try:
            os.makedirs(_state_dir(), exist_ok=True)
            with open(_scheduled_path(), "w", encoding="utf-8") as f:
                json.dump(items, f)
        except Exception as e:
            sys.stderr.write(f"WARNING: could not save scheduled sends: {e}\n")
            sys.stderr.flush()


def _remove_scheduled(item_id: str) -> None:
    items = _load_scheduled()
    _save_scheduled([i for i in items if i.get("id") != item_id])


def _templates_path() -> str:
    return os.path.join(_state_dir(), "email_templates.json")


def _load_templates() -> Dict[str, Dict[str, Any]]:
    try:
        if os.path.exists(_templates_path()):
            with open(_templates_path(), "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        sys.stderr.write(f"WARNING: could not load templates: {e}\n")
        sys.stderr.flush()
    return {}


def _save_templates(templates: Dict[str, Dict[str, Any]]) -> None:
    with _state_lock:
        try:
            os.makedirs(_state_dir(), exist_ok=True)
            with open(_templates_path(), "w", encoding="utf-8") as f:
                json.dump(templates, f)
        except Exception as e:
            sys.stderr.write(f"WARNING: could not save templates: {e}\n")
            sys.stderr.flush()


def _default_from() -> str:
    try:
        return _client().get_profile().get("emailAddress", "")
    except Exception:
        return ""


def _send(
    to: str,
    subject: str,
    body: str,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    importance: Optional[str] = None,
    read_receipt: Optional[str] = None,
    attachments: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    html = _looks_html(body)
    raw = _build_raw_message(
        to,
        subject,
        body,
        cc=cc,
        bcc=bcc,
        html=html,
        importance=importance,
        read_receipt=read_receipt,
        attachments=attachments,
        from_addr=_default_from(),
    )
    return _client().send_message(b64url_encode(raw))


def _ok(status="success", **extra) -> Dict[str, str]:
    result: Dict[str, str] = {"status": status}
    result.update({k: str(v) for k, v in extra.items()})
    return result


def _err(message: str) -> Dict[str, str]:
    return {"status": "error", "message": message}


def _suggested_action(m: Dict[str, Any]) -> str:
    subject = (m.get("subject") or "").strip()
    sender = (m.get("from") or "").lower()
    low_subject = subject.lower()
    if low_subject.startswith("re:"):
        return "reply_needed"
    if "IMPORTANT" in (m.get("labels") or []):
        return "priority"
    if m.get("has_attachments"):
        return "review"
    if any(k in low_subject or k in sender for k in ("unsubscribe", "newsletter", "digest", "promotions", "no-reply", "no_reply")):
        return "newsletter"
    return "read_later"


# --------------------------------------------------------------------------
# tools: connection
# --------------------------------------------------------------------------

@mcp.tool()
def get_server_info() -> Dict[str, Any]:
    """Return server name, signed-in Gmail account, API endpoint, and config."""
    try:
        profile = _client().get_profile()
        return {
            "name": "Gmail MCP Server (Gmail REST API)",
            "version": __version__,
            "account": profile.get("emailAddress", ""),
            "messages_total": profile.get("messagesTotal", 0),
            "threads_total": profile.get("threadsTotal", 0),
            "endpoint": "https://gmail.googleapis.com/gmail/v1",
            "token_dir": _state_dir(),
            "full_access": os.getenv("GMAIL_FULL_ACCESS") == "1",
        }
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def test_email_connection() -> Dict[str, str]:
    """Verify Gmail API connectivity and auth by fetching the current user profile."""
    try:
        profile = _client().get_profile()
        return {
            "status": "success",
            "message": f"Connected to Gmail as {profile.get('emailAddress', 'unknown')}",
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@mcp.tool()
def get_email_profile() -> Dict[str, Any]:
    """Return the signed-in Gmail account profile and mailbox totals."""
    try:
        profile = _client().get_profile()
        return {
            "email_address": profile.get("emailAddress", ""),
            "messages_total": profile.get("messagesTotal", 0),
            "threads_total": profile.get("threadsTotal", 0),
            "history_id": profile.get("historyId", ""),
        }
    except Exception as e:
        return _err(str(e))


# --------------------------------------------------------------------------
# tools: read
# --------------------------------------------------------------------------

@mcp.tool()
def read_emails(folder: str = DEFAULT_FOLDER, limit: int = 5, full_content: bool = False) -> List[Dict[str, Any]]:
    """Read emails from a folder/label, newest first. Never marks them as read.

    folder: label name, path, or id (default INBOX). limit: max messages.
    full_content: False returns a preview (snippet); True returns the full body.
    """
    try:
        client = _client()
        label_id = _resolve_label(client, folder)
        resp = client.list_messages(label_ids=[label_id], max_results=min(limit, 500))
        result: List[Dict[str, Any]] = []
        for item in resp.get("messages") or []:
            try:
                msg = client.get_message(item["id"], "full")
                result.append(_message_summary(msg, full=full_content))
            except Exception as e:
                result.append({"id": item.get("id", ""), "error": str(e)})
        return result
    except Exception as e:
        return [_err(str(e))]


@mcp.tool()
def read_full_email(email_id: str, folder: str = DEFAULT_FOLDER) -> Dict[str, Any]:
    """Read a single email completely: full body, recipients, date, labels, attachments."""
    try:
        msg = _client().get_message(email_id, "full")
        summary = _message_summary(msg, full=True)
        text_parts: List[str] = []
        html_parts: List[str] = []
        attachments: List[Dict[str, Any]] = []
        _walk_parts(msg.get("payload") or {}, text_parts, html_parts, attachments)
        summary["body_html"] = "".join(html_parts)
        summary["attachments"] = attachments
        return summary
    except Exception as e:
        return _err(f"Failed to read email: {str(e)}")


@mcp.tool()
def get_unread_emails(folder: str = DEFAULT_FOLDER, limit: int = 10) -> List[Dict[str, Any]]:
    """Return only unread emails (preview mode) from a folder."""
    try:
        client = _client()
        label_id = _resolve_label(client, folder)
        resp = client.list_messages(
            q="is:unread", label_ids=[label_id], max_results=min(limit, 500)
        )
        result: List[Dict[str, Any]] = []
        for item in resp.get("messages") or []:
            try:
                msg = client.get_message(item["id"], "full")
                result.append(_message_summary(msg))
            except Exception as e:
                result.append({"id": item.get("id", ""), "error": str(e)})
        return result
    except Exception as e:
        return [_err(str(e))]


@mcp.tool()
def search_emails(query: str, folder: str = DEFAULT_FOLDER, limit: int = 10) -> List[Dict[str, Any]]:
    """Search emails by keyword (Gmail search syntax, e.g. 'from:user is:unread')."""
    try:
        client = _client()
        label_id = _resolve_label(client, folder)
        resp = client.list_messages(
            q=query, label_ids=[label_id], max_results=min(limit, 500)
        )
        result: List[Dict[str, Any]] = []
        for item in resp.get("messages") or []:
            try:
                msg = client.get_message(item["id"], "full")
                result.append(_message_summary(msg))
            except Exception as e:
                result.append({"id": item.get("id", ""), "error": str(e)})
        return result
    except Exception as e:
        return [_err(str(e))]


def _build_gmail_query(
    query: Optional[str] = None,
    from_address: Optional[str] = None,
    to_address: Optional[str] = None,
    subject: Optional[str] = None,
    after_date: Optional[str] = None,
    before_date: Optional[str] = None,
    unread_only: bool = False,
    has_attachments: Optional[bool] = None,
) -> str:
    parts: List[str] = []
    if query:
        parts.append(query)
    if from_address:
        parts.append(f"from:({from_address})")
    if to_address:
        parts.append(f"to:({to_address})")
    if subject:
        parts.append(f"subject:({subject})")
    if unread_only:
        parts.append("is:unread")
    if has_attachments is not None:
        parts.append("has:attachment" if has_attachments else "-has:attachment")
    if after_date:
        rel = _parse_gmail_relative(after_date)
        if rel:
            parts.append(f"newer_than:{rel}")
        else:
            dt = _parse_datetime(after_date)
            if dt:
                parts.append(f"after:{dt.strftime('%Y/%m/%d')}")
    if before_date:
        rel = _parse_gmail_relative(before_date)
        if rel:
            parts.append(f"older_than:{rel}")
        else:
            dt = _parse_datetime(before_date)
            if dt:
                parts.append(f"before:{dt.strftime('%Y/%m/%d')}")
    return " ".join(p for p in parts if p)


@mcp.tool()
def advanced_search_emails(
    query: Optional[str] = None,
    folder: str = DEFAULT_FOLDER,
    limit: int = 20,
    from_address: Optional[str] = None,
    to_address: Optional[str] = None,
    subject: Optional[str] = None,
    after_date: Optional[str] = None,
    before_date: Optional[str] = None,
    unread_only: bool = False,
    has_attachments: Optional[bool] = None,
) -> List[Dict[str, Any]]:
    """Combine free-text query with structured filters (Gmail search syntax).

    Dates accept ISO 8601 or relative strings ('2h', '3d'). Filters are AND-combined.
    """
    try:
        client = _client()
        q = _build_gmail_query(
            query=query,
            from_address=from_address,
            to_address=to_address,
            subject=subject,
            after_date=after_date,
            before_date=before_date,
            unread_only=unread_only,
            has_attachments=has_attachments,
        )
        if not q and folder in ("", None):
            return [_err("Provide at least one search filter")]
        label_id = _resolve_label(client, folder) if folder else None
        resp = client.list_messages(
            q=q, label_ids=[label_id] if label_id else None, max_results=min(limit, 500)
        )
        result: List[Dict[str, Any]] = []
        for item in resp.get("messages") or []:
            try:
                msg = client.get_message(item["id"], "full")
                result.append(_message_summary(msg))
            except Exception as e:
                result.append({"id": item.get("id", ""), "error": str(e)})
        return result
    except Exception as e:
        return [_err(str(e))]


@mcp.tool()
def get_email_thread(email_id: str, limit: int = 50) -> Dict[str, Any]:
    """Return the full conversation thread for an email."""
    try:
        client = _client()
        msg = client.get_message(email_id, "minimal")
        thread_id = msg.get("threadId", "")
        thread = client.get_thread(thread_id, "full")
        out: List[Dict[str, Any]] = []
        for m in thread.get("messages") or []:
            if len(out) >= limit:
                break
            try:
                out.append(_message_summary(m, full=True))
            except Exception as e:
                out.append({"id": m.get("id", ""), "error": str(e)})
        return {"status": "success", "thread_id": thread_id, "count": len(out), "messages": out}
    except Exception as e:
        return _err(f"Failed to get thread: {str(e)}")


@mcp.tool()
def get_message_metadata(email_id: str) -> Dict[str, Any]:
    """Return just the metadata (headers + labels) of an email, not its body."""
    try:
        msg = _client().get_message(email_id, "metadata")
        headers = {h["name"]: h["value"] for h in (msg.get("payload") or {}).get("headers") or []}
        return {
            "id": msg.get("id", ""),
            "thread_id": msg.get("threadId", ""),
            "labels": msg.get("labelIds", []),
            "snippet": msg.get("snippet", ""),
            "headers": headers,
        }
    except Exception as e:
        return _err(f"Failed to get metadata: {str(e)}")


@mcp.tool()
def export_email_mime(email_id: str, folder: str = DEFAULT_FOLDER) -> Dict[str, Any]:
    """Export the raw MIME message as a base64 string plus byte count."""
    try:
        msg = _client().get_message(email_id, "raw")
        raw_bytes = b64url_decode(msg.get("raw", ""))
        return {
            "status": "success",
            "message": f"Exported {len(raw_bytes)} bytes",
            "mime_base64": base64.b64encode(raw_bytes).decode("ascii"),
            "size_bytes": len(raw_bytes),
        }
    except Exception as e:
        return _err(f"Failed to export MIME: {str(e)}")


# --------------------------------------------------------------------------
# tools: send
# --------------------------------------------------------------------------

@mcp.tool()
def send_email(
    to: str,
    subject: str,
    body: str,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    importance: Optional[str] = None,
    read_receipt: Optional[str] = None,
) -> Dict[str, str]:
    """Send a plain email. Separate multiple To/Cc/Bcc recipients with commas.

    importance: low|normal|high. read_receipt: an address to request a read receipt from.
    """
    try:
        result = _send(
            to, subject, body, cc=cc, bcc=bcc, importance=importance, read_receipt=read_receipt
        )
        return _ok(message="Email sent", email_id=result.get("id", ""), thread_id=result.get("threadId", ""))
    except Exception as e:
        return _err(f"Failed to send email: {str(e)}")


@mcp.tool()
def send_email_with_attachments(
    to: str,
    subject: str,
    body: str,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    attachments: Optional[List[Dict[str, Any]]] = None,
    importance: Optional[str] = None,
    read_receipt: Optional[str] = None,
) -> Dict[str, str]:
    """Send an email with base64 attachments.

    attachments: list of {"content": <base64 string>, "filename": <name>,
    "content_type": <MIME, optional>}. Max total ~25MB.
    """
    try:
        result = _send(
            to, subject, body, cc=cc, bcc=bcc, importance=importance,
            read_receipt=read_receipt, attachments=attachments,
        )
        return _ok(message="Email sent with attachments", email_id=result.get("id", ""), thread_id=result.get("threadId", ""))
    except Exception as e:
        return _err(f"Failed to send email: {str(e)}")


@mcp.tool()
def send_email_from_template(
    template_name: str,
    to: str,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    subject_override: Optional[str] = None,
    body_replace: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, str]:
    """Send using a saved template. body_replace: list of {"from", "to"} replacements."""
    try:
        templates = _load_templates()
        if template_name not in templates:
            return _err(f"Template '{template_name}' not found")
        template = templates[template_name]
        subject = subject_override or template.get("subject", "")
        body = template.get("body", "")
        for r in body_replace or []:
            body = body.replace(r.get("from", ""), r.get("to", ""))
        result = _send(to, subject, body, cc=cc, bcc=bcc)
        return _ok(message=f"Email sent from template '{template_name}'", email_id=result.get("id", ""))
    except Exception as e:
        return _err(f"Failed to send from template: {str(e)}")


# --------------------------------------------------------------------------
# tools: compose
# --------------------------------------------------------------------------

@mcp.tool()
def send_reply(email_id: str, body: str, reply_all: bool = False, recipients: Optional[str] = None) -> Dict[str, str]:
    """Reply to an email. reply_all=True replies to sender + all recipients."""
    try:
        client = _client()
        orig = client.get_message(email_id, "raw")
        raw = b64url_decode(orig.get("raw", ""))
        msg = email.message_from_bytes(raw)
        subject = msg.get("Subject") or ""
        if subject and not subject.lower().startswith("re:"):
            subject = "Re: " + subject
        in_reply_to = msg.get("Message-ID") or ""
        references = msg.get("References") or ""
        if references and in_reply_to:
            references = references + " " + in_reply_to
        elif in_reply_to:
            references = in_reply_to
        to = recipients or msg.get("Reply-To") or msg.get("From") or ""
        cc = msg.get("Cc") if reply_all and not recipients else None
        extra = {"In-Reply-To": in_reply_to, "References": references}
        body_text = _quote_original(raw, body)
        html = _looks_html(body_text)
        raw_msg = _build_raw_message(
            to, subject, body_text, cc=cc, html=html,
            extra_headers=extra, from_addr=_default_from(),
        )
        result = client.send_message(b64url_encode(raw_msg))
        return _ok(message="Reply sent", email_id=result.get("id", ""))
    except Exception as e:
        return _err(f"Failed to send reply: {str(e)}")


@mcp.tool()
def forward_email(email_id: str, to: str, comment: Optional[str] = None) -> Dict[str, str]:
    """Forward an email to comma-separated recipients, with an optional comment."""
    try:
        client = _client()
        orig = client.get_message(email_id, "raw")
        raw = b64url_decode(orig.get("raw", ""))
        msg = email.message_from_bytes(raw)
        subject = msg.get("Subject") or ""
        if subject and not subject.lower().startswith("fwd:"):
            subject = "Fwd: " + subject
        original = _extract_text(msg).strip()
        header = (
            f"---------- Forwarded message ---------\n"
            f"From: {msg.get('From', '')}\nDate: {msg.get('Date', '')}\n"
            f"Subject: {msg.get('Subject', '')}\nTo: {msg.get('To', '')}\n\n"
        )
        body = f"{comment}\n\n" if comment else ""
        body += header + original
        raw_msg = _build_raw_message(to, subject, body, from_addr=_default_from())
        result = client.send_message(b64url_encode(raw_msg))
        return _ok(message="Email forwarded", email_id=result.get("id", ""))
    except Exception as e:
        return _err(f"Failed to forward email: {str(e)}")


@mcp.tool()
def create_draft(
    to: str,
    subject: str,
    body: str,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    importance: Optional[str] = None,
    read_receipt: Optional[str] = None,
) -> Dict[str, str]:
    """Create an unsent draft. Send later with send_draft."""
    try:
        raw = _build_raw_message(
            to, subject, body, cc=cc, bcc=bcc, html=_looks_html(body),
            importance=importance, read_receipt=read_receipt, from_addr=_default_from(),
        )
        draft = _client().create_draft(b64url_encode(raw))
        return _ok(
            message="Draft created",
            draft_id=draft.get("id", ""),
            message_id=(draft.get("message") or {}).get("id", ""),
        )
    except Exception as e:
        return _err(f"Failed to create draft: {str(e)}")


@mcp.tool()
def edit_draft(
    draft_id: str,
    to: Optional[str] = None,
    subject: Optional[str] = None,
    body: Optional[str] = None,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
) -> Dict[str, str]:
    """Update only the fields provided on an existing draft. Pass an empty string to clear a field."""
    try:
        client = _client()
        draft = client.get_draft(draft_id, "raw")
        raw_b64 = (draft.get("message") or {}).get("raw", "")
        new_raw = _apply_draft_edits(raw_b64, to=to, subject=subject, body=body, cc=cc, bcc=bcc)
        updated = client.update_draft(draft_id, new_raw)
        return _ok(
            message=f"Draft {draft_id} updated",
            draft_id=draft_id,
            message_id=(updated.get("message") or {}).get("id", ""),
        )
    except Exception as e:
        return _err(f"Failed to edit draft: {str(e)}")


@mcp.tool()
def send_draft(draft_id: str) -> Dict[str, str]:
    """Send a previously created draft."""
    try:
        result = _client().send_draft(draft_id)
        return _ok(message="Draft sent", email_id=result.get("id", ""), thread_id=result.get("threadId", ""))
    except Exception as e:
        return _err(f"Failed to send draft: {str(e)}")


@mcp.tool()
def schedule_send(
    to: str,
    subject: str,
    body: str,
    send_at: str,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    importance: Optional[str] = None,
) -> Dict[str, str]:
    """Schedule an email to be sent later.

    send_at: ISO 8601 (e.g. "2026-08-07T10:30:00") or relative ("30m", "2h", "1d").
    Sends while the server process is running; the queue is persisted to disk so
    flush_scheduled_sends can catch up overdue items after a restart.
    """
    try:
        when = _parse_datetime(send_at)
        if when is None:
            return _err(f"Could not parse send_at: {send_at}. Use ISO 8601 or relative like '30m', '2h', '1d'.")
        item_id = base64.urlsafe_b64encode(os.urandom(6)).decode().rstrip("=")
        item = {
            "id": item_id,
            "send_at": when.isoformat(),
            "to": to,
            "subject": subject,
            "body": body,
            "cc": cc,
            "bcc": bcc,
            "importance": importance,
        }
        items = _load_scheduled()
        items.append(item)
        _save_scheduled(items)

        def _worker() -> None:
            try:
                delay = (when - datetime.now(timezone.utc)).total_seconds()
                if delay > 0:
                    time.sleep(delay)
                _send(to, subject, body, cc=cc, bcc=bcc, importance=importance)
                _remove_scheduled(item_id)
            except Exception as e:
                sys.stderr.write(f"schedule_send {item_id} failed: {e}\n")
                sys.stderr.flush()

        threading.Thread(target=_worker, daemon=True).start()
        return _ok(message=f"Scheduled email to {to} at {when.isoformat()}", schedule_id=item_id)
    except Exception as e:
        return _err(f"Failed to schedule email: {str(e)}")


@mcp.tool()
def list_scheduled_sends() -> List[Dict[str, Any]]:
    """List all pending scheduled emails."""
    return [
        {
            "schedule_id": i.get("id"),
            "send_at": i.get("send_at"),
            "to": i.get("to"),
            "subject": i.get("subject"),
        }
        for i in _load_scheduled()
    ]


@mcp.tool()
def cancel_scheduled_send(schedule_id: str) -> Dict[str, str]:
    """Cancel a pending scheduled email by its schedule_id."""
    before = len(_load_scheduled())
    _remove_scheduled(schedule_id)
    after = len(_load_scheduled())
    if after == before:
        return _err(f"No scheduled send found with id {schedule_id}")
    return _ok(message=f"Cancelled scheduled send {schedule_id}")


@mcp.tool()
def flush_scheduled_sends() -> Dict[str, Any]:
    """Send any scheduled emails that are now due (e.g. after a restart)."""
    now = datetime.now(timezone.utc)
    sent: List[str] = []
    failed: List[Dict[str, str]] = []
    for item in _load_scheduled():
        try:
            when = datetime.fromisoformat(item["send_at"])
            if when <= now:
                _send(
                    item["to"], item["subject"], item["body"],
                    cc=item.get("cc"), bcc=item.get("bcc"), importance=item.get("importance"),
                )
                _remove_scheduled(item["id"])
                sent.append(item["id"])
        except Exception as e:
            failed.append({"schedule_id": item.get("id", ""), "error": str(e)})
    return {"sent": sent, "failed": failed, "pending": len(_load_scheduled())}


@mcp.tool()
def save_email_template(name: str, subject: str, body: str) -> Dict[str, str]:
    """Save a reusable email template (subject + body) under a name."""
    try:
        templates = _load_templates()
        templates[name] = {"subject": subject, "body": body}
        _save_templates(templates)
        return _ok(message=f"Template '{name}' saved")
    except Exception as e:
        return _err(f"Failed to save template: {str(e)}")


@mcp.tool()
def list_email_templates() -> List[Dict[str, Any]]:
    """List all saved email templates."""
    return [
        {"name": name, "subject": t.get("subject", ""), "body_preview": (t.get("body", "") or "")[:100]}
        for name, t in _load_templates().items()
    ]


@mcp.tool()
def delete_email_template(name: str) -> Dict[str, str]:
    """Delete a saved email template."""
    templates = _load_templates()
    if name not in templates:
        return _err(f"Template '{name}' not found")
    del templates[name]
    _save_templates(templates)
    return _ok(message=f"Template '{name}' deleted")


# --------------------------------------------------------------------------
# tools: manage (labels / folders)
# --------------------------------------------------------------------------

@mcp.tool()
def get_email_folders() -> List[Dict[str, Any]]:
    """List all Gmail labels (folders). A message can belong to multiple labels."""
    try:
        client = _client()
        labels = client.list_labels()
        client.reset_label_cache()
        return [
            {
                "id": l.get("id"),
                "name": l.get("name"),
                "type": l.get("type"),
                "total": l.get("messagesTotal", 0),
                "unread": l.get("messagesUnread", 0),
                "threads_total": l.get("threadsTotal", 0),
                "hidden": (l.get("labelListVisibility") or "labelShow") != "labelShow",
            }
            for l in labels
        ]
    except Exception as e:
        return [_err(str(e))]


@mcp.tool()
def mark_email_read(email_id: str, folder: str = DEFAULT_FOLDER) -> Dict[str, str]:
    """Mark an email as read."""
    try:
        _client().modify_message(email_id, remove_labels=["UNREAD"])
        return _ok(message=f"Email {email_id} marked read")
    except Exception as e:
        return _err(f"Failed to mark read: {str(e)}")


@mcp.tool()
def mark_email_unread(email_id: str, folder: str = DEFAULT_FOLDER) -> Dict[str, str]:
    """Mark an email as unread."""
    try:
        _client().modify_message(email_id, add_labels=["UNREAD"])
        return _ok(message=f"Email {email_id} marked unread")
    except Exception as e:
        return _err(f"Failed to mark unread: {str(e)}")


@mcp.tool()
def move_email(email_id: str, source_folder: str, destination_folder: str) -> Dict[str, str]:
    """Move an email between labels. Destination 'Archive' removes INBOX/SPAM/TRASH.

    Gmail message ids are stable across moves (unlike Outlook).
    """
    try:
        client = _client()
        if _is_archive(destination_folder):
            client.modify_message(email_id, remove_labels=["INBOX", "SPAM", "TRASH"])
        else:
            dest_id = _resolve_label(client, destination_folder)
            add = [dest_id] if dest_id != "INBOX" else []
            src_id = _resolve_label(client, source_folder) if source_folder else None
            remove = [src_id] if src_id and src_id != dest_id else None
            client.modify_message(email_id, add_labels=add, remove_labels=remove)
        return _ok(message=f"Email {email_id} moved to {destination_folder}")
    except Exception as e:
        return _err(f"Failed to move email: {str(e)}")


@mcp.tool()
def move_emails(email_ids: List[str], source_folder: str, destination_folder: str) -> Dict[str, Any]:
    """Move multiple emails in one call (Gmail batch modify)."""
    try:
        client = _client()
        if _is_archive(destination_folder):
            client.batch_modify(email_ids, remove_labels=["INBOX", "SPAM", "TRASH"])
        else:
            dest_id = _resolve_label(client, destination_folder)
            src_id = _resolve_label(client, source_folder) if source_folder else None
            add = [dest_id] if dest_id != "INBOX" else []
            remove = [src_id] if src_id and src_id != dest_id else None
            client.batch_modify(email_ids, add_labels=add, remove_labels=remove)
        return _ok(message=f"Moved {len(email_ids)} emails to {destination_folder}", moved_count=len(email_ids))
    except Exception as e:
        return _err(f"Failed to move emails: {str(e)}")


@mcp.tool()
def copy_email(email_id: str, source_folder: str, destination_folder: str) -> Dict[str, str]:
    """Apply a destination label to an email (the original stays). Gmail has no physical copies."""
    try:
        if _is_archive(destination_folder):
            return _err("Cannot copy to Archive; archives are the absence of a label")
        dest_id = _resolve_label(_client(), destination_folder)
        _client().modify_message(email_id, add_labels=[dest_id])
        return _ok(message=f"Applied label {destination_folder} to {email_id}")
    except Exception as e:
        return _err(f"Failed to copy email: {str(e)}")


@mcp.tool()
def delete_email(email_id: str, folder: str = DEFAULT_FOLDER) -> Dict[str, str]:
    """Soft-delete an email (moves it to Trash)."""
    try:
        _client().trash_message(email_id)
        return _ok(message=f"Email {email_id} moved to Trash")
    except Exception as e:
        return _err(f"Failed to delete email: {str(e)}")


@mcp.tool()
def permanent_delete_email(email_id: str, folder: str = DEFAULT_FOLDER) -> Dict[str, str]:
    """Permanently delete an email; cannot be undone. Requires GMAIL_FULL_ACCESS=1."""
    try:
        if os.getenv("GMAIL_FULL_ACCESS") != "1":
            return _err("permanent_delete_email requires GMAIL_FULL_ACCESS=1 (mail.google.com scope)")
        _client().delete_message(email_id)
        return _ok(message=f"Email {email_id} permanently deleted")
    except Exception as e:
        return _err(f"Failed to permanently delete email: {str(e)}")


@mcp.tool()
def create_folder(folder_name: str, parent_folder: Optional[str] = None) -> Dict[str, str]:
    """Create a new label. Gmail labels are flat; parent_folder is ignored."""
    try:
        label = _client().create_label(folder_name)
        _client().reset_label_cache()
        return _ok(message=f"Label '{folder_name}' created", folder_id=label.get("id", ""))
    except Exception as e:
        return _err(f"Failed to create label: {str(e)}")


@mcp.tool()
def rename_folder(folder_name: str, new_name: str) -> Dict[str, str]:
    """Rename an existing label."""
    try:
        client = _client()
        label_id = _resolve_label(client, folder_name)
        client.patch_label(label_id, {"name": new_name})
        client.reset_label_cache()
        return _ok(message=f"Label '{folder_name}' renamed to '{new_name}'")
    except Exception as e:
        return _err(f"Failed to rename label: {str(e)}")


@mcp.tool()
def delete_folder(folder_name: str) -> Dict[str, str]:
    """Delete a label. Only removes the label — messages are NOT deleted."""
    try:
        client = _client()
        label_id = _resolve_label(client, folder_name)
        client.delete_label(label_id)
        client.reset_label_cache()
        return _ok(message=f"Label '{folder_name}' deleted")
    except Exception as e:
        return _err(f"Failed to delete label: {str(e)}")


@mcp.tool()
def list_message_ids(folder: str = DEFAULT_FOLDER, limit: int = 100) -> List[str]:
    """Cheap id-only listing of messages in a folder."""
    try:
        label_id = _resolve_label(_client(), folder)
        resp = _client().list_messages(label_ids=[label_id], max_results=min(limit, 500))
        return [m.get("id", "") for m in resp.get("messages") or []]
    except Exception as e:
        return [str(e)]


@mcp.tool()
def clear_folder(folder: str = DEFAULT_FOLDER, permanent: bool = False) -> Dict[str, Any]:
    """Delete all messages in a folder. permanent=True bypasses Trash (full-access scope)."""
    try:
        client = _client()
        label_id = _resolve_label(client, folder)
        ids: List[str] = []
        page_token: Optional[str] = None
        while len(ids) < 500:
            resp = client.list_messages(
                label_ids=[label_id], max_results=500, page_token=page_token
            )
            ids += [m.get("id", "") for m in resp.get("messages") or []]
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        if permanent:
            if os.getenv("GMAIL_FULL_ACCESS") != "1":
                return _err("permanent clear requires GMAIL_FULL_ACCESS=1 (mail.google.com scope)")
            if ids:
                client.batch_delete(ids)
            return _ok(message=f"Permanently deleted {len(ids)} messages from {folder}", deleted=len(ids))
        for i in ids:
            client.trash_message(i)
        return _ok(message=f"Trashed {len(ids)} messages from {folder}", trashed=len(ids))
    except Exception as e:
        return _err(f"Failed to clear folder: {str(e)}")


@mcp.tool()
def mark_folder_read(folder: str = DEFAULT_FOLDER, limit: int = 500) -> Dict[str, Any]:
    """Mark unread messages in a folder as read."""
    try:
        client = _client()
        label_id = _resolve_label(client, folder)
        ids: List[str] = []
        resp = client.list_messages(
            q="is:unread", label_ids=[label_id], max_results=min(limit, 500)
        )
        ids = [m.get("id", "") for m in resp.get("messages") or []]
        if ids:
            client.batch_modify(ids, remove_labels=["UNREAD"])
        return _ok(message=f"Marked {len(ids)} messages read in {folder}", marked=len(ids))
    except Exception as e:
        return _err(f"Failed to mark folder read: {str(e)}")


@mcp.tool()
def flag_email(email_id: str, flag_status: str = "flagged", due_date: Optional[str] = None, start_date: Optional[str] = None) -> Dict[str, str]:
    """Star or unstar an email (Gmail has no follow-up flags). flag_status: flagged|notFlagged."""
    try:
        if flag_status == "notFlagged":
            _client().modify_message(email_id, remove_labels=["STARRED"])
            return _ok(message=f"Email {email_id} unstarred")
        _client().modify_message(email_id, add_labels=["STARRED"])
        return _ok(message=f"Email {email_id} starred")
    except Exception as e:
        return _err(f"Failed to flag email: {str(e)}")


@mcp.tool()
def follow_up_email(email_id: str, due_date: str, start_date: Optional[str] = None) -> Dict[str, str]:
    """Convenience star for follow-up (Gmail has no due-date flags)."""
    try:
        _client().modify_message(email_id, add_labels=["STARRED"])
        return _ok(message=f"Email {email_id} starred for follow-up")
    except Exception as e:
        return _err(f"Failed to follow up: {str(e)}")


@mcp.tool()
def clear_email_flag(email_id: str) -> Dict[str, str]:
    """Remove the star from an email."""
    try:
        _client().modify_message(email_id, remove_labels=["STARRED"])
        return _ok(message=f"Star removed from {email_id}")
    except Exception as e:
        return _err(f"Failed to clear flag: {str(e)}")


@mcp.tool()
def pin_email(email_id: str, pinned: bool = True) -> Dict[str, str]:
    """Pin/unpin an email via the IMPORTANT label."""
    try:
        if pinned:
            _client().modify_message(email_id, add_labels=["IMPORTANT"])
        else:
            _client().modify_message(email_id, remove_labels=["IMPORTANT"])
        return _ok(message=f"Email {email_id} {'pinned' if pinned else 'unpinned'}")
    except Exception as e:
        return _err(f"Failed to pin email: {str(e)}")


# --------------------------------------------------------------------------
# tools: attachments
# --------------------------------------------------------------------------

@mcp.tool()
def list_email_attachments(email_id: str, folder: str = DEFAULT_FOLDER) -> List[Dict[str, Any]]:
    """List attachments on an email: attachment id, filename, content type, size, inline."""
    try:
        msg = _client().get_message(email_id, "full")
        text_parts: List[str] = []
        html_parts: List[str] = []
        attachments: List[Dict[str, Any]] = []
        _walk_parts(msg.get("payload") or {}, text_parts, html_parts, attachments)
        return attachments
    except Exception as e:
        return [_err(str(e))]


@mcp.tool()
def download_attachment(email_id: str, attachment_id: str, folder: str = DEFAULT_FOLDER) -> Dict[str, Any]:
    """Download an attachment as base64. Pass the attachment_id from list_email_attachments."""
    try:
        client = _client()
        try:
            data = client.get_attachment(email_id, attachment_id).get("data", "")
            raw = b64url_decode(data)
            return {"status": "success", "content_base64": base64.b64encode(raw).decode("ascii"), "size_bytes": len(raw)}
        except GmailError:
            msg = client.get_message(email_id, "full")
            text_parts: List[str] = []
            html_parts: List[str] = []
            attachments: List[Dict[str, Any]] = []
            _walk_parts(msg.get("payload") or {}, text_parts, html_parts, attachments)
            for att in attachments:
                if att.get("attachment_id") == attachment_id or att.get("part_id") == attachment_id:
                    if att.get("attachment_id"):
                        data = client.get_attachment(email_id, att["attachment_id"]).get("data", "")
                    else:
                        part = _find_part(msg.get("payload"), att.get("part_id"))
                        data = (part.get("body") or {}).get("data", "")
                    raw = b64url_decode(data)
                    return {"status": "success", "content_base64": base64.b64encode(raw).decode("ascii"), "size_bytes": len(raw)}
            raise GmailError(f"Attachment {attachment_id} not found")
    except Exception as e:
        return _err(f"Failed to download attachment: {str(e)}")


def _find_part(payload: Dict[str, Any], part_id: str) -> Dict[str, Any]:
    if payload.get("partId") == part_id:
        return payload
    for part in payload.get("parts") or []:
        found = _find_part(part, part_id)
        if found:
            return found
    return {}


@mcp.tool()
def add_attachment_to_email(email_id: str, content_base64: str, name: str, content_type: Optional[str] = None, folder: str = DEFAULT_FOLDER, draft_id: Optional[str] = None) -> Dict[str, str]:
    """Add an attachment to a draft. Gmail cannot edit sent messages.

    email_id: the draft's message id (a draft has the DRAFT label). After any
        draft edit the message id changes, so pass draft_id instead (or use the
        message_id returned by the previous draft operation).
    draft_id: optional; if given, skips the message-id lookup entirely.
    """
    try:
        client = _client()
        if not draft_id:
            msg = client.get_message(email_id, "minimal")
            if "DRAFT" not in (msg.get("labelIds") or []):
                return _err("Gmail cannot add attachments to sent messages; target a draft instead")
            draft_id = _find_draft_for_message(client, email_id)
        draft = client.get_draft(draft_id, "raw")
        raw_b64 = (draft.get("message") or {}).get("raw", "")
        new_raw = _mime_attach(raw_b64, content_base64, name, content_type)
        updated = client.update_draft(draft_id, new_raw)
        return _ok(
            message=f"Attachment '{name}' added to draft {draft_id}",
            draft_id=draft_id,
            message_id=(updated.get("message") or {}).get("id", ""),
        )
    except Exception as e:
        return _err(f"Failed to add attachment: {str(e)}")


@mcp.tool()
def remove_attachment_from_email(email_id: str, attachment_id: str, folder: str = DEFAULT_FOLDER, draft_id: Optional[str] = None) -> Dict[str, str]:
    """Remove an attachment from a draft. Pass the filename (or Content-ID) from list_email_attachments.

    email_id: the draft's message id. After any draft edit the message id changes,
        so pass draft_id instead (or use the message_id returned by the previous draft operation).
    draft_id: optional; if given, skips the message-id lookup entirely.
    """
    try:
        client = _client()
        if not draft_id:
            msg = client.get_message(email_id, "minimal")
            if "DRAFT" not in (msg.get("labelIds") or []):
                return _err("Gmail cannot edit sent messages; target a draft instead")
            draft_id = _find_draft_for_message(client, email_id)
        draft = client.get_draft(draft_id, "raw")
        raw_b64 = (draft.get("message") or {}).get("raw", "")
        new_raw = _mime_detach(raw_b64, attachment_id)
        updated = client.update_draft(draft_id, new_raw)
        return _ok(
            message=f"Attachment removed from draft {draft_id}",
            draft_id=draft_id,
            message_id=(updated.get("message") or {}).get("id", ""),
        )
    except Exception as e:
        return _err(f"Failed to remove attachment: {str(e)}")


def _find_draft_for_message(client: GmailClient, message_id: str) -> str:
    try:
        target = client.get_message(message_id, "minimal")
    except Exception:
        target = {}
    target_thread = target.get("threadId") or ""
    drafts = (client.list_drafts(max_results=100).get("drafts") or [])
    for d in drafts:
        try:
            full = client.get_draft(d["id"], "minimal")
            if (full.get("message") or {}).get("id") == message_id:
                return d["id"]
        except Exception:
            continue
    if target_thread:
        for d in drafts:
            try:
                full = client.get_draft(d["id"], "minimal")
                if (full.get("message") or {}).get("threadId") == target_thread:
                    return d["id"]
            except Exception:
                continue
    raise GmailError(f"No draft found for message {message_id}")


# --------------------------------------------------------------------------
# tools: automation
# --------------------------------------------------------------------------

@mcp.tool()
def triage_inbox(limit: int = 50) -> Dict[str, Any]:
    """Classify recent inbox emails into reply_needed, priority, review, newsletter, read_later."""
    try:
        client = _client()
        resp = client.list_messages(label_ids=["INBOX"], max_results=min(limit, 500))
        buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for item in resp.get("messages") or []:
            try:
                msg = client.get_message(item["id"], "full")
                summary = _message_summary(msg)
                action = _suggested_action(summary)
                buckets[action].append(
                    {
                        "email_id": summary["id"],
                        "subject": summary["subject"],
                        "from": summary["from"],
                        "date": summary["date"],
                    }
                )
            except Exception:
                continue
        summary_counts = {k: len(v) for k, v in buckets.items()}
        return {
            "status": "success",
            "message": f"Triaged {sum(len(v) for v in buckets.values())} emails",
            "summary": summary_counts,
            "buckets": dict(buckets),
        }
    except Exception as e:
        return _err(f"Failed to triage inbox: {str(e)}")


@mcp.tool()
def auto_organize(
    folder: str = DEFAULT_FOLDER,
    limit: int = 100,
    move_to: Optional[str] = None,
    mark_newsletters_read: bool = False,
    archive_older_than: Optional[str] = None,
) -> Dict[str, Any]:
    """Apply basic rules to a folder.

    move_to: move all emails to this label. mark_newsletters_read: mark newsletter-like
    emails (unsubscribe/no-reply senders) as read. archive_older_than: relative age
    ('30d') — emails older than this are archived (INBOX removed).
    """
    try:
        client = _client()
        label_id = _resolve_label(client, folder)
        resp = client.list_messages(label_ids=[label_id], max_results=min(limit, 500))
        ids = [m.get("id") for m in resp.get("messages") or []]
        moved: List[str] = []
        marked_read: List[str] = []
        archived: List[str] = []
        if move_to:
            dest_id = _resolve_label(client, move_to)
            add = [dest_id] if dest_id != "INBOX" else []
            remove = [label_id] if label_id != dest_id else None
            if ids:
                client.batch_modify(ids, add_labels=add, remove_labels=remove)
                moved = ids
        if mark_newsletters_read:
            to_mark: List[str] = []
            for i in ids:
                try:
                    msg = client.get_message(i, "full")
                    s = _message_summary(msg)
                    sender = (s.get("from") or "").lower()
                    subject = (s.get("subject") or "").lower()
                    if any(k in subject or k in sender for k in ("unsubscribe", "newsletter", "digest", "promotions", "no-reply", "no_reply")):
                        to_mark.append(i)
                except Exception:
                    continue
            if to_mark:
                client.batch_modify(to_mark, remove_labels=["UNREAD"])
                marked_read = to_mark
        if archive_older_than:
            cutoff = _parse_datetime(archive_older_than)
            if cutoff is None:
                return _err(f"Could not parse archive_older_than: {archive_older_than}")
            for i in ids:
                try:
                    msg = client.get_message(i, "minimal")
                    internal = msg.get("internalDate")
                    if not internal:
                        continue
                    when = datetime.fromtimestamp(int(internal) / 1000, tz=timezone.utc)
                    if when < cutoff:
                        client.modify_message(i, remove_labels=["INBOX", "SPAM", "TRASH"])
                        archived.append(i)
                except Exception:
                    continue
        return {
            "status": "success",
            "message": f"Organization pass complete on {folder}",
            "moved": len(moved),
            "marked_read": len(marked_read),
            "archived": len(archived),
        }
    except Exception as e:
        return _err(f"Failed to auto-organize: {str(e)}")


@mcp.tool()
def send_email_digest(
    folder: str = DEFAULT_FOLDER,
    limit: int = 20,
    to: Optional[str] = None,
    subject: Optional[str] = None,
) -> Dict[str, str]:
    """Send yourself a digest of the most recent emails in a folder."""
    try:
        client = _client()
        label_id = _resolve_label(client, folder)
        resp = client.list_messages(label_ids=[label_id], max_results=min(limit, 500))
        lines: List[str] = []
        for item in resp.get("messages") or []:
            try:
                s = _message_summary(client.get_message(item["id"], "full"))
                lines.append(f"- ({s['date']}) {s['from']} | {s['subject']}")
            except Exception:
                continue
        recipient = to or _default_from()
        digest_body = "\n".join(lines) or "No emails found."
        digest_subject = subject or f"Email digest: {folder} ({len(lines)} messages)"
        result = _send(recipient, digest_subject, digest_body)
        return _ok(message="Digest sent", email_id=result.get("id", ""))
    except Exception as e:
        return _err(f"Failed to send digest: {str(e)}")


@mcp.tool()
def email_analytics(folder: str = DEFAULT_FOLDER, limit: int = 500) -> Dict[str, Any]:
    """Compute simple analytics over the most recent messages in a folder."""
    try:
        client = _client()
        label_id = _resolve_label(client, folder)
        resp = client.list_messages(label_ids=[label_id], max_results=min(limit, 500))
        summaries: List[Dict[str, Any]] = []
        for item in resp.get("messages") or []:
            try:
                summaries.append(_message_summary(client.get_message(item["id"], "full")))
            except Exception:
                continue
        senders = Counter((s.get("from") or "unknown").lower() for s in summaries)
        per_day = Counter((s.get("date") or "")[:10] for s in summaries)
        threads = {s.get("thread_id") for s in summaries}
        return {
            "status": "success",
            "folder": folder,
            "total": len(summaries),
            "unread": sum(1 for s in summaries if s.get("unread")),
            "with_attachments": sum(1 for s in summaries if s.get("has_attachments")),
            "thread_count": len(threads),
            "top_senders": senders.most_common(10),
            "messages_per_day": dict(sorted(per_day.items())),
        }
    except Exception as e:
        return _err(f"Failed to compute analytics: {str(e)}")


@mcp.tool()
def dedupe_emails(folder: str = DEFAULT_FOLDER, limit: int = 500, dry_run: bool = True) -> Dict[str, Any]:
    """Find duplicates (same subject + sender + received day). dry_run=False trashes the extras."""
    try:
        client = _client()
        label_id = _resolve_label(client, folder)
        resp = client.list_messages(label_ids=[label_id], max_results=min(limit, 500))
        summaries: List[Dict[str, Any]] = []
        for item in resp.get("messages") or []:
            try:
                summaries.append(_message_summary(client.get_message(item["id"], "full")))
            except Exception:
                continue
        groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for s in summaries:
            key = f"{s.get('from', '').lower()}|{s.get('subject', '').lower()}|{(s.get('date') or '')[:10]}"
            groups[key].append(s)
        duplicates: List[Dict[str, Any]] = []
        extra_ids: List[str] = []
        for key, group in groups.items():
            if len(group) > 1:
                for extra in group[1:]:
                    duplicates.append(
                        {
                            "email_id": extra["id"],
                            "subject": extra["subject"],
                            "from": extra["from"],
                            "date": extra["date"],
                        }
                    )
                    extra_ids.append(extra["id"])
        result: Dict[str, Any] = {
            "status": "success",
            "dry_run": dry_run,
            "duplicate_count": len(duplicates),
            "duplicates": duplicates,
        }
        if not dry_run and extra_ids:
            for i in extra_ids:
                client.trash_message(i)
            result["trashed"] = len(extra_ids)
        return result
    except Exception as e:
        return _err(f"Failed to dedupe emails: {str(e)}")


@mcp.tool()
def batch_request(requests: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Run a batch of Gmail API requests in one HTTP call (max 100).

    Each request: {"id": 1, "method": "GET", "url": "/gmail/v1/users/me/messages", ...}
    """
    try:
        if not requests:
            return _err("No requests provided")
        result = _client().batch(requests)
        responses = result.get("responses", [])
        return {
            "status": "success",
            "message": f"Batch executed ({len(responses)} responses)",
            "responses": responses,
        }
    except Exception as e:
        return _err(f"Failed to execute batch: {str(e)}")


# --------------------------------------------------------------------------
# tools: gmail extras
# --------------------------------------------------------------------------

@mcp.tool()
def star_email(email_id: str) -> Dict[str, str]:
    """Star an email."""
    try:
        _client().modify_message(email_id, add_labels=["STARRED"])
        return _ok(message=f"Email {email_id} starred")
    except Exception as e:
        return _err(f"Failed to star email: {str(e)}")


@mcp.tool()
def unstar_email(email_id: str) -> Dict[str, str]:
    """Unstar an email."""
    try:
        _client().modify_message(email_id, remove_labels=["STARRED"])
        return _ok(message=f"Star removed from {email_id}")
    except Exception as e:
        return _err(f"Failed to unstar email: {str(e)}")


@mcp.tool()
def mark_important(email_id: str) -> Dict[str, str]:
    """Mark an email as important."""
    try:
        _client().modify_message(email_id, add_labels=["IMPORTANT"])
        return _ok(message=f"Email {email_id} marked important")
    except Exception as e:
        return _err(f"Failed to mark important: {str(e)}")


@mcp.tool()
def unmark_important(email_id: str) -> Dict[str, str]:
    """Remove the important marker from an email."""
    try:
        _client().modify_message(email_id, remove_labels=["IMPORTANT"])
        return _ok(message=f"Email {email_id} no longer important")
    except Exception as e:
        return _err(f"Failed to unmark important: {str(e)}")


@mcp.tool()
def list_email_aliases() -> List[Dict[str, Any]]:
    """List the send-as aliases configured on the account."""
    try:
        return [
            {
                "email": a.get("sendAsEmail", ""),
                "display_name": a.get("displayName", ""),
                "is_default": bool(a.get("isDefault")),
                "is_verified": bool(a.get("isVerified")),
                "reply_to": a.get("replyToAddress", ""),
            }
            for a in _client().list_send_as()
        ]
    except Exception as e:
        return [_err(str(e))]


@mcp.tool()
def set_vacation_responder(enabled: bool, subject: Optional[str] = None, body: Optional[str] = None) -> Dict[str, str]:
    """Enable or disable the vacation auto-reply, optionally setting subject/body."""
    try:
        _client().set_vacation(enabled, subject=subject, body=body)
        return _ok(message="Vacation responder " + ("enabled" if enabled else "disabled"))
    except Exception as e:
        return _err(f"Failed to set vacation responder: {str(e)}")


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail MCP server (Gmail REST API)")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http", "sse", "streamable-http"],
        default=None,
        help="MCP transport (default: stdio, or the GMAIL_TRANSPORT env var)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("PORT", "8000")),
        help="HTTP bind port (default: $PORT or 8000)",
    )
    parser.add_argument("--path", default="/mcp", help="HTTP path")
    args = parser.parse_args()

    transport = args.transport or os.getenv("GMAIL_TRANSPORT", "stdio")
    if transport == "stdio":
        mcp.run()
    else:
        mcp.run(
            transport=transport,
            host=args.host,
            port=args.port,
            path=args.path,
        )


if __name__ == "__main__":
    main()
