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

    def test_external_symlinked_photo_is_not_indexed_as_library_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = parent / 'library'
            external = parent / 'external'
            root.mkdir()
            external.mkdir()
            own = root / 'own.jpg'
            own.write_bytes(b'original')
            foreign = external / 'someone-else.jpg'
            foreign.write_bytes(b'never scan outside root')
            link = root / 'foreign.jpg'
            try:
                link.symlink_to(foreign)
            except (OSError, NotImplementedError):
                self.skipTest('source-file symlinks not available')
            self.assertEqual(list(core.iter_images(root, recursive=False)), [own])
            self.assertEqual(list(core.iter_images(root, recursive=True)), [own])
            self.assertEqual(foreign.read_bytes(), b'never scan outside root')

    def test_external_symlinked_folder_is_not_recursed(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = parent / 'library'
            external = parent / 'external'
            root.mkdir()
            external.mkdir()
            (external / 'outside.jpg').write_bytes(b'external')
            shortcut = root / 'shortcut'
            try:
                shortcut.symlink_to(external, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest('directory symlinks unavailable')
            self.assertEqual(list(core.iter_images(root, recursive=True)), [])
            self.assertEqual((external / 'outside.jpg').read_bytes(), b'external')

    def test_scan_error_handler_receives_unavailable_directory_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            errors = []
            original = core.os.scandir

            def scanner(folder):
                if str(folder) == str(root):
                    raise PermissionError('unavailable volume')
                return original(folder)

            from unittest.mock import patch
            with patch.object(core.os, 'scandir', side_effect=scanner):
                self.assertEqual(
                    list(core.iter_images(root, recursive=True, on_error=errors.append)),
                    [],
                )
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], PermissionError)


if __name__ == "__main__":
    unittest.main()
