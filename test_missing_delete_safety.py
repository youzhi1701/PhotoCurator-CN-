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

    def test_single_file_enqueue_failure_never_marks_photo_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            photo = Path(tmp) / "one.jpg"
            photo.write_bytes(b"test")
            with patch.dict(photo_curator.state, {"folder": tmp}), \
                 patch.object(photo_curator, "_known_step_paths",
                              return_value={str(photo)}), \
                 patch.object(photo_curator, "_safe_image_path",
                              return_value=photo), \
                 patch.object(photo_curator, "_find_original_for_path",
                              return_value=str(photo)), \
                 patch.object(photo_curator, "_media_state_get",
                              return_value={"state": "normal"}), \
                 patch.object(photo_curator, "_apply_media_lifecycle") as lifecycle, \
                 patch.object(photo_curator.TASK_MANAGER, "enqueue",
                              side_effect=OSError("queue unavailable")):
                response = photo_curator.app.test_client().post(
                    "/api/delete-photo",
                    json={"step": "cull", "path": str(photo), "mode": "trash"}
                )
            self.assertEqual(response.status_code, 503, response.get_json())
            lifecycle.assert_not_called()
            self.assertTrue(photo.exists())

    def test_direct_permanent_requires_fresh_single_use_file_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            photo = Path(tmp) / "precious.jpg"
            photo.write_bytes(b"original")
            with patch.dict(photo_curator.state, {"folder": tmp}), \
                 patch.object(photo_curator, "_known_step_paths",
                              return_value={str(photo)}), \
                 patch.object(photo_curator, "_safe_image_path",
                              return_value=photo), \
                 patch.object(photo_curator, "_find_original_for_path",
                              return_value=str(photo)), \
                 patch.object(photo_curator, "_media_state_get",
                              return_value={"state": "normal"}), \
                 patch.object(photo_curator, "_apply_media_lifecycle") as lifecycle, \
                 patch.object(photo_curator, "_activity"), \
                 patch.object(photo_curator.TASK_MANAGER, "enqueue",
                              return_value=(555, True)) as queued, \
                 patch.object(photo_curator.TASK_MANAGER, "get",
                              return_value=None):
                client = photo_curator.app.test_client()
                original = {"step": "cull", "path": str(photo), "mode": "permanent"}
                for invalid in ({}, {"review_token": "fake"}):
                    denied = client.post("/api/delete-photo", json={**original, **invalid})
                    self.assertEqual(denied.status_code, 409, denied.get_json())
                queued.assert_not_called()
                lifecycle.assert_not_called()
                review = client.post("/api/review-permanent",
                                     json={"step": "cull", "path": str(photo)})
                self.assertEqual(review.status_code, 200, review.get_json())
                token = review.get_json()["review_token"]
                accepted = client.post("/api/delete-photo",
                                       json={**original, "review_token": token})
                self.assertEqual(accepted.status_code, 202, accepted.get_json())
                replayed = client.post("/api/delete-photo",
                                       json={**original, "review_token": token})
                self.assertEqual(replayed.status_code, 409, replayed.get_json())
                queued.assert_called_once()
                lifecycle.assert_not_called()
            self.assertTrue(photo.exists())

    def test_direct_permanent_refuses_file_replaced_after_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            photo = Path(tmp) / "replace.jpg"
            photo.write_bytes(b"original")
            with patch.dict(photo_curator.state, {"folder": tmp}), \
                 patch.object(photo_curator, "_known_step_paths",
                              return_value={str(photo)}), \
                 patch.object(photo_curator, "_safe_image_path",
                              return_value=photo), \
                 patch.object(photo_curator.TASK_MANAGER, "enqueue") as queued:
                client = photo_curator.app.test_client()
                token = client.post("/api/review-permanent",
                                    json={"step": "cull", "path": str(photo)}
                                    ).get_json()["review_token"]
                photo.write_bytes(b"substituted file content")
                denied = client.post(
                    "/api/delete-photo",
                    json={"step": "cull", "path": str(photo),
                          "mode": "permanent", "review_token": token}
                )
                self.assertEqual(denied.status_code, 409, denied.get_json())
                queued.assert_not_called()
            self.assertEqual(photo.read_bytes(), b"substituted file content")

    def test_dedup_batch_requires_exact_review_and_atomic_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            keep = Path(tmp) / "keep.jpg"
            discard = Path(tmp) / "discard.jpg"
            keep.write_bytes(b"keep")
            discard.write_bytes(b"discard")
            group = {
                "group_id": 3, "group_key": "group-3",
                "status": "reviewed", "selected_paths": [str(keep)],
                "members": [
                    {"path": str(keep), "selected": True, "lifecycle": "normal"},
                    {"path": str(discard), "selected": False, "lifecycle": "normal"},
                ],
            }
            with patch.dict(photo_curator.state, {"folder": tmp}), \
                 patch.dict(photo_curator.state["dedup"],
                            {"groups_data": [group]}, clear=False), \
                 patch.object(photo_curator, "_sync_dedup_with_cull"), \
                 patch.object(photo_curator, "_cull_allowed_for_dedup",
                              return_value=None), \
                 patch.object(photo_curator, "_find_original_for_path",
                              side_effect=lambda x: x), \
                 patch.object(photo_curator.TASK_MANAGER, "enqueue_many",
                              return_value=[(707, True)]) as enqueue, \
                 patch.object(photo_curator.TASK_MANAGER, "get",
                              return_value=None), \
                 patch.object(photo_curator, "_apply_media_lifecycle") as lifecycle, \
                 patch.object(photo_curator, "_activity"):
                client = photo_curator.app.test_client()
                preview = client.get("/api/review-dedup-apply").get_json()
                self.assertEqual(preview["total"], 1)
                self.assertEqual(preview["items"][0]["path"], str(discard))
                for payload in ({}, {"review_token": "forged"}):
                    response = client.post("/api/dedup-apply", json=payload)
                    self.assertEqual(response.status_code, 409, response.get_json())
                enqueue.assert_not_called()
                lifecycle.assert_not_called()
                token = preview["review_token"]
                accepted = client.post("/api/dedup-apply",
                                       json={"review_token": token})
                self.assertEqual(accepted.status_code, 202, accepted.get_json())
                submitted = enqueue.call_args.args[0]
                self.assertEqual(len(submitted), 1)
                self.assertEqual(submitted[0]["payload"]["path"], str(discard))
                self.assertEqual(accepted.get_json()["task_ids"], [707])
                repeat = client.post("/api/dedup-apply",
                                     json={"review_token": token})
                self.assertEqual(repeat.status_code, 409)
                enqueue.assert_called_once()
            self.assertTrue(keep.exists())
            self.assertTrue(discard.exists())

    def test_dedup_batch_stale_keeper_change_rejected_and_queue_failure_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b, c = [Path(tmp) / (name + ".jpg") for name in "abc"]
            for photo in (a, b, c):
                photo.write_bytes(b"test")
            members = [{"path": str(p), "lifecycle": "normal"}
                       for p in (a, b, c)]
            group = {"group_id": 11, "status": "reviewed",
                     "selected_paths": [str(a)], "members": members}
            with patch.dict(photo_curator.state, {"folder": tmp}), \
                 patch.dict(photo_curator.state["dedup"],
                            {"groups_data": [group]}, clear=False), \
                 patch.object(photo_curator, "_sync_dedup_with_cull"), \
                 patch.object(photo_curator, "_cull_allowed_for_dedup",
                              return_value=None), \
                 patch.object(photo_curator.TASK_MANAGER, "enqueue_many",
                              side_effect=OSError("database unavailable")) as enqueue, \
                 patch.object(photo_curator, "_apply_media_lifecycle") as lifecycle:
                client = photo_curator.app.test_client()
                stale = client.get("/api/review-dedup-apply").get_json()["review_token"]
                group["selected_paths"] = [str(b)]
                changed = client.post("/api/dedup-apply",
                                      json={"review_token": stale})
                self.assertEqual(changed.status_code, 409, changed.get_json())
                enqueue.assert_not_called()
                fresh = client.get("/api/review-dedup-apply").get_json()["review_token"]
                failed = client.post("/api/dedup-apply",
                                     json={"review_token": fresh})
                self.assertEqual(failed.status_code, 503, failed.get_json())
                self.assertEqual(failed.get_json()["queued"], 0)
                self.assertEqual(failed.get_json()["task_ids"], [])
                enqueue.assert_called_once()
                lifecycle.assert_not_called()
            self.assertTrue(all(p.exists() for p in (a, b, c)))

    def test_dedup_batch_rejects_outside_and_offline_path_before_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "source"
            folder.mkdir()
            keep = folder / "keep.jpg"
            keep.write_bytes(b"k")
            outside = Path(tmp) / "outside.jpg"
            outside.write_bytes(b"x")
            for invalid in (str(outside), str(folder / "offline.jpg")):
                group = {"group_id": 8, "status": "reviewed",
                         "selected_paths": [str(keep)],
                         "members": [
                            {"path": str(keep), "lifecycle": "normal"},
                            {"path": invalid, "lifecycle": "normal"},
                         ]}
                with patch.dict(photo_curator.state, {"folder": str(folder)}), \
                     patch.dict(photo_curator.state["dedup"],
                                {"groups_data": [group]}, clear=False), \
                     patch.object(photo_curator, "_sync_dedup_with_cull"), \
                     patch.object(photo_curator, "_cull_allowed_for_dedup",
                                  return_value=None), \
                     patch.object(photo_curator.TASK_MANAGER, "enqueue_many") as enqueue, \
                     patch.object(photo_curator, "_apply_media_lifecycle") as lifecycle:
                    client = photo_curator.app.test_client()
                    token = client.get("/api/review-dedup-apply").get_json()["review_token"]
                    response = client.post("/api/dedup-apply",
                                           json={"review_token": token})
                    self.assertEqual(response.status_code, 409, response.get_json())
                    enqueue.assert_not_called()
                    lifecycle.assert_not_called()
            self.assertTrue(keep.exists())
            self.assertTrue(outside.exists())

    def test_offline_trash_listing_keeps_every_device_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            a_root = Path(tmp) / "device_A"
            b_root = Path(tmp) / "device_B"
            a_root.mkdir()
            b_root.mkdir()
            a_missing = a_root / ".PhotoCuratorTrash" / "missing.jpg"
            b_online = b_root / ".PhotoCuratorTrash" / "present.jpg"
            b_online.parent.mkdir()
            b_online.write_bytes(b"test")
            db_path = Path(tmp) / "trash.sqlite"
            with patch.object(photo_curator, "INDEX_DB", db_path):
                photo_curator._db_init()
                with connect_db(db_path) as db:
                    for path, root in ((a_missing, a_root), (b_online, b_root)):
                        db.execute(
                            "INSERT INTO software_trash "
                            "(original_path, trash_path, source_step, deleted_at) "
                            "VALUES(?,?,?,?)",
                            (str(root / "original.jpg"), str(path), "cull", 1.0)
                        )
                    db.commit()
                with patch.object(photo_curator, "thumb_url", return_value="/thumb"):
                    online = photo_curator._trash_rows(b_root)
                    offline = photo_curator._trash_rows(a_root)
                photo_curator._write_trash_manifest(a_root)
                manifest = photo_curator._trash_manifest_file(a_root)
                manifest_items = __import__("json").loads(manifest.read_text(encoding="utf-8"))["items"]
                with connect_db(db_path) as db:
                    retained = db.execute("SELECT COUNT(*) FROM software_trash").fetchone()[0]
            self.assertEqual(retained, 2, "离线文件记录不得被删除")
            self.assertEqual(len(manifest_items), 1)
            self.assertEqual(manifest_items[0]["trash_path"], str(a_missing))
            self.assertEqual(len(online), 1)
            self.assertTrue(online[0]["available"])
            self.assertEqual(len(offline), 1)
            self.assertFalse(offline[0]["available"])

    def test_offline_trash_items_cannot_be_purged_or_restored(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing-trash.jpg"
            rows = [{"id": 89, "path": str(missing),
                     "original_path": str(Path(tmp) / "original.jpg"),
                     "source_step": "cull", "available": False}]
            with patch.dict(photo_curator.state, {"folder": tmp}), \
                 patch.object(photo_curator, "_trash_rows", return_value=rows), \
                 patch.object(photo_curator.TASK_MANAGER, "enqueue_many") as bulk, \
                 patch.object(photo_curator.TASK_MANAGER, "enqueue") as single:
                client = photo_curator.app.test_client()
                token = client.get("/api/trash").get_json()["purge_token"]
                for endpoint, payload in (
                    ("/api/trash-purge", {"all": True, "purge_token": token}),
                    ("/api/trash-restore-all", {}),
                    ("/api/trash-restore", {"id": 89})
                ):
                    resp = client.post(endpoint, json=payload)
                    self.assertEqual(resp.status_code, 409, resp.get_json())
                bulk.assert_not_called()
                single.assert_not_called()

    def test_available_trash_item_can_purge_without_erasing_offline_peer(self):
        with tempfile.TemporaryDirectory() as tmp:
            online_file = Path(tmp) / "online-trash.jpg"
            online_file.write_bytes(b"test")
            rows = [
                {"id": 101, "path": str(Path(tmp) / "missing.jpg"),
                 "original_path": str(Path(tmp) / "a.jpg"),
                 "available": False},
                {"id": 102, "path": str(online_file),
                 "original_path": str(Path(tmp) / "b.jpg"),
                 "available": True},
            ]
            with patch.dict(photo_curator.state, {"folder": tmp}), \
                 patch.object(photo_curator, "_trash_rows", return_value=rows), \
                 patch.object(photo_curator.TASK_MANAGER, "enqueue",
                              return_value=(999, True)) as queued:
                client = photo_curator.app.test_client()
                token = client.get("/api/trash").get_json()["purge_token"]
                response = client.post("/api/trash-purge",
                                       json={"id": 102, "purge_token": token})
                self.assertEqual(response.status_code, 202, response.get_json())
                queued.assert_called_once()
            self.assertTrue(online_file.exists())

    def test_trash_purge_requires_current_grant_and_enqueues_all_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            deleted = Path(tmp) / "trash.jpg"
            deleted.write_bytes(b"original")
            row = {"id": 17, "path": str(deleted),
                   "original_path": str(Path(tmp) / "original.jpg"),
                   "source_step": "cull"}
            with patch.dict(photo_curator.state, {"folder": tmp}), \
                 patch.object(photo_curator, "_trash_rows", return_value=[row]), \
                 patch.object(photo_curator.TASK_MANAGER, "enqueue_many",
                              return_value=[(313, True)]) as queued:
                client = photo_curator.app.test_client()
                preview = client.get("/api/trash").get_json()
                self.assertEqual(preview["count"], 1)
                for payload in ({"all": True}, {
                    "all": True, "purge_token": "forged"
                }):
                    response = client.post("/api/trash-purge", json=payload)
                    self.assertEqual(response.status_code, 409, response.get_json())
                queued.assert_not_called()
                request_body = {"all": True, "purge_token": preview["purge_token"]}
                response = client.post("/api/trash-purge", json=request_body)
                self.assertEqual(response.status_code, 202, response.get_json())
                self.assertEqual(response.get_json()["task_ids"], [313])
                repeat = client.post("/api/trash-purge", json=request_body)
                self.assertEqual(repeat.status_code, 409, repeat.get_json())
                queued.assert_called_once()
            self.assertTrue(deleted.exists())

    def test_trash_batch_failure_keeps_files_and_no_partial_task_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            originals = [Path(tmp) / ("trashed" + str(i) + ".jpg") for i in range(2)]
            for path in originals:
                path.write_bytes(b"test")
            rows = [{"id": i + 5, "path": str(path),
                     "original_path": str(Path(tmp) / ("before" + str(i) + ".jpg")),
                     "source_step": "cull"}
                    for i, path in enumerate(originals)]
            with patch.dict(photo_curator.state, {"folder": tmp}), \
                 patch.object(photo_curator, "_trash_rows", return_value=rows), \
                 patch.object(photo_curator.TASK_MANAGER, "enqueue_many",
                              side_effect=OSError("disk unavailable")):
                client = photo_curator.app.test_client()
                token = client.get("/api/trash").get_json()["purge_token"]
                response = client.post("/api/trash-purge",
                                       json={"all": True, "purge_token": token})
            self.assertEqual(response.status_code, 503, response.get_json())
            self.assertEqual(response.get_json()["task_ids"], [])
            self.assertTrue(all(path.exists() for path in originals))

    def test_trash_restore_all_failure_leaves_lifecycle_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            originals = [Path(tmp) / ("original" + str(i) + ".jpg") for i in range(2)]
            rows = [{"id": i + 11,
                     "path": str(Path(tmp) / ("trash" + str(i) + ".jpg")),
                     "original_path": str(path), "source_step": "cull"}
                    for i, path in enumerate(originals)]
            with patch.dict(photo_curator.state, {"folder": tmp}), \
                 patch.object(photo_curator, "_trash_rows", return_value=rows), \
                 patch.object(photo_curator, "_active_restore_reservations",
                              return_value=set()), \
                 patch.object(photo_curator.TASK_MANAGER, "enqueue_many",
                              side_effect=OSError("database unavailable")) as queued, \
                 patch.object(photo_curator, "_media_state_set") as lifecycle:
                response = photo_curator.app.test_client().post(
                    "/api/trash-restore-all", json={}
                )
            self.assertEqual(response.status_code, 503, response.get_json())
            self.assertEqual(response.get_json()["task_ids"], [])
            queued.assert_called_once()
            lifecycle.assert_not_called()

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

    def test_move_grant_rejects_replaced_photo_at_same_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            photo = Path(tmp) / "selected.jpg"
            photo.write_bytes(b"original image")
            selected = [{"path": str(photo), "tier": "sharp",
                         "move_selected": True, "lifecycle": "normal"}]
            with patch.dict(photo_curator.state, {"folder": tmp}), \
                 patch.dict(photo_curator.state["cull"],
                            {"photos": selected}, clear=False), \
                 patch.object(photo_curator.TASK_MANAGER, "enqueue_many") as enqueue:
                client = photo_curator.app.test_client()
                token = client.get("/api/review-pending").get_json()["review_token"]
                photo.write_bytes(b"unrelated photo with different bytes and size")
                denied = client.post("/api/move-blurry",
                                     json={"review_token": token})
                self.assertEqual(denied.status_code, 409, denied.get_json())
                enqueue.assert_not_called()
            self.assertEqual(photo.read_bytes(),
                             b"unrelated photo with different bytes and size")

    def test_dedup_grant_rejects_replacement_even_when_selection_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            photo = Path(tmp) / "duplicate.jpg"
            photo.write_bytes(b"old bytes")
            rows = [{"path": str(photo), "group": "g1",
                     "keep": str(Path(tmp) / "keep.jpg")}]
            with patch.dict(photo_curator.state, {"folder": tmp}), \
                 patch.object(photo_curator, "_sync_dedup_with_cull"), \
                 patch.object(photo_curator, "_dedup_apply_rows",
                              return_value=rows), \
                 patch.object(photo_curator.TASK_MANAGER, "enqueue_many") as enqueue:
                client = photo_curator.app.test_client()
                token = client.get("/api/review-dedup-apply").get_json()["review_token"]
                photo.write_bytes(b"different bytes")
                denied = client.post("/api/dedup-apply",
                                     json={"review_token": token})
                self.assertEqual(denied.status_code, 409, denied.get_json())
                enqueue.assert_not_called()
            self.assertTrue(photo.exists())

    def test_trash_grant_rejects_changed_file_before_purge(self):
        with tempfile.TemporaryDirectory() as tmp:
            trash = Path(tmp) / "trash.jpg"
            trash.write_bytes(b"former file")
            rows = [{"id": 15, "path": str(trash),
                     "original_path": str(Path(tmp) / "original.jpg"),
                     "available": True}]
            with patch.dict(photo_curator.state, {"folder": tmp}), \
                 patch.object(photo_curator, "_trash_rows", return_value=rows), \
                 patch.object(photo_curator.TASK_MANAGER, "enqueue") as enqueue:
                client = photo_curator.app.test_client()
                token = client.get("/api/trash").get_json()["purge_token"]
                trash.write_bytes(b"unexpected replacement!")
                denied = client.post("/api/trash-purge",
                                     json={"id": 15, "purge_token": token})
                self.assertEqual(denied.status_code, 409, denied.get_json())
                enqueue.assert_not_called()
            self.assertTrue(trash.exists())

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


class BackgroundWorkerIdentityTests(unittest.TestCase):
    def test_matching_source_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "photo.jpg"
            image.write_bytes(b"photograph")
            signature = photo_curator._file_action_signature(image)
            self.assertTrue(photo_curator._verify_file_action_source(image, signature))
            self.assertEqual(image.read_bytes(), b"photograph")

    def test_replaced_path_is_rejected_even_if_size_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "photo.jpg"
            image.write_bytes(b"original")
            signature = photo_curator._file_action_signature(image)
            old = Path(tmp) / "old.jpg"
            image.rename(old)
            image.write_bytes(b"replaced")
            with self.assertRaisesRegex(RuntimeError, "已变化"):
                photo_curator._verify_file_action_source(image, signature)
            self.assertEqual(image.read_bytes(), b"replaced")

    def test_unknown_legacy_action_is_not_implicitly_authorized(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "photo.jpg"
            image.write_bytes(b"original")
            with self.assertRaisesRegex(RuntimeError, "旧版文件任务"):
                photo_curator._verify_file_action_source(image, None)
            self.assertTrue(image.exists())

    def test_recovery_refuses_unrelated_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "gone.jpg"
            source.write_bytes(b"original")
            signature = photo_curator._file_action_signature(source)
            source.unlink()
            destination = Path(tmp) / "trash.jpg"
            destination.write_bytes(b"stranger")
            with self.assertRaisesRegex(RuntimeError, "身份"):
                photo_curator._verify_file_action_source(
                    source, signature, moved_to=destination)
            self.assertTrue(destination.exists())

    def test_worker_permanent_delete_cannot_delete_replaced_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "photo.jpg"
            image.write_bytes(b"original")
            signature = photo_curator._file_action_signature(image)
            image.unlink()
            image.write_bytes(b"stranger")
            with patch.object(photo_curator, "_find_original_for_path",
                              return_value=str(image)), \\
                 patch.object(photo_curator, "_apply_media_lifecycle"):
                with self.assertRaisesRegex(RuntimeError, "已变化"):
                    photo_curator._background_permanent_delete({
                        "path": str(image), "source_identity": signature})
            self.assertEqual(image.read_bytes(), b"stranger")


if __name__ == "__main__":
    unittest.main()
