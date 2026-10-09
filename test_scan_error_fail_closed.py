#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A failed scan-error journal cannot authorize missing-photo reconciliation."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import photo_curator as core


class ScanErrorFailClosedTests(unittest.TestCase):
    def test_error_callback_failure_escapes_directory_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "photos"
            root.mkdir()
            def failing_scan(_root):
                raise PermissionError("unreadable subdirectory")
            with patch.object(core.os, "scandir", side_effect=failing_scan):
                with self.assertRaisesRegex(OSError, "cannot journal scan error"):
                    list(core.iter_images(
                        root, recursive=True,
                        on_error=lambda error: (_ for _ in ()).throw(
                            OSError("cannot journal scan error"))))

    def test_failed_catalog_error_record_aborts_shared_scan_before_finalize(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "photographs"
            root.mkdir()
            session = {"session_id": "test-session", "root_id": "test-root",
                       "root_path": str(root), "identity_key": "disk-A"}
            def fail_during_walk(_folder, recursive=False, on_error=None):
                self.assertTrue(recursive)
                on_error(PermissionError("I/O failure"))
                return []
            with patch.object(core, "_SCAN_SNAPSHOTS", {}), \
                 patch.object(core, "begin_catalog_scan", return_value=session), \
                 patch.object(core, "iter_images", side_effect=fail_during_walk), \
                 patch.object(core, "note_catalog_scan_error",
                              side_effect=OSError("database unavailable")) as journal, \
                 patch.object(core, "abort_catalog_scan") as abort, \
                 patch.object(core, "finish_catalog_scan") as finish:
                with self.assertRaisesRegex(RuntimeError, "安全终止"):
                    core._shared_list_images(root, recursive=True, max_age=0)
                self.assertEqual(journal.call_count, 1)
                self.assertEqual(abort.call_count, 1)
                finish.assert_not_called()
                snapshot = core.current_scan_snapshot(root)
                self.assertEqual(snapshot.get("state"), "failed")


if __name__ == "__main__":
    unittest.main()
