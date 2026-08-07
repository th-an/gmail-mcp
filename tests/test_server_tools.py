import asyncio

import server

EXPECTED_CATEGORIES = {
    "Connection": {"get_server_info", "test_email_connection", "get_email_profile"},
    "Read": {
        "read_emails",
        "read_full_email",
        "get_unread_emails",
        "search_emails",
        "advanced_search_emails",
        "get_email_thread",
        "get_message_metadata",
        "export_email_mime",
    },
    "Send": {"send_email", "send_email_with_attachments", "send_email_from_template"},
    "Compose": {
        "send_reply",
        "forward_email",
        "create_draft",
        "edit_draft",
        "send_draft",
        "schedule_send",
        "list_scheduled_sends",
        "cancel_scheduled_send",
        "flush_scheduled_sends",
        "save_email_template",
        "list_email_templates",
        "delete_email_template",
    },
    "Manage": {
        "get_email_folders",
        "mark_email_read",
        "mark_email_unread",
        "move_email",
        "move_emails",
        "copy_email",
        "delete_email",
        "permanent_delete_email",
        "create_folder",
        "rename_folder",
        "delete_folder",
        "list_message_ids",
        "clear_folder",
        "mark_folder_read",
        "flag_email",
        "follow_up_email",
        "clear_email_flag",
        "pin_email",
    },
    "Attachments": {
        "list_email_attachments",
        "download_attachment",
        "add_attachment_to_email",
        "remove_attachment_from_email",
    },
    "Automation": {
        "triage_inbox",
        "auto_organize",
        "send_email_digest",
        "email_analytics",
        "dedupe_emails",
        "batch_request",
    },
    "Gmail extras": {
        "star_email",
        "unstar_email",
        "mark_important",
        "unmark_important",
        "list_email_aliases",
        "set_vacation_responder",
    },
}


def _all_tools():
    return asyncio.run(server.mcp.list_tools())


def test_tool_count():
    assert len(_all_tools()) == 60


def test_all_expected_tools_present():
    names = {t.name for t in _all_tools()}
    expected = set().union(*EXPECTED_CATEGORIES.values())
    assert expected == names


def test_every_tool_has_description():
    for t in _all_tools():
        assert t.description, f"tool {t.name} is missing a description"


def test_category_counts():
    names = {t.name for t in _all_tools()}
    for category, tools in EXPECTED_CATEGORIES.items():
        assert tools <= names, f"{category} missing {tools - names}"
