#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Same-stem RAW+JPEG review must never hide unrelated image formats."""
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
        self.assertEqual(collapse_raw_jpg_pairs(files,'raw'),([files[0],files[2]],1))


if __name__ == '__main__':
    unittest.main()
