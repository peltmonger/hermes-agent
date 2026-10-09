"""Tests for the terminal-handoff subscription contract.

Proves:
- A worker or cron context with ``subscribe_terminal=True`` on ``kanban_create``
  creates a ``wake-terminal-once`` subscription with the correct profile/session
  anchors.
- The subscription wakes only on accepted child completion or blockage (not on
  intermediate states like dependency, retry, crash, timeout, review).
- Failed delivery retries (cursor rewinds).
- No replay after the cursor advances (tick/reopen safe).
- No descendant inheritance.
- The wake synth carries session/result context (task title, assignee, handoff).
- Existing ``notify``, ``notify+wake``, and ``wake`` behavior is preserved.
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_MEDIA_ALLOW_DIRS", str(tmp_path))
    kb.init_db()
    return home


@pytest.fixture
def worker_env(monkeypatch, kanban_home):
    """Simulate a dispatcher-spawned worker environment."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "")
    monkeypatch.setenv("HERMES_PROFILE", "product-engineer")
    # Clear gateway session ContextVars (the dispatcher strips them).
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    return monkeypatch


# ---------------------------------------------------------------------------
# Contract: worker creates child with subscribe_terminal, subscription is
# wake-terminal-once with profile/session anchors
# ---------------------------------------------------------------------------


def test_subscribe_terminal_worker_context_creates_wake_terminal_once(kanban_home, worker_env):
    """A worker with HERMES_KANBAN_TASK set and a session_id on its owning task
    creates a wake-terminal-once subscription when subscribe_terminal=True."""
    # Set up: create the parent (worker's own) task with a session_id.
    conn = kbc.connect()
    try:
        parent_tid = kb.create_task(
            conn, title="parent worker task", assignee="product-engineer",
            session_id="origin-session-123",
        )
        os.environ["HERMES_KANBAN_TASK"] = parent_tid
        # Create a child task with subscribe_terminal=True via the tool handler.
        from tools.kanban_tools import _handle_create
        result = json.loads(_handle_create({
            "title": "child task",
            "assignee": "researcher-a",
            "subscribe_terminal": True,
        }))
        child_tid = result["task_id"]
        subs = kbn.list_notify_subs(conn, child_tid)
        assert len(subs) == 1, f"expected 1 subscription, got {len(subs)}"
        sub = subs[0]
        assert sub["delivery_mode"] == "wake-terminal-once"
        assert sub["platform"] == "api_server"
        assert sub["chat_id"] == "origin-session-123"
        assert sub["notifier_profile"] == "product-engineer"
    finally:
        os.environ.pop("HERMES_KANBAN_TASK", None)
        conn.close()


def test_subscribe_terminal_without_worker_task_returns_none(kanban_home, monkeypatch):
    """When no HERMES_KANBAN_TASK is set, _resolve_terminal_subscription_target
    returns None and no subscription is created."""
    monkeypatch.setenv("HERMES_PROFILE", "product-engineer")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_CRON_AUTO_DELIVER_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_CRON_AUTO_DELIVER_CHAT_ID", raising=False)
    from tools.kanban_tools import _resolve_terminal_subscription_target
    assert _resolve_terminal_subscription_target() is None


def test_subscribe_terminal_worker_without_session_id_returns_none(kanban_home, worker_env):
    """When the owning task has no session_id, the terminal target is None."""
    conn = kbc.connect()
    try:
        parent_tid = kb.create_task(
            conn, title="parent no session", assignee="product-engineer",
        )
        os.environ["HERMES_KANBAN_TASK"] = parent_tid
        from tools.kanban_tools import _resolve_terminal_subscription_target
        assert _resolve_terminal_subscription_target() is None
    finally:
        os.environ.pop("HERMES_KANBAN_TASK", None)
        conn.close()


# ---------------------------------------------------------------------------
# Contract: cron context resolves delivery target
# ---------------------------------------------------------------------------


