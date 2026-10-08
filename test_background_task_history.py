#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression: retrying an idempotent task must not destroy audit history."""
import tempfile
import time
import unittest
from pathlib import Path

from background_tasks import BackgroundTaskManager
from db_runtime import connect_db


class BackgroundTaskHistoryTests(unittest.TestCase):
    def test_retry_preserves_completed_task_and_active_deduplication(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = BackgroundTaskManager(Path(tmp) / "tasks.sqlite", workers=1)
            try:
                manager.register("copy", lambda payload: payload["value"])
                first, created = manager.enqueue(
                    "copy", {"value": 1}, idempotency_key="same-operation"
                )
                self.assertTrue(created)
                self._wait_for_state(manager, first, "done")
                second, created = manager.enqueue(
                    "copy", {"value": 2}, idempotency_key="same-operation"
                )
                self.assertTrue(created)
                self.assertNotEqual(first, second)
                self._wait_for_state(manager, second, "done")
                self.assertEqual(manager.get(first)["result"], 1)
                self.assertEqual(manager.get(second)["result"], 2)
                with connect_db(manager.db_path) as db:
                    rows = db.execute(
                        "SELECT id, idempotency_key FROM background_task ORDER BY id"
                    ).fetchall()
                self.assertEqual(rows, [(first, None), (second, "same-operation")])
            finally:
                manager.shutdown(timeout=3)

    def test_deferred_start_waits_for_handler_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = BackgroundTaskManager(Path(tmp) / "deferred.sqlite", workers=1, autostart=False)
            try:
                task_id, created = manager.enqueue(
                    "late-handler", {"value": 7}, idempotency_key="late-handler-key"
                )
                self.assertTrue(created)
                self.assertEqual(manager.get(task_id)["state"], "queued")
                manager.register("late-handler", lambda payload: payload["value"])
                manager.start()
                manager.start()  # Repeated startup must never spawn extra workers.
                self._wait_for_state(manager, task_id, "done")
                self.assertEqual(manager.get(task_id)["result"], 7)
                self.assertEqual(len(manager._threads), 1)
            finally:
                manager.shutdown(timeout=3)

    def test_missing_handler_requeue_keeps_original_priority(self):
        from unittest.mock import patch
        import background_tasks

        with tempfile.TemporaryDirectory() as tmp:
            manager = BackgroundTaskManager(Path(tmp) / "priority.sqlite", workers=1, autostart=False)
            original_push = background_tasks.heapq.heappush
            observed = []
            def record_push(heap, item):
                if item[2] == delayed:
                    observed.append(item[0])
                return original_push(heap, item)

            try:
                delayed, _ = manager.enqueue("not-registered-yet", {"value": 1}, priority=95)
                ready, _ = manager.enqueue("registered", {"value": 2}, priority=40)
                manager.register("registered", lambda payload: payload["value"])
                with patch.object(background_tasks.heapq, "heappush", side_effect=record_push):
                    manager.start()
                    self._wait_for_state(manager, ready, "done")
                    deadline = time.monotonic() + 3
                    while not observed and time.monotonic() < deadline:
                        time.sleep(0.02)
                self.assertTrue(observed, "Missing handler was never requeued")
                self.assertEqual(set(observed), {95})
                self.assertEqual(manager.get(delayed)["state"], "queued")
            finally:
                manager.shutdown(timeout=3)

    @staticmethod
    def _wait_for_state(manager, task_id, state):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            item = manager.get(task_id)
            if item and item["state"] == state:
                return
            time.sleep(0.02)
        raise AssertionError(f"task {task_id} did not reach {state}")


if __name__ == "__main__":
    unittest.main()
