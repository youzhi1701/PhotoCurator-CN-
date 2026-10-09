#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Static release gate: Windows uninstall must never traverse user photo data."""
import re
import unittest
from pathlib import Path


class WindowsUninstallRetentionTests(unittest.TestCase):
    def test_uninstaller_preserves_all_runtime_data_and_user_originals(self):
        src = (Path(__file__).parent / "packaging" / "PhotoCurator.iss").read_text(
            encoding="utf-8")
        uninstaller = src.split("[UninstallDelete]", 1)[1].split("[Code]", 1)[0]
        self.assertNotRegex(uninstaller, r"(?im)^\s*Type\s*:\s*filesandordirs")
        self.assertNotRegex(src, r"(?i)\bDelTree\s*\(")
        self.assertIn("CurUninstallStepChanged", src)
        self.assertIn("ClearRecents", src)
        self.assertIn("if not UninstallSilent then", src)
        self.assertIn("recents.json", src)
        self.assertNotIn("DeleteFile(ExpandConstant('{localappdata}\\PhotoCurator\\data\\config\\library_index.sqlite3'))", src)
        self.assertNotRegex(src, r"(?im)^\s*Type\s*:\s*filesandordirs;\s*Name:\s*\"\{localappdata\}")
        # The release installer is allowed to replace ONLY its own program
        # directory during an *upgrade*, never any user runtime data.
        install = src.split("[InstallDelete]", 1)[1].split("[Files]", 1)[0]
        # Comments explain retained data locations; only executable Inno
        # Setup directives can delete anything during an upgrade.
        directives = [line.strip() for line in install.splitlines()
                      if line.strip() and not line.lstrip().startswith(';')]
        self.assertEqual(directives,
                         ['Type: filesandordirs; Name: "{app}\\app"'])


if __name__ == "__main__":
    unittest.main()