def test_subscribe_terminal_cron_context(kanban_home, monkeypatch):
    """A cron job with auto-deliver target creates a wake-terminal-once
    subscription on the cron's delivery destination."""
    monkeypatch.setenv("HERMES_PROFILE", "product-engineer")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    # Simulate cron auto-deliver ContextVars.
    from gateway.session_context import _VAR_MAP
    _VAR_MAP["HERMES_CRON_AUTO_DELIVER_PLATFORM"].set("telegram")
    _VAR_MAP["HERMES_CRON_AUTO_DELIVER_CHAT_ID"].set("cron-chat-456")
    _VAR_MAP["HERMES_CRON_AUTO_DELIVER_THREAD_ID"].set("")
    try:
        from tools.kanban_tools import _resolve_terminal_subscription_target
        target = _resolve_terminal_subscription_target()
        assert target is not None
        assert target["platform"] == "telegram"
        assert target["chat_id"] == "cron-chat-456"
        assert target["delivery_mode"] == "wake-terminal-once"
    finally:
        _VAR_MAP["HERMES_CRON_AUTO_DELIVER_PLATFORM"].set("")
        _VAR_MAP["HERMES_CRON_AUTO_DELIVER_CHAT_ID"].set("")
        _VAR_MAP["HERMES_CRON_AUTO_DELIVER_THREAD_ID"].set("")


# ---------------------------------------------------------------------------
# Contract: no descendant inheritance
# ---------------------------------------------------------------------------


def test_wake_terminal_once_not_inherited_by_children(kanban_home, worker_env):
    """A wake-terminal-once subscription must NOT be inherited by child tasks."""
    conn = kbc.connect()
    try:
        parent_tid = kb.create_task(
            conn, title="parent", assignee="product-engineer",
            session_id="parent-session",
        )
        kbn.add_notify_sub(
            conn, task_id=parent_tid, platform="api_server",
            chat_id="parent-session", notifier_profile="product-engineer",
            delivery_mode="wake-terminal-once",
        )
        # Create a child task.
        child_tid = kb.create_task(
            conn, title="child", assignee="researcher-a",
            parents=(parent_tid,),
        )
        child_subs = kbn.list_notify_subs(conn, child_tid)
        assert len(child_subs) == 0, (
            "wake-terminal-once subscription was inherited by child — "
            "the contract requires no descendant inheritance"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Contract: no replay after tick/reopen (cursor-based dedup)
# ---------------------------------------------------------------------------


def test_wake_terminal_once_no_replay_after_advance(kanban_home):
    """After the cursor advances past a terminal event, a second tick must not
    re-claim the same event."""
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="no-replay", assignee="worker")
        kbn.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            delivery_mode="wake-terminal-once",
        )
        kb.complete_task(conn, tid, summary="first completion")
        # Claim the event.
        old_cursor, new_cursor, events = kbn.claim_unseen_events_for_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            kinds=["completed", "blocked"],
        )
        assert len(events) == 1
        assert events[0].kind == "completed"
        # Advance the cursor (simulating successful delivery).
        kbn.advance_notify_cursor(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            new_cursor=new_cursor,
        )
        # Second claim must return no events.
        _, _, events2 = kbn.claim_unseen_events_for_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            kinds=["completed", "blocked"],
        )
        assert len(events2) == 0, "cursor advanced but event was re-claimed"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Contract: failed delivery retries (cursor rewinds)
# ---------------------------------------------------------------------------


def test_wake_terminal_once_failed_delivery_rewinds_cursor(kanban_home):
    """When delivery fails, the cursor is rewound so the next tick retries."""
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="retry-test", assignee="worker")
        kbn.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            delivery_mode="wake-terminal-once",
        )
        kb.complete_task(conn, tid, summary="retry me")
        # Claim the event.
        old_cursor, new_cursor, events = kbn.claim_unseen_events_for_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            kinds=["completed", "blocked"],
        )
        assert len(events) == 1
        # Simulate delivery failure: rewind the cursor.
        rewound = kbn.rewind_notify_cursor(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            claimed_cursor=new_cursor, old_cursor=old_cursor,
        )
        assert rewound, "cursor rewind should succeed"
        # Next claim should see the event again.
        _, _, events2 = kbn.claim_unseen_events_for_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            kinds=["completed", "blocked"],
        )
        assert len(events2) == 1, "event should be re-claimable after rewind"
        assert events2[0].kind == "completed"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Contract: wake synth carries session/result context
