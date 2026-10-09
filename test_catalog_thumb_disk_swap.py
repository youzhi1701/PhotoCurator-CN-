#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The history gallery must never decode a stranger's disk as an old photo."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import photo_curator as appmod


class CatalogThumbnailDiskIdentityTests(unittest.TestCase):
    @staticmethod
    def _record(root, photo, *, size=None, mtime_ns=None):
        st = photo.stat()
        return {
            'media_id': 'a' * 64,
            'current_root': str(root),
            'source_identity_key': 'disk-original',
            'relative_path': photo.name,
            'current_path': str(photo),
            'original_path': str(photo),
            'size': st.st_size if size is None else size,
            'mtime_ns': st.st_mtime_ns if mtime_ns is None else mtime_ns,
            'state': 'present',
            'lifecycle': 'normal',
        }

    def test_offline_cached_preview_is_still_visible_without_disk(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            previews = root / "offline"
            previews.mkdir()
            (previews / ('a' * 64 + '.jpg')).write_bytes(b'cached-pixel-data')
            record = {'media_id': 'a' * 64, 'current_root': str(root / 'gone')}
            with patch.object(appmod, 'OFFLINE_PREVIEW_DIR', previews), \
                 patch.object(appmod, 'catalog_media_record', return_value=record), \
                 patch.object(appmod, 'catalog_volume_info_for_path') as probe, \
                 patch.object(appmod, 'make_thumb_file') as decode:
                result = appmod.app.test_client().get('/api/catalog-thumb?media_id=' + 'a'*64)
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.data, b'cached-pixel-data')
                result.close()
                probe.assert_not_called()
                decode.assert_not_called()

    def test_reused_mount_letter_never_reads_foreign_photo(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'reused-F-drive'
            root.mkdir()
            current = root / 'portrait.jpg'
            current.write_bytes(b'new-disk-photo')
            row = self._record(root, current)
            with patch.object(appmod, 'OFFLINE_PREVIEW_DIR', Path(td) / 'empty'), \
                 patch.object(appmod, 'catalog_media_record', return_value=row), \
                 patch.object(appmod, 'catalog_volume_info_for_path',
                              return_value={'identity_key': 'disk-foreign'}), \
                 patch.object(appmod, 'make_thumb_file') as decode:
                result = appmod.app.test_client().get('/api/catalog-thumb?media_id=' + 'a'*64)
                self.assertEqual(result.status_code, 404)
                decode.assert_not_called()

    def test_matching_disk_but_replaced_image_cannot_be_rendered_as_history(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'library'
            root.mkdir()
            current = root / 'photo.jpg'
            current.write_bytes(b'changed')
            row = self._record(root, current, size=999999)
            with patch.object(appmod, 'OFFLINE_PREVIEW_DIR', Path(td) / 'empty'), \
                 patch.object(appmod, 'catalog_media_record', return_value=row), \
                 patch.object(appmod, 'catalog_volume_info_for_path',
                              return_value={'identity_key': 'disk-original'}), \
                 patch.object(appmod, 'make_thumb_file') as decode:
                result = appmod.app.test_client().get('/api/catalog-thumb?media_id=' + 'a'*64)
                self.assertEqual(result.status_code, 404)
                decode.assert_not_called()

    def test_valid_disk_and_unchanged_photo_only_use_ephemeral_cache(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'library'
            root.mkdir()
            current = root / 'image.jpg'
            current.write_bytes(b'photo-bytes')
            row = self._record(root, current)
            preview = Path(td) / 'rebuildable.jpg'
            preview.write_bytes(b'preview')
            with patch.object(appmod, 'OFFLINE_PREVIEW_DIR', Path(td) / 'empty'), \
                 patch.object(appmod, 'catalog_media_record', return_value=row), \
                 patch.object(appmod, 'catalog_volume_info_for_path',
                              return_value={'identity_key': 'disk-original'}), \
                 patch.object(appmod, 'make_thumb_file', return_value=preview) as decode:
                result = appmod.app.test_client().get('/api/catalog-thumb?media_id=' + 'a'*64)
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.data, b'preview')
                result.close()
                args, kwargs = decode.call_args
                self.assertEqual(args[0], str(current))
                self.assertEqual(kwargs['expected_signature'],
                                 appmod._file_action_signature(current))
                self.assertNotIn('trusted_media_id', kwargs)

    def test_mid_decode_drive_swap_refuses_durable_preview(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'source'
            root.mkdir()
            image = root / 'photo.jpg'
            Image.new('RGB', (100, 70), 'blue').save(image)
            offline = Path(td) / 'offline'
            offline.mkdir()
            signature = appmod._file_action_signature(image)
            observations = [
                {'identity_key': 'disk-original'},
                {'identity_key': 'disk-original'},
                {'identity_key': 'disk-replaced'},
            ]
            with (
                patch.object(appmod, 'OFFLINE_PREVIEW_DIR', offline),
                patch.object(appmod, 'catalog_volume_info_for_path',
                             side_effect=observations),
            ):
                result = appmod.make_thumb_file(
                    str(image), expected_signature=signature,
                    trusted_media_id='a'*64,
                    verified_source=(str(root), 'disk-original'),
                )
            self.assertIsNone(result)
            self.assertEqual(list(offline.glob('*.jpg')), [])
            self.assertEqual(list(offline.glob('*.tmp')), [])
            self.assertTrue(image.exists())

    def test_regular_thumbnail_cache_cannot_claim_offline_media_id(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            original = root / 'original.jpg'
            original.write_bytes(b'bytes')
            with patch.object(appmod, 'THUMB_DIR', root / 'temporary'), \
                 patch.object(appmod, 'OFFLINE_PREVIEW_DIR', root / 'offline'), \
                 patch.object(appmod, 'catalog_media_id_for_path') as db_lookup:
                target = appmod._thumb_cache_path(original)
                self.assertEqual(target.parent, root / 'temporary')
                db_lookup.assert_not_called()
                self.assertEqual(
                    appmod._thumb_cache_path(original, trusted_media_id='a'*64).parent,
                    root / 'offline')
                self.assertIsNone(appmod.make_thumb_file(
                    str(original), trusted_media_id='a'*64))
                self.assertFalse((root / 'offline').exists())


if __name__ == '__main__':
    unittest.main()
