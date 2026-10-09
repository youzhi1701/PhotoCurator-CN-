#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Same-stem RAW+JPEG review must never hide unrelated image formats."""
import os
import unittest
from photo_curator import collapse_raw_jpg_pairs


class SameCaptureProtectionTests(unittest.TestCase):
    def test_only_raw_and_jpg_are_collapsed(self):
        paths=['F:/gallery/IMG_100.CR2','F:/gallery/IMG_100.JPG',
               'F:/gallery/IMG_100.PNG','F:/gallery/IMG_100.HEIC']
        keep_raw,n=collapse_raw_jpg_pairs(paths,'raw')
        keep_jpg,m=collapse_raw_jpg_pairs(paths,'jpg')
        self.assertEqual(n,1)
        self.assertEqual(m,1)
        self.assertEqual(keep_raw,[paths[0],paths[2],paths[3]])
        self.assertEqual(keep_jpg,[paths[1],paths[2],paths[3]])

    def test_raw_plus_nonjpeg_formats_never_disappear(self):
        files=['F:/gallery/P1.NEF','F:/gallery/P1.PNG','F:/gallery/P1.HEIC']
        for option in ('raw','jpg'):
            self.assertEqual(collapse_raw_jpg_pairs(files,option),(files,0))

    def test_only_same_directory_and_same_basename_are_pairs(self):
        files=['F:/one/A.CR2','F:/two/A.JPG','F:/one/B.JPG']
        self.assertEqual(collapse_raw_jpg_pairs(files,'raw'),(files,0))

    def test_keep_both_is_noop_and_same_shot_jpeg_casefolds(self):
        files=['F:/one/A.CR2','F:/one/a.jpeg','F:/one/Z.jpg']
        self.assertEqual(collapse_raw_jpg_pairs(files,'both'),(files,0))
        expected = ([files[0], files[2]], 1) if os.name == 'nt' else (files, 0)
        self.assertEqual(collapse_raw_jpg_pairs(files, 'raw'), expected)

    def test_multiple_raw_types_share_stem_without_disappearing(self):
        files = ['F:/gallery/C001.CR2', 'F:/gallery/C001.DNG',
                 'F:/gallery/C001.JPG', 'F:/gallery/C001.PNG']
        for preference in ('raw', 'jpg'):
            with self.subTest(preference=preference):
                self.assertEqual(collapse_raw_jpg_pairs(files, preference), (files, 0))

    def test_multiple_jpeg_variants_share_stem_without_disappearing(self):
        files = ['F:/gallery/C002.NEF', 'F:/gallery/C002.JPG',
                 'F:/gallery/C002.JPEG']
        for preference in ('raw', 'jpg'):
            with self.subTest(preference=preference):
                self.assertEqual(collapse_raw_jpg_pairs(files, preference), (files, 0))

    def test_duplicate_input_entries_do_not_make_unambiguous_pair_ambiguous(self):
        files = ['F:/gallery/C003.CR3', 'F:/gallery/C003.JPG',
                 'F:/gallery/C003.CR3']
        self.assertEqual(
            collapse_raw_jpg_pairs(files, 'raw'),
            ([files[0], files[2]], 1),
        )

    @unittest.skipIf(os.name == 'nt', 'Windows file identity is case-insensitive')
    def test_posix_mixed_case_filename_is_not_a_companion(self):
        files = ['/gallery/A.CR2', '/gallery/a.JPG', '/gallery/B.CR2',
                 '/gallery/B.JPG']
        self.assertEqual(collapse_raw_jpg_pairs(files, 'raw'),
                         ([files[0], files[1], files[2]], 1))

    @unittest.skipIf(os.name == 'nt', 'Windows file identity is case-insensitive')
    def test_posix_mixed_case_directory_is_not_a_companion(self):
        files = ['/gallery/A/P1.CR2', '/gallery/a/P1.JPG']
        self.assertEqual(collapse_raw_jpg_pairs(files, 'jpg'), (files, 0))



if __name__ == '__main__':
    unittest.main()
