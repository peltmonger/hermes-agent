"""CEO completion subscription filtering and single-delivery guarantees."""
from __future__ import annotations

import asyncio

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn


class RecordingAdapter:
    def __init__(self, fail_send: bool = False):
        self.sent = []
        self.fail_send = fail_send

    async def send(self, chat_id, text, metadata=None):
        self.sent.append((chat_id, text, metadata or {}))
        if self.fail_send:
            raise RuntimeError("transient send failure")


async def _tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    return runner


def _special_sub(conn, task_id):
    kbn.add_notify_sub(
        conn, task_id=task_id, platform="telegram", chat_id="ceo-chat",
        delivery_mode="notify", notice_policy="ceo-completion",
    )


def test_ceo_notice_only_sends_first_needs_input_event_and_unsubscribes(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb.init_db()
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="CEO parent", assignee="worker")
        _special_sub(conn, task_id)
        kb._append_event(conn, task_id, "blocked", {"kind": "capability", "reason": "no token"})
        kb._append_event(conn, task_id, "blocked", {"kind": "needs_input", "reason": "CEO choice needed"})
        kb._append_event(conn, task_id, "completed", {"summary": "must not send twice"})

    adapter = RecordingAdapter()
    asyncio.run(_tick(monkeypatch, _runner(adapter)))

    assert len(adapter.sent) == 1
    assert "CEO choice needed" in adapter.sent[0][1]
    with kbc.connect() as conn:
        assert kbn.list_notify_subs(conn, task_id) == []

    asyncio.run(_tick(monkeypatch, _runner(adapter)))
    assert len(adapter.sent) == 1


def test_ceo_notice_rewinds_after_failure_then_sends_once(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb.init_db()
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="CEO parent", assignee="worker")
        _special_sub(conn, task_id)
        kb.complete_task(conn, task_id, summary="done")

    failed = RecordingAdapter(fail_send=True)
    asyncio.run(_tick(monkeypatch, _runner(failed)))
    with kbc.connect() as conn:
        assert len(kbn.list_notify_subs(conn, task_id)) == 1

    healthy = RecordingAdapter()
    asyncio.run(_tick(monkeypatch, _runner(healthy)))
    assert len(healthy.sent) == 1
    with kbc.connect() as conn:
        assert kbn.list_notify_subs(conn, task_id) == []
