#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Overlapping parent and child scan roots must not inflate library totals."""
import tempfile
import unittest
from pathlib import Path
from catalog import init_catalog_schema, list_sources
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
                        """INSERT INTO library_root(root_id,source_id,original_root,current_root,
                           display_name,created_at,photo_count) VALUES(?,?,?,?,?,?,?)""",
                        (rid, "disk1", root, root, rid, 1.0, 2 if rid == "parent" else 1),
                    )
                for media_id, rid, path, state in (
                    ("m1", "parent", "F:/photos/trip/A.JPG", "present"),
                    ("m2", "child", "f:/photos/trip/a.jpg", "present"),
                    ("m3", "parent", "F:/photos/other.jpg", "present"),
                    ("m4", "child", "F:/photos/trip/missing.jpg", "missing"),
                ):
                    db.execute(
                        """INSERT INTO media_catalog
                           (media_id,source_id,root_id,relative_path,original_path,
                            current_path,state,first_seen_at,last_seen_at)
                           VALUES(?,?,?,?,?,?,?,?,?)""",
                        (media_id, "disk1", rid, media_id, path, path, state, 1.0, 1.0),
                    )
                db.commit()
            sources = list_sources(db_path, refresh=False)
            self.assertEqual(len(sources), 1)
            self.assertFalse(sources[0]["connected"])
            self.assertEqual(sum(x["photo_count"] for x in sources[0]["roots"]), 3)
            self.assertEqual(sources[0]["unique_photo_count"], 2)

if __name__ == "__main__":
    unittest.main()
