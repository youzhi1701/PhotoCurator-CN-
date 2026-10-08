#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Byte-exact duplicate indexing: no visual false positives and no file actions."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from exact_duplicates import exact_duplicate_groups
from photo_dedup_batch import FastBatchDeduplicator


class Score:
    def __init__(self, path, quality=50):
        self.path = str(path)
        self.overall_score = quality
        self.focus = quality
        self.filename = Path(path).name


class ExactDuplicateIndexTests(unittest.TestCase):
    def test_identical_bytes_across_folders_only_group_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p = root / "a.jpg"
            q = root / "nested" / "b.jpg"
            q.parent.mkdir()
            other = root / "different.jpg"
            p.write_bytes(b"same pixel data including EXIF" * 100)
            q.write_bytes(p.read_bytes())
            other.write_bytes(b"other photo")
            before = [p.read_bytes(), q.read_bytes(), other.read_bytes()]
            groups = exact_duplicate_groups(
                [p, other, q, q], cache_path=root / "exact-cache.json")
            self.assertEqual(groups, [[str(p), str(q)]])
            self.assertEqual(before,
                             [p.read_bytes(), q.read_bytes(), other.read_bytes()])
            self.assertEqual(groups, exact_duplicate_groups(
                [p, other, q], cache_path=root / "exact-cache.json"))

    def test_equal_size_is_not_proof_of_same_photo(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / "a.jpg"
            b = Path(tmp) / "b.jpg"
            a.write_bytes(b"ABCD")
            b.write_bytes(b"DCBA")
            self.assertEqual(exact_duplicate_groups([a, b]), [])

    def test_changed_content_with_new_mtime_invalidates_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / "a.jpg"
            b = Path(tmp) / "b.jpg"
            a.write_bytes(b"ABCD")
            b.write_bytes(b"ABCD")
            cache = Path(tmp) / "cache.json"
            self.assertEqual(len(exact_duplicate_groups([a, b], cache_path=cache)), 1)
            previous = b.stat()
            b.write_bytes(b"DCBA")
            os.utime(b, ns=(previous.st_atime_ns,
                            previous.st_mtime_ns + 2_000_000_000))
            self.assertEqual(exact_duplicate_groups([a, b], cache_path=cache), [])

    def test_unavailable_paths_never_become_exact_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / "a.jpg"
            a.write_bytes(b"photo")
            missing = Path(tmp) / "disconnected" / "b.jpg"
            self.assertEqual(exact_duplicate_groups([a, missing]), [])
            self.assertEqual(exact_duplicate_groups([a, a]), [])

    def test_cancelled_hashing_yields_no_partial_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "a.jpg", Path(tmp) / "b.jpg"
            a.write_bytes(b"1234")
            b.write_bytes(b"1234")
            self.assertEqual(exact_duplicate_groups(
                [a, b], cancelled=lambda: True), [])

    def test_byte_identical_alias_is_grouped_even_when_image_decode_fails(self):
        dd = FastBatchDeduplicator(use_orb_confirm=False)
        dd.reset()
        first = Score("fake_a.jpg")
        alias = Score("fake_b.jpg")
        with patch.object(dd, "_signature", return_value=None), \
             patch.object(dd, "_capture_time", return_value=None):
            self.assertTrue(dd.add_photo(first))
            self.assertTrue(dd.attach_exact_duplicate(alias, first.path))
        self.assertEqual(len(dd.clusters), 1)
        self.assertEqual([x.path for x in dd.clusters[0].members],
                         ["fake_a.jpg", "fake_b.jpg"])
        self.assertFalse(dd.attach_exact_duplicate(
            Score("unknown.jpg"), "not-indexed.jpg"))


if __name__ == "__main__":
    unittest.main()
