import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import server


class CodexAppServerRateLimitTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows desktop installation discovery')
    def test_finds_desktop_codex_when_tracker_path_has_no_codex(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            install = Path(temp_dir) / 'OpenAI' / 'Codex' / 'bin'
            older = install / 'older-version' / 'codex.exe'
            newer = install / 'newer-version' / 'codex.exe'
            for executable in (older, newer):
                executable.parent.mkdir(parents=True)
                executable.touch()
            os.utime(older, (1000, 1000))
            os.utime(newer, (2000, 2000))
            (install / 'incomplete-update').mkdir()
            with mock.patch('shutil.which', return_value=None), \
                 mock.patch.dict(os.environ, {'LOCALAPPDATA': temp_dir}):
                self.assertEqual(server.find_codex_executable(), str(newer))

    def test_keeps_executable_selected_on_path(self):
        with mock.patch('shutil.which', return_value='configured-codex'):
            self.assertEqual(server.find_codex_executable(), 'configured-codex')

    @unittest.skipUnless(os.name == 'nt', 'Windows desktop installation discovery')
    def test_missing_installation_returns_none_for_log_fallback(self):
        with tempfile.TemporaryDirectory() as temp_dir, \
             mock.patch('shutil.which', return_value=None), \
             mock.patch.dict(os.environ, {'LOCALAPPDATA': temp_dir}):
            self.assertIsNone(server.find_codex_executable())

    def test_normalizes_live_app_server_snapshot(self):
        now = datetime(2026, 9, 7, 1, 30, tzinfo=timezone.utc)
        payload = {
            'accountId': 'account-test',
            'rateLimits': {},
            'rateLimitsByLimitId': {
                'codex': {
                    'limitId': 'codex',
                    'limitName': 'Codex',
                    'planType': 'plus',
                    'primary': {
                        'usedPercent': 24,
                        'windowDurationMins': 300,
                        'resetsAt': 1788746400,
                    },
                    'secondary': {
                        'usedPercent': 93,
                        'windowDurationMins': 10080,
                        'resetsAt': 1788748063,
                    },
                }
            },
        }

        result = server.normalize_codex_app_server_rate_limits(payload, now_utc=now)

        self.assertTrue(result['available'])
        self.assertEqual(result['source'], 'codex_app_server')
        self.assertEqual(result['account_id'], 'account-test')
        self.assertEqual([w['window_minutes'] for w in result['windows']], [300, 10080])
        self.assertEqual(result['windows'][0]['remaining_percent'], 76.0)
        self.assertEqual(result['windows'][1]['remaining_percent'], 7.0)

    def test_prefers_legacy_live_snapshot_if_named_bucket_is_empty(self):
        payload = {
            'rateLimits': {
                'primary': {'usedPercent': 11, 'windowDurationMins': 300, 'resetsAt': 1788746400},
                'secondary': {'usedPercent': 22, 'windowDurationMins': 10080, 'resetsAt': 1788748063},
            },
            'rateLimitsByLimitId': {'codex': {'primary': None, 'secondary': None}},
        }

        result = server.normalize_codex_app_server_rate_limits(payload)

        self.assertTrue(result['available'])
        self.assertEqual(result['windows'][0]['used_percent'], 11.0)
        self.assertEqual(result['windows'][1]['used_percent'], 22.0)

    def test_get_codex_rate_limits_prefers_live_rpc(self):
        live = {'available': True, 'source': 'codex_app_server', 'windows': [{'window_minutes': 300}]}
        with mock.patch.object(server, 'read_codex_rate_limits_app_server', return_value=live), \
             mock.patch.object(server, 'scan_codex_rate_limits') as fallback:
            result = server.get_codex_rate_limits()

        self.assertIs(result, live)
        fallback.assert_not_called()

    def test_get_codex_rate_limits_falls_back_to_session_logs(self):
        fallback = {'available': True, 'source': 'local_session_logs', 'windows': [{'window_minutes': 300}]}
        with mock.patch.object(server, 'read_codex_rate_limits_app_server', return_value=None), \
             mock.patch.object(server, 'scan_codex_rate_limits', return_value=fallback):
            result = server.get_codex_rate_limits()

        self.assertIs(result, fallback)
        self.assertIn('fallback_reason', result)


if __name__ == '__main__':
    unittest.main()
