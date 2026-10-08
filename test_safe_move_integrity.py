#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cross-volume moves must verify bytes, reject source swaps and preserve staging."""
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


    def test_cross_volume_source_replaced_during_copy_is_never_deleted(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            src, dst = root / 'original.jpg', root / 'output' / 'original.jpg'
            src.write_bytes(b'original-data')
            dst.parent.mkdir()
            real_copy = photo_curator.shutil.copy2
            real_rename = photo_curator._rename_no_replace

            def replace_after_copy(a, b):
                result = real_copy(a, b)
                src.rename(root / 'moved-original.jpg')
                src.write_bytes(b'original-data')
                return result

            def cross_volume_only(a, b):
                if Path(a) == src:
                    raise OSError(errno.EXDEV, 'simulate different volume')
                return real_rename(a, b)

            with (patch.object(photo_curator, '_rename_no_replace',
                              side_effect=cross_volume_only),
                 patch.object(photo_curator.shutil, 'copy2',
                              side_effect=replace_after_copy)):
                with self.assertRaisesRegex(RuntimeError, '原照片发生变化'):
                    photo_curator._safe_move_file(src, dst)
            self.assertEqual(src.read_bytes(), b'original-data')
            self.assertFalse(dst.exists())
            self.assertEqual((root / 'moved-original.jpg').read_bytes(),
                             b'original-data')

    def test_cross_volume_source_replaced_after_target_commit_stays_safe(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            src, dst = root / 'original.jpg', root / 'output' / 'original.jpg'
            src.write_bytes(b'original-data')
            dst.parent.mkdir()
            real_rename = photo_curator._rename_no_replace

            def replace_after_claim(a, b):
                if Path(a) == src:
                    raise OSError(errno.EXDEV, 'simulate different volume')
                result = real_rename(a, b)
                if Path(b) == dst:
                    src.rename(root / 'original-preserved.jpg')
                    src.write_bytes(b'stranger-content')
                return result

            with patch.object(photo_curator, '_rename_no_replace',
                              side_effect=replace_after_claim):
                with self.assertRaisesRegex(RuntimeError, '禁止删除新原片'):
                    photo_curator._safe_move_file(src, dst)
            self.assertEqual(src.read_bytes(), b'stranger-content')
            self.assertEqual(dst.read_bytes(), b'original-data')
            self.assertEqual((root / 'original-preserved.jpg').read_bytes(),
                             b'original-data')

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



class ReviewedSidecarTests(unittest.TestCase):
    def test_changed_sidecar_blocks_photo_move(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            photo=root/'photo.jpg'
            aux=root/'photo.xmp'
            photo.write_bytes(b'photo')
            aux.write_bytes(b'original')
            record=photo_curator._sidecar_review_snapshot(photo)
            aux.unlink()
            aux.write_bytes(b'replaced')
            with self.assertRaises(RuntimeError):
                photo_curator._move_photo_bundle(
                    photo, root/'bin'/'photo.jpg', sidecar_records=record)
            self.assertTrue(photo.is_file())
            self.assertEqual(aux.read_bytes(), b'replaced')

    def test_new_sidecar_after_queue_blocks_photo_move(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            photo=root/'photo.jpg'
            photo.write_bytes(b'photo')
            record=photo_curator._sidecar_review_snapshot(photo)
            (root/'photo.xmp').write_bytes(b'later')
            with self.assertRaisesRegex(RuntimeError, '发生变化'):
                photo_curator._move_photo_bundle(
                    photo, root/'bin'/'photo.jpg', sidecar_records=record)
            self.assertTrue(photo.is_file())

    def test_resume_sidecar_after_photo_crash(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            photo=root/'photo.jpg'
            aux=root/'photo.xmp'
            photo.write_bytes(b'photo')
            aux.write_bytes(b'metadata')
            record=photo_curator._sidecar_review_snapshot(photo)
            dst=root/'bin'
            dst.mkdir()
            photo.rename(dst/'photo.jpg')
            photo_curator._resume_sidecar_bundle(photo,dst/'photo.jpg',record)
            self.assertEqual((dst/'photo.xmp').read_bytes(),b'metadata')
            photo_curator._resume_sidecar_bundle(photo,dst/'photo.jpg',record)

    def test_symlink_sidecar_not_transferred(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            photo=root/'photo.jpg'
            other=root/'other.txt'
            photo.write_bytes(b'photo')
            other.write_bytes(b'secret')
            try:
                (root/'photo.xmp').symlink_to(other)
            except (OSError,NotImplementedError):
                self.skipTest('symlink unavailable')
            self.assertEqual(photo_curator._sidecar_review_snapshot(photo),[])
            self.assertEqual(other.read_bytes(),b'secret')


if __name__ == '__main__':
    unittest.main()
