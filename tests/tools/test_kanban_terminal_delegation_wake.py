"""Direct dispatcher-worker terminal wake subscriptions are opt-in and one-shot."""
from __future__ import annotations

import json

from hermes_state import SessionDB


def _profiles(home):
    for name in ("parent", "child"):
        profile = home / "profiles" / name
        profile.mkdir(parents=True, exist_ok=True)
        (profile / "config.yaml").write_text("{}\n")


def _worker(monkeypatch, tmp_path):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    home = tmp_path / ".hermes"
    home.mkdir()
    _profiles(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "parent")
    monkeypatch.setenv("HERMES_SESSION_ID", "parent-terminal-session")
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    state = SessionDB(db_path=home / "state.db")
    state.create_session("parent-terminal-session", source="tui", profile_name="parent")
    state.close()
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="parent", session_id="parent-terminal-session")
        kb.claim_task(conn, parent)
        run_id = kb.get_task(conn, parent).current_run_id
    monkeypatch.setenv("HERMES_KANBAN_TASK", parent)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return parent


def test_direct_terminal_wake_is_opt_in_and_persists_exact_provenance(monkeypatch, tmp_path):
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_notify as kbn
    from tools import kanban_tools as kt

    parent = _worker(monkeypatch, tmp_path)
    created = json.loads(kt._handle_create({
        "title": "direct child", "assignee": "child", "delivery_mode": "wake-terminal-once",
    }))

    assert created["ok"] is True, created
    assert created["subscribed"] is True
    assert created["terminal_wake_subscribed"] is True
    with kbc.connect() as conn:
        subs = kbn.list_notify_subs(conn, created["task_id"])
    assert len(subs) == 1
    sub = subs[0]
    assert sub["delivery_mode"] == "wake-terminal-once"
    assert sub["platform"] == "tui"
    assert sub["chat_id"] == "parent-terminal-session"
    assert sub["source_task_id"] == parent
    assert sub["source_profile"] == "parent"
    assert sub["source_session_id"] == "parent-terminal-session"


def test_create_without_direct_mode_persists_no_terminal_wake(monkeypatch, tmp_path):
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_notify as kbn
    from tools import kanban_tools as kt

    _worker(monkeypatch, tmp_path)
    created = json.loads(kt._handle_create({"title": "ordinary child", "assignee": "child"}))

    assert created["ok"] is True, created
    assert created["terminal_wake_subscribed"] is False
    with kbc.connect() as conn:
        assert kbn.list_notify_subs(conn, created["task_id"]) == []


def test_direct_terminal_wake_fails_closed_for_same_profile(monkeypatch, tmp_path):
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_notify as kbn
    from tools import kanban_tools as kt

    _worker(monkeypatch, tmp_path)
    created = json.loads(kt._handle_create({
        "title": "same profile", "assignee": "parent", "delivery_mode": "wake-terminal-once",
    }))

    assert created["ok"] is True, created
    assert created["terminal_wake_subscribed"] is False
    with kbc.connect() as conn:
        assert kbn.list_notify_subs(conn, created["task_id"]) == []
