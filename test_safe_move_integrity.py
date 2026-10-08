#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cross-volume file moves must verify data and preserve unrelated staging."""
import errno
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import photo_curator


class SafeMoveTests(unittest.TestCase):
    def test_cross_volume_fallback_verifies_bytes_and_preserves_stale_part(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            src = root / 'photo.jpg'
            dst = root / 'elsewhere' / 'photo.jpg'
            src.write_bytes(b'original-photo-data' * 500)
            dst.parent.mkdir()
            stale = dst.with_name(dst.name + '.photocurator-part')
            stale.write_bytes(b'an unrelated interrupted operation')
            real_rename = photo_curator._rename_no_replace
            def fail_first_rename(a, b):
                if str(a) == str(src):
                    raise OSError(errno.EXDEV, 'simulate different volumes')
                return real_rename(a, b)
            with patch.object(photo_curator, '_rename_no_replace',
                              side_effect=fail_first_rename):
                photo_curator._safe_move_file(src, dst)
            self.assertFalse(src.exists())
            self.assertEqual(dst.read_bytes(), b'original-photo-data' * 500)
            self.assertEqual(stale.read_bytes(), b'an unrelated interrupted operation')
            self.assertEqual(list(dst.parent.glob('*.photocurator-part')), [stale])

    def test_verification_failure_preserves_original_and_target(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            src = root / 'photo.jpg'
            dst = root / 'output' / 'photo.jpg'
            src.write_bytes(b'abcde')
            dst.parent.mkdir()
            def corrupt_copy(source, destination):
                Path(destination).write_bytes(b'abcdf')  # identical size, different content
            with (patch.object(photo_curator, '_rename_no_replace',
                               side_effect=OSError(errno.EXDEV, 'different volume')),
                  patch.object(photo_curator.shutil, 'copy2', side_effect=corrupt_copy)):
                with self.assertRaisesRegex(IOError, '文件内容不一致'):
                    photo_curator._safe_move_file(src, dst)
            self.assertEqual(src.read_bytes(), b'abcde')
            self.assertFalse(dst.exists())
            self.assertEqual(list(dst.parent.glob('*.photocurator-part')), [])


    def test_same_volume_racing_destination_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            src, dst = root / 'src.jpg', root / 'dst.jpg'
            src.write_bytes(b'valuable original')
            real_rename = photo_curator._rename_no_replace
            def concurrent_creation(a, b):
                dst.write_bytes(b'file created by another application')
                return real_rename(a, b)
            with patch.object(photo_curator, '_rename_no_replace',
                              side_effect=concurrent_creation):
                with self.assertRaises(FileExistsError):
                    photo_curator._safe_move_file(src, dst)
            self.assertEqual(src.read_bytes(), b'valuable original')
            self.assertEqual(dst.read_bytes(), b'file created by another application')

    def test_cross_volume_commit_cannot_replace_racing_destination(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            src = root / 'src.jpg'
            dst = root / 'output' / 'dst.jpg'
            src.write_bytes(b'keep all original bytes')
            dst.parent.mkdir()
            real_rename = photo_curator._rename_no_replace
            def simulate_other_volume_and_racing_target(a, b):
                if str(a) == str(src):
                    raise OSError(errno.EXDEV, 'different volumes')
                dst.write_bytes(b'unrelated target claimed concurrently')
                return real_rename(a, b)
            with patch.object(photo_curator, '_rename_no_replace',
                              side_effect=simulate_other_volume_and_racing_target):
                with self.assertRaises(FileExistsError):
                    photo_curator._safe_move_file(src, dst)
            self.assertEqual(src.read_bytes(), b'keep all original bytes')
            self.assertEqual(dst.read_bytes(), b'unrelated target claimed concurrently')
            self.assertEqual(list(dst.parent.glob('*.photocurator-part')), [])


if __name__ == '__main__':
    unittest.main()
