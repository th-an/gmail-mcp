import base64
import email

import server
from gmail_client import b64url_decode, b64url_encode


def test_b64url_roundtrip():
    raw = b"Subject: test\r\n\r\nhello"
    assert b64url_decode(b64url_encode(raw)) == raw
    assert b64url_encode(b"\xfb\xff") == "-_8"


def test_build_plain_message():
    raw = server._build_raw_message(
        "a@b.com, c@d.com", "Hi", "Body text", cc="x@y.com", from_addr="me@gmail.com"
    )
    msg = email.message_from_bytes(raw)
    assert msg["To"] == "a@b.com, c@d.com"
    assert msg["Cc"] == "x@y.com"
    assert msg["From"] == "me@gmail.com"
    assert msg["Subject"] == "Hi"
    assert msg["Message-ID"]
    assert msg["Date"]


def test_build_message_importance_and_receipt():
    raw = server._build_raw_message(
        "a@b.com", "S", "B", importance="high", read_receipt="a@b.com"
    )
    msg = email.message_from_bytes(raw)
    assert msg["X-Priority"] == "1"
    assert msg["Disposition-Notification-To"] == "a@b.com"


def test_build_message_with_attachments():
    content = base64.b64encode(b"file-bytes").decode()
    raw = server._build_raw_message(
        "a@b.com",
        "S",
        "B",
        attachments=[{"content": content, "filename": "f.txt", "content_type": "text/plain"}],
    )
    msg = email.message_from_bytes(raw)
    parts = [p for p in msg.walk()]
    filenames = [p.get_filename() for p in parts if p.get_filename()]
    assert filenames == ["f.txt"]


def test_looks_html():
    assert server._looks_html("<p>hello</p>")
    assert server._looks_html("<html><body>x</body></html>")
    assert not server._looks_html("plain text with no tags")


def test_message_summary_from_gmail_payload():
    message = {
        "id": "m1",
        "threadId": "t1",
        "labelIds": ["INBOX", "UNREAD"],
        "snippet": "hi there",
        "internalDate": "1750000000000",
        "payload": {
            "headers": [
                {"name": "From", "value": "Sender <s@x.com>"},
                {"name": "To", "value": "me@gmail.com"},
                {"name": "Subject", "value": "Greetings"},
            ],
            "mimeType": "text/plain",
            "body": {"data": b64url_encode(b"full body text")},
        },
    }
    summary = server._message_summary(message)
    assert summary["id"] == "m1"
    assert summary["unread"] is True
    assert summary["from"] == "Sender <s@x.com>"
    assert summary["subject"] == "Greetings"
    assert summary["has_attachments"] is False
    assert summary["body"] == "hi there"  # preview uses snippet


def test_message_summary_full_content():
    message = {
        "id": "m2",
        "threadId": "t1",
        "labelIds": ["INBOX"],
        "internalDate": "1750000000000",
        "payload": {
            "headers": [{"name": "Subject", "value": "X"}],
            "mimeType": "text/plain",
            "body": {"data": b64url_encode(b"full body text")},
        },
    }
    summary = server._message_summary(message, full=True)
    assert summary["body"] == "full body text"


def test_message_summary_attachments():
    message = {
        "id": "m3",
        "threadId": "t1",
        "labelIds": [],
        "payload": {
            "headers": [{"name": "Subject", "value": "X"}],
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "partId": "0",
                    "mimeType": "text/plain",
                    "body": {"data": b64url_encode(b"body")},
                },
                {
                    "partId": "1",
                    "mimeType": "application/pdf",
                    "filename": "doc.pdf",
                    "body": {"attachmentId": "ATT1", "size": 42},
                },
            ],
        },
    }
    summary = server._message_summary(message)
    assert summary["has_attachments"] is True


def test_quote_original():
    raw = server._build_raw_message("s@x.com", "S", "orig body", from_addr="me@gmail.com")
    quoted = server._quote_original(raw, "my reply")
    assert "my reply" in quoted
    assert "orig body" in quoted


def test_mime_attach_wraps_single_part():
    raw = server._build_raw_message("a@b.com", "S", "body", from_addr="me@gmail.com")
    out = server._mime_attach(server.b64url_encode(raw), server.b64url_encode(b"data"), "a.txt", None)
    msg = email.message_from_bytes(server.b64url_decode(out))
    filenames = [p.get_filename() for p in msg.walk() if p.get_filename()]
    assert filenames == ["a.txt"]


def test_mime_detach():
    raw = server._build_raw_message(
        "a@b.com",
        "S",
        "body",
        attachments=[{"content": server.b64url_encode(b"data"), "filename": "a.txt"}],
    )
    out = server._mime_detach(server.b64url_encode(raw), "a.txt")
    msg = email.message_from_bytes(server.b64url_decode(out))
    filenames = [p.get_filename() for p in msg.walk() if p.get_filename()]
    assert filenames == []


def test_apply_draft_edits():
    raw = server._build_raw_message("a@b.com", "Old", "old body", cc="c@x.com", from_addr="me@gmail.com")
    edited = server._apply_draft_edits(server.b64url_encode(raw), to="new@b.com", subject="New", body="new body")
    msg = email.message_from_bytes(server.b64url_decode(edited))
    assert msg["To"] == "new@b.com"
    assert msg["Subject"] == "New"
    assert "new body" in msg.as_string()
    assert "old body" not in msg.as_string()


def test_apply_draft_edits_clear_field():
    raw = server._build_raw_message("a@b.com", "S", "body", cc="c@x.com", from_addr="me@gmail.com")
    edited = server._apply_draft_edits(server.b64url_encode(raw), cc="")
    msg = email.message_from_bytes(server.b64url_decode(edited))
    assert msg["Cc"] is None
