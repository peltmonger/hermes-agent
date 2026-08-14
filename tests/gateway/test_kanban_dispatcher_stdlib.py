"""Plugin-free gateway dispatcher regression coverage.

These tests intentionally use only ``unittest``, ``asyncio`` and ``unittest.mock``.
They execute the real watcher against temporary board storage, so the critical
failure paths remain runnable where pytest-asyncio is unavailable.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gateway.kanban_watchers import GatewayKanbanWatchersMixin
from hermes_cli import kanban_db as kb


class _Runner(GatewayKanbanWatchersMixin):
    def __init__(self) -> None:
        self._running = True


class GatewayDispatcherStdlibTests(unittest.TestCase):
    """Exercise one real dispatcher watcher iteration per scenario."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / ".hermes"
        self.home.mkdir()
        self.env = patch.dict(os.environ, {"HERMES_HOME": str(self.home)}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.old_board_env = {
            key: os.environ.pop(key, None)
            for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD")
        }
        self.addCleanup(self._restore_board_env)
        self.addCleanup(self.tempdir.cleanup)
        kb.init_db()

    def _restore_board_env(self) -> None:
        for key, value in self.old_board_env.items():
            if value is not None:
                os.environ[key] = value

    @staticmethod
    def _config() -> dict:
        return {"kanban": {"dispatch_in_gateway": True, "dispatch_interval_seconds": 1}}

    def _run_one_tick(self, *, patches=()):
        runner = _Runner()
        calls = []

        async def to_thread(fn, *args, **kwargs):
            calls.append(getattr(fn, "__name__", ""))
            result = fn(*args, **kwargs)
            # health is the final to_thread operation of a watcher iteration.
            if getattr(fn, "__name__", "") == "_health_tick":
                runner._running = False
            return result

        async def sleep(_delay):
            return None

        base = [
            patch("hermes_cli.config.load_config", self._config),
            patch("gateway.kanban_watchers.asyncio.to_thread", to_thread),
            patch("gateway.kanban_watchers.asyncio.sleep", sleep),
            patch.object(kb, "migrate_board_owners", lambda: None),
            patch("hermes_cli.profiles.profile_exists", return_value=True),
            *patches,
        ]
        with base[0], base[1], base[2], base[3]:
            for extra in base[4:]:
                extra.start()
            try:
                asyncio.run(asyncio.wait_for(runner._kanban_dispatcher_watcher(), timeout=3))
            finally:
                for extra in reversed(base[4:]):
                    extra.stop()
        return calls

    def _outcomes(self):
        conn = kb.connect(ignore_env=True)
        try:
            return list(conn.execute("SELECT outcome, metadata FROM dispatcher_tick_outcome ORDER BY id"))
        finally:
            conn.close()

    def test_lease_acquisition_failure_is_durable(self):
        calls = self._run_one_tick(patches=(
            patch.object(kb, "acquire_dispatcher_lease", return_value=False),
        ))
        self.assertIn("_tick_once", calls)
        self.assertIn("lease_acquisition_failed", [row["outcome"] for row in self._outcomes()])

    def test_enumeration_failure_uses_default_metadata_and_records_outcome(self):
        durable_writes = []
        real_record = kb.record_dispatcher_tick_outcome

        def record(conn, **kwargs):
            durable_writes.append(kwargs)
            return real_record(conn, **kwargs)

        calls = self._run_one_tick(patches=(
            patch.object(kb, "list_boards", side_effect=OSError("registry down")),
            patch.object(kb, "record_dispatcher_tick_outcome", side_effect=record),
        ))
        self.assertIn("_tick_once", calls)
        rows = self._outcomes()
        self.assertTrue(rows)
        self.assertTrue(any(
            item["outcome"] == "board_enumeration_failed"
            and item["metadata"].get("fallback") == "default_metadata"
            for item in durable_writes
        ))

    def test_coordination_open_failure_writes_fsynced_jsonl_fallback(self):
        real_connect = kb.connect

        def fail_default(*args, **kwargs):
            if kwargs.get("board") == kb.DEFAULT_BOARD and kwargs.get("ignore_env"):
                raise OSError("coordination unavailable")
            return real_connect(*args, **kwargs)

        self._run_one_tick(patches=(patch.object(kb, "connect", side_effect=fail_default),))
        fallback = self.home / "kanban" / "dispatcher_tick_outcomes.jsonl"
        self.assertTrue(fallback.exists())
        payloads = [json.loads(line) for line in fallback.read_text().splitlines()]
        self.assertTrue(any(item["outcome"] == "board_enumeration_failed" for item in payloads))

    def test_quarantined_board_and_health_failure_are_isolated(self):
        broken = self.home / "kanban" / "boards" / "broken" / "kanban.db"
        broken.parent.mkdir(parents=True)
        broken.write_text("not sqlite", encoding="utf-8")
        boards = [{"slug": kb.DEFAULT_BOARD}, {"slug": "broken", "db_path": str(broken)}]
        health_seen = []
        real_health = kb.board_health

        def capture_health(conn, **kwargs):
            health_seen.append(kwargs.get("board"))
            return real_health(conn, **kwargs)

        self._run_one_tick(patches=(
            patch.object(kb, "list_boards", return_value=boards),
            patch.object(kb, "board_health", side_effect=capture_health),
        ))
        self.assertIn(kb.DEFAULT_BOARD, health_seen)
        self.assertIn("board_scan_failed", [row["outcome"] for row in self._outcomes()])

    def test_outcome_db_failure_writes_jsonl_sidecar(self):
        self._run_one_tick(patches=(
            patch.object(kb, "record_dispatcher_tick_outcome", side_effect=OSError("write failed")),
        ))
        fallback = self.home / "kanban" / "dispatcher_tick_outcomes.jsonl"
        self.assertTrue(fallback.exists())
        self.assertTrue(any(json.loads(line)["outcome"] == "ok" for line in fallback.read_text().splitlines()))

    def test_owner_replacement_before_claim_leaves_ready_and_review_unmutated(self):
        conn = kb.connect(ignore_env=True)
        try:
            ready_id = kb.create_task(conn, title="ready", assignee="worker")
            review_id = kb.create_task(conn, title="review", assignee="worker")
            conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (review_id,))
            conn.commit()
        finally:
            conn.close()

        # Acquisition succeeds, then the live fence observes replacement before
        # dispatch. The real watcher must leave both kinds of claim untouched.
        self._run_one_tick(patches=(
            patch.object(kb, "dispatcher_lease_owned", return_value=False),
        ))
        conn = kb.connect(ignore_env=True)
        try:
            ready = kb.get_task(conn, ready_id)
            review = kb.get_task(conn, review_id)
        finally:
            conn.close()
        self.assertIsNotNone(ready)
        self.assertIsNotNone(review)
        self.assertEqual((ready.status, ready.claim_lock), ("ready", None))
        self.assertEqual((review.status, review.claim_lock), ("review", None))

    def test_explicit_boards_ignore_conflicting_database_pin_for_dispatch_and_health(self):
        explicit = "explicit"
        kb.create_board(explicit)
        explicit_conn = kb.connect(board=explicit, ignore_env=True)
        try:
            task_id = kb.create_task(explicit_conn, title="dispatch me", assignee="worker")
        finally:
            explicit_conn.close()
        pinned = self.home / "pinned.db"
        kb.init_db(pinned)
        dispatched = []
        health_boards = []

        def no_spawn(task, workspace):
            dispatched.append(task.id)
            return 12345

        def health(conn, **kwargs):
            health_boards.append(kwargs["board"])
            return []

        with patch.dict(os.environ, {"HERMES_KANBAN_DB": str(pinned)}):
            self._run_one_tick(patches=(
                patch.object(kb, "list_boards", return_value=[{"slug": explicit}]),
                patch.object(kb, "_default_spawn", side_effect=no_spawn),
                patch.object(kb, "board_health", side_effect=health),
            ))
        self.assertEqual(dispatched, [task_id])
        self.assertEqual(health_boards, [explicit])


if __name__ == "__main__":
    unittest.main()