# ---------------------------------------------------------------------------


def test_wake_terminal_once_synth_carries_result_context(kanban_home):
    """The wake synth text carries the task title, assignee, board, and the
    terminal handoff (summary for completed, reason for blocked)."""
    from gateway.config import Platform
    from gateway.kanban_watchers_notifier import _KanbanNotification
    from gateway.run import GatewayRunner

    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="context test", assignee="researcher-a")
        kbn.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            delivery_mode="wake-terminal-once",
        )
        kb.complete_task(conn, tid, summary="artifact: /tmp/result.pdf")
    finally:
        conn.close()

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    fake_adapter = MagicMock()
    fake_adapter.send = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: fake_adapter}

    # Claim the event.
    conn = kbc.connect()
    try:
        old_cursor, cursor, events = kbn.claim_unseen_events_for_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            kinds=["completed", "blocked"],
        )
        task = kb.get_task(conn, tid)
    finally:
        conn.close()

    d = {"sub": {"task_id": tid, "platform": "telegram", "chat_id": "chat-1",
                 "thread_id": "", "delivery_mode": "wake-terminal-once",
                 "notifier_profile": "", "delivery_metadata": {}},
         "old_cursor": old_cursor, "cursor": cursor, "events": events,
         "task": task, "board": "default"}
    notification = _KanbanNotification(
        runner, d, platform_cls=Platform, sub_fail_counts={})
    notification.plat = Platform("telegram")
    notification.adapter = fake_adapter
    from gateway.wake import adapter_supports_push
    notification.is_push_adapter = adapter_supports_push(fake_adapter)

    # Truncate events for wake-terminal-once (mirrors deliver()).
    notification.d = {**d, "events": events[:1]}
    for ev in events[:1]:
        notification.format_event(ev)
    notification.build_wake_text()

    assert notification.wake_terminal_once
    assert "context test" in notification.synth
    assert "researcher-a" in notification.synth
    assert "artifact: /tmp/result.pdf" in notification.synth
    assert "[Kanban terminal wake" in notification.synth


def test_wake_terminal_once_blocked_carries_reason(kanban_home):
    """When the terminal event is 'blocked', the synth carries the block reason."""
    from gateway.config import Platform
    from gateway.kanban_watchers_notifier import _KanbanNotification
    from gateway.run import GatewayRunner

    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="block context", assignee="worker")
        kbn.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            delivery_mode="wake-terminal-once",
        )
        kb.block_task(conn, tid, reason="need CEO approval for budget")
    finally:
        conn.close()

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    fake_adapter = MagicMock()
    fake_adapter.send = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: fake_adapter}

    conn = kbc.connect()
    try:
        old_cursor, cursor, events = kbn.claim_unseen_events_for_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            kinds=["completed", "blocked"],
        )
        task = kb.get_task(conn, tid)
    finally:
        conn.close()

    d = {"sub": {"task_id": tid, "platform": "telegram", "chat_id": "chat-1",
                 "thread_id": "", "delivery_mode": "wake-terminal-once",
                 "notifier_profile": "", "delivery_metadata": {}},
         "old_cursor": old_cursor, "cursor": cursor, "events": events,
         "task": task, "board": "default"}
    notification = _KanbanNotification(
        runner, d, platform_cls=Platform, sub_fail_counts={})
    notification.plat = Platform("telegram")
    notification.adapter = fake_adapter
    from gateway.wake import adapter_supports_push
    notification.is_push_adapter = adapter_supports_push(fake_adapter)

    notification.d = {**d, "events": events[:1]}
    for ev in events[:1]:
        notification.format_event(ev)
    notification.build_wake_text()

    assert "need CEO approval for budget" in notification.synth
    assert "block context" in notification.synth


