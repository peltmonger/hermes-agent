"""CEO completion route creation and strict body classification."""
from __future__ import annotations

import json

import pytest


CEO_PARENT = """notification: ceo-completion
accountable_lead: engineering-lead
ceo_origin: direct

Implement the approved change."""


@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("kanban:\n  auto_subscribe_on_create: false\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_worker")
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "ceo-chat")
    monkeypatch.setenv("HERMES_SESSION_THREAD_ID", "topic-1")
    monkeypatch.setenv("HERMES_SESSION_CHAT_TYPE", "dm")
    from gateway.session_context import reset_session_vars
    reset_session_vars()
    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()


def _subs(task_id):
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_notify as kbn
    with kbc.connect() as conn:
        return kbn.list_notify_subs(conn, task_id)


def test_classify_ceo_completion_parent_is_strict_and_crlf_safe():
    from hermes_cli.kanban_db_notify import classify_ceo_completion_parent

    assert classify_ceo_completion_parent(CEO_PARENT)
    assert classify_ceo_completion_parent(CEO_PARENT.replace("\n", "\r\n"))
    assert not classify_ceo_completion_parent("\n" + CEO_PARENT)
    assert not classify_ceo_completion_parent(CEO_PARENT.replace("engineering-lead", ""))
    assert not classify_ceo_completion_parent(CEO_PARENT.replace("engineering-lead", "Engineering Lead"))
    assert not classify_ceo_completion_parent(CEO_PARENT.replace(
        "ceo_origin: direct", "ceo_origin: direct\nceo_notification: none",
    ))
    assert not classify_ceo_completion_parent(CEO_PARENT.replace(
        "ceo_origin: direct", "ceo_origin: direct\nceo_completion_parent: t_root",
    ))
    assert not classify_ceo_completion_parent("notification: ceo-completion\nceo_origin: direct\naccountable_lead: engineering-lead")


def test_tool_create_routes_only_qualifying_parent_despite_auto_subscribe_false(worker_env):
    from tools import kanban_tools as kt

    parent = json.loads(kt._handle_create({"title": "CEO parent", "body": CEO_PARENT, "assignee": "peer"}))
    child = json.loads(kt._handle_create({
        "title": "Delegated child",
        "body": "ceo_completion_parent: t_root\nceo_notification: none\nwork",
        "assignee": "peer",
    }))
    ordinary = json.loads(kt._handle_create({"title": "ordinary", "body": "work", "assignee": "peer"}))

    assert parent["subscribed"] is True
    [sub] = _subs(parent["task_id"])
    assert sub["platform"] == "telegram"
    assert sub["chat_id"] == "ceo-chat"
    assert sub["thread_id"] == "topic-1"
    assert sub["delivery_mode"] == "notify"
    assert sub["notice_policy"] == "ceo-completion"
    assert child["subscribed"] is False
    assert ordinary["subscribed"] is False
    assert _subs(child["task_id"]) == []
    assert _subs(ordinary["task_id"]) == []


def test_gateway_slash_route_uses_same_ceo_classifier(monkeypatch, tmp_path):
    import asyncio
    from gateway.config import Platform
    from gateway.platforms.event import MessageEvent
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_notify as kbn

    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb.init_db()
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="CEO parent", body=CEO_PARENT)
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._kanban_notifier_profile = "default"
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="ceo-chat", chat_type="dm", thread_id="topic-1")
    event = MessageEvent(text="/kanban create", source=source)

    assert asyncio.run(runner._kanban_auto_subscribe(event, task_id, None))
    with kbc.connect() as conn:
        [sub] = kbn.list_notify_subs(conn, task_id)
    assert sub["delivery_mode"] == "notify"
    assert sub["notice_policy"] == "ceo-completion"
