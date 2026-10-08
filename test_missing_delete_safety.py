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

    def test_manual_mark_survives_restart_and_quality_reclassification(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "favorite.jpg")
            Path(path).write_bytes(b"fake-image")
            db_path = Path(tmp) / "review.sqlite"
            with patch.object(photo_curator, "INDEX_DB", db_path):
                photo_curator._db_init()
                photo_curator._save_review_overrides([(path, "sharp", True)])
                saved = photo_curator._load_review_overrides([path])
                self.assertTrue(saved[path]["move_selected"])
                self.assertEqual(saved[path]["tier"], "sharp")
                item = {
                    "path": path, "tier": "sharp", "move_selected": True,
                    "badge": "清晰", "badgeType": "good",
                }
                with patch.dict(photo_curator.state["cull"],
                                {"photos": [item], "sharp_paths": [path],
                                 "overrides": {}}, clear=False), \
                     patch.object(photo_curator, "_sync_dedup_with_cull"), \
                     patch.object(photo_curator, "_request_rank_score"), \
                     patch.object(photo_curator, "thumb_url", return_value="/thumbnail"), \
                     patch.object(photo_curator, "_activity"):
                    response = photo_curator.app.test_client().post(
                        "/api/toggle-status", json={"path": path, "tier": "soft"}
                    )
                self.assertEqual(response.status_code, 200, response.get_json())
                self.assertTrue(response.get_json()["move_selected"])
                reloaded = photo_curator._load_review_overrides([path])
                self.assertEqual(reloaded[path]["tier"], "soft")
                self.assertTrue(reloaded[path]["move_selected"])

    def test_move_queue_rejects_stale_or_outside_path_before_any_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "library"
            root.mkdir()
            good = root / "valid.jpg"
            good.write_bytes(b"test")
            outside = Path(tmp) / "outside.jpg"
            outside.write_bytes(b"test")
            for invalid in (str(outside), str(root / "missing.jpg")):
                with patch.dict(photo_curator.state, {"folder": str(root)}), \
                     patch.dict(photo_curator.state["cull"],
                                {"photos": [
                                    {"path": str(good), "tier": "sharp",
                                     "move_selected": True, "lifecycle": "normal"},
                                    {"path": invalid, "tier": "blurry",
                                     "move_selected": True, "lifecycle": "normal"}
                                ]}, clear=False), \
                     patch.object(photo_curator, "_apply_media_lifecycle") as lifecycle, \
                     patch.object(photo_curator.TASK_MANAGER, "enqueue_many") as enqueue:
                    client = photo_curator.app.test_client()
                    preview = client.get("/api/review-pending").get_json()
                    response = client.post("/api/move-blurry",
                                           json={"review_token": preview["review_token"]})
                self.assertEqual(response.status_code, 409, response.get_json())
                lifecycle.assert_not_called()
                enqueue.assert_not_called()
                self.assertTrue(good.exists())
                self.assertTrue(outside.exists())

    def test_failed_queue_submission_restores_nonpending_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "library"
            root.mkdir()
            photo = root / "one.jpg"
            photo.write_bytes(b"test")
            with patch.dict(photo_curator.state, {"folder": str(root)}), \
                 patch.dict(photo_curator.state["cull"],
                            {"photos": [{"path": str(photo), "tier": "sharp",
                                         "move_selected": True, "lifecycle": "normal"}]},
                            clear=False), \
                 patch.object(photo_curator, "_apply_media_lifecycle") as lifecycle, \
                 patch.object(photo_curator, "_find_original_for_path",
                              return_value=str(photo)), \
                 patch.object(photo_curator.TASK_MANAGER, "enqueue_many",
                              side_effect=OSError("queue unavailable")):
                client = photo_curator.app.test_client()
                preview = client.get("/api/review-pending").get_json()
                response = client.post("/api/move-blurry",
                                       json={"review_token": preview["review_token"]})
            self.assertEqual(response.status_code, 503, response.get_json())
            lifecycle.assert_not_called()
            self.assertTrue(photo.exists())

    def test_review_pending_is_read_only_and_supports_all_quality_tiers(self):
        with tempfile.TemporaryDirectory() as tmp:
            sharp = str(Path(tmp) / "clear.jpg")
            soft = str(Path(tmp) / "soft.jpg")
            with patch.dict(photo_curator.state["cull"], {
                "photos": [
                    {"path": sharp, "tier": "sharp", "move_selected": True,
                     "lifecycle": "normal", "name": "清晰照片.jpg"},
                    {"path": soft, "tier": "soft", "move_selected": True,
                     "lifecycle": "normal", "name": "稍软.jpg"},
                    {"path": "ignored.jpg", "tier": "blurry",
                     "move_selected": False, "lifecycle": "normal"},
                ],
            }, clear=False), \
                 patch.object(photo_curator.TASK_MANAGER, "enqueue_many") as enqueue:
                response = photo_curator.app.test_client().get("/api/review-pending")
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload["total"], 2)
            self.assertEqual([r["tier"] for r in payload["items"]], ["sharp", "soft"])
            enqueue.assert_not_called()

    def test_move_queue_rejects_missing_or_forged_review_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            photo = Path(tmp) / "clear.jpg"
            photo.write_bytes(b"test")
            selected = [{"path": str(photo), "tier": "sharp",
                         "move_selected": True, "lifecycle": "normal"}]
            with patch.dict(photo_curator.state, {"folder": tmp}), \
                 patch.dict(photo_curator.state["cull"],
                            {"photos": selected}, clear=False), \
                 patch.object(photo_curator.TASK_MANAGER, "enqueue_many") as enqueue, \
                 patch.object(photo_curator, "_apply_media_lifecycle") as lifecycle:
                client = photo_curator.app.test_client()
                preview = client.get("/api/review-pending").get_json()
                for payload in ({}, {"review_token": ""}, {
                    "review_token": photo_curator._pending_review_token(selected)
                }, {"review_token": "forged"}):
                    response = client.post("/api/move-blurry", json=payload)
                    self.assertEqual(response.status_code, 409, response.get_json())
                self.assertEqual(preview["total"], 1)
                enqueue.assert_not_called()
                lifecycle.assert_not_called()
            self.assertTrue(photo.exists())

    def test_review_grant_is_single_use_and_expires(self):
        with tempfile.TemporaryDirectory() as tmp:
            photo = Path(tmp) / "clear.jpg"
            photo.write_bytes(b"test")
            selected = [{"path": str(photo), "tier": "sharp",
                         "move_selected": True, "lifecycle": "normal"}]
            with patch.dict(photo_curator.state, {"folder": tmp}), \
                 patch.dict(photo_curator.state["cull"],
                            {"photos": selected}, clear=False), \
                 patch.object(photo_curator, "_find_original_for_path",
                              return_value=str(photo)), \
                 patch.object(photo_curator.TASK_MANAGER, "enqueue_many",
                              return_value=[("fake-task", True)]) as enqueue, \
                 patch.object(photo_curator.TASK_MANAGER, "get", return_value=None), \
                 patch.object(photo_curator, "_apply_media_lifecycle"):
                client = photo_curator.app.test_client()
                token = client.get("/api/review-pending").get_json()["review_token"]
                first = client.post("/api/move-blurry", json={"review_token": token})
                second = client.post("/api/move-blurry", json={"review_token": token})
                self.assertEqual(first.status_code, 202, first.get_json())
                self.assertEqual(second.status_code, 409, second.get_json())
                self.assertEqual(enqueue.call_count, 1)
                expired = client.get("/api/review-pending").get_json()["review_token"]
                with photo_curator._REVIEW_GRANT_LOCK:
                    fingerprint, _ = photo_curator._REVIEW_GRANTS[expired]
                    photo_curator._REVIEW_GRANTS[expired] = (fingerprint, -1.0)
                denied = client.post("/api/move-blurry", json={"review_token": expired})
                self.assertEqual(denied.status_code, 409, denied.get_json())
                self.assertEqual(enqueue.call_count, 1)
            self.assertTrue(photo.exists())

    def test_review_token_rejects_changed_selection_without_file_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            first = folder / "a.jpg"
            second = folder / "b.jpg"
            first.write_bytes(b"a")
            second.write_bytes(b"b")
            selected = [
                {"path": str(first), "tier": "sharp", "move_selected": True,
                 "lifecycle": "normal", "name": "a.jpg"},
                {"path": str(second), "tier": "blurry", "move_selected": False,
                 "lifecycle": "normal", "name": "b.jpg"},
            ]
            with patch.dict(photo_curator.state, {"folder": str(folder)}), \
                 patch.dict(photo_curator.state["cull"], {"photos": selected}, clear=False), \
                 patch.object(photo_curator.TASK_MANAGER, "enqueue_many") as enqueue, \
                 patch.object(photo_curator, "_apply_media_lifecycle") as lifecycle:
                client = photo_curator.app.test_client()
                preview = client.get("/api/review-pending").get_json()
                self.assertEqual(preview["total"], 1)
                selected[1]["move_selected"] = True
                response = client.post("/api/move-blurry",
                                       json={"review_token": preview["review_token"]})
                self.assertEqual(response.status_code, 409)
                enqueue.assert_not_called()
                lifecycle.assert_not_called()
            self.assertTrue(first.exists())
            self.assertTrue(second.exists())

if __name__ == "__main__":
    unittest.main()
