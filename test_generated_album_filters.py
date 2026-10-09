#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""User-created albums resembling exports must remain visible in full scans."""
import tempfile
import unittest
from pathlib import Path

import photo_curator as core


class GeneratedAlbumExclusionTests(unittest.TestCase):
    def test_only_actual_export_naming_shapes_are_excluded(self):
        for name in (
            "TOP_50", "TOP_50_20261009_091500", "TOP_50_20261009_091500_2",
            "PhoneBG", "PhoneBG_20261009_091500", "PhoneBG_20261009_091500_3",
            "PhotoCurator_Result（照片筛选结果）",
            "PhotoCurator_RecycleBin（软件回收站）",
        ):
            with self.subTest(generated=name):
                self.assertTrue(core._is_output_dir_name(name))
        for name in (
            "Top_Family", "TOP_50_BestPhotos", "TOP_999", "PhoneBG_Trip",
            "PhoneBackgrounds", "TOP_25_Notes", "top_vacation",
        ):
            with self.subTest(original=name):
                self.assertFalse(core._is_output_dir_name(name))

    def test_real_user_albums_not_silently_omitted_from_recursive_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wanted = ("Top_Family", "PhoneBG_Trip", "top_vacation")
            generated = ("TOP_50", "PhoneBG_20261009_100500")
            for album in wanted + generated:
                d = root / album
                d.mkdir()
                (d / "photo.jpg").write_bytes(b"source")
            discovered = list(core.iter_images(root, recursive=True))
            self.assertEqual({p.parent.name for p in discovered}, set(wanted))


if __name__ == "__main__":
    unittest.main()
