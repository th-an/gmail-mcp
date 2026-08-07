import os

import verify_setup


def test_load_env_missing(tmp_path):
    env = verify_setup.load_env(str(tmp_path / ".env"))
    assert "__missing__" in env


def test_load_env_parses(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        '# comment\nGMAIL_CLIENT_ID="abc.apps.googleusercontent.com"\n\n'
        "GMAIL_FULL_ACCESS=1\nGMAIL_CLIENT_SECRET='secret123'\n"
    )
    env = verify_setup.load_env(str(env_file))
    assert env["GMAIL_CLIENT_ID"] == "abc.apps.googleusercontent.com"
    assert env["GMAIL_FULL_ACCESS"] == "1"
    assert env["GMAIL_CLIENT_SECRET"] == "secret123"
    assert "__missing__" not in env


def test_client_id_regex():
    good = [
        "123456789012-abc123.apps.googleusercontent.com",
        "8765432109876-a1b2c3d4e5f6g7h8.apps.googleusercontent.com",
    ]
    bad = [
        "abc.apps.googleusercontent.com",
        "123456789012-apps.googleusercontent.com",
        "123456789012-abc.googleusercontent.com",
        "",
    ]
    for value in good:
        assert verify_setup.CLIENT_ID_RE.match(value), value
    for value in bad:
        assert not verify_setup.CLIENT_ID_RE.match(value), value
