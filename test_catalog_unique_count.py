#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from unittest.mock import patch
"""Overlapping parent and child scan roots must not inflate library totals."""
import tempfile
import sqlite3
import catalog
import unittest
from pathlib import Path
from catalog import (init_catalog_schema, list_sources, begin_catalog_scan,
                     catalog_scan_batch, finish_catalog_scan, abort_catalog_scan)
from db_runtime import connect_db

class CatalogUniqueCountTests(unittest.TestCase):
    def test_parent_child_overlap_and_offline_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "catalog.sqlite"
            init_catalog_schema(db_path)
            with connect_db(db_path) as db:
                db.execute(
                    "INSERT INTO data_source(source_id,identity_key,kind,display_name,created_at) VALUES(?,?,?,?,?)",
                    ("disk1", "test-disk1", "volume", "照片硬盘", 1.0),
                )
                for rid, root in (("parent", "F:/photos"), ("child", "F:/photos/trip")):
                    db.execute(
                        """INSERT INTO library_root(root_id,source_id,relative_root,original_root,current_root,
                           display_name,created_at,photo_count) VALUES(?,?,?,?,?,?,?,?)""",
                        (rid, "disk1", "photos" if rid == "parent" else "photos/trip",
                         root, root, rid, 1.0, 2 if rid == "parent" else 1),
                    )
                for media_id, rid, rel, path, state in (
                    ("m1", "parent", "trip/A.JPG", "F:/photos/trip/A.JPG", "present"),
                    ("m2", "child", "a.jpg", "G:/photos/trip/a.jpg", "present"),
                    ("m3", "parent", "other.jpg", "F:/photos/other.jpg", "present"),
                    ("m4", "child", "missing.jpg", "G:/photos/trip/missing.jpg", "missing"),
                ):
                    db.execute(
                        """INSERT INTO media_catalog
                           (media_id,source_id,root_id,relative_path,original_path,
                            current_path,state,first_seen_at,last_seen_at)
                           VALUES(?,?,?,?,?,?,?,?,?)""",
                        (media_id, "disk1", rid, rel, path, path, state, 1.0, 1.0),
                    )
                db.commit()
            sources = list_sources(db_path, refresh=False)
            self.assertEqual(len(sources), 1)
            self.assertFalse(sources[0]["connected"])
            self.assertEqual(sum(x["photo_count"] for x in sources[0]["roots"]), 3)
            self.assertEqual(sources[0]["unique_photo_count"], 2)

class CatalogScanGuardTests(unittest.TestCase):
    def test_interrupted_scan_cannot_write_stale_batches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "photos"
            root.mkdir()
            photo = root / "a.jpg"
            photo.write_bytes(b"fake-image")
            db_path = Path(tmp) / "catalog.sqlite"
            session = begin_catalog_scan(db_path, root)
            abort_catalog_scan(db_path, session, "cancelled")
            with self.assertRaisesRegex(RuntimeError, "no longer active"):
                catalog_scan_batch(db_path, session, [photo])
            with connect_db(db_path) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM media_catalog").fetchone()[0], 0)

    def test_scan_outside_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "photos"
            root.mkdir()
            outside = Path(tmp) / "outside.jpg"
            outside.write_bytes(b"fake-image")
            db_path = Path(tmp) / "catalog.sqlite"
            session = begin_catalog_scan(db_path, root)
            self.assertEqual(catalog_scan_batch(db_path, session, [outside]), 0)
            self.assertGreater(session["error_count"], 0)
            with connect_db(db_path) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM media_catalog").fetchone()[0], 0)


