#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V2 large-library review uses whole-result filters and bounded browser pages."""
import unittest
from unittest.mock import patch

import photo_curator as core


class CullPagedReviewTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            {'path': '/sample/1.jpg', 'tier': 'sharp', 'fmt': 'JPG',
             'raw': False, 'heic': False, 'move_selected': False},
            {'path': '/sample/2.jpg', 'tier': 'blurry', 'fmt': 'JPG',
             'raw': False, 'heic': False, 'move_selected': True},
            {'path': '/sample/3.arw', 'tier': 'soft', 'fmt': 'ARW',
             'raw': True, 'heic': False, 'move_selected': False},
            {'path': '/sample/4.heic', 'tier': 'blurry', 'fmt': 'HEIC',
             'raw': False, 'heic': True, 'move_selected': False},
            {'path': '/sample/5.jpg', 'tier': 'blurry', 'fmt': 'JPG',
             'raw': False, 'heic': False, 'move_selected': True,
             'lifecycle': 'trashed'},
            {'path': '/sample/6.nef', 'tier': 'sharp', 'fmt': 'NEF',
             'raw': True, 'heic': False, 'move_selected': True},
            {'path': '/sample/7.jpg', 'tier': 'soft', 'fmt': 'JPG',
             'raw': False, 'heic': False, 'move_selected': True,
             'lifecycle': 'pending_trash'},
        ]
        self.original = core.state['cull']
        core.state['cull'] = {**self.original, 'photos': self.rows}
        self.addCleanup(lambda: core.state.__setitem__('cull', self.original))
        self.client = core.app.test_client()

    def page(self, query):
        response = self.client.get('/api/results/cull', query_string=query)
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()

    def test_pending_counts_apply_to_full_review_not_first_page(self):
        result = self.page({'filter': 'pending', 'offset': 0, 'limit': 1})
        self.assertEqual([x['path'] for x in result['photos']], ['/sample/2.jpg'])
        self.assertEqual(result['total'], 2)
        self.assertEqual(result['stats'], {'move_selected': 2, 'markable': 5})
        self.assertFalse(result['done'])
        second = self.page({'filter': 'pending', 'offset': 1, 'limit': 1})
        self.assertEqual([x['path'] for x in second['photos']], ['/sample/6.nef'])
        self.assertTrue(second['done'])

    def test_blurry_and_raw_filter_combination(self):
        result = self.page({'filter': 'soft', 'ftype': 'raw'})
        self.assertEqual([x['path'] for x in result['photos']], ['/sample/3.arw'])
        result = self.page({'filter': 'all', 'ftype': 'ext:nef'})
        self.assertEqual([x['path'] for x in result['photos']], ['/sample/6.nef'])
        result = self.page({'filter': 'all', 'ftype': 'heic'})
        self.assertEqual([x['path'] for x in result['photos']], ['/sample/4.heic'])
        result = self.page({'filter': 'blurry', 'ftype': 'standard'})
        self.assertEqual([x['path'] for x in result['photos']], ['/sample/2.jpg'])

    def test_hidden_lifecycle_not_exposed_in_review_pages(self):
        result = self.page({'filter': 'all'})
        self.assertEqual(result['total'], 5)
        self.assertEqual(result['all_total'], 7)
        self.assertEqual(len(result['photos']), 5)
        result = self.page({'filter': 'blurry'})
        self.assertEqual(result['total'], 2)

    def test_reject_invalid_filters_and_ranges(self):
        for q in ({'filter': 'deleted'}, {'ftype': 'ext:../file'},
                  {'offset': '-1'}, {'limit': '0'}, {'limit': '1000000'}):
            response = self.client.get('/api/results/cull', query_string=q)
            self.assertEqual(response.status_code, 400, q)

    def test_big_library_page_size_and_deep_filter(self):
        synthetic = [{'path': f'/sample/{i}.jpg',
                      'tier': 'soft' if i%2 else 'sharp',
                      'fmt': 'JPG', 'raw': False, 'heic': False,
                      'move_selected': i>=18998} for i in range(19000)]
        core.state['cull']['photos'] = synthetic
        result = self.page({'filter': 'pending', 'limit': 2})
        self.assertEqual(result['total'], 2)
        self.assertEqual(result['stats']['markable'], 19000)
        self.assertEqual([p['path'] for p in result['photos']],
                         ['/sample/18998.jpg', '/sample/18999.jpg'])


if __name__ == '__main__':
    unittest.main()
