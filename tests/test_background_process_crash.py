#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Actual interpreter death must not lose a claimed durable task."""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from background_tasks import BackgroundTaskManager
from db_runtime import connect_db


_CRASH_WORKER = r'''
import os
import sys
import time
from pathlib import Path
from background_tasks import BackgroundTaskManager

database, marker = sys.argv[1:3]
manager = BackgroundTaskManager(database, workers=1, autostart=False)
def crash(payload):
    Path(marker).write_text(str(payload["value"]), encoding="utf-8")
    # Exit without finally, shutdown or commit of the claimed task.
    os._exit(47)
manager.register("crash-once", crash)
manager.start()
time.sleep(12)
os._exit(91)
'''


class ActualCrashRecoveryTests(unittest.TestCase):
    def test_abrupt_process_exit_retries_same_task_and_keeps_audit(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "tasks.sqlite3"
            marker = root / "handler-entered.txt"
            producer = BackgroundTaskManager(db_path, workers=1, autostart=False)
            try:
                task_id, created = producer.enqueue(
                    "crash-once", {"value": "original"},
                    idempotency_key="verified-crash:original",
                )
                self.assertTrue(created)
            finally:
                producer.shutdown()

            environment = dict(os.environ)
            project = str(Path(__file__).resolve().parent)
            environment["PYTHONPATH"] = os.pathsep.join(
                (project, environment.get("PYTHONPATH", ""))
            )
            child = subprocess.run(
                [sys.executable, "-c", _CRASH_WORKER, str(db_path), str(marker)],
                cwd=project, env=environment, timeout=18,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(
                child.returncode, 47,
                f"crash simulation didn't reach handler: {child.stderr[-1200:]!r}",
            )
            self.assertEqual(marker.read_text(encoding="utf-8"), "original")
            with connect_db(db_path) as conn:
                state = conn.execute(
                    "SELECT state FROM background_task WHERE id=?", (task_id,)
                ).fetchone()[0]
            self.assertEqual(state, "running")

            # Fresh process/manager reconstructs the queue from SQLite WAL.
            recovered = BackgroundTaskManager(db_path, workers=1, autostart=False)
            completed = []
            try:
                pending = recovered.get(task_id)
                self.assertEqual(pending["state"], "queued")
                self.assertIn("意外中断", pending["error"])
                def complete(payload):
                    completed.append(payload["value"])
                    return {"recovered": True, "value": payload["value"]}
                recovered.register("crash-once", complete)
                recovered.start()
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    if recovered.get(task_id)["state"] == "done":
                        break
                    time.sleep(0.02)
                final = recovered.get(task_id)
                self.assertEqual(final["state"], "done")
                self.assertEqual(final["result"]["value"], "original")
                self.assertEqual(completed, ["original"])
                self.assertEqual(marker.read_text(encoding="utf-8"), "original")
                with connect_db(db_path) as conn:
                    self.assertEqual(conn.execute(
                        "SELECT COUNT(*) FROM background_task WHERE id=?", (task_id,)
                    ).fetchone()[0], 1)
            finally:
                recovered.shutdown(timeout=4)


if __name__ == "__main__":
    unittest.main()
