#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A cleanup request must never cross runtime storage into photo source folders."""
import tempfile
import unittest
from pathlib import Path

from catalog import clear_offline_previews, clear_rebuildable_storage


class StorageCleanupContainmentTests(unittest.TestCase):
    def test_preview_cleanup_keeps_durable_offline_previews(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            thumbs = data / "cache" / "thumbnails"
            offline = data / "offline_previews"
            thumbs.mkdir(parents=True)
            offline.mkdir(parents=True)
            (thumbs / "cache.jpg").write_bytes(b"rebuildable")
            (offline / "favorite.jpg").write_bytes(b"must remain")
            result = clear_rebuildable_storage(data, "previews")
            self.assertEqual(result["removed"], 1)
            self.assertFalse((thumbs / "cache.jpg").exists())
            self.assertEqual((offline / "favorite.jpg").read_bytes(), b"must remain")

    def test_symlinked_storage_roots_cannot_delete_external_photos(self):
        for kind, parts in (
            ("offline", ("offline_previews",)),
            ("previews", ("cache", "thumbnails")),
            ("features", ("config", "dedup_features")),
            ("logs", ("logs",)),
        ):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                data = base / "data"
                photos = base / "photos"
                photos.mkdir()
                original = photos / "original.jpg"
                original.write_bytes(b"private original, never touch")
                link = data.joinpath(*parts)
                link.parent.mkdir(parents=True, exist_ok=True)
                try:
                    link.symlink_to(photos, target_is_directory=True)
                except (OSError, NotImplementedError) as exc:
                    self.skipTest(f"platform forbids symlink creation: {exc}")
                with self.assertRaises(ValueError):
                    if kind == "offline":
                        clear_offline_previews(data, base / "unused.sqlite3")
                    else:
                        clear_rebuildable_storage(data, kind)
                self.assertEqual(original.read_bytes(), b"private original, never touch")


if __name__ == "__main__":
    unittest.main()
