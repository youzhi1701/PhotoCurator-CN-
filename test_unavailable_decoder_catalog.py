#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Missing optional codecs must not make previously indexed source photos vanish."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import catalog
import photo_curator as core
from raw_loader import RAW_EXTS, HEIF_EXTS


class CodecAvailabilityCatalogTests(unittest.TestCase):
    def test_all_camera_formats_remain_discoverable(self):
        self.assertTrue(RAW_EXTS.issubset(core.IMG_EXTS))
        self.assertTrue(HEIF_EXTS.issubset(core.IMG_EXTS))
        self.assertTrue(core.DECODABLE_IMG_EXTS.issubset(core.IMG_EXTS))

    def test_missing_raw_heif_codecs_do_not_erase_offline_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "library"
            root.mkdir()
            raw = root / "IMG_0001.NEF"
            heif = root / "IMG_0002.HEIC"
            jpg = root / "IMG_0003.JPG"
            for photo in (raw, heif, jpg):
                photo.write_bytes(b"keep camera original: " + photo.name.encode())
            database = Path(tmp) / "data" / "index.sqlite3"
            before = {p.name: p.read_bytes() for p in (raw, heif, jpg)}

            def catalog_rescan():
                discovered = list(core.iter_images(root, recursive=True))
                names = {p.name for p in discovered}
                self.assertEqual(names, set(before))
                return catalog.catalog_media_scan(
                    database, root, discovered, full_scan=True)

            first = catalog_rescan()
            self.assertEqual(first["state"], "completed")
            # Only the decoder hints change. Discovery/canonical catalog media
            # identity must NOT depend on rawpy/libheif being installed.
            with patch.object(core, "HAS_RAWPY", False), \
                 patch.object(core, "HAS_HEIF", False):
                second = catalog_rescan()
            self.assertEqual(second["state"], "completed")
            self.assertEqual(second["photo_count"], 3)
            snap = catalog.root_snapshot(database, first["root_id"], limit=10)
            self.assertEqual(snap["counts"].get("present"), 3)
            self.assertEqual(snap["counts"].get("missing", 0), 0)
            for name, payload in before.items():
                self.assertEqual((root / name).read_bytes(), payload)


if __name__ == "__main__":
    unittest.main()
