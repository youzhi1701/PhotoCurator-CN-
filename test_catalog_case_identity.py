#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Different-case originals must never inherit each other's file decisions."""
import os
import tempfile
import unittest
from pathlib import Path

from catalog import init_catalog_schema, list_sources, update_media_lifecycle, _canonical_path
from db_runtime import connect_db


class CaseSensitiveCatalogIdentityTests(unittest.TestCase):
    def test_lifecycle_only_changes_matching_posix_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "library.sqlite3"
            folder = Path(tmp) / "camera"
            folder.mkdir()
            upper = _canonical_path(folder / "IMG_1.JPG")
            lower = _canonical_path(folder / "img_1.jpg")
            dest = _canonical_path(folder / "moved" / "IMG_1.JPG")
            init_catalog_schema(db)
            with connect_db(db) as conn:
                for name, path in (("upper", upper), ("lower", lower)):
                    conn.execute(
                        """INSERT INTO media_catalog(media_id,source_id,root_id,relative_path,
                           original_path,current_path,first_seen_at,last_seen_at)
                           VALUES(?,?,?,?,?,?,1,1)""",
                        (name, "source", "root", name, path, path),
                    )
                conn.commit()
            update_media_lifecycle(db, upper, dest, "pending_trash")
            with connect_db(db) as conn:
                states = {row[0]: (row[1], row[2]) for row in conn.execute(
                    "SELECT media_id,current_path,lifecycle FROM media_catalog")}
            self.assertEqual(states["upper"], (dest, "pending_trash"))
            if os.name != "nt":
                self.assertEqual(states["lower"], (lower, "normal"))

    def test_multi_root_overlap_counts_only_identical_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "library.sqlite3"
            init_catalog_schema(db)
            with connect_db(db) as conn:
                conn.execute(
                    """INSERT INTO data_source
                       (source_id,identity_key,kind,display_name,created_at)
                       VALUES('source','volume-id','volume','disk',1)"""
                )
                for rid, rel in (("parent", "photos"), ("child", "photos/trip")):
                    conn.execute(
                        """INSERT INTO library_root
                           (root_id,source_id,relative_root,original_root,current_root,
                            display_name,created_at,photo_count)
                           VALUES(?,?,?,?,?,?,1,1)""",
                        (rid, "source", rel, rel, rel, rid),
                    )
                for mid, root, rel in (
                    ("parent-A", "parent", "trip/A.jpg"),
                    ("child-a", "child", "a.jpg"),
                ):
                    conn.execute(
                        """INSERT INTO media_catalog
                           (media_id,source_id,root_id,relative_path,original_path,
                            current_path,first_seen_at,last_seen_at)
                           VALUES(?,?,?,?,?,?,1,1)""",
                        (mid, "source", root, rel, mid, mid),
                    )
                conn.commit()
            sources = list_sources(db, refresh=False)
            self.assertEqual(len(sources), 1)
            self.assertEqual(sources[0]["unique_photo_count"], 1 if os.name == "nt" else 2)


if __name__ == "__main__":
    unittest.main()
