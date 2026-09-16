import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import server


def obs(ts, pct, tracked, cycle='cycle-a'):
    return {
        'timestamp': ts,
        'remaining_pct': pct,
        'tracked_used_tokens': tracked,
        'cycle_id': cycle,
        'pct_rounding_half_width': 0.5,
    }


class QuotaEstimatorTests(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self._orig_accounts = getattr(server, 'ACCOUNTS_FILE', None)
        self._orig_real = getattr(server, 'REAL_QUOTAS_FILE', None)
        self._orig_obs = getattr(server, 'QUOTA_OBSERVATIONS_FILE', None)
        self._orig_codex = getattr(server, 'CODEX_USAGE_FILE', None)
        self._orig_ts = getattr(server, 'TIME_SERIES_FILE', None)
        self._orig_vscdb = getattr(server, 'VSCDB_PATH', None)
        self._orig_worker_state = getattr(server, 'GEMINI_WORKER_STATE_DIR', None)

        server.ACCOUNTS_FILE = os.path.join(self._temp_dir.name, 'accounts.json')
        server.REAL_QUOTAS_FILE = os.path.join(self._temp_dir.name, 'real_quotas.json')
        server.QUOTA_OBSERVATIONS_FILE = os.path.join(self._temp_dir.name, 'quota_observations.json')
        server.CODEX_USAGE_FILE = os.path.join(self._temp_dir.name, 'codex_usage.json')
        server.TIME_SERIES_FILE = os.path.join(self._temp_dir.name, 'time_series_history.json')
        server.VSCDB_PATH = os.path.join(self._temp_dir.name, 'nonexistent.vscdb')
        server.GEMINI_WORKER_STATE_DIR = os.path.join(self._temp_dir.name, 'worker-state')

    def tearDown(self):
        server.ACCOUNTS_FILE = self._orig_accounts
        server.REAL_QUOTAS_FILE = self._orig_real
        server.QUOTA_OBSERVATIONS_FILE = self._orig_obs
        server.CODEX_USAGE_FILE = self._orig_codex
        server.TIME_SERIES_FILE = self._orig_ts
        server.VSCDB_PATH = self._orig_vscdb
        server.GEMINI_WORKER_STATE_DIR = self._orig_worker_state
        self._temp_dir.cleanup()

    def test_clean_observations_learn_two_million_capacity(self):
        observations = [
            obs('2026-08-20T00:00:00+00:00', 100, 0),
            obs('2026-08-20T00:20:00+00:00', 96, 80_000),
            obs('2026-08-20T00:40:00+00:00', 92, 160_000),
            obs('2026-08-20T01:00:00+00:00', 88, 240_000),
        ]

        result = server.estimate_quota_capacity(observations, 500_000, 5 * 60 * 60)

        self.assertEqual(result['method'], 'multi_observation_robust')
        self.assertAlmostEqual(result['capacity_tokens'], 2_000_000, delta=25_000)
        self.assertGreaterEqual(result['confidence'], 0.55)

    def test_outlier_does_not_dominate_capacity(self):
        observations = [
            obs('2026-08-20T00:00:00+00:00', 100, 0),
            obs('2026-08-20T00:20:00+00:00', 96, 80_000),
            obs('2026-08-20T00:40:00+00:00', 92, 160_000),
            obs('2026-08-20T01:00:00+00:00', 88, 240_000),
            obs('2026-08-20T01:20:00+00:00', 84, 320_000),
            obs('2026-08-20T01:40:00+00:00', 55, 360_000),
        ]

        result = server.estimate_quota_capacity(observations, 500_000, 5 * 60 * 60)

        self.assertAlmostEqual(result['capacity_tokens'], 2_000_000, delta=100_000)
        self.assertGreaterEqual(result['outlier_observations'], 1)

    def test_rolling_expiry_uses_net_usage_delta(self):
        observations = [
            obs('2026-08-20T00:00:00+00:00', 75.0, 500_000),
            obs('2026-08-20T00:20:00+00:00', 77.5, 450_000),
            obs('2026-08-20T00:40:00+00:00', 72.5, 550_000),
            obs('2026-08-20T01:00:00+00:00', 70.0, 600_000),
        ]

        result = server.estimate_quota_capacity(observations, 500_000, 5 * 60 * 60)

        self.assertAlmostEqual(result['capacity_tokens'], 2_000_000, delta=50_000)

    def test_integer_percentage_rounding_is_refined_across_many_observations(self):
        # True capacity is 2M. The underlying percentage moves by 0.75 points
        # per sample, but the IDE only exposes rounded integer percentages.
        # A pure median of pairwise ratios lands around 1.875M for this shape;
        # the within-cycle regression should remove most of that quantization
        # bias after the robust outlier stage has selected the observations.
        observations = [
            obs('2026-08-20T00:00:00+00:00', 90, 0),
            obs('2026-08-20T00:10:00+00:00', 90, 15_000),
            obs('2026-08-20T00:20:00+00:00', 89, 30_000),
            obs('2026-08-20T00:30:00+00:00', 88, 45_000),
            obs('2026-08-20T00:40:00+00:00', 87, 60_000),
            obs('2026-08-20T00:50:00+00:00', 87, 75_000),
            obs('2026-08-20T01:00:00+00:00', 86, 90_000),
            obs('2026-08-20T01:10:00+00:00', 85, 105_000),
            obs('2026-08-20T01:20:00+00:00', 84, 120_000),
        ]

        result = server.estimate_quota_capacity(observations, 500_000, 5 * 60 * 60)

        self.assertEqual(result['refinement_method'], 'within_cycle_least_squares')
        self.assertAlmostEqual(result['capacity_tokens'], 2_000_000, delta=60_000)

    def test_reset_boundary_does_not_create_cross_cycle_slope(self):
        observations = [
            obs('2026-08-20T00:00:00+00:00', 30, 0, 'cycle-a'),
            obs('2026-08-20T00:30:00+00:00', 25, 100_000, 'cycle-a'),
            obs('2026-08-20T01:00:00+00:00', 100, 0, 'cycle-b'),
            obs('2026-08-20T01:30:00+00:00', 95, 100_000, 'cycle-b'),
            obs('2026-08-20T02:00:00+00:00', 90, 200_000, 'cycle-b'),
        ]

        result = server.estimate_quota_capacity(observations, 500_000, 5 * 60 * 60)

        self.assertAlmostEqual(result['capacity_tokens'], 2_000_000, delta=50_000)
        self.assertEqual(result['latest_observation']['cycle_id'], 'cycle-b')
        self.assertEqual(result['latest_trusted_observation']['cycle_id'], 'cycle-b')
        self.assertEqual(result['latest_trusted_observation']['remaining_pct'], 90.0)

    def test_legacy_real_quota_file_migrates_to_one_account_only(self):
        old_real_path = server.REAL_QUOTAS_FILE
        old_accounts_path = server.ACCOUNTS_FILE
        try:
            with tempfile.TemporaryDirectory() as tmp:
                server.REAL_QUOTAS_FILE = os.path.join(tmp, 'real_quotas.json')
                server.ACCOUNTS_FILE = os.path.join(tmp, 'accounts.json')

                with open(server.ACCOUNTS_FILE, 'w', encoding='utf-8') as f:
                    json.dump({
                        'active_email': 'first@example.com',
                        'accounts': {
                            'first@example.com': {},
                            'second@example.com': {},
                        },
                    }, f)
                with open(server.REAL_QUOTAS_FILE, 'w', encoding='utf-8') as f:
                    json.dump({
                        'gemini_5h_pct': 77.0,
                        'gemini_weekly_pct': 66.0,
                        'updated_at': '2026-08-20T00:00:00+00:00',
                    }, f)

                first = server.load_real_quota_profile('first@example.com')
                second = server.load_real_quota_profile('second@example.com')
                with open(server.REAL_QUOTAS_FILE, 'r', encoding='utf-8') as f:
                    migrated = json.load(f)

                self.assertEqual(first['gemini_5h_pct'], 77.0)
                self.assertEqual(second, {})
                self.assertEqual(migrated['version'], 2)
                self.assertIn('first@example.com', migrated['accounts'])
                self.assertNotIn('second@example.com', migrated['accounts'])
        finally:
            server.REAL_QUOTAS_FILE = old_real_path
            server.ACCOUNTS_FILE = old_accounts_path

    def test_newest_manual_observation_overrides_older_trusted_as_live_anchor_without_changing_capacity(self):
        # Initial clean observations establish a robust learned capacity ~2M tokens.
        observations = [
            obs('2026-08-20T00:00:00+00:00', 100.0, 0),
            obs('2026-08-20T00:20:00+00:00', 96.0, 80_000),
            obs('2026-08-20T00:40:00+00:00', 92.0, 160_000),
            obs('2026-08-20T01:00:00+00:00', 88.0, 240_000),
        ]
        cal = server.estimate_quota_capacity(observations, 500_000, 5 * 60 * 60)
        self.assertAlmostEqual(cal['capacity_tokens'], 2_000_000, delta=25_000)
        self.assertEqual(cal['latest_trusted_observation']['remaining_pct'], 88.0)

        # User adds a new manual observation (e.g. 50.0% at 240,000 tokens)
        new_obs = obs('2026-08-20T01:30:00+00:00', 50.0, 240_000)
        observations_with_new = observations + [new_obs]
        cal_new = server.estimate_quota_capacity(observations_with_new, 500_000, 5 * 60 * 60)

        # Robust capacity remains unchanged (~2M), while latest_observation is at 50%
        self.assertAlmostEqual(cal_new['capacity_tokens'], 2_000_000, delta=50_000)
        self.assertEqual(cal_new['latest_observation']['remaining_pct'], 50.0)

        # predict_quota_from_anchor immediately anchors to the latest accepted manual reading (50%)
        prediction = server.predict_quota_from_anchor(
            tracked_used_tokens=240_000,
            capacity_tokens=cal_new['capacity_tokens'],
            calibration=cal_new
        )
        self.assertEqual(prediction['percentage_remaining'], 50.0)
        self.assertEqual(prediction['prediction_source'], 'observation_anchor_delta')
        self.assertEqual(prediction['remaining_tokens'], int(round(cal_new['capacity_tokens'] * 0.5)))

    def test_worker_reports_are_bounded_deduplicated_and_keep_exact_units_separate(self):
        root = server.GEMINI_WORKER_STATE_DIR
        os.makedirs(root, exist_ok=True)

        def write_report(folder, run_id, total, finished, backend='antigravity_cli'):
            target = os.path.join(root, folder)
            os.makedirs(target, exist_ok=True)
            with open(os.path.join(target, 'report.json'), 'w', encoding='utf-8') as f:
                json.dump({'metadata': {
                    'run_id': run_id, 'backend': backend, 'finished_at': finished,
                    'model': 'gemini-3.7-flash-high',
                    'worker_usage': {'total_tokens': total, 'input_tokens': total - 10,
                                     'output_tokens': 10, 'cache_read_tokens': 999999},
                }}, f)

        write_report('one', 'run-1', 1_000_000, '2026-08-24T12:00:00+00:00')
        write_report('duplicate', 'run-1', 1_000_000, '2026-08-24T12:01:00+00:00')
        write_report('two', 'run-2', 500_000, '2026-08-24T10:00:00+00:00')
        write_report('ignored-parent', 'batch-1', 9_000_000, '2026-08-24T12:00:00+00:00', 'batch')
        os.makedirs(os.path.join(root, 'malformed'), exist_ok=True)
        with open(os.path.join(root, 'malformed', 'report.json'), 'w', encoding='utf-8') as f:
            f.write('{bad json')

        result = server.scan_gemini_worker_usage(
            now=datetime(2026, 8, 24, 13, 0, tzinfo=timezone.utc))
        self.assertTrue(result['available'])
        self.assertEqual(result['five_hour_tokens'], 1_500_000)
        self.assertEqual(result['weekly_tokens'], 1_500_000)
        self.assertEqual(result['five_hour_runs'], 2)
        self.assertEqual(result['diagnostics']['duplicates'], 1)
        self.assertEqual(result['estimator_id'], server.GEMINI_WORKER_ESTIMATOR_ID)

        bounded = server.scan_gemini_worker_usage(
            now=datetime(2026, 8, 24, 13, 0, tzinfo=timezone.utc), max_files=1)
        self.assertTrue(bounded['diagnostics']['truncated'])

    def test_worker_report_numeric_schema_rejects_string_float_and_negative_tokens(self):
        root = server.GEMINI_WORKER_STATE_DIR
        os.makedirs(root, exist_ok=True)
        values = (100, '100', 100.5, -1)
        for index, total in enumerate(values):
            target = os.path.join(root, str(index))
            os.makedirs(target, exist_ok=True)
            with open(os.path.join(target, 'report.json'), 'w', encoding='utf-8') as f:
                json.dump({'metadata': {
                    'run_id': f'run-{index}', 'backend': 'antigravity_cli',
                    'finished_at': '2026-08-24T12:00:00+00:00',
                    'worker_usage': {'total_tokens': total, 'input_tokens': 90,
                                     'output_tokens': 10, 'cache_read_tokens': 0},
                }}, f)
        result = server.scan_gemini_worker_usage(
            now=datetime(2026, 8, 24, 13, 0, tzinfo=timezone.utc))
        self.assertEqual(result['five_hour_tokens'], 100)
        self.assertEqual(result['five_hour_runs'], 1)
        self.assertEqual(result['diagnostics']['malformed'], 3)

    def test_worker_calibration_reproduces_official_56_to_38_without_mixing_units(self):
        anchor = obs('2026-08-24T05:14:05+00:00', 56.0, 27_915)
        anchor.update({'worker_used_tokens': 6_933_055,
                       'worker_estimator_id': server.GEMINI_WORKER_ESTIMATOR_ID})
        current = obs('2026-08-24T07:14:05+00:00', 38.0, 33_430)
        current.update({'worker_used_tokens': 11_972_632,
                        'worker_estimator_id': server.GEMINI_WORKER_ESTIMATOR_ID})
        transcript_capacity = 740_532

        worker_cal = server.estimate_worker_quota_capacity(
            [anchor, current], transcript_capacity, window_seconds=5 * 60 * 60)
        self.assertEqual(worker_cal['method'], 'worker_observation_pair')
        self.assertGreater(worker_cal['capacity_tokens'], 28_000_000)
        self.assertLess(worker_cal['capacity_tokens'], 31_000_000)

        transcript_cal = {'latest_observation': anchor, 'latest_trusted_observation': anchor}
        predicted = server.predict_quota_from_anchor(
            33_430, transcript_capacity, transcript_cal,
            worker_used_tokens=11_972_632, worker_calibration=worker_cal)
        self.assertEqual(predicted['percentage_remaining'], 38.0)
        self.assertTrue(predicted['worker_prediction_applied'])
        self.assertEqual(predicted['tracked_used_tokens'], 33_430)
        self.assertEqual(predicted['worker_used_tokens'], 11_972_632)

    def test_calculate_quotas_keeps_transcript_capacity_clean_and_applies_worker_delta(self):
        anchor = obs('2026-08-24T05:14:05+00:00', 56.0, 27_915)
        anchor.update({'worker_used_tokens': 6_933_055,
                       'worker_estimator_id': server.GEMINI_WORKER_ESTIMATOR_ID})
        current = obs('2026-08-24T07:14:05+00:00', 38.0, 33_430)
        current.update({'worker_used_tokens': 11_972_632,
                        'worker_estimator_id': server.GEMINI_WORKER_ESTIMATOR_ID})
        with open(server.ACCOUNTS_FILE, 'w', encoding='utf-8') as f:
            json.dump({'active_email': 'user@example.com', 'accounts': {
                'user@example.com': {'limits': {'gemini_5h_tokens': 740_532}}}}, f)
        with open(server.QUOTA_OBSERVATIONS_FILE, 'w', encoding='utf-8') as f:
            json.dump({'version': 1, 'accounts': {'user@example.com': {
                'gemini_5h': [anchor, current]}}}, f)

        rolling = {
            'gem_5h_used': 33_430, 'gem_5h_reqs': 0,
            'ext_5h_used': 0, 'ext_5h_reqs': 0,
            'gem_wk_used': 0, 'gem_wk_reqs': 0,
            'ext_wk_used': 0, 'ext_wk_reqs': 0,
            'gem_worker_5h_used': 11_972_632, 'gem_worker_5h_runs': 4,
            'gem_worker_wk_used': 11_972_632, 'gem_worker_wk_runs': 4,
            'gem_worker_events': [],
        }
        quotas = server.calculate_quotas(
            rolling, {'gemini_5h_tokens': 740_532}, 'user@example.com')
        gemini = quotas['five_hour_window']['gemini']
        self.assertEqual(gemini['limit_tokens'], 740_532)
        self.assertEqual(gemini['percentage_remaining'], 38.0)
        self.assertTrue(gemini['worker_prediction_applied'])
        self.assertGreater(gemini['worker_calibration']['capacity_tokens'], 28_000_000)

    def test_multisource_pair_fit_converges_separate_capacities_and_mixed_display_series(self):
        base = datetime(2026, 8, 24, tzinfo=timezone.utc)
        movements = [(0, 0), (100_000, 3_000_000), (300_000, 4_500_000),
                     (500_000, 9_000_000), (800_000, 10_500_000)]
        observations = []
        for index, (transcript, worker) in enumerate(movements):
            pct = 100.0 - (100.0 * transcript / 2_000_000.0) - (100.0 * worker / 30_000_000.0)
            item = obs((base + timedelta(hours=index)).isoformat(), round(pct, 1), transcript)
            item.update({
                'worker_used_tokens': worker,
                'worker_estimator_id': server.GEMINI_WORKER_ESTIMATOR_ID,
                'token_estimator_id': server.TOKEN_ESTIMATOR_ID,
            })
            observations.append(item)

        calibration = server.estimate_quota_capacity(
            observations, 2_500_000, 5 * 60 * 60, worker_fallback_capacity=30_000_000)
        self.assertAlmostEqual(calibration['capacity_tokens'], 2_000_000, delta=100_000)
        self.assertAlmostEqual(calibration['worker_capacity_tokens'], 30_000_000, delta=1_500_000)
        self.assertTrue(calibration['identifiability']['rank_identifiable'])
        self.assertIsNotNone(calibration['mixed_capacity_tokens'])
        self.assertTrue(calibration['source_mix_dependent'])
        self.assertTrue(calibration['same_cycle_only'])

    def test_multisource_collinear_evidence_retains_priors_and_explains_reason(self):
        base = datetime(2026, 8, 24, tzinfo=timezone.utc)
        observations = []
        for index, (transcript, worker, pct) in enumerate(
                [(0, 0, 100.0), (100_000, 1_000_000, 94.0), (200_000, 2_000_000, 88.0)]):
            item = obs((base + timedelta(hours=index)).isoformat(), pct, transcript)
            item.update({'worker_used_tokens': worker,
                         'worker_estimator_id': server.GEMINI_WORKER_ESTIMATOR_ID,
                         'token_estimator_id': server.TOKEN_ESTIMATOR_ID})
            observations.append(item)
        calibration = server.estimate_quota_capacity(
            observations, 2_000_000, 5 * 60 * 60, worker_fallback_capacity=30_000_000)
        self.assertFalse(calibration['identifiability']['rank_identifiable'])
        self.assertEqual(calibration['capacity_tokens'], 2_000_000)
        self.assertEqual(calibration['worker_capacity_tokens'], 30_000_000)
        self.assertIn('source_movement_not_identifiable', calibration['retained_reasons'])

    def test_capacity_events_are_bounded_and_duplicate_state_is_not_repeated(self):
        store = {'version': 1, 'accounts': {}}
        calibration = {'capacity_tokens': 2_000_000, 'worker_capacity_tokens': 30_000_000,
                       'mixed_capacity_tokens': 12_000_000, 'confidence': 0.8,
                       'method': 'multi_source_robust', 'observations': 4,
                       'usable_pairs': 3, 'accepted': True,
                       'identifiability': {'transcript': True, 'worker': True}}
        worker = {'capacity_tokens': 30_000_000, 'confidence': 0.8,
                  'method': 'multi_source_robust', 'pair_count': 3, 'accepted': True}
        first = server.record_capacity_calibration_event(
            store, 'user@example.com', 'gemini_5h', calibration, worker,
            cycle_id='cycle-a', timestamp='2026-08-24T10:00:00+00:00')
        second = server.record_capacity_calibration_event(
            store, 'user@example.com', 'gemini_5h', calibration, worker,
            cycle_id='cycle-a', timestamp='2026-08-24T10:01:00+00:00')
        self.assertEqual(first, second)
        self.assertEqual(len(store['accounts']['user@example.com']['capacity_events']['gemini_5h']), 1)

    def test_duplicate_reading_does_not_fabricate_observation_history(self):
        store = {'version': 1, 'accounts': {}}
        first = server.record_quota_observation(
            store, 'user@example.com', 'gemini_5h', 90.0, 100_000,
            cycle_id='cycle-a', timestamp='2026-08-24T10:00:00+00:00',
            worker_used_tokens=2_000_000)
        second = server.record_quota_observation(
            store, 'user@example.com', 'gemini_5h', 90.0, 100_000,
            cycle_id='cycle-a', timestamp='2026-08-24T10:00:05+00:00',
            worker_used_tokens=2_000_000)
        history = store['accounts']['user@example.com']['gemini_5h']
        self.assertEqual(first, second)
        self.assertEqual(len(history), 1)

    def test_capacity_only_snapshot_change_is_not_debounced_and_api_has_both_windows(self):
        with open(server.ACCOUNTS_FILE, 'w', encoding='utf-8') as f:
            json.dump({'active_email': 'user@example.com', 'accounts': {
                'user@example.com': {}}}, f)
        store = {'version': 1, 'accounts': {}}
        for bucket, cap in (('gemini_5h', 2_000_000), ('gemini_weekly', 30_000_000)):
            server.record_capacity_calibration_event(
                store, 'user@example.com', bucket,
                {'capacity_tokens': cap, 'worker_capacity_tokens': cap * 10,
                 'mixed_capacity_tokens': cap * 5, 'confidence': 0.8,
                 'method': 'multi_source_robust', 'observations': 4,
                 'usable_pairs': 3, 'accepted': True},
                cycle_id='cycle-a', timestamp='2026-08-24T10:00:00+00:00')
        server.save_quota_observations(store)

        def quotas(capacity):
            cal = {'capacity_tokens': capacity, 'worker_capacity_tokens': 30_000_000,
                   'mixed_capacity_tokens': 12_000_000, 'method': 'multi_source_robust',
                   'confidence': 0.8}
            worker_cal = {'capacity_tokens': 30_000_000, 'method': 'multi_source_robust',
                          'confidence': 0.8}
            return {
                'five_hour_window': {
                    'gemini': {'used_tokens': 100, 'percentage_remaining': 99,
                               'limit_tokens': capacity, 'calibration': cal,
                               'worker_calibration': worker_cal},
                    'external': {'used_tokens': 0, 'percentage_remaining': 100,
                                 'limit_tokens': 60_000}},
                'weekly_window': {
                    'gemini': {'used_tokens': 100, 'percentage_remaining': 99,
                               'limit_tokens': 2_500_000, 'calibration': cal,
                               'worker_calibration': worker_cal},
                    'external': {'used_tokens': 0, 'percentage_remaining': 100,
                                 'limit_tokens': 250_000}},
            }

        server.record_time_series_snapshot(quotas(1_000_000), [])
        server.record_time_series_snapshot(quotas(1_100_000), [])
        with open(server.TIME_SERIES_FILE, encoding='utf-8') as f:
            snapshots = json.load(f)['snapshots']
        self.assertEqual(len(snapshots), 2)

        analytics = server.build_time_series_analytics([], quotas(1_100_000))
        evolution = analytics['capacity_evolution']
        self.assertEqual(len(evolution['five_hour_history']), 1)
        self.assertEqual(len(evolution['weekly_history']), 1)
        self.assertEqual(evolution['five_hour']['source'], 'calibration_history')
        self.assertEqual(evolution['five_hour']['transcript_capacity'], [2_000_000])
        self.assertEqual(evolution['weekly']['worker_capacity'], [300_000_000])
        self.assertEqual(evolution['five_hour']['mixed_capacity'], [10_000_000])

    def test_policy_shift_requires_two_consistent_new_capacity_points(self):
        def event(ts, cap, mixed):
            return {
                'timestamp': ts,
                'transcript_capacity_tokens': cap,
                'mixed_capacity_tokens': mixed,
                'confidence': 0.92,
                'evidence_count': 8,
                'pair_count': 5,
                'identifiability': {'transcript': True},
            }

        history = {'gemini_5h': [
            event('2026-09-01T00:00:00+00:00', 1_000_000, 8_000_000),
            event('2026-09-01T01:00:00+00:00', 1_010_000, 8_500_000),
            event('2026-09-01T02:00:00+00:00', 995_000, 9_000_000),
            event('2026-09-02T00:00:00+00:00', 1_500_000, 20_000_000),
        ]}

        self.assertEqual(server.detect_quota_policy_shifts(history), [])

    def test_policy_shift_confirms_repeated_fifty_percent_increase(self):
        def event(ts, cap):
            return {
                'timestamp': ts,
                'transcript_capacity_tokens': cap,
                'mixed_capacity_tokens': cap * 9,
                'confidence': 0.95,
                'evidence_count': 10,
                'pair_count': 6,
                'identifiability': {'transcript': True},
            }

        history = {'gemini_5h': [
            event('2026-09-01T00:00:00+00:00', 1_000_000),
            event('2026-09-01T01:00:00+00:00', 1_010_000),
            event('2026-09-01T02:00:00+00:00', 995_000),
            event('2026-09-02T00:00:00+00:00', 1_500_000),
            event('2026-09-02T01:00:00+00:00', 1_505_000),
        ]}

        shifts = server.detect_quota_policy_shifts(history)
        self.assertEqual(len(shifts), 1)
        self.assertEqual(shifts[0]['type'], 'INCREASE')
        self.assertAlmostEqual(shifts[0]['old_capacity'], 1_000_000, delta=15_000)
        self.assertAlmostEqual(shifts[0]['new_capacity'], 1_500_000, delta=15_000)
        self.assertGreaterEqual(float(shifts[0]['change_pct'].rstrip('%+')), 48.0)
        self.assertEqual(shifts[0]['source'], 'transcript_capacity_tokens')
        self.assertEqual(shifts[0]['confirmations'], 2)

    def test_policy_shift_ignores_large_mixed_source_change(self):
        events = []
        for index, mixed in enumerate((8_000_000, 9_000_000, 25_000_000, 28_000_000, 30_000_000)):
            events.append({
                'timestamp': f'2026-09-0{index + 1}T00:00:00+00:00',
                'transcript_capacity_tokens': 1_000_000 + (index % 2) * 5_000,
                'mixed_capacity_tokens': mixed,
                'confidence': 0.94,
                'evidence_count': 10 + index,
                'pair_count': 6 + index,
                'identifiability': {'transcript': True},
            })

        self.assertEqual(server.detect_quota_policy_shifts({'gemini_5h': events}), [])

    def test_recent_cycle_candidates_detect_shift_before_long_term_converges(self):
        def event(ts, cap, recent=None, evidence_id=None):
            item = {
                'timestamp': ts, 'transcript_capacity_tokens': cap,
                'confidence': 0.95, 'evidence_count': 10, 'pair_count': 6,
                'identifiability': {'transcript': True},
            }
            if recent is not None:
                item.update({
                    'recent_cycle_capacity_tokens': recent,
                    'recent_cycle_confidence': 0.92,
                    'recent_cycle_evidence_count': 5,
                    'recent_cycle_pair_count': 3,
                    'recent_cycle_id': 'cycle-new',
                    'recent_cycle_evidence_id': evidence_id,
                    'recent_cycle_accepted': True,
                })
            return item

        history = {'gemini_5h': [
            event('2026-09-01T00:00:00+00:00', 1_000_000),
            event('2026-09-01T01:00:00+00:00', 1_010_000),
            event('2026-09-01T02:00:00+00:00', 995_000),
            event('2026-09-02T00:00:00+00:00', 1_000_000, 1_500_000, 'r1'),
            event('2026-09-02T01:00:00+00:00', 1_000_000, 1_505_000, 'r2'),
        ]}
        shifts = server.detect_quota_policy_shifts(history)
        self.assertEqual(len(shifts), 1)
        self.assertEqual(shifts[0]['status'], 'DETECTED_EARLY_CONFIRMED')
        self.assertEqual(shifts[0]['source'], 'recent_cycle_capacity_tokens')
        self.assertAlmostEqual(shifts[0]['old_capacity'], 1_000_000, delta=15_000)
        self.assertAlmostEqual(shifts[0]['new_capacity'], 1_500_000, delta=15_000)

    def test_single_recent_cycle_candidate_does_not_alert(self):
        def event(ts, cap, recent=None, evidence_id=None):
            item = {
                'timestamp': ts, 'transcript_capacity_tokens': cap,
                'confidence': 0.95, 'evidence_count': 10, 'pair_count': 6,
                'identifiability': {'transcript': True},
            }
            if recent is not None:
                item.update({
                    'recent_cycle_capacity_tokens': recent,
                    'recent_cycle_confidence': 0.92,
                    'recent_cycle_evidence_count': 5,
                    'recent_cycle_pair_count': 3,
                    'recent_cycle_id': 'cycle-new',
                    'recent_cycle_evidence_id': evidence_id,
                    'recent_cycle_accepted': True,
                })
            return item

        history = {'gemini_5h': [
            event('2026-09-01T00:00:00+00:00', 1_000_000),
            event('2026-09-01T01:00:00+00:00', 1_010_000),
            event('2026-09-01T02:00:00+00:00', 995_000),
            event('2026-09-02T00:00:00+00:00', 1_000_000, 1_500_000, 'r1'),
        ]}
        self.assertEqual(server.detect_quota_policy_shifts(history), [])

    def test_duplicate_recent_cycle_evidence_does_not_fabricate_confirmation(self):
        def event(ts, evidence_id):
            return {
                'timestamp': ts, 'transcript_capacity_tokens': 1_000_000,
                'confidence': 0.95, 'evidence_count': 10, 'pair_count': 6,
                'identifiability': {'transcript': True},
                'recent_cycle_capacity_tokens': 1_500_000,
                'recent_cycle_confidence': 0.92,
                'recent_cycle_evidence_count': 5,
                'recent_cycle_pair_count': 3,
                'recent_cycle_id': 'cycle-new',
                'recent_cycle_evidence_id': evidence_id,
                'recent_cycle_accepted': True,
            }

        history = {'gemini_5h': [
            {
                'timestamp': '2026-09-01T00:00:00+00:00',
                'transcript_capacity_tokens': 1_000_000,
                'confidence': 0.95, 'evidence_count': 10, 'pair_count': 6,
                'identifiability': {'transcript': True},
            },
            {
                'timestamp': '2026-09-01T01:00:00+00:00',
                'transcript_capacity_tokens': 1_005_000,
                'confidence': 0.95, 'evidence_count': 10, 'pair_count': 6,
                'identifiability': {'transcript': True},
            },
            event('2026-09-02T00:00:00+00:00', 'same-evidence'),
            event('2026-09-02T00:01:00+00:00', 'same-evidence'),
        ]}
        self.assertEqual(server.detect_quota_policy_shifts(history), [])

    def test_recent_cycle_candidate_uses_latest_cycle_only(self):
        observations = [
            obs('2026-09-01T00:00:00+00:00', 100.0, 0, 'cycle-old'),
            obs('2026-09-01T00:20:00+00:00', 90.0, 100_000, 'cycle-old'),
            obs('2026-09-01T00:40:00+00:00', 80.0, 200_000, 'cycle-old'),
            obs('2026-09-02T00:00:00+00:00', 100.0, 0, 'cycle-new'),
            obs('2026-09-02T00:20:00+00:00', 96.0, 60_000, 'cycle-new'),
            obs('2026-09-02T00:40:00+00:00', 92.0, 120_000, 'cycle-new'),
            obs('2026-09-02T01:00:00+00:00', 88.0, 180_000, 'cycle-new'),
        ]
        for item in observations:
            item['token_estimator_id'] = server.TOKEN_ESTIMATOR_ID

        candidate = server.estimate_recent_cycle_capacity_candidate(
            observations, 5 * 60 * 60)

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate['cycle_id'], 'cycle-new')
        self.assertAlmostEqual(candidate['capacity_tokens'], 1_500_000, delta=40_000)

    def test_worker_only_movement_does_not_create_recent_transcript_candidate(self):
        base = datetime(2026, 9, 2, tzinfo=timezone.utc)
        observations = []
        for index, (worker, pct) in enumerate(((0, 100.0), (3_000_000, 90.0), (6_000_000, 80.0), (9_000_000, 70.0))):
            item = obs((base + timedelta(minutes=20 * index)).isoformat(), pct, 0, 'cycle-worker')
            item.update({
                'worker_used_tokens': worker,
                'worker_estimator_id': server.GEMINI_WORKER_ESTIMATOR_ID,
                'token_estimator_id': server.TOKEN_ESTIMATOR_ID,
            })
            observations.append(item)

        candidate = server.estimate_recent_cycle_capacity_candidate(
            observations, 5 * 60 * 60, worker_fallback_capacity=30_000_000)

        self.assertIsNone(candidate)

    def test_long_term_convergence_to_early_shift_does_not_duplicate_alert(self):
        def event(ts, cap, recent=None, evidence_id=None):
            item = {
                'timestamp': ts, 'transcript_capacity_tokens': cap,
                'confidence': 0.95, 'evidence_count': 10, 'pair_count': 6,
                'identifiability': {'transcript': True},
            }
            if recent is not None:
                item.update({
                    'recent_cycle_capacity_tokens': recent,
                    'recent_cycle_confidence': 0.92,
                    'recent_cycle_evidence_count': 5,
                    'recent_cycle_pair_count': 3,
                    'recent_cycle_id': 'cycle-new',
                    'recent_cycle_evidence_id': evidence_id,
                    'recent_cycle_accepted': True,
                })
            return item

        history = {'gemini_5h': [
            event('2026-09-01T00:00:00+00:00', 1_000_000),
            event('2026-09-01T01:00:00+00:00', 1_010_000),
            event('2026-09-01T02:00:00+00:00', 995_000),
            event('2026-09-02T00:00:00+00:00', 1_000_000, 1_500_000, 'r1'),
            event('2026-09-02T01:00:00+00:00', 1_000_000, 1_505_000, 'r2'),
            event('2026-09-03T00:00:00+00:00', 1_500_000),
            event('2026-09-03T01:00:00+00:00', 1_505_000),
        ]}
        shifts = server.detect_quota_policy_shifts(history)

        self.assertEqual(len(shifts), 1)
        self.assertEqual(shifts[0]['status'], 'DETECTED_EARLY_CONFIRMED')
        self.assertEqual(shifts[0]['source'], 'recent_cycle_capacity_tokens')


if __name__ == '__main__':
    unittest.main()
