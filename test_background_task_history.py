#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression: retrying an idempotent task must not destroy audit history."""
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from background_tasks import BackgroundTaskManager


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
                with sqlite3.connect(manager.db_path) as db:
                    rows = db.execute(
                        "SELECT id, idempotency_key FROM background_task ORDER BY id"
                    ).fetchall()
                self.assertEqual(rows, [(first, None), (second, "same-operation")])
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
