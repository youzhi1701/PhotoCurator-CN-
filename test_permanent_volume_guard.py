#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Irreversible actions must remain bound to the same physical drive at consent."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import photo_curator as core


class PermanentVolumeSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'library'
        self.root.mkdir()
        self.photo = self.root / 'photo.jpg'
        self.photo.write_bytes(b'valuable original image')
        self.other = Path(self.temp.name) / 'other.jpg'
        self.other.write_bytes(b'unrelated photo')

    def _media(self, key):
        return {'identity_key': key}

    def _payload(self, source_key):
        return {'folder': str(self.root), 'path': str(self.photo),
                'source_volume_key': source_key,
                'source_identity': core._file_action_signature(self.photo),
                'sidecar_records': [], 'step': 'cull'}

    def test_swapped_disk_cannot_purge_matching_path_and_stat(self):
        payload = self._payload('win-guid:original-disk')
        with patch.object(core, '_verified_trash_library_volume',
                          return_value=True), \
             patch.object(core, 'catalog_volume_info_for_path',
                          return_value=self._media('win-guid:replacement-disk')), \
             patch.object(core, '_find_original_for_path',
                          return_value=str(self.photo)), \
             patch.object(core, '_apply_media_lifecycle'):
            with self.assertRaisesRegex(RuntimeError, '切换'):
                core._background_permanent_delete(payload)
        self.assertEqual(self.photo.read_bytes(), b'valuable original image')

    def test_old_queued_delete_without_device_claim_fails_closed(self):
        payload = self._payload('win-guid:original-disk')
        del payload['source_volume_key']
        with patch.object(core, '_find_original_for_path',
                          return_value=str(self.photo)), \
             patch.object(core, '_apply_media_lifecycle'):
            with self.assertRaisesRegex(RuntimeError, '旧版永久删除任务'):
                core._background_permanent_delete(payload)
        self.assertTrue(self.photo.exists())

    def test_review_token_invalid_after_drive_identity_changes(self):
        client = core.app.test_client()
        with patch.dict(core.state, {'folder': str(self.root)}, clear=False), \
             patch.object(core, '_known_step_paths',
                          return_value={str(self.photo)}), \
             patch.object(core, '_safe_image_path', return_value=self.photo), \
             patch.object(core, '_verified_trash_library_volume',
                          return_value=True), \
             patch.object(core, 'catalog_volume_info_for_path',
                          side_effect=[self._media('win-guid:original-disk'),
                                       self._media('win-guid:replacement-disk')]), \
             patch.object(core.TASK_MANAGER, 'enqueue') as enqueue:
            review = client.post('/api/review-permanent',
                                 json={'step': 'cull', 'path': str(self.photo)})
            self.assertEqual(review.status_code, 200, review.get_json())
            attempt = client.post('/api/delete-photo',
                                  json={'step': 'cull', 'mode': 'permanent',
                                        'path': str(self.photo),
                                        'review_token': review.get_json()['review_token']})
            self.assertEqual(attempt.status_code, 409, attempt.get_json())
            enqueue.assert_not_called()
        self.assertEqual(self.photo.read_bytes(), b'valuable original image')

    def test_review_cannot_consent_to_photo_outside_selected_library(self):
        client = core.app.test_client()
        with patch.dict(core.state, {'folder': str(self.root)}, clear=False), \
             patch.object(core, '_known_step_paths',
                          return_value={str(self.other)}), \
             patch.object(core, '_safe_image_path', return_value=self.other), \
             patch.object(core, '_verified_trash_library_volume',
                          return_value=True):
            review = client.post('/api/review-permanent',
                                 json={'step': 'cull', 'path': str(self.other)})
            self.assertEqual(review.status_code, 409, review.get_json())
        self.assertTrue(self.other.exists())

    def test_matching_device_key_only_passes_when_source_exists_and_is_inside(self):
        with patch.object(core, '_verified_trash_library_volume',
                          return_value=True), \
             patch.object(core, 'catalog_volume_info_for_path',
                          return_value=self._media('win-guid:original-disk')):
            self.assertEqual(
                core._verified_irreversible_source_volume(self.root, self.photo),
                'win-guid:original-disk')
            with self.assertRaisesRegex(RuntimeError, '不属于'):
                core._verified_irreversible_source_volume(self.root, self.other)

    def test_explicit_device_disconnection_rejects_worker_delete(self):
        payload = self._payload('win-guid:original-disk')
        with patch.object(core, '_verified_trash_library_volume',
                          return_value=False), \
             patch.object(core, '_find_original_for_path',
                          return_value=str(self.photo)), \
             patch.object(core, '_apply_media_lifecycle'):
            with self.assertRaisesRegex(RuntimeError, '已断开'):
                core._background_permanent_delete(payload)
        self.assertEqual(self.photo.read_bytes(), b'valuable original image')


if __name__ == '__main__':
    unittest.main()
