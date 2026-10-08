#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import unittest
from types import SimpleNamespace
from quality_annotations import annotate_quality


class QualitySuggestionTests(unittest.TestCase):
    def sample(self, **kwargs):
        d = dict(overall_score=85, sharpness=84, exposure=90, noise=88,
                 dynamic_range=88)
        d.update(kwargs)
        return SimpleNamespace(**d)

    def test_balanced_quality_is_only_a_candidate(self):
        r = annotate_quality(self.sample())
        self.assertEqual(r['tier'], '优质候选')
        self.assertTrue(r['manual_only'])
        self.assertEqual(r['scene'], '未识别')

    def test_multiple_technical_defects_require_manual_review(self):
        r = annotate_quality(self.sample(sharpness=12, exposure=10,
                                         overall_score=20))
        self.assertEqual(r['tier'], '建议重点复核')
        self.assertIn('疑似失焦', r['tags'])
        self.assertTrue(r['manual_only'])

    def test_single_low_key_exposure_is_not_called_waste(self):
        r = annotate_quality(self.sample(exposure=15))
        self.assertEqual(r['tier'], '一般候选')
        self.assertTrue(r['manual_only'])

    def test_reweighting_updates_review_tier_without_disk_action(self):
        hi = annotate_quality(self.sample(), weighted_score=85)
        low = annotate_quality(self.sample(), weighted_score=30)
        self.assertNotEqual(hi['tier'], low['tier'])
        self.assertTrue(low['manual_only'])

    def test_missing_and_non_finite_scores_cannot_claim_a_scene(self):
        r = annotate_quality(SimpleNamespace(sharpness=float('nan'), overall_score=52))
        self.assertEqual(r['scene'], '未识别')
        self.assertTrue(r['tags'])


if __name__ == '__main__':
    unittest.main()