class CatalogFinalizationGuardTests(unittest.TestCase):
    def test_aborted_scan_cannot_finalize(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "photos"
            root.mkdir()
            db_path = Path(tmp) / "catalog.sqlite"
            session = begin_catalog_scan(db_path, root)
            abort_catalog_scan(db_path, session, "drive disconnected")
            with self.assertRaisesRegex(RuntimeError, "not active"):
                finish_catalog_scan(db_path, session, full_scan=True)
            with connect_db(db_path) as db:
                row = db.execute("SELECT state FROM scan_session WHERE session_id=?", (session["session_id"],)).fetchone()
            self.assertEqual(row[0], "interrupted")

class CatalogScanStartTests(unittest.TestCase):
    def test_new_scan_supersedes_old_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "photos"
            root.mkdir()
            sample = root / "example.jpg"
            sample.write_bytes(b"example")
            db_path = Path(tmp) / "catalog.sqlite"
            first = begin_catalog_scan(db_path, root)
            second = begin_catalog_scan(db_path, root)
            self.assertNotEqual(first["session_id"], second["session_id"])
            with self.assertRaisesRegex(RuntimeError, "no longer active"):
                catalog_scan_batch(db_path, first, [sample])
            self.assertEqual(catalog_scan_batch(db_path, second, [sample]), 1)
            with connect_db(db_path) as db:
                states = dict(db.execute(
                    "SELECT session_id,state FROM scan_session"
                ).fetchall())
            self.assertEqual(states[first["session_id"]], "interrupted")
            self.assertEqual(states[second["session_id"]], "running")

class CatalogRelinkCollisionTests(unittest.TestCase):
    def test_path_collision_preserves_both_manual_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_root = Path(tmp) / "original"
            new_root = Path(tmp) / "remounted"
            old_root.mkdir()
            new_root.mkdir()
            db_path = Path(tmp) / "catalog.sqlite"
            init_catalog_schema(db_path)
            old_path = str(old_root / "a.jpg")
            new_path = str(new_root / "a.jpg")
            with connect_db(db_path) as db:
                db.execute(
                    "INSERT INTO data_source(source_id,identity_key,kind,display_name,created_at) "
                    "VALUES('source','device-1','volume','disk',1)"
                )
                db.execute(
                    """INSERT INTO library_root
                       (root_id,source_id,relative_root,original_root,current_root,
                        display_name,created_at)
                       VALUES('root','source','photos',?,?, 'photos',1)""",
                    (str(old_root), str(old_root)),
                )
                db.execute(
                    """INSERT INTO media_catalog
                       (media_id,source_id,root_id,relative_path,original_path,
                        current_path,first_seen_at,last_seen_at)
                       VALUES('media','source','root','a.jpg',?,?,1,1)""",
                    (old_path, old_path),
                )
                db.execute("CREATE TABLE review_override(path TEXT PRIMARY KEY)")
                db.execute("INSERT INTO review_override(path) VALUES(?)", (old_path,))
                db.execute("INSERT INTO review_override(path) VALUES(?)", (new_path,))
                db.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                with connect_db(db_path) as db:
                    catalog._rebase_persisted_paths(
                        db, "root", str(old_root), str(new_root)
                    )
            with connect_db(db_path) as db:
                saved = [row[0] for row in db.execute(
                    "SELECT path FROM review_override ORDER BY path"
                ).fetchall()]
                media = db.execute(
                    "SELECT current_path FROM media_catalog WHERE media_id='media'"
                ).fetchone()[0]
            self.assertEqual(saved, sorted([old_path, new_path]))
            self.assertEqual(media, old_path)


class CatalogPhysicalSourceGuardTest(unittest.TestCase):
    def test_drive_swap_during_scan_does_not_mark_photos_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "photos"
            root.mkdir()
            old_photo = root / "safe.jpg"
            old_photo.write_bytes(b"sample")
            db_path = Path(tmp) / "catalog.sqlite"
            catalog.catalog_media_scan(db_path, root, [old_photo])
            session = begin_catalog_scan(db_path, root)
            with patch.object(catalog, "volume_info_for_path",
                              return_value={"identity_key": "replaced-device"}):
                with self.assertRaisesRegex(RuntimeError, "身份发生变化"):
                    finish_catalog_scan(db_path, session, full_scan=True)
            with connect_db(db_path) as db:
                media_state = db.execute(
                    "SELECT state FROM media_catalog WHERE original_path=?",
                    (str(old_photo),)).fetchone()
                scan_state = db.execute(
                    "SELECT state FROM scan_session WHERE session_id=?",
                    (session["session_id"],)).fetchone()
            self.assertEqual(media_state[0], "present")
            self.assertEqual(scan_state[0], "interrupted")

    def test_disconnected_root_keeps_index_and_logs_interrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "photos"
            root.mkdir()
            old_photo = root / "safe.jpg"
            old_photo.write_bytes(b"sample")
            db_path = Path(tmp) / "catalog.sqlite"
            catalog.catalog_media_scan(db_path, root, [old_photo])
            session = begin_catalog_scan(db_path, root)
            root.rename(Path(tmp) / "disconnected")
            with self.assertRaisesRegex(RuntimeError, "历史图库"):
                finish_catalog_scan(db_path, session, full_scan=True)
            with connect_db(db_path) as db:
                self.assertEqual(db.execute(
                    "SELECT state FROM media_catalog LIMIT 1").fetchone()[0], "present")
                self.assertEqual(db.execute(
                    "SELECT state FROM scan_session WHERE session_id=?",
                    (session["session_id"],)).fetchone()[0], "interrupted")



class CatalogBatchVolumeIdentityTests(unittest.TestCase):
    def test_replaced_source_aborts_before_writing_any_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "photos"
            root.mkdir()
            candidate = root / "incoming.jpg"
            candidate.write_bytes(b"other-device")
            db_path = Path(tmp) / "index.sqlite"
            session = begin_catalog_scan(db_path, root)
            with patch.object(catalog, "volume_info_for_path",
                              return_value={"identity_key": "different-volume"}):
                with self.assertRaisesRegex(RuntimeError, "批次未写入"):
                    catalog_scan_batch(db_path, session, [candidate])
            with connect_db(db_path) as db:
                count = db.execute("SELECT COUNT(*) FROM media_catalog").fetchone()[0]
                state = db.execute(
                    "SELECT state FROM scan_session WHERE session_id=?",
                    (session["session_id"],)).fetchone()[0]
            self.assertEqual(count, 0)
            self.assertEqual(state, "interrupted")

    def test_unplug_after_previous_batch_keeps_scanned_photos(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "photos"
            root.mkdir()
            original = root / "original.jpg"
            original.write_bytes(b"original")
            db_path = Path(tmp) / "index.sqlite"
            session = begin_catalog_scan(db_path, root)
            self.assertEqual(catalog_scan_batch(db_path, session, [original]), 1)
            newfile = root / "new.jpg"
            newfile.write_bytes(b"unrelated")
            with patch.object(catalog, "volume_info_for_path",
                              side_effect=OSError("drive removed")):
                with self.assertRaisesRegex(RuntimeError, "批次未写入"):
                    catalog_scan_batch(db_path, session, [newfile])
            with connect_db(db_path) as db:
                rows = db.execute(
                    "SELECT original_path,state FROM media_catalog").fetchall()
            self.assertEqual([(r[0], r[1]) for r in rows],
                             [(str(original), "present")])

if __name__ == "__main__":
    unittest.main()
