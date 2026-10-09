#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Untrusted USB trash metadata must never import or expose outside files."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import photo_curator
from db_runtime import connect_db


class TrashManifestContainmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'library'
        self.root.mkdir()
        self.bin = self.root / photo_curator.SOFTWARE_TRASH_DIR
        self.bin.mkdir()
        self.manifest = self.bin / photo_curator.TRASH_MANIFEST_NAME
        self.outside = Path(self.tmp.name) / 'outside.jpg'
        self.outside.write_bytes(b'never delete this')
        self.photo = self.root / 'photo.jpg'
        self.trashed = self.bin / 'photo.jpg'
        self.trashed.write_bytes(b'valid photo')
        self.db_path = Path(self.tmp.name) / 'metadata.sqlite'
        self.patch_db = patch.object(photo_curator, 'INDEX_DB', self.db_path)
        self.patch_db.start()
        self.addCleanup(self.patch_db.stop)
        with connect_db(self.db_path) as db:
            db.execute('''CREATE TABLE software_trash (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_path TEXT, trash_path TEXT,
                source_step TEXT, deleted_at REAL)''')
            db.commit()

    def _rows(self):
        with connect_db(self.db_path) as db:
            return db.execute(
                'SELECT original_path,trash_path FROM software_trash ORDER BY id'
            ).fetchall()

    def _write_items(self, items, version=1):
        self.manifest.write_text(
            json.dumps({'version': version, 'items': items}, ensure_ascii=False),
            encoding='utf-8',
        )

    def _item(self, original, trashed):
        return {'original_path': str(original), 'trash_path': str(trashed),
                'source_step': 'cull', 'deleted_at': 12345678}

    def test_forged_manifest_cannot_import_external_or_nested_prefix_path(self):
        near = Path(self.tmp.name) / 'library-other'
        near.mkdir()
        near_photo = near / 'near.jpg'
        near_photo.write_bytes(b'unrelated')
        self._write_items([
            self._item(self.outside, self.trashed),
            self._item(self.photo, self.outside),
            self._item(near_photo, self.trashed),
            self._item(self.photo, near_photo),
            self._item(self.photo, self.trashed),
        ])
        photo_curator._import_trash_manifest(self.root)
        self.assertEqual(self._rows(), [(str(self.photo), str(self.trashed))])
        self.assertEqual(self.outside.read_bytes(), b'never delete this')

    def test_valid_local_manifest_restores_record_without_file_mutation(self):
        self._write_items([self._item(self.photo, self.trashed)])
        photo_curator._import_trash_manifest(self.root)
        photo_curator._import_trash_manifest(self.root)
        self.assertEqual(len(self._rows()), 1)
        self.assertEqual(self.trashed.read_bytes(), b'valid photo')

    def test_import_rejects_symlink_and_unknown_manifest_version(self):
        self._write_items([self._item(self.photo, self.trashed)], version=404)
        photo_curator._import_trash_manifest(self.root)
        self.assertEqual(self._rows(), [])
        self.manifest.unlink()
        try:
            self.manifest.symlink_to(self.outside)
        except (OSError, NotImplementedError):
            self.skipTest('symlinks unavailable')
        photo_curator._import_trash_manifest(self.root)
        self.assertEqual(self._rows(), [])
        self.assertEqual(self.outside.read_bytes(), b'never delete this')

    def test_write_excludes_untrusted_rows_and_leaves_no_temp_files(self):
        with connect_db(self.db_path) as db:
            db.executemany(
                'INSERT INTO software_trash(original_path,trash_path,source_step,deleted_at)'
                ' VALUES(?,?,?,?)',
                [(str(self.photo), str(self.trashed), 'cull', 1.0),
                 (str(self.outside), str(self.outside), 'cull', 2.0)],
            )
            db.commit()
        photo_curator._write_trash_manifest(self.root)
        photo_curator._write_trash_manifest(self.root)
        data = json.loads(self.manifest.read_text(encoding='utf-8'))
        self.assertEqual(len(data['items']), 1)
        self.assertEqual(data['items'][0]['trash_path'], str(self.trashed))
        self.assertEqual(list(self.bin.glob('*.tmp')), [])

    def test_trash_list_never_shows_injected_db_path(self):
        with connect_db(self.db_path) as db:
            db.executemany(
                'INSERT INTO software_trash(original_path,trash_path,source_step,deleted_at)'
                ' VALUES(?,?,?,?)',
                [(str(self.photo), str(self.trashed), 'cull', 1.0),
                 (str(self.outside), str(self.outside), 'cull', 2.0)],
            )
            db.commit()
        rows = photo_curator._trash_rows(self.root)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['original_path'], str(self.photo))

    def test_move_refuses_photo_outside_root_before_touching_it(self):
        with self.assertRaisesRegex(ValueError, '不属于当前图库'):
            photo_curator._trash_destination(self.outside, self.root)
        with self.assertRaisesRegex(ValueError, '不属于当前照片库'):
            photo_curator._move_to_software_trash(
                self.outside, self.root, 'cull',
                planned_trash_path=self.bin / 'outside.jpg',
            )
        self.assertEqual(self.outside.read_bytes(), b'never delete this')


if __name__ == '__main__':
    unittest.main()
