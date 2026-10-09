#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch
import numpy as np
import scene_labels
from quality_annotations import annotate_quality
from types import SimpleNamespace


class FakeFaceDetector:
    def detectMultiScale(self, gray, **kwargs):
        if max(gray.shape) > 240:
            raise AssertionError('scene detector exceeded bounded thumbnail size')
        return [(3,4,40,40)]


class SceneEvidenceTests(unittest.TestCase):
    def test_real_pixel_based_face_evidence(self):
        with patch.object(scene_labels, '_face_detector', return_value=FakeFaceDetector()):
            hint=scene_labels.content_scene_hint(np.ones((1600,1200), dtype=np.uint8)*127)
        self.assertEqual(hint['label'], '人像候选')
        self.assertEqual(hint['face_count'], 1)

    def test_classifier_missing_is_not_falsely_landscape(self):
        with patch.object(scene_labels, '_face_detector', return_value=None):
            hint=scene_labels.content_scene_hint(np.ones((100,150),dtype=np.uint8))
        self.assertEqual(hint['label'], '未识别')

    def test_tiny_image_does_not_force_a_scene(self):
        with patch.object(scene_labels, '_face_detector', return_value=FakeFaceDetector()):
            hint=scene_labels.content_scene_hint(np.ones((20,40),dtype=np.uint8))
        self.assertEqual(hint['label'], '未识别')

    def test_scene_hint_cannot_change_manual_only_status(self):
        score=SimpleNamespace(overall_score=80,sharpness=90,exposure=85,
                              noise=75,dynamic_range=90,
                              meta={'scene_hint': {'label':'人像候选','face_count':1}})
        review=annotate_quality(score)
        self.assertEqual(review['scene'], '人像候选')
        self.assertTrue(review['manual_only'])
        self.assertIn('画面中检测到人脸（可能漏检）',review['tags'])


if __name__ == '__main__':
    unittest.main()
