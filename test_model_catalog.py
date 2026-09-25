import json
import copy
import os
import tempfile
import unittest
from datetime import datetime, timezone

import server


class ModelCatalogTests(unittest.TestCase):
    def test_sol_max_and_luna_xhigh_have_distinct_chart_colors(self):
        sol = server.get_model_color('gpt-6-sol max')
        luna = server.get_model_color('5.6 luna xhigh')
        channels = lambda color: tuple(int(color[index:index + 2], 16) for index in (1, 3, 5))
        self.assertGreater(sum(abs(a - b) for a, b in zip(channels(sol), channels(luna))), 250)

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
        self.assertEqual(len(selectable), 38)
        for name in ('gpt-6-astra ultra', '5.6 sol ultra', '5.6 terra ultra'):
            self.assertIn(name, selectable)
            model = server.get_benchmark_for_model(name)
            self.assertEqual(model['reasoning_effort'], 'ultra')
            self.assertEqual(model['benchmark_source'], 'unbenchmarked')
            self.assertIsNone(model['intelligence_index'])
            self.assertIn(model['availability_source'], (
                'Codex desktop runtime', 'Codex local model cache',
            ))
            if model['availability_source'] == 'Codex desktop runtime':
                self.assertEqual(model['availability_as_of'], '2026-09-23')
            else:
                self.assertRegex(str(model['availability_as_of']), r'^\d{4}-\d{2}-\d{2}$')

        self.assertNotIn('5.6 luna ultra', selectable)
        for name in ('gpt-6-sol high', 'gpt-6-sol ultra', 'gpt-6-luna max'):
            self.assertIn(name, selectable)
        self.assertEqual(server.get_benchmark_for_model('gpt-6-sol high')['intelligence_index'], 43.0)
        self.assertIsNone(server.get_benchmark_for_model('gpt-6-sol ultra')['intelligence_index'])
        self.assertEqual(server.get_benchmark_for_model('gpt-6-luna max')['intelligence_index'], 37.0)
        self.assertNotIn('gpt-6-luna ultra', selectable)

    def test_gpt6_generation_is_distinct_in_logs_and_pricing(self):
        self.assertEqual(
            server.normalize_codex_model_effort('gpt-6-sol', 'high')[:2],
            ('gpt-6-sol high', 'gpt-6-sol'),
        )
        self.assertEqual(
            server.normalize_codex_model_effort('gpt-6-luna', 'xhigh')[:2],
            ('gpt-6-luna xhigh', 'gpt-6-luna'),
        )
        self.assertEqual(
            server.normalize_codex_model_effort('gpt-5.6-sol', 'high')[:2],
            ('5.6 sol high', 'gpt-5.6-sol'),
        )
        self.assertEqual(server.get_benchmark_for_model('gpt-6-sol')['price_in_1m'], 2.0)
        self.assertEqual(server.get_benchmark_for_model('gpt-6-luna high')['price_out_1m'], 0.5)
        self.assertEqual(server.get_benchmark_for_model('gpt-6-sol high')['codex_credit_in_1m'], 50.0)
        self.assertEqual(server.get_benchmark_for_model('gpt-6-sol high')['codex_credit_out_1m'], 250.0)
        self.assertEqual(server.get_benchmark_for_model('gpt-6-luna high')['codex_credit_in_1m'], 2.5)
        self.assertEqual(server.get_benchmark_for_model('gpt-6-luna high')['codex_credit_out_1m'], 12.5)
        self.assertAlmostEqual(server.estimate_model_cost('gpt-6-sol high', 1_000_000, 1_000_000), 12.0)
        self.assertAlmostEqual(server.estimate_model_cost('gpt-6-luna high', 1_000_000, 1_000_000), 0.6)

    def test_new_picker_model_appears_from_local_cache_without_invented_price(self):
        original = copy.deepcopy(server.BENCHMARK_DATABASE)
        cache_path = os.path.join(self._temp_dir.name, 'runtime-models.json')
        with open(cache_path, 'w', encoding='utf-8') as handle:
            json.dump({'fetched_at': '2026-09-23T01:00:00Z', 'models': [
                {'slug': 'gpt-7-sol', 'display_name': 'GPT-7-Sol',
                 'visibility': 'list', 'supported_reasoning_levels': [
                     {'effort': 'medium'}, {'effort': 'high'}]},
                {'slug': 'gpt-reserve', 'visibility': 'hide',
                 'supported_reasoning_levels': [{'effort': 'medium'}]},
                {'slug': 'chatgpt-web/medium', 'display_name': 'ChatGPT Web — Medium',
                 'visibility': 'list', 'supported_reasoning_levels': [{'effort': 'medium'}]},
            ]}, handle)
        try:
            self.assertTrue(server.refresh_codex_runtime_models(cache_path))
            new = server.BENCHMARK_DATABASE['gpt-7-sol high']
            self.assertTrue(new['selectable_in_codex'])
            self.assertEqual(new['availability_source'], 'Codex local model cache')
            self.assertEqual(new['availability_as_of'], '2026-09-23')
            self.assertIsNone(new['price_in_1m'])
            self.assertIsNone(new.get('codex_credit_in_1m'))
            self.assertFalse(server.BENCHMARK_DATABASE['gpt-6-sol high']['selectable_in_codex'])
            self.assertNotIn('gpt-reserve standard', server.BENCHMARK_DATABASE)
            self.assertTrue(server.BENCHMARK_DATABASE['chatgpt-web/medium']['selectable_in_codex'])
            self.assertEqual(server.BENCHMARK_DATABASE['chatgpt-web/medium']['display_name'], 'ChatGPT Web — Medium')
        finally:
            server.BENCHMARK_DATABASE.clear()
            server.BENCHMARK_DATABASE.update(original)

    def test_legacy_cache_rebuilds_gpt6_usage_instead_of_retaining_gpt56_attribution(self):
        sessions = os.path.join(self._temp_dir.name, 'sessions')
        cache_path = os.path.join(self._temp_dir.name, 'models-cache.json')
        os.makedirs(sessions)
        log_path = os.path.join(sessions, 'gpt6.jsonl')
        with open(log_path, 'w', encoding='utf-8') as handle:
            handle.write(json.dumps({'type': 'turn_context', 'payload': {
                'model': 'gpt-6-sol', 'effort': 'high',
            }}) + '\n')
            handle.write(json.dumps({'type': 'event_msg',
                                     'timestamp': datetime.now(timezone.utc).isoformat(),
                                     'payload': {'type': 'token_count', 'info': {
                                         'last_token_usage': {
                                             'input_tokens': 80, 'output_tokens': 20,
                                             'total_tokens': 100,
                                         },
                                     }}}) + '\n')

        first = server.scan_codex_model_usage(sessions_dir=sessions, cache_file=cache_path)
        self.assertEqual(first['models']['gpt-6-sol high']['total_tokens'], 100)
        with open(cache_path, 'r', encoding='utf-8') as handle:
            old_cache = json.load(handle)
        old_cache['version'] = 4
        entry = next(iter(old_cache['files'].values()))
        entry.pop('attribution_version')
        entry['all_time']['5.6 sol high'] = entry['all_time'].pop('gpt-6-sol high')
        with open(cache_path, 'w', encoding='utf-8') as handle:
            json.dump(old_cache, handle)

        rebuilt = server.scan_codex_model_usage(sessions_dir=sessions, cache_file=cache_path)
        self.assertEqual(rebuilt['models']['gpt-6-sol high']['total_tokens'], 100)
        self.assertNotIn('5.6 sol high', rebuilt['models'])
        self.assertEqual(rebuilt['diagnostics']['files_rebuilt'], 1)

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
            'gpt-6-sol max': 48.0,
            'gpt-6-sol high': 43.0,
            'gpt-6-luna max': 37.0,
            'gpt-6-luna high': 32.0,
        }
        for name, score in expected.items():
            model = server.get_benchmark_for_model(name)
            self.assertEqual(model['intelligence_index'], score, name)
            self.assertEqual(model['benchmark_source'], 'Artificial Analysis', name)
            self.assertTrue(model['benchmark_source_url'].startswith('https://artificialanalysis.ai/'), name)
            self.assertEqual(model['benchmark_as_of'],
                             '2026-09-23' if name.startswith('gpt-6-sol') or name.startswith('gpt-6-luna') else '2026-09-21', name)
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

    def test_aa_value_ratio_preserves_low_cost_models(self):
        self.assertEqual(server.aa_value_score(21, 0.0045), 4666.7)
        self.assertEqual(server.aa_value_score(43, 0.37), 116.2)
        self.assertIsNone(server.aa_value_score(21, 0))

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

    def test_codex_fast_keeps_base_model_quality_but_uses_credit_weighted_cost(self):
        standard = '5.6 sol xhigh'
        fast = '5.6 sol xhigh (fast)'
        self.assertEqual(server.get_benchmark_for_model(fast), server.get_benchmark_for_model(standard))
        self.assertEqual(
            server.normalize_codex_model_effort('gpt-5.6-sol', 'xhigh', 'priority')[0],
            fast,
        )
        self.assertAlmostEqual(
            server.estimate_model_cost(fast, input_tokens=1_000_000),
            2.5 * server.estimate_model_cost(standard, input_tokens=1_000_000),
        )
        self.assertEqual(
            server.get_pricing_provenance(fast)['pricing_source'],
            'chatgpt_fast_credit_equivalent',
        )
        self.assertFalse(server.get_pricing_provenance(fast)['pricing_verified'])
        self.assertAlmostEqual(
            server.estimate_model_cost('gpt-5.4 high (fast)', input_tokens=1_000_000),
            2.0 * server.estimate_model_cost('gpt-5.4 high', input_tokens=1_000_000),
        )

if __name__ == '__main__':
    unittest.main()
