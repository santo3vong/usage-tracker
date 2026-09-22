import os
import tempfile
import unittest

import server


class ModelCatalogTests(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self._orig_accounts = getattr(server, 'ACCOUNTS_FILE', None)
        self._orig_real = getattr(server, 'REAL_QUOTAS_FILE', None)
        self._orig_obs = getattr(server, 'QUOTA_OBSERVATIONS_FILE', None)
        self._orig_codex = getattr(server, 'CODEX_USAGE_FILE', None)
        self._orig_codex_cache = getattr(server, 'CODEX_MODELS_CACHE_FILE', None)
        self._orig_ts = getattr(server, 'TIME_SERIES_FILE', None)
        self._orig_vscdb = getattr(server, 'VSCDB_PATH', None)

        server.ACCOUNTS_FILE = os.path.join(self._temp_dir.name, 'accounts.json')
        server.REAL_QUOTAS_FILE = os.path.join(self._temp_dir.name, 'real_quotas.json')
        server.QUOTA_OBSERVATIONS_FILE = os.path.join(self._temp_dir.name, 'quota_observations.json')
        server.CODEX_USAGE_FILE = os.path.join(self._temp_dir.name, 'codex_usage.json')
        server.CODEX_MODELS_CACHE_FILE = os.path.join(self._temp_dir.name, 'codex_models_cache.json')
        server.TIME_SERIES_FILE = os.path.join(self._temp_dir.name, 'time_series_history.json')
        server.VSCDB_PATH = os.path.join(self._temp_dir.name, 'nonexistent.vscdb')

    def tearDown(self):
        server.ACCOUNTS_FILE = self._orig_accounts
        server.REAL_QUOTAS_FILE = self._orig_real
        server.QUOTA_OBSERVATIONS_FILE = self._orig_obs
        server.CODEX_USAGE_FILE = self._orig_codex
        server.CODEX_MODELS_CACHE_FILE = self._orig_codex_cache
        server.TIME_SERIES_FILE = self._orig_ts
        server.VSCDB_PATH = self._orig_vscdb
        self._temp_dir.cleanup()

    def test_gpt56_registers_all_reasoning_variants(self):
        expected = {
            'sol': ('none', 'low', 'standard', 'high', 'xhigh', 'max', 'ultra'),
            'terra': ('none', 'low', 'standard', 'high', 'xhigh', 'max', 'ultra'),
            'luna': ('none', 'low', 'standard', 'high', 'xhigh', 'max'),
        }
        for family, labels in expected.items():
            for label in labels:
                self.assertIn(f'5.6 {family} {label}', server.BENCHMARK_DATABASE)

    def test_current_codex_picker_choices_are_registered_even_without_aa_scores(self):
        selectable = {
            key for key, value in server.BENCHMARK_DATABASE.items()
            if value.get('selectable_in_codex')
        }
        self.assertEqual(len(selectable), 27)
        for name in ('gpt-6-astra ultra', '5.6 sol ultra', '5.6 terra ultra'):
            self.assertIn(name, selectable)
            model = server.get_benchmark_for_model(name)
            self.assertEqual(model['reasoning_effort'], 'ultra')
            self.assertEqual(model['benchmark_source'], 'unbenchmarked')
            self.assertIsNone(model['intelligence_index'])
            self.assertEqual(model['availability_source'], 'Codex desktop runtime')
            self.assertEqual(model['availability_as_of'], '2026-09-21')

        self.assertNotIn('5.6 luna ultra', selectable)

    def test_sol_uses_official_price_and_context(self):
        model = server.get_benchmark_for_model('gpt-5.6-sol xhigh')
        self.assertEqual(model['model_id'], 'gpt-5.6-sol')
        self.assertEqual(model['reasoning_effort'], 'xhigh')
        self.assertEqual(model['price_in_1m'], 4.0)
        self.assertEqual(model['price_cached_in_1m'], 0.4)
        self.assertEqual(model['price_out_1m'], 20.0)
        self.assertEqual(model['context_window'], '1.05M tokens')
        self.assertEqual(model['max_input_tokens'], 922_000)
        self.assertEqual(model['max_output_tokens'], 128_000)

    def test_estimate_model_cost_cached_and_cache_write(self):
        # 1M input (no cache), 1M output on Sol
        cost = server.estimate_model_cost('gpt-5.6-sol', input_tokens=1_000_000, output_tokens=1_000_000)
        self.assertAlmostEqual(cost, 24.0, places=4)

        # 1M input with 500k cached, 200k cache-write, 300k uncached, 100k output on Sol:
        # uncached: 0.3 * 4.0 = 1.20
        # cached: 0.5 * 0.4 = 0.20
        # cache write: 0.2 * (4.0 * 1.25) = 1.00
        # output: 0.1 * 20.0 = 2.00
        # total = 4.40
        cost_cached = server.estimate_model_cost(
            'gpt-5.6-sol',
            input_tokens=1_000_000,
            output_tokens=100_000,
            cached_input_tokens=500_000,
            cache_write_input_tokens=200_000
        )
        self.assertAlmostEqual(cost_cached, 4.40, places=4)

    def test_terra_and_luna_use_current_aa_v43_estimates(self):
        terra = server.get_benchmark_for_model('gpt-5.6-terra high')
        luna = server.get_benchmark_for_model('gpt-5.6-luna max')
        self.assertEqual((terra['price_in_1m'], terra['price_out_1m']), (2.0, 12.0))
        self.assertEqual((luna['price_in_1m'], luna['price_out_1m']), (0.2, 1.2))
        self.assertEqual(terra['intelligence_index'], 34.0)
        self.assertEqual(terra['speed_tps'], 91.0)
        self.assertEqual(luna['intelligence_index'], 37.0)
        self.assertEqual(luna['speed_tps'], 165.0)
        self.assertIsNone(luna['coding_score'])
        self.assertEqual(terra['benchmark_source'], 'Artificial Analysis')
        self.assertEqual(luna['benchmark_source'], 'Artificial Analysis')
        self.assertEqual(terra['benchmark_status'], 'estimate')
        self.assertEqual(luna['benchmark_status'], 'estimate')

    def test_standard_maps_to_openai_default_medium_effort(self):
        terra = server.get_benchmark_for_model('gpt-5.6-terra')
        luna = server.get_benchmark_for_model('5.6 luna medium')
        self.assertEqual(terra['reasoning_effort'], 'medium')
        self.assertEqual(luna['reasoning_effort'], 'medium')

    def test_gpt56_alias_routes_to_sol_standard(self):
        model = server.get_benchmark_for_model('gpt-5.6')
        self.assertEqual(model['model_id'], 'gpt-5.6-sol')
        self.assertEqual(model['reasoning_effort'], 'medium')

    def test_astra_uses_official_cached_pricing_for_observed_variants(self):
        bare = server.get_benchmark_for_model('gpt-6-astra')
        high = server.get_benchmark_for_model('gpt-6-astra high')
        for model in (bare, high):
            self.assertEqual(model['model_id'], 'gpt-6-astra')
            self.assertEqual(model['price_in_1m'], 10.0)
            self.assertEqual(model['price_cached_in_1m'], 1.0)
            self.assertEqual(model['price_out_1m'], 50.0)
            self.assertEqual(model['pricing_source'], 'official')

        cost = server.estimate_model_cost(
            'gpt-6-astra high',
            input_tokens=1_000_000,
            cached_input_tokens=600_000,
            output_tokens=100_000,
        )
        self.assertAlmostEqual(cost, 9.6, places=6)

    def test_gemini_36_flash_high_uses_current_official_pricing(self):
        model = server.get_benchmark_for_model('Gemini 3.6 Flash (High)')
        self.assertEqual(model['model_id'], 'gemini-3.6-flash')
        self.assertEqual(model['price_in_1m'], 0.75)
        self.assertEqual(model['price_cached_in_1m'], 0.075)
        self.assertEqual(model['price_out_1m'], 3.75)
        self.assertEqual(model['pricing_valid_through'], '2026-12-31')
        self.assertEqual(model['scheduled_price_in_1m_from_2027'], 1.5)

        cost = server.estimate_model_cost(
            'Gemini 3.6 Flash (High)',
            input_tokens=1_000_000,
            cached_input_tokens=400_000,
            output_tokens=100_000,
        )
        self.assertAlmostEqual(cost, 0.855, places=6)

    def test_gemini_38_flash_uses_current_official_pricing(self):
        low = server.get_benchmark_for_model('Gemini 3.8 Flash (Low)')
        medium = server.get_benchmark_for_model('Gemini 3.8 Flash')
        high = server.get_benchmark_for_model('Gemini 3.8 Flash (High)')
        for model in (low, medium, high):
            self.assertEqual(model['model_id'], 'gemini-3.8-flash')
            self.assertEqual(model['price_in_1m'], 0.75)
            self.assertEqual(model['price_cached_in_1m'], 0.075)
            self.assertEqual(model['price_out_1m'], 3.75)
            self.assertEqual(model['pricing_valid_through'], '2026-12-31')
            self.assertEqual(model['scheduled_price_in_1m_from_2027'], 1.5)
            self.assertEqual(model['max_output_tokens'], 64_000)
            self.assertEqual(model['pricing_source'], 'official')

        self.assertEqual(medium['reasoning_effort'], 'medium')
        self.assertEqual(high['reasoning_effort'], 'high')
        self.assertEqual(low['reasoning_effort'], 'low')
        self.assertEqual(low['intelligence_index'], 33.0)

    def test_gpt55_and_gpt54_observed_variants_use_official_pricing(self):
        gpt55 = server.get_benchmark_for_model('gpt-5.5 xhigh')
        self.assertEqual(
            (gpt55['price_in_1m'], gpt55['price_cached_in_1m'], gpt55['price_out_1m']),
            (5.0, 0.5, 30.0),
        )
        self.assertEqual(gpt55['reasoning_effort'], 'xhigh')

        gpt54 = server.get_benchmark_for_model('gpt-5.4 high')
        self.assertEqual(
            (gpt54['price_in_1m'], gpt54['price_cached_in_1m'], gpt54['price_out_1m']),
            (2.5, 0.25, 15.0),
        )
        self.assertEqual(gpt54['reasoning_effort'], 'high')

    def test_verified_artificial_analysis_scores_are_registered(self):
        expected = {
            'gpt-6-astra high': 51.0,
            'gpt-6-astra xhigh': 52.0,
            'gpt-6-astra max': 53.0,
            'gpt-5.5 xhigh': 39.0,
            'gpt-5.5 high': 37.0,
            'gpt-5.5 standard': 34.0,
            'gpt-5.4 xhigh': 39.0,
            'Gemini 3.6 Flash (High)': 34.0,
            '5.6 sol max': 47.0,
            '5.6 sol xhigh': 44.0,
            '5.6 sol high': 42.0,
            '5.6 sol standard': 39.0,
        }
        for name, score in expected.items():
            model = server.get_benchmark_for_model(name)
            self.assertEqual(model['intelligence_index'], score, name)
            self.assertEqual(model['benchmark_source'], 'Artificial Analysis', name)
            self.assertTrue(model['benchmark_source_url'].startswith('https://artificialanalysis.ai/'), name)
            self.assertEqual(model['benchmark_as_of'], '2026-09-21', name)
            self.assertEqual(model['benchmark_index_version'], '4.3.2', name)

    def test_aa_leaderboard_hides_local_heuristic_scores(self):
        heuristic = server.get_benchmark_for_model('Claude Opus 4.6 (Thinking)')
        self.assertEqual(heuristic.get('benchmark_source', 'local_heuristic'), 'local_heuristic')
        hidden = server.get_verified_aa_metrics(heuristic)
        self.assertIsNone(hidden['intelligence_index'])
        self.assertIsNone(hidden['coding_score'])
        self.assertIsNone(hidden['speed_tps'])

        verified = server.get_benchmark_for_model('gpt-6-astra high')
        visible = server.get_verified_aa_metrics(verified)
        self.assertEqual(visible['intelligence_index'], 51.0)
        self.assertEqual(visible['cost_per_task'], 1.73)

    def test_aa_ranking_uses_dense_ties_and_leaves_unverified_unranked(self):
        rows = [
            {'model_name': 'unverified', 'intelligence_index': None},
            {'model_name': 'astra-xhigh', 'intelligence_index': 53.0},
            {'model_name': 'astra-max', 'intelligence_index': 53.0},
            {'model_name': 'astra-high', 'intelligence_index': 51.0},
            {'model_name': 'sol-xhigh', 'intelligence_index': 44.0},
        ]
        server.sort_and_assign_aa_ranks(rows)
        ranks = {row['model_name']: row['rank'] for row in rows}
        self.assertEqual(ranks['astra-xhigh'], 1)
        self.assertEqual(ranks['astra-max'], 1)
        self.assertEqual(ranks['astra-high'], 2)
        self.assertEqual(ranks['sol-xhigh'], 3)
        self.assertIsNone(ranks['unverified'])

    def test_unknown_model_is_explicitly_unpriced(self):
        model = server.get_benchmark_for_model('my-private-custom-model')
        self.assertIsNone(model['price_in_1m'])
        self.assertIsNone(model['price_out_1m'])
        self.assertFalse(server.model_has_verified_pricing('my-private-custom-model'))
        self.assertIsNone(server.estimate_model_cost('my-private-custom-model', 100_000, 50_000))

    def test_model_breakdown_uses_same_calibrated_weekly_capacity_as_quota_summary(self):
        stats = {
            'Gemini 3.7 Flash (High)': {
                'total_tokens': 150_000,
                'weekly_tokens': 100_000,
                'five_hour_tokens': 20_000,
                'input_tokens': 100_000,
                'output_tokens': 50_000,
                'thinking_tokens': 10_000,
                'cost_usd': 0.01,
                'weekly_cost_usd': 0.005,
                'sessions_count': 2,
                'model_responses': 10,
                'tool_calls': 3,
                'duration_minutes': 12.0,
            }
        }
        quotas = {
            'weekly_window': {
                'gemini': {'limit_tokens': 2_000_000},
                'external': {'limit_tokens': 300_000},
            }
        }

        rows = server.build_models_breakdown(stats, [], quota_data=quotas)

        self.assertEqual(rows[0]['weekly_limit_tokens'], 2_000_000)
        self.assertEqual(rows[0]['weekly_quota_pct_used'], 5.0)

    def test_codex_overwrite_can_reset_metrics_to_zero(self):
        data = {
            'models': {
                'o3 high': {
                    'total_tokens': 1000,
                    'weekly_tokens': 500,
                    'input_tokens': 700,
                    'output_tokens': 300,
                    'thinking_tokens': 100,
                    'cost_usd': 1.25,
                    'weekly_cost_usd': 0.75,
                    'sessions': 3,
                    'responses': 10,
                    'tool_calls': 4,
                }
            }
        }
        result = server.apply_codex_usage_update(data, {
            'action': 'upsert_model',
            'mode': 'overwrite',
            'model_name': 'o3 high',
            'total_tokens': 0,
            'weekly_tokens': 0,
            'input_tokens': 0,
            'output_tokens': 0,
            'thinking_tokens': 0,
            'cost_usd': 0,
            'weekly_cost_usd': 0,
            'sessions': 0,
            'responses': 0,
            'tool_calls': 0,
        })
        model = result['models']['o3 high']
        self.assertEqual(model['total_tokens'], 0)
        self.assertEqual(model['weekly_tokens'], 0)
        self.assertEqual(model['cost_usd'], 0)
        self.assertEqual(model['responses'], 0)

    def test_codex_weekly_usage_is_independent_from_all_time(self):
        data = {
            'models': {
                '5.6 sol standard': {
                    'total_tokens': 1000,
                    'weekly_tokens': 300,
                    'input_tokens': 700,
                    'output_tokens': 300,
                    'thinking_tokens': 0,
                    'cost_usd': 0.01,
                    'weekly_cost_usd': 0.004,
                    'cost_known': True,
                    'weekly_cost_known': True,
                    'sessions': 1,
                    'responses': 2,
                    'tool_calls': 0,
                }
            }
        }
        result = server.apply_codex_usage_update(data, {
            'action': 'upsert_model',
            'mode': 'add',
            'model_name': '5.6 sol standard',
            'total_tokens': 500,
            'weekly_tokens': 100,
            'cost_usd': 0.005,
            'weekly_cost_usd': 0.001,
        })
        model = result['models']['5.6 sol standard']
        self.assertEqual(model['total_tokens'], 1500)
        self.assertEqual(model['weekly_tokens'], 400)
        self.assertEqual(model['cost_usd'], 0.015)
        self.assertEqual(model['weekly_cost_usd'], 0.005)

    def test_unknown_codex_cost_stays_explicitly_unknown(self):
        result = server.apply_codex_usage_update({'models': {}}, {
            'action': 'upsert_model',
            'mode': 'overwrite',
            'model_name': 'private-model',
            'total_tokens': 1000,
            'weekly_tokens': 400,
        })
        model = result['models']['private-model']
        self.assertFalse(model['cost_known'])
        self.assertFalse(model['weekly_cost_known'])

if __name__ == '__main__':
    unittest.main()
