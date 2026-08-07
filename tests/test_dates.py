from datetime import datetime, timedelta, timezone

import server


def test_parse_datetime_relative():
    before = datetime.now(timezone.utc)
    dt = server._parse_datetime("2h")
    after = datetime.now(timezone.utc)
    assert before + timedelta(seconds=7200) - dt < timedelta(seconds=10)
    assert dt >= before
    assert dt <= after + timedelta(seconds=7200)


def test_parse_datetime_iso():
    dt = server._parse_datetime("2026-08-07T10:30:00")
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.hour == 10

    dt2 = server._parse_datetime("2026-08-07")
    assert dt2 is not None


def test_parse_datetime_invalid():
    assert server._parse_datetime("not-a-date") is None


def test_parse_gmail_relative():
    assert server._parse_gmail_relative("2h") == "2h"
    assert server._parse_gmail_relative("30m") == "30m"
    assert server._parse_gmail_relative("3d") == "3d"
    assert server._parse_gmail_relative("nope") is None


def test_build_gmail_query_all_filters():
    q = server._build_gmail_query(
        query="budget",
        from_address="a@b.com",
        to_address="me@gmail.com",
        subject="report",
        after_date="2h",
        before_date="2026/08/01",
        unread_only=True,
        has_attachments=True,
    )
    assert "budget" in q
    assert "from:(a@b.com)" in q
    assert "to:(me@gmail.com)" in q
    assert "subject:(report)" in q
    assert "is:unread" in q
    assert "has:attachment" in q
    assert "newer_than:2h" in q
    assert "before:2026/08/01" in q


def test_build_gmail_query_no_attachments():
    q = server._build_gmail_query(has_attachments=False)
    assert "-has:attachment" in q


def test_build_gmail_query_empty():
    assert server._build_gmail_query() == ""


def test_resolve_label_well_known():
    from gmail_client import GmailClient

    client = GmailClient()
    assert server._resolve_label(client, "INBOX") == "INBOX"
    assert server._resolve_label(client, "Sent Items") == "SENT"
    assert server._resolve_label(client, "spam") == "SPAM"
    assert server._resolve_label(client, "Deleted Items") == "TRASH"


def test_is_archive():
    assert server._is_archive("Archive")
    assert server._is_archive("ARCHIVE")
    assert not server._is_archive("INBOX")
