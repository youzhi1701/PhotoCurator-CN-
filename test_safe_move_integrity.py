#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cross-volume file moves must verify data and preserve unrelated staging."""
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
            real_replace = photo_curator.os.replace
            def fail_first_replace(a, b):
                if str(a) == str(src):
                    raise OSError('simulate different volumes')
                return real_replace(a, b)
            with patch.object(photo_curator.os, 'replace', side_effect=fail_first_replace):
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
            with (patch.object(photo_curator.os, 'replace', side_effect=OSError('different volume')),
                  patch.object(photo_curator.shutil, 'copy2', side_effect=corrupt_copy)):
                with self.assertRaisesRegex(IOError, '文件内容不一致'):
                    photo_curator._safe_move_file(src, dst)
            self.assertEqual(src.read_bytes(), b'abcde')
            self.assertFalse(dst.exists())
            self.assertEqual(list(dst.parent.glob('*.photocurator-part')), [])


if __name__ == '__main__':
    unittest.main()
