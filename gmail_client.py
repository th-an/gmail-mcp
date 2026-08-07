"""Thin Gmail REST API client for the Gmail MCP server.

Mirrors graph.py in the Outlook MCP server: minimal urllib-based client with
token management, a label (folder) cache, transient-error retry, and helpers
for base64url-encoded RFC 2822 messages.
"""

import base64
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, List, Optional

import gmail_auth

GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1"
BATCH_BASE = "https://gmail.googleapis.com/batch/gmail/v1"
TOKEN_TTL = 3300  # access tokens last 1h; refresh a bit early
USER = "me"

TRANSIENT_STATUS = {429, 500, 502, 503, 504}


def quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64url_decode(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded)


class GmailError(Exception):
    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GmailClient:
    def __init__(self) -> None:
        self._token: Optional[str] = None
        self._token_expiry = 0.0
        self._label_cache: Dict[str, str] = {}  # uppercase label name -> id

    # ---- low level -------------------------------------------------------

    def _ensure_token(self) -> None:
        if time.time() > self._token_expiry:
            self._token = gmail_auth.acquire_access_token()
            self._token_expiry = time.time() + TOKEN_TTL

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        payload: Optional[Dict[str, Any]] = None,
        raw: bool = False,
    ) -> Any:
        last_exc: Optional[Exception] = None
        for attempt in range(4):
            self._ensure_token()
            url = GMAIL_BASE + path
            if params:
                url += "?" + urllib.parse.urlencode(params)
            req = urllib.request.Request(url, method=method)
            req.add_header("Authorization", f"Bearer {self._token}")
            if not raw:
                req.add_header("Accept", "application/json")
            if payload is not None:
                req.add_header("Content-Type", "application/json")
                req.data = json.dumps(payload).encode("utf-8")
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    data = resp.read()
                    if raw:
                        return data
                    return json.loads(data.decode("utf-8", errors="replace")) if data else {}
            except urllib.error.HTTPError as e:
                try:
                    body = json.loads(e.read().decode("utf-8", errors="replace"))
                except Exception:
                    body = {}
                message = " ".join(
                    m.get("message", "") for m in body.get("error", {}).get("errors", [])
                ) or body.get("error", {}).get("message", str(e))
                if e.code in TRANSIENT_STATUS and attempt < 3:
                    last_exc = GmailError(message, e.code)
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                raise GmailError(message, e.code) from e
            except urllib.error.URLError as e:
                last_exc = e
                time.sleep(0.5 * (2 ** attempt))
        raise last_exc if last_exc else GmailError("Request failed")

    # ---- profile ---------------------------------------------------------

    def get_profile(self) -> Dict[str, Any]:
        return self._request("GET", f"/users/{USER}/profile")

    # ---- messages --------------------------------------------------------

    def list_messages(
        self,
        q: Optional[str] = None,
        label_ids: Optional[List[str]] = None,
        max_results: int = 100,
        include_spam_trash: bool = False,
        page_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"maxResults": max_results}
        if q:
            params["q"] = q
        if label_ids:
            params["labelIds"] = ",".join(label_ids)
        if include_spam_trash:
            params["includeSpamTrash"] = "true"
        if page_token:
            params["pageToken"] = page_token
        return self._request("GET", f"/users/{USER}/messages", params=params)

    def get_message(self, message_id: str, fmt: str = "full") -> Dict[str, Any]:
        params = {"format": fmt}
        return self._request("GET", f"/users/{USER}/messages/{quote(message_id)}", params=params)

    def send_message(self, raw_b64: str) -> Dict[str, Any]:
        return self._request(
            "POST", f"/users/{USER}/messages/send", payload={"raw": raw_b64}
        )

    def trash_message(self, message_id: str) -> Dict[str, Any]:
        return self._request(
            "POST", f"/users/{USER}/messages/{quote(message_id)}/trash", payload={}
        )

    def untrash_message(self, message_id: str) -> Dict[str, Any]:
        return self._request(
            "POST", f"/users/{USER}/messages/{quote(message_id)}/untrash", payload={}
        )

    def delete_message(self, message_id: str) -> None:
        self._request("DELETE", f"/users/{USER}/messages/{quote(message_id)}")

    def modify_message(
        self,
        message_id: str,
        add_labels: Optional[List[str]] = None,
        remove_labels: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        if add_labels:
            payload["addLabelIds"] = add_labels
        if remove_labels:
            payload["removeLabelIds"] = remove_labels
        return self._request(
            "POST", f"/users/{USER}/messages/{quote(message_id)}/modify", payload=payload
        )

    def batch_modify(
        self,
        message_ids: List[str],
        add_labels: Optional[List[str]] = None,
        remove_labels: Optional[List[str]] = None,
    ) -> None:
        payload: Dict[str, Any] = {"ids": message_ids}
        if add_labels:
            payload["addLabelIds"] = add_labels
        if remove_labels:
            payload["removeLabelIds"] = remove_labels
        self._request("POST", f"/users/{USER}/messages/batchModify", payload=payload)

    def batch_delete(self, message_ids: List[str]) -> None:
        self._request(
            "POST", f"/users/{USER}/messages/batchDelete", payload={"ids": message_ids}
        )

    # ---- threads ---------------------------------------------------------

    def list_threads(
        self,
        q: Optional[str] = None,
        label_ids: Optional[List[str]] = None,
        max_results: int = 100,
        include_spam_trash: bool = False,
        page_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"maxResults": max_results}
        if q:
            params["q"] = q
        if label_ids:
            params["labelIds"] = ",".join(label_ids)
        if include_spam_trash:
            params["includeSpamTrash"] = "true"
        if page_token:
            params["pageToken"] = page_token
        return self._request("GET", f"/users/{USER}/threads", params=params)

    def get_thread(self, thread_id: str, fmt: str = "full") -> Dict[str, Any]:
        params = {"format": fmt}
        return self._request(
            "GET", f"/users/{USER}/threads/{quote(thread_id)}", params=params
        )

    # ---- drafts ----------------------------------------------------------

    def create_draft(self, raw_b64: str) -> Dict[str, Any]:
        return self._request(
            "POST", f"/users/{USER}/drafts", payload={"message": {"raw": raw_b64}}
        )

    def list_drafts(self, max_results: int = 100) -> Dict[str, Any]:
        return self._request("GET", f"/users/{USER}/drafts", params={"maxResults": max_results})

    def get_draft(self, draft_id: str, fmt: str = "full") -> Dict[str, Any]:
        return self._request(
            "GET", f"/users/{USER}/drafts/{quote(draft_id)}", params={"format": fmt}
        )

    def update_draft(self, draft_id: str, raw_b64: str) -> Dict[str, Any]:
        return self._request(
            "PUT",
            f"/users/{USER}/drafts/{quote(draft_id)}",
            payload={"message": {"raw": raw_b64}},
        )

    def send_draft(self, draft_id: str) -> Dict[str, Any]:
        return self._request(
            "POST", f"/users/{USER}/drafts/send", payload={"id": draft_id}
        )

    def delete_draft(self, draft_id: str) -> None:
        self._request("DELETE", f"/users/{USER}/drafts/{quote(draft_id)}")

    # ---- labels ----------------------------------------------------------

    def list_labels(self) -> List[Dict[str, Any]]:
        return (self._request("GET", f"/users/{USER}/labels") or {}).get("labels", [])

    def get_label(self, label_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/users/{USER}/labels/{quote(label_id)}")

    def create_label(self, name: str, list_visibility: Optional[str] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "name": name,
            "labelListVisibility": list_visibility or "labelShow",
            "messageListVisibility": "show",
        }
        return self._request("POST", f"/users/{USER}/labels", payload=payload)

    def patch_label(self, label_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        return self._request(
            "PATCH", f"/users/{USER}/labels/{quote(label_id)}", payload=fields
        )

    def delete_label(self, label_id: str) -> None:
        self._request("DELETE", f"/users/{USER}/labels/{quote(label_id)}")

    def _ensure_label_cache(self) -> None:
        if not self._label_cache:
            for label in self.list_labels():
                self._label_cache[label.get("name", "").upper()] = label["id"]

    def resolve_label_id(self, label: str) -> Optional[str]:
        """Resolve a label name/path/id to a label id. Returns None if not found."""
        self._ensure_label_cache()
        name = label.upper()
        if name in self._label_cache:
            return self._label_cache[name]
        for lbl_name, lbl_id in self._label_cache.items():
            if lbl_id == label:
                return lbl_id
        self._label_cache = {}
        self._ensure_label_cache()
        if name in self._label_cache:
            return self._label_cache[name]
        for lbl_name, lbl_id in self._label_cache.items():
            if lbl_id == label:
                return lbl_id
        return None

    def reset_label_cache(self) -> None:
        self._label_cache = {}

    # ---- attachments -----------------------------------------------------

    def get_attachment(self, message_id: str, attachment_id: str) -> Dict[str, Any]:
        return self._request(
            "GET",
            f"/users/{USER}/messages/{quote(message_id)}/attachments/{quote(attachment_id)}",
        )

    # ---- settings --------------------------------------------------------

    def list_send_as(self) -> List[Dict[str, Any]]:
        return (self._request("GET", f"/users/{USER}/settings/sendAs") or {}).get(
            "sendAs", []
        )

    def get_vacation(self) -> Dict[str, Any]:
        return self._request("GET", f"/users/{USER}/settings/vacation")

    def set_vacation(
        self,
        enabled: bool,
        subject: Optional[str] = None,
        body: Optional[str] = None,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"enableAutoReply": enabled}
        if subject is not None:
            payload["responseSubject"] = subject
        if body is not None:
            payload["responseBodyHtml"] = body
        if start_time is not None:
            payload["startTime"] = start_time
        if end_time is not None:
            payload["endTime"] = end_time
        return self._request("PUT", f"/users/{USER}/settings/vacation", payload=payload)

    # ---- batch -----------------------------------------------------------

    def batch(self, requests: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Run a Gmail batch request (max 100 sub-requests)."""
        boundary = f"gmail_mcp_{uuid.uuid4().hex}"
        lines: List[str] = []
        for item in requests[:100]:
            rid = item.get("id", item.get("contentId", ""))
            method = item.get("method", "GET")
            url = item.get("url", "")
            if not url.startswith("/"):
                url = "/" + url.lstrip()
            lines.append(f"--{boundary}")
            lines.append("Content-Type: application/http")
            lines.append("Content-Transfer-Encoding: binary")
            lines.append(f"Content-ID: <{rid}>")
            lines.append("")
            lines.append(f"{method} {url} HTTP/1.1")
            lines.append("Accept: application/json")
            if "body" in item:
                body = json.dumps(item["body"])
                lines.append("Content-Type: application/json")
                lines.append(f"Content-Length: {len(body.encode())}")
                lines.append("")
                lines.append(body)
            else:
                lines.append("")
        lines.append(f"--{boundary}--")
        body = "\r\n".join(lines) + "\r\n"

        req = urllib.request.Request(BATCH_BASE, method="POST")
        self._ensure_token()
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("Content-Type", f"multipart/mixed; boundary={boundary}")
        req.data = body.encode("utf-8")

        responses: List[Dict[str, Any]] = []
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            raise GmailError(e.read().decode("utf-8", errors="replace"), e.code) from e

        for part in re.split(r"--[a-zA-Z0-9]+", raw):
            if "Content-ID:" not in part:
                continue
            cid = ""
            m = re.search(r"Content-ID: <([^>]+)>", part)
            if m:
                cid = m.group(1)
            m = re.search(r"HTTP/\d\.\d (\d{3})", part)
            status = int(m.group(1)) if m else 0
            m = re.search(r"\{\n", part)
            payload: Any = {}
            if m:
                try:
                    payload = json.loads(part[m.start():])
                except Exception:
                    payload = {"raw": part[m.start():]}
            responses.append({"id": cid, "status": status, "body": payload})
        return {"responses": responses}
