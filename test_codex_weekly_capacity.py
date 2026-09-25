import json
import os
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone

import server


class CodexWeeklyCapacityTests(unittest.TestCase):
    def observation(self, session, model, ts, weekly_pct, tokens, reset='2026-09-30T00:00:00+00:00'):
        return {
            'session': session,
            'model_key': model,
            'ts': ts,
            'weekly_used_percent': weekly_pct,
            'weekly_resets_at': reset,
            'total_tokens': tokens,
            'input_tokens': tokens,
            'cached_input_tokens': 0,
            'cache_write_input_tokens': 0,
            'output_tokens': 0,
        }

    def test_extracts_weekly_window_without_five_hour_window(self):
        result = server._extract_codex_5h_quota_observation(
            {'secondary': {'window_minutes': 10080, 'used_percent': 12,
                           'resets_at': '2026-09-30T00:00:00+00:00'}},
            '2026-09-23T00:00:00+00:00', 'gpt-5.6-sol', 'high',
            {'total_tokens': 1000, 'input_tokens': 1000}, 'session-1',
        )
        self.assertIsNotNone(result)
        self.assertEqual(result['weekly_used_percent'], 12)
        self.assertIsNone(result['used_percent'])

    def test_each_measurement_and_recent_median_are_kept_across_models(self):
        rows = [
            self.observation('old', '5.6 sol high', '2026-09-01T10:00:00+00:00', 10, 100),
            self.observation('old', '5.6 sol high', '2026-09-01T10:10:00+00:00', 20, 1_000_100),
            self.observation('new', '5.6 luna xhigh', '2026-09-20T10:00:00+00:00', 10, 100),
            self.observation('new', '5.6 luna xhigh', '2026-09-20T10:10:00+00:00', 20, 1_000_100),
            self.observation('newer', '5.6 sol high', '2026-09-21T10:00:00+00:00', 10, 100),
            self.observation('newer', '5.6 sol high', '2026-09-21T10:10:00+00:00', 20, 2_000_100),
        ]
        result = server.build_codex_weekly_capacity_history(
            rows, now=datetime(2026, 9, 22, tzinfo=timezone.utc)
        )
        self.assertEqual(result['sample_count'], 3)
        points = result['points']
        self.assertEqual([point['full_week_usd'] for point in points], [40, 2, 80])
        self.assertEqual(points[-1]['recent']['7']['sample_count'], 2)
        self.assertEqual(points[-1]['recent']['7']['median_usd'], 41)
        self.assertEqual(points[-1]['cumulative_median_usd'], 40)
        self.assertEqual({point['model_key'] for point in points}, {'5.6 sol high', '5.6 luna xhigh'})

    def test_excludes_reset_and_model_switch_and_unpriced_intervals(self):
        rows = [
            self.observation('reset', '5.6 sol high', '2026-09-20T10:00:00+00:00', 10, 100),
            self.observation('reset', '5.6 sol high', '2026-09-20T10:10:00+00:00', 20, 1_000_100,
                             reset='2026-10-07T00:00:00+00:00'),
            self.observation('switch', '5.6 sol high', '2026-09-20T10:00:00+00:00', 10, 100),
            self.observation('switch', '5.6 luna xhigh', '2026-09-20T10:10:00+00:00', 20, 1_000_100),
            self.observation('unpriced', 'unpriced fake model', '2026-09-20T10:00:00+00:00', 10, 100),
            self.observation('unpriced', 'unpriced fake model', '2026-09-20T10:10:00+00:00', 20, 1_000_100),
        ]
        result = server.build_codex_weekly_capacity_history(
            rows, now=datetime(2026, 9, 22, tzinfo=timezone.utc)
        )
        self.assertFalse(result['available'])
        self.assertEqual(result['sample_count'], 0)
        self.assertEqual(result['unpriced_interval_count'], 1)

    def test_scanner_backfills_weekly_measurements_from_old_cache(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_dir = os.path.join(temp_dir, 'sessions')
            os.makedirs(session_dir)
            cache_path = os.path.join(temp_dir, 'cache.json')
            with open(cache_path, 'w', encoding='utf-8') as handle:
                json.dump({'version': 5, 'files': {}}, handle)
            session_path = os.path.join(session_dir, 'session.jsonl')
            events = [{'type': 'turn_context', 'payload': {'model': 'gpt-5.6-sol', 'effort': 'high'}}]
            for ts, pct, total in [
                ('2026-09-20T10:00:00+00:00', 10, 100),
                ('2026-09-20T10:10:00+00:00', 20, 1_000_100),
            ]:
                events.append({
                    'type': 'event_msg', 'timestamp': ts,
                    'payload': {
                        'type': 'token_count',
                        'rate_limits': {
                            'primary': {'window_minutes': 300, 'used_percent': pct,
                                        'resets_at': '2026-09-20T15:00:00+00:00'},
                            'secondary': {'window_minutes': 10080, 'used_percent': pct,
                                          'resets_at': '2026-09-27T15:00:00+00:00'},
                        },
                        'info': {
                            'last_token_usage': {'input_tokens': total, 'output_tokens': 0,
                                                 'total_tokens': total},
                            'total_token_usage': {'input_tokens': total, 'output_tokens': 0,
                                                  'total_tokens': total},
                        },
                    },
                })
            with open(session_path, 'w', encoding='utf-8') as handle:
                for event in events:
                    handle.write(json.dumps(event) + '\n')
            result = server.scan_codex_model_usage(
                sessions_dir=session_dir, cache_file=cache_path,
                now=datetime(2026, 9, 21, tzinfo=timezone.utc),
            )
            self.assertEqual(result['weekly_capacity_history']['sample_count'], 1)
            self.assertEqual(result['weekly_capacity_history']['points'][0]['full_week_usd'], 40)
            with open(cache_path, encoding='utf-8') as handle:
                cache = json.load(handle)
            self.assertEqual(cache['version'], server.CODEX_MODELS_CACHE_VERSION)
            observation = next(iter(cache['files'].values()))['quota_observations'][0]
            self.assertEqual(observation['weekly_used_percent'], 10)

    def test_identity_matches_only_both_windows_near_a_live_snapshot(self):
        at = '2026-09-23T10:00:00+00:00'
        snapshot = {
            'observed_at': at, 'account_id': 'account-a',
            'weekly': {'used_percent': 10, 'resets_at': '2026-09-30T00:00:00+00:00'},
            'five_hour': {'used_percent': 20, 'resets_at': '2026-09-23T15:00:00+00:00'},
        }
        close = self.observation('s', '5.6 sol high', at, 10, 100)
        close['resets_at'] = snapshot['five_hour']['resets_at']
        close['used_percent'] = 20
        wrong_five_hour = dict(close, resets_at='2026-09-23T16:00:00+00:00')
        old = dict(close, ts='2026-09-23T09:30:00+00:00')
        rows = server.attribute_codex_quota_observations(
            [close, wrong_five_hour, old], [snapshot]
        )
        self.assertEqual(rows[0]['account_id'], 'account-a')
        self.assertNotIn('account_id', rows[1])
        self.assertNotIn('account_id', rows[2])
        self.assertNotIn('account_id', close)

    def test_account_switch_is_not_used_as_a_quota_delta_and_medians_are_separate(self):
        rows = [
            self.observation('shared', '5.6 sol high', '2026-09-23T10:00:00+00:00', 10, 100),
            self.observation('shared', '5.6 sol high', '2026-09-23T10:10:00+00:00', 20, 1_000_100),
            self.observation('shared', '5.6 sol high', '2026-09-23T10:20:00+00:00', 30, 2_000_100),
            self.observation('second', '5.6 sol high', '2026-09-23T10:00:00+00:00', 10, 100),
            self.observation('second', '5.6 sol high', '2026-09-23T10:10:00+00:00', 20, 2_000_100),
        ]
        for row, account in zip(rows, ('account-a', 'account-b', 'account-b', 'account-a', 'account-a')):
            row['account_id'] = account
        result = server.build_codex_weekly_capacity_history(
            rows, now=datetime(2026, 9, 24, tzinfo=timezone.utc)
        )
        self.assertEqual(result['sample_count'], 2)
        self.assertEqual(result['account_sample_counts'], {'account-a': 1, 'account-b': 1})
        self.assertEqual({point['account_id']: point['cumulative_median_usd']
                          for point in result['points']}, {'account-a': 80, 'account-b': 40})

    def test_switch_and_early_reset_have_distinct_observed_event_types(self):
        def snap(account, at, pct, reset):
            return {'account_id': account, 'observed_at': at,
                    'weekly': {'used_percent': pct, 'resets_at': reset}}
        rows = [
            snap('a', '2026-09-23T10:00:00+00:00', 70, '2026-09-28T00:00:00+00:00'),
            snap('b', '2026-09-23T10:05:00+00:00', 10, '2026-09-29T00:00:00+00:00'),
            snap('a', '2026-09-23T10:10:00+00:00', 70, '2026-09-28T00:00:00+00:00'),
            snap('a', '2026-09-23T10:15:00+00:00', 0, '2026-09-30T10:15:00+00:00'),
        ]
        events = server.build_codex_quota_identity_events(rows)
        self.assertEqual([event['kind'] for event in events], [
            'account_switch_observed', 'account_switch_observed',
            'early_weekly_reset_unverified',
        ])
        self.assertFalse(events[-1]['cause_confirmed'])

    def test_live_snapshot_is_persisted_without_email_and_without_duplicate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, 'identity.json')
            live = {
                'source': 'codex_app_server', 'account_id': 'uuid-opaque',
                'observed_at': '2026-09-23T10:00:00+00:00', 'plan_type': 'pro',
                'windows': [
                    {'window_minutes': 300, 'used_percent': 5,
                     'resets_at': '2026-09-23T15:00:00+00:00'},
                    {'window_minutes': 10080, 'used_percent': 10,
                     'resets_at': '2026-09-30T00:00:00+00:00'},
                ],
            }
            with patch.object(server, 'CODEX_QUOTA_IDENTITY_FILE', path):
                self.assertTrue(server.record_codex_quota_identity(live))
                self.assertFalse(server.record_codex_quota_identity(live))
                rows = server.load_codex_quota_identity()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['account_id'], 'uuid-opaque')
            self.assertNotIn('email', rows[0])


if __name__ == '__main__':
    unittest.main()
