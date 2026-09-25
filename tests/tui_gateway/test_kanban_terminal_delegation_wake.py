"""Terminal direct-delegation wake filtering and exactly-once collection."""
from __future__ import annotations

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from tui_gateway.server import _collect_kanban_notifications

SOURCE_TASK = "t_parent"
SOURCE_SESSION = "parent-terminal-session"


def _session():
    return {"session_key": SOURCE_SESSION}


def _direct_child():
    conn = kbc.connect()
    try:
        child = kb.create_task(conn, title="child title", assignee="child")
        kbn.add_notify_sub(
            conn, task_id=child, platform="tui", chat_id=SOURCE_SESSION,
            notifier_profile="parent", delivery_mode="wake-terminal-once",
            source_task_id=SOURCE_TASK, source_profile="parent", source_session_id=SOURCE_SESSION,
        )
        return child
    finally:
        conn.close()


def _block(child, kind):
    conn = kbc.connect()
    try:
        assert kb.block_task(conn, child, reason=f"{kind} reason", kind=kind)
    finally:
        conn.close()


def test_terminal_wake_ignores_transient_blocker_then_wakes_once_for_completion():
    child = _direct_child()
    _block(child, "transient")

    assert _collect_kanban_notifications(_session()) == []
    conn = kbc.connect()
    try:
        assert kb.unblock_task(conn, child)
        assert kb.complete_task(conn, child, summary="child completed", metadata={"verified": True})
    finally:
        conn.close()

    wakes = _collect_kanban_notifications(_session())
    assert len(wakes) == 1
    assert child in wakes[0]
    assert SOURCE_TASK in wakes[0]
    assert "completed" in wakes[0]
    assert "child completed" in wakes[0]
    assert _collect_kanban_notifications(_session()) == []


def test_terminal_wake_accepts_only_needs_input_or_capability_blockers():
    for kind in ("needs_input", "capability"):
        child = _direct_child()
        _block(child, kind)
        wakes = _collect_kanban_notifications(_session())
        assert len(wakes) == 1
        assert kind in wakes[0]
        assert _collect_kanban_notifications(_session()) == []
