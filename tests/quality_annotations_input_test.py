#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for malformed cached Rank metadata and weighted scores."""
import math
import unittest
from types import SimpleNamespace

from quality_annotations import annotate_quality


def make_score(meta=None):
    return SimpleNamespace(sharpness=70, exposure=70, noise=70,
                           dynamic_range=70, overall_score=75, meta=meta)


class QualityAnnotationInputTests(unittest.TestCase):
    def test_non_finite_weighted_scores_use_cached_overall(self):
        for value in (float('nan'), float('inf'), float('-inf')):
            with self.subTest(value=value):
                self.assertEqual(
                    annotate_quality(make_score(), weighted_score=value)['tier'],
                    annotate_quality(make_score())['tier'])

    def test_malformed_weighted_scores_do_not_crash(self):
        for value in ('invalid', object(), {}, 10**10000):
            with self.subTest(type=type(value).__name__):
                result = annotate_quality(make_score(), weighted_score=value)
                self.assertTrue(result['manual_only'])
                self.assertEqual(result['scene'], '未识别')

    def test_non_dictionary_meta_does_not_crash(self):
        for meta in ('bad', [], 42, object()):
            with self.subTest(type=type(meta).__name__):
                result = annotate_quality(make_score(meta=meta))
                self.assertEqual(result['scene'], '未识别')

    def test_confirmed_face_hint_remains_manual_only(self):
        result = annotate_quality(make_score(meta={'scene_hint': {'label': '人像候选'}}))
        self.assertEqual(result['scene'], '人像候选')
        self.assertTrue(result['manual_only'])


if __name__ == '__main__':
    unittest.main()