# ---------------------------------------------------------------------------
# Contract: silent for intermediate states (dependency, retry, crash, etc.)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["crashed", "timed_out", "gave_up", "status", "review_requested"])
def test_wake_terminal_once_silent_for_intermediate_kinds(kanban_home, kind):
    """wake-terminal-once only claims completed/blocked events; intermediate
    kinds are not claimed."""
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="intermediate", assignee="worker")
        kbn.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            delivery_mode="wake-terminal-once",
        )
        # Emit an intermediate event.
        kb._append_event(conn, tid, kind=kind, payload={"status": "review"} if kind == "status" else {})
        # Claim with the wake-terminal-once event kinds filter.
        _, _, events = kbn.claim_unseen_events_for_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            kinds=["completed", "blocked"],
        )
        assert len(events) == 0, f"intermediate kind '{kind}' was claimed by wake-terminal-once"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Contract: one-shot delivery (unsub after delivery)
# ---------------------------------------------------------------------------


def test_wake_terminal_once_unsubs_after_delivery(kanban_home):
    """After successful delivery, the subscription is removed (one-shot)."""
    from gateway.config import Platform
    from gateway.run import GatewayRunner

    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="one-shot", assignee="worker")
        kbn.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            delivery_mode="wake-terminal-once",
        )
        kb.complete_task(conn, tid, summary="done one-shot")
    finally:
        conn.close()

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    fake_adapter = MagicMock()
    fake_adapter.send = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: fake_adapter}

    _orig_sleep = asyncio.sleep
    tick_count = 0

    async def _fast_sleep(delay):
        nonlocal tick_count
        if delay == 5:
            return
        await _orig_sleep(0)
        tick_count += 1
        if tick_count >= 3:
            runner._running = False

    async def _run():
        wake_mock = AsyncMock()
        with patch("gateway.run.asyncio.sleep", side_effect=_fast_sleep), \
             patch("gateway.wake.deliver_wake", new=wake_mock):
            await asyncio.wait_for(
                runner._kanban_notifier_watcher(interval=1),
                timeout=10.0,
            )

    asyncio.run(_run())

    # The subscription should be removed after one-shot delivery.
    conn = kbc.connect()
    try:
        subs = kbn.list_notify_subs(conn, tid)
    finally:
        conn.close()
    assert len(subs) == 0, "wake-terminal-once subscription should be removed after delivery"


# ---------------------------------------------------------------------------
# Contract: existing notify/notify+wake/wake behavior preserved
# ---------------------------------------------------------------------------


def test_existing_notify_mode_preserved(kanban_home):
    """Plain notify subscriptions still work and are not affected by the
    terminal subscription path."""
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="plain notify", assignee="worker")
        kbn.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
        )
        subs = kbn.list_notify_subs(conn, tid)
        assert len(subs) == 1
        assert subs[0]["delivery_mode"] == "notify"
    finally:
        conn.close()


def test_existing_notify_plus_wake_preserved(kanban_home):
    """notify+wake subscriptions still work."""
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="notify+wake", assignee="worker")
        kbn.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            delivery_mode="notify+wake",
        )
        subs = kbn.list_notify_subs(conn, tid)
        assert subs[0]["delivery_mode"] == "notify+wake"
    finally:
        conn.close()


def test_existing_wake_only_preserved(kanban_home):
    """wake-only subscriptions still work."""
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="wake only", assignee="worker")
        kbn.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            delivery_mode="wake",
        )
        subs = kbn.list_notify_subs(conn, tid)
        assert subs[0]["delivery_mode"] == "wake"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Contract: subscribe_terminal=False (default) does not create terminal sub
# ---------------------------------------------------------------------------


def test_subscribe_terminal_false_does_not_create_terminal_sub(kanban_home, worker_env):
    """When subscribe_terminal is False (default), no terminal subscription
    is created in a worker context (no passive session ContextVars)."""
    conn = kbc.connect()
    try:
        parent_tid = kb.create_task(
            conn, title="parent", assignee="product-engineer",
            session_id="parent-session",
        )
        os.environ["HERMES_KANBAN_TASK"] = parent_tid
        from tools.kanban_tools import _handle_create
        result = json.loads(_handle_create({
            "title": "child",
            "assignee": "researcher-a",
            # subscribe_terminal omitted (default False)
        }))
        child_tid = result["task_id"]
        subs = kbn.list_notify_subs(conn, child_tid)
        assert len(subs) == 0, "no subscription should be created without subscribe_terminal"
    finally:
        os.environ.pop("HERMES_KANBAN_TASK", None)
        conn.close()
