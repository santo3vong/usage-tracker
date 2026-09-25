import copy
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

import aa_sync
import server


def aa_model(name, slug='gpt-6-sol-xhigh', score=44):
    return {
        'name': name,
        'slug': slug,
        'model_creator': {'name': 'OpenAI'},
        'evaluations': {
            'artificial_analysis_intelligence_index': score,
            'artificial_analysis_coding_index': 39.5,
        },
        'artificial_analysis_intelligence_index_cost': {
            'cost_per_task': {'total_cost': 0.53},
        },
        'performance': {
            'median_output_tokens_per_second': 128,
            'median_time_to_first_token_seconds': 1.1,
        },
    }


class ArtificialAnalysisSyncTests(unittest.TestCase):
    def test_only_exact_verified_variants_are_mapped(self):
        self.assertEqual(aa_sync.canonical_model_key(aa_model('GPT-6 Sol (xhigh)')),
                         'gpt-6-sol xhigh')
        self.assertEqual(aa_sync.canonical_model_key(aa_model('GPT-6 Luna (medium)')),
                         'gpt-6-luna standard')
        self.assertEqual(aa_sync.canonical_model_key(aa_model('GPT-6 Sol (Non-reasoning)')),
                         'gpt-6-sol none')
        wrong_creator = aa_model('GPT-6 Sol (xhigh)')
        wrong_creator['model_creator']['name'] = 'Other'
        self.assertIsNone(aa_sync.canonical_model_key(wrong_creator))
        self.assertIsNone(aa_sync.canonical_model_key(aa_model('GPT-6 Sol')))

    def test_free_api_pages_are_combined_without_exposing_key(self):
        pages = [
            {'intelligence_index_version': 4.3,
             'pagination': {'page': 1, 'has_more': True},
             'data': [aa_model('GPT-6 Sol (xhigh)')]},
            {'intelligence_index_version': 4.3,
             'pagination': {'page': 2, 'has_more': False},
             'data': [aa_model('GPT-6 Luna (medium)', 'gpt-6-luna-medium', 29)]},
        ]
        requests = []

        def opener(request, timeout):
            requests.append((request.full_url, request.get_header('X-api-key'), timeout))
            return io.BytesIO(json.dumps(pages[len(requests) - 1]).encode())

        snapshot = aa_sync.fetch_free_models('local-secret', opener=opener)
        self.assertEqual(set(snapshot['models']), {'gpt-6-sol xhigh', 'gpt-6-luna standard'})
        self.assertEqual(snapshot['models']['gpt-6-sol xhigh']['cost_per_task'], 0.53)
        self.assertEqual(snapshot['index_version'], '4.3')
        self.assertEqual([url.rsplit('=', 1)[-1] for url, _, _ in requests], ['1', '2'])
        self.assertTrue(all(header == 'local-secret' for _, header, _ in requests))
        self.assertNotIn('local-secret', json.dumps(snapshot))

    def test_bad_page_does_not_return_a_partial_snapshot(self):
        bad = {'pagination': {'page': 2, 'has_more': False},
               'data': [aa_model('GPT-6 Sol (xhigh)')]}
        with self.assertRaises(ValueError):
            aa_sync.fetch_free_models('key', opener=lambda *_args, **_kwargs:
                                      io.BytesIO(json.dumps(bad).encode()))
        missing_has_more = {'pagination': {'page': 1},
                            'data': [aa_model('GPT-6 Sol (xhigh)')]}
        with self.assertRaises(ValueError):
            aa_sync.fetch_free_models('key', opener=lambda *_args, **_kwargs:
                                      io.BytesIO(json.dumps(missing_has_more).encode()))

    def test_cached_api_data_updates_known_model_and_preserves_offline_fallback(self):
        key = 'gpt-6-sol xhigh'
        original = copy.deepcopy(server.BENCHMARK_DATABASE[key])
        old_cache, old_key = server.AA_BENCHMARK_CACHE_FILE, server.AA_API_KEY_FILE
        try:
            with tempfile.TemporaryDirectory() as folder:
                server.AA_BENCHMARK_CACHE_FILE = os.path.join(folder, 'cache.json')
                server.AA_API_KEY_FILE = os.path.join(folder, 'missing-key.txt')
                with mock.patch.dict(os.environ, {'ARTIFICIAL_ANALYSIS_API_KEY': ''}):
                    status = server.prepare_aa_benchmarks()
                    self.assertEqual(status['state'], 'needs_key')
                    self.assertEqual(server.BENCHMARK_DATABASE[key]['intelligence_index'], 44)

                snapshot = {
                    'fetched_at': datetime.now(timezone.utc).isoformat(),
                    'index_version': '4.3',
                    'models': {key: {**aa_sync.parse_free_model(aa_model('GPT-6 Sol (xhigh)', score=45))[1],
                                     'intelligence_index': 45}},
                }
                with open(server.AA_BENCHMARK_CACHE_FILE, 'w', encoding='utf-8') as target:
                    json.dump(snapshot, target)
                with mock.patch.dict(os.environ, {'ARTIFICIAL_ANALYSIS_API_KEY': 'local-secret'}):
                    status = server.prepare_aa_benchmarks()
                self.assertEqual(status['state'], 'current')
                self.assertEqual(status['cached_models'], 1)
                self.assertEqual(server.BENCHMARK_DATABASE[key]['intelligence_index'], 45)
                self.assertEqual(server.BENCHMARK_DATABASE[key]['benchmark_status'], 'published')
                self.assertEqual(server.BENCHMARK_DATABASE[key]['benchmark_index_version'], '4.3')
        finally:
            server.BENCHMARK_DATABASE[key] = original
            server.AA_BENCHMARK_CACHE_FILE = old_cache
            server.AA_API_KEY_FILE = old_key

    def test_background_refresh_commits_last_good_snapshot_atomically(self):
        old_cache = server.AA_BENCHMARK_CACHE_FILE
        old_error, old_retry = server.AA_LAST_ERROR, server.AA_RETRY_AFTER
        try:
            with tempfile.TemporaryDirectory() as folder:
                server.AA_BENCHMARK_CACHE_FILE = os.path.join(folder, 'cache.json')
                snapshot = {
                    'fetched_at': datetime.now(timezone.utc).isoformat(),
                    'index_version': '4.3',
                    'models': {'gpt-6-sol xhigh': aa_sync.parse_free_model(
                        aa_model('GPT-6 Sol (xhigh)'))[1]},
                }
                with mock.patch.object(server.aa_sync, 'fetch_free_models', return_value=snapshot) as fetch:
                    server._aa_refresh_worker('local-secret')
                fetch.assert_called_once_with('local-secret')
                with open(server.AA_BENCHMARK_CACHE_FILE, encoding='utf-8') as source:
                    self.assertEqual(json.load(source), snapshot)
                self.assertIsNone(server.AA_LAST_ERROR)
        finally:
            server.AA_BENCHMARK_CACHE_FILE = old_cache
            server.AA_LAST_ERROR, server.AA_RETRY_AFTER = old_error, old_retry


if __name__ == '__main__':
    unittest.main()
