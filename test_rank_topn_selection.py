#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from photo_curator import _top_ranked_available


class RankedWindowSelectionTests(unittest.TestCase):
    def fake(self, path, score):
        return SimpleNamespace(path=str(path), overall_score=score)

    def test_large_library_returns_highest_available_n(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'photo.jpg'
            path.write_bytes(b'photo')
            photos=[self.fake(path,score) for score in range(19000)]
            ranked=_top_ranked_available(photos, {'overall_score':1}, 50)
            self.assertEqual([p.overall_score for p in ranked],
                             list(range(18999,18949,-1)))

    def test_unavailable_best_scores_fallback_returns_correct_topn(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            good=root/'exists.jpg'
            good.write_bytes(b'photo')
            offline=root/'offline.jpg'
            items=[self.fake(offline,n) for n in range(100,40,-1)]
            items += [self.fake(good,n) for n in range(40,20,-1)]
            selected=_top_ranked_available(items, {'overall_score':1}, 7)
            self.assertEqual([x.overall_score for x in selected],
                             [40,39,38,37,36,35,34])

    def test_equal_score_keeps_input_order(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            photos=[]
            for i in range(10):
                path=root/f'{i}.jpg'
                path.write_bytes(b'p')
                photos.append(self.fake(path,50))
            result=_top_ranked_available(photos,{'overall_score':1},3)
            self.assertEqual([p.path for p in result],[p.path for p in photos[:3]])

    def test_empty_and_short_results(self):
        self.assertEqual(_top_ranked_available([],{'overall_score':1},50),[])
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'photo.jpg'
            p.write_bytes(b'p')
            self.assertEqual(len(_top_ranked_available(
                [self.fake(p,40)],{'overall_score':1},50)),1)


if __name__ == '__main__':
    unittest.main()
