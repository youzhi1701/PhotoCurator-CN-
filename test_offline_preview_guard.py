#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Prevent changed/replaced/remounted images from contaminating durable thumbnails."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import photo_curator


class OfflinePreviewSourceGuardTests(unittest.TestCase):
    def snapshot(self, root, identity="disk-A"):
        return {"root": {"current_root": str(root)},
                "source": {"identity_key": identity}}

    def test_reused_letter_with_different_device_cannot_build_previews(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "images"
            root.mkdir()
            snap = self.snapshot(root)
            with patch.object(photo_curator, "catalog_volume_info_for_path",
                              return_value={"identity_key": "disk-B"}):
                self.assertFalse(photo_curator._offline_preview_source_matches(snap))

    def test_same_real_disk_accepted_and_offline_root_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "images"
            root.mkdir()
            with patch.object(photo_curator, "catalog_volume_info_for_path",
                              return_value={"identity_key": "disk-A"}):
                self.assertTrue(photo_curator._offline_preview_source_matches(
                    self.snapshot(root)))
                self.assertFalse(photo_curator._offline_preview_source_matches(
                    self.snapshot(root / "removed")))

    def test_replaced_same_path_is_not_reused_as_old_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            photo = root / "photo.jpg"
            photo.write_bytes(b"old-capture")
            before = photo.stat()
            old = {"current_path": str(photo), "size": before.st_size,
                   "mtime_ns": before.st_mtime_ns}
            self.assertIsNotNone(
                photo_curator._offline_preview_candidate_signature(root, old))
            photo.write_bytes(b"new-photo-content-replacing-old")
            self.assertIsNone(
                photo_curator._offline_preview_candidate_signature(root, old))

    def test_symlink_outside_library_not_eligible_for_thumbnail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "library"
            root.mkdir()
            outside = Path(tmp) / "private.jpg"
            outside.write_bytes(b"private")
            path = root / "photo.jpg"
            try:
                path.symlink_to(outside)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")
            st = outside.stat()
            row = {"current_path": str(path), "size": st.st_size,
                   "mtime_ns": st.st_mtime_ns}
            self.assertIsNone(
                photo_curator._offline_preview_candidate_signature(root, row))

    def test_worker_aborts_wrong_device_before_decoding_any_photo(self):
        with tempfile.TemporaryDirectory() as tmp:
            snap = self.snapshot(Path(tmp))
            snap["items"] = [{
                "media_id": "a" * 64, "state": "present",
                "current_path": str(Path(tmp) / "candidate.jpg"),
                "size": 10, "mtime_ns": 1,
            }]
            with patch.object(photo_curator, "catalog_root_snapshot",
                              return_value=snap), patch.object(
                                  photo_curator, "catalog_volume_info_for_path",
                                  return_value={"identity_key": "disk-B"}), patch.object(
                                  photo_curator, "make_thumb_file"
                              ) as thumb:
                outcome = photo_curator._background_build_offline_previews(
                    {"root_id": "r1", "offset": 0})
                self.assertFalse(outcome["ok"])
                thumb.assert_not_called()


if __name__ == "__main__":
    unittest.main()
