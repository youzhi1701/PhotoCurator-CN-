#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Persistent library devices must not reconnect based on a reused mount name."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import catalog


class PhysicalMediaIdentityTests(unittest.TestCase):
    def test_reused_drive_letter_cannot_reconnect_another_volume(self):
        reused_mount = {"identity_key": "win-guid:second-disk", "mount_path": "F:\\\\ "}
        observed = {
            "win-mount:f:\\\\": reused_mount,
            "win-guid:second-disk": reused_mount,
        }
        self.assertIsNone(catalog._verified_windows_mount("win-mount:f:\\\\", observed))
        self.assertIsNone(catalog._verified_windows_mount("win-volume:old-fallback", observed))
        self.assertIsNone(catalog._verified_windows_mount("win-guid:first-disk", observed))

    def test_only_matching_guid_reconnects(self):
        source = {"identity_key": "win-guid:original", "mount_path": "G:\\\\"}
        self.assertIs(catalog._verified_windows_mount(
            "win-guid:original", {"win-guid:original": source}), source)
        with patch.dict(source, {"identity_key": "win-guid:other"}):
            self.assertIsNone(catalog._verified_windows_mount(
                "win-guid:original", {"win-guid:original": source}))

    def test_mount_name_not_enough_for_posix_reconnect(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount = Path(tmp) / "disk"
            mount.mkdir()
            with patch.object(catalog, "volume_info_for_path",
                              return_value={"identity_key": "posix-volume:other-disk"}):
                self.assertFalse(catalog._verified_posix_mount(
                    "posix-volume:original-disk", str(mount)))
                self.assertTrue(catalog._verified_posix_mount(
                    "posix-volume:other-disk", str(mount)))
            self.assertFalse(catalog._verified_posix_mount(
                "posix-volume:original-disk", str(mount / "disconnected")))

    @unittest.skipIf(os.name == "nt", "POSIX-specific mount discovery")
    def test_posix_source_has_its_real_mount_and_device(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "photos"
            root.mkdir()
            info = catalog.volume_info_for_path(root)
            self.assertEqual(info["volume_serial"], str(root.stat().st_dev))
            self.assertTrue(str(root).startswith(info["mount_path"]))
            self.assertTrue(catalog._verified_posix_mount(
                info["identity_key"], info["mount_path"]))
            with patch.object(catalog, "_posix_mount_for_path", return_value=str(root)):
                synthetic = catalog.volume_info_for_path(root)
            if info["mount_path"] != str(root):
                self.assertNotEqual(synthetic["identity_key"], info["identity_key"])

if __name__ == "__main__":
    unittest.main()
