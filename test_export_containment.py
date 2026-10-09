#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Exported TOP-N/PhoneBG output must not overwrite user-owned files."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
import photo_curator as core


class ExportFileContainmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'library'
        self.root.mkdir()
        self.outside = Path(self.temp.name) / 'outside'
        self.outside.mkdir()
        self.photo = self.root / 'photo.jpg'
        Image.new('RGB', (12, 14), '#778899').save(self.photo, format='JPEG')
        self.item = {'path': str(self.photo), 'rank': 1}
        self.client = core.app.test_client()

    def _export(self, endpoint, album, *, wallpaper=False):
        with patch.dict(core.state, {'folder': str(self.root)}, clear=False), \
             patch.object(core, 'build_topn', return_value=[self.item]), \
             patch.object(core, '_reject_mutation_while_running', return_value=None), \
             patch.object(core, '_verified_trash_library_volume', return_value=True):
            if wallpaper:
                with patch.dict(core.state, {'phone_bg': {str(self.photo)}}, clear=False), \
                     patch.object(core, 'crop_to_phone',
                                  return_value=Image.new('RGB', (8, 14))):
                    return self.client.post(endpoint, json={})
            return self.client.post(endpoint, json={'topn': 1})

    def test_top_export_never_uses_existing_symlink_target(self):
        existing = self.outside / '001_photo.jpg'
        existing.write_bytes(b'valuable user photo')
        shortcut = self.root / 'TOP_1'
        try:
            shortcut.symlink_to(self.outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('directory symlinks unavailable')
        response = self._export('/api/export', 'TOP_1')
        self.assertEqual(response.status_code, 200, response.get_json())
        body = response.get_json()
        self.assertEqual((body['copied'], body['failed']), (1, 0))
        dest = Path(body['dest'])
        self.assertNotEqual(dest, shortcut)
        self.assertEqual(dest.parent, self.root)
        self.assertEqual((dest / '001_photo.jpg').read_bytes(), self.photo.read_bytes())
        self.assertEqual(existing.read_bytes(), b'valuable user photo')

    def test_export_never_reuses_existing_empty_directory(self):
        (self.root / 'TOP_1').mkdir()
        first = self._export('/api/export', 'TOP_1')
        second = self._export('/api/export', 'TOP_1')
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        first_dest, second_dest = Path(first.get_json()['dest']), Path(second.get_json()['dest'])
        self.assertNotEqual(first_dest, second_dest)
        self.assertNotEqual(first_dest, self.root / 'TOP_1')
        self.assertEqual(first_dest.joinpath('001_photo.jpg').read_bytes(), self.photo.read_bytes())
        self.assertEqual(second_dest.joinpath('001_photo.jpg').read_bytes(), self.photo.read_bytes())

    def test_exclusive_copy_refuses_racing_output_and_cleans_staging(self):
        output = self.root / 'keep.jpg'
        output.write_bytes(b'do not overwrite')
        with self.assertRaises(FileExistsError):
            core._copy_export_photo_no_replace(self.photo, output)
        self.assertEqual(output.read_bytes(), b'do not overwrite')
        self.assertEqual(list(self.root.glob('*.photocurator-export')), [])

    def test_source_replaced_during_export_does_not_publish_output(self):
        output = self.root / 'export.jpg'
        old = self.root / 'moved-original.jpg'
        original = self.photo.read_bytes()
        original_copy = core.shutil.copyfileobj

        def change_source(reader, writer, length=1024 * 1024):
            original_copy(reader, writer, length=length)
            self.photo.rename(old)
            self.photo.write_bytes(b'replacement')

        with patch.object(core.shutil, 'copyfileobj', side_effect=change_source):
            with self.assertRaises(RuntimeError):
                core._copy_export_photo_no_replace(self.photo, output)
        self.assertFalse(output.exists())
        self.assertEqual(old.read_bytes(), original)
        self.assertEqual(self.photo.read_bytes(), b'replacement')
        self.assertEqual(list(self.root.glob('*.photocurator-export')), [])

    def test_wallpaper_export_never_follows_existing_album_link(self):
        destination = self.outside / '原始照片'
        destination.mkdir()
        existing = destination / '001_photo.jpg'
        existing.write_bytes(b'protected wallpaper source')
        (self.outside / '壁纸_19.5x9').mkdir()
        shortcut = self.root / 'PhoneBG'
        try:
            shortcut.symlink_to(self.outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('directory symlinks unavailable')
        response = self._export('/api/export-phonebg', 'PhoneBG', wallpaper=True)
        self.assertEqual(response.status_code, 200, response.get_json())
        body = response.get_json()
        self.assertEqual((body['copied'], body['cropped'], body['failed']), (1, 1, 0))
        dest = Path(body['dest'])
        self.assertNotEqual(dest, shortcut)
        self.assertEqual((dest / '原始照片' / '001_photo.jpg').read_bytes(), self.photo.read_bytes())
        self.assertTrue((dest / '壁纸_19.5x9' / '001_photo.jpg').is_file())
        self.assertEqual(existing.read_bytes(), b'protected wallpaper source')

    def test_existing_wallpaper_file_cannot_be_overwritten(self):
        out = self.root / 'wallpaper.jpg'
        out.write_bytes(b'protected')
        with self.assertRaises(FileExistsError):
            core._save_wallpaper_no_replace(Image.new('RGB', (5, 5)), out)
        self.assertEqual(out.read_bytes(), b'protected')
        self.assertEqual(list(self.root.glob('*.jpg.tmp')), [])

    def test_wrong_drive_identity_denies_export_before_writing(self):
        with patch.dict(core.state, {'folder': str(self.root)}, clear=False), \
             patch.object(core, 'build_topn', return_value=[self.item]), \
             patch.object(core, '_reject_mutation_while_running', return_value=None), \
             patch.object(core, '_verified_trash_library_volume', return_value=False):
            response = self.client.post('/api/export', json={'topn': 1})
        self.assertEqual(response.status_code, 409, response.get_json())
        self.assertFalse((self.root / 'TOP_1').exists())
        self.assertTrue(self.photo.is_file())


if __name__ == '__main__':
    unittest.main()
