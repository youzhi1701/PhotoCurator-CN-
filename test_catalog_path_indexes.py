#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Large catalog path lookups must be indexed without merging POSIX case-distinct names."""
import os
import tempfile
import unittest
from pathlib import Path

from catalog import init_catalog_schema, media_id_for_path, _canonical_path
from db_runtime import connect_db


class MediaLookupIndexTests(unittest.TestCase):
    def test_indexes_cover_both_current_and_original_locations(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "library.sqlite3"
            init_catalog_schema(db_path)
            expected_suffix = "nocase" if os.name == "nt" else "binary"
            with connect_db(db_path) as db:
                names = {row[1] for row in db.execute("PRAGMA index_list(media_catalog)")}
                self.assertIn(f"idx_media_catalog_current_path_{expected_suffix}", names)
                self.assertIn(f"idx_media_catalog_original_path_{expected_suffix}", names)
                for field in ("current_path", "original_path"):
                    index = f"idx_media_catalog_{field}_{expected_suffix}"
                    xinfo = db.execute(f'PRAGMA index_xinfo("{index}")').fetchall()
                    self.assertEqual(xinfo[0][2], field)
                    self.assertEqual(xinfo[0][4], "NOCASE" if os.name == "nt" else "BINARY")

    def test_offline_and_case_sensitive_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "photos"
            root.mkdir()
            first = root / "A.jpg"
            second = root / "a.jpg"
            first.write_bytes(b"original-A")
            # On Linux both paths are independent; Windows is case-insensitive
            # on typical installations, so don't create conflicting files.
            if os.name != "nt":
                second.write_bytes(b"original-a")
            db_path = Path(tmp) / "catalog.sqlite3"
            init_catalog_schema(db_path)
            with connect_db(db_path) as db:
                for media_id, file_path, stamp in (
                    ("media-A", first, 10),
                    ("media-a", second, 20),
                ):
                    canonical = _canonical_path(file_path)
                    db.execute(
                        """INSERT INTO media_catalog
                           (media_id,source_id,root_id,relative_path,
                            original_path,current_path,first_seen_at,last_seen_at)
                           VALUES (?,?,?,?,?,?,?,?)""",
                        (media_id, "disk", "root", media_id, canonical, canonical,
                         stamp, stamp),
                    )
                db.commit()
            if os.name == "nt":
                self.assertEqual(media_id_for_path(db_path, first), "media-a")
            else:
                self.assertEqual(media_id_for_path(db_path, first), "media-A")
                self.assertEqual(media_id_for_path(db_path, second), "media-a")


if __name__ == "__main__":
    unittest.main()
