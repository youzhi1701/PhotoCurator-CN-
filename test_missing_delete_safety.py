#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""File safety: a disconnected device must never be mistaken for deletion."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import photo_curator
from db_runtime import connect_db


class MissingFileDeleteSafetyTests(unittest.TestCase):
    def test_missing_file_does_not_become_permanently_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "missing.jpg")
            with patch.object(photo_curator, "_find_original_for_path", return_value=missing), \
                 patch.object(photo_curator, "_media_state_get", return_value={"state": "pending_permanent_delete"}), \
                 patch.object(photo_curator, "_apply_media_lifecycle") as lifecycle:
                with self.assertRaises(FileNotFoundError):
                    photo_curator._background_permanent_delete({"path": missing})
                lifecycle.assert_not_called()

    def test_previously_verified_deletion_remains_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "already_deleted.jpg")
            with patch.object(photo_curator, "_find_original_for_path", return_value=missing), \
                 patch.object(photo_curator, "_media_state_get", return_value={"state": "permanently_deleted"}):
                result = photo_curator._background_permanent_delete({"path": missing})
            self.assertTrue(result["ok"])
            self.assertTrue(result["already_done"])

    def test_missing_trash_file_keeps_recovery_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "records.sqlite"
            missing = str(Path(tmp) / "disconnected" / "photo.jpg")
            with connect_db(db_path) as db:
                db.execute(
                    "CREATE TABLE software_trash (id INTEGER PRIMARY KEY, trash_path TEXT NOT NULL)"
                )
                db.execute("INSERT INTO software_trash(id,trash_path) VALUES(?,?)", (1, missing))
                db.commit()
            with patch.object(photo_curator, "INDEX_DB", db_path):
                with self.assertRaises(FileNotFoundError):
                    photo_curator._purge_trash_item(1)
            with connect_db(db_path) as db:
                row = db.execute("SELECT trash_path FROM software_trash WHERE id=1").fetchone()
            self.assertEqual(row[0], missing)


    def test_remove_library_metadata_never_cleans_offline_previews(self):
        with patch.object(photo_curator, "catalog_remove_library_root",
                          return_value={"source_id": "device", "display_name": "test",
                                        "media_count": 1, "source_removed": False}) as remove, \
             patch.object(photo_curator, "catalog_clear_offline_previews") as clear, \
             patch.object(photo_curator, "_activity"):
            response = photo_curator.app.test_client().post(
                "/api/library-root-remove", json={"root_id": "sample"}
            )
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(response.get_json()["preview_preserved"])
        self.assertEqual(response.get_json()["preview_removed"], 0)
        remove.assert_called_once()
        clear.assert_not_called()

    def test_catalog_failure_never_silently_scans_unindexed_photos(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "library"
            root.mkdir()
            with patch.object(photo_curator, "begin_catalog_scan",
                              side_effect=OSError("database unavailable")), \
                 patch.object(photo_curator, "iter_images") as walker:
                with self.assertRaisesRegex(RuntimeError, "图库索引无法建立"):
                    photo_curator._shared_list_images(root, recursive=True)
                walker.assert_not_called()

    def test_manual_delete_marker_accepts_clear_photos_without_file_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "good.jpg")
            Path(path).write_bytes(b"test")
            original = {
                "path": path, "tier": "sharp", "move_selected": False,
                "lifecycle": "normal",
            }
            with patch.dict(photo_curator.state["cull"],
                            {"photos": [original], "overrides": {}}, clear=False), \
                 patch.object(photo_curator, "_save_review_overrides") as persist, \
                 patch.object(photo_curator, "_activity"), \
                 patch.object(photo_curator.TASK_MANAGER, "enqueue") as enqueue:
                response = photo_curator.app.test_client().post(
                    "/api/select-blurry", json={"path": path, "selected": True}
                )
                self.assertEqual(response.status_code, 200, response.get_json())
                self.assertEqual(response.get_json()["selected"], 1)
                self.assertTrue(original["move_selected"])
                self.assertEqual(original["tier"], "sharp")
                persist.assert_called_once_with([(path, "sharp", True)])
                enqueue.assert_not_called()

if __name__ == "__main__":
    unittest.main()
