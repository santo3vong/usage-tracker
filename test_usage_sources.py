import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import server


class UsageSourcesTests(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self._orig_accounts = getattr(server, 'ACCOUNTS_FILE', None)
        self._orig_real = getattr(server, 'REAL_QUOTAS_FILE', None)
        self._orig_obs = getattr(server, 'QUOTA_OBSERVATIONS_FILE', None)
        self._orig_codex = getattr(server, 'CODEX_USAGE_FILE', None)
        self._orig_codex_cache = getattr(server, 'CODEX_MODELS_CACHE_FILE', None)
        self._orig_codex_mission_cache = getattr(server, 'CODEX_MISSION_TURNS_CACHE_FILE', None)
        self._orig_codex_mission_reviews = getattr(server, 'CODEX_MISSION_REVIEWS_FILE', None)
        self._orig_ts = getattr(server, 'TIME_SERIES_FILE', None)
        self._orig_vscdb = getattr(server, 'VSCDB_PATH', None)
        self._orig_sources = getattr(server, 'TRANSCRIPT_SOURCES', None)
        self._orig_codex_dir = getattr(server, 'CODEX_SESSIONS_DIR', None)

        server.ACCOUNTS_FILE = os.path.join(self._temp_dir.name, 'accounts.json')
        server.REAL_QUOTAS_FILE = os.path.join(self._temp_dir.name, 'real_quotas.json')
        server.QUOTA_OBSERVATIONS_FILE = os.path.join(self._temp_dir.name, 'quota_observations.json')
        server.CODEX_USAGE_FILE = os.path.join(self._temp_dir.name, 'codex_usage.json')
        server.CODEX_MODELS_CACHE_FILE = os.path.join(self._temp_dir.name, 'codex_models_cache.json')
        server.CODEX_MISSION_TURNS_CACHE_FILE = os.path.join(self._temp_dir.name, 'codex_mission_turns_cache.json')
        server.CODEX_MISSION_REVIEWS_FILE = os.path.join(self._temp_dir.name, 'codex_mission_reviews.json')
        server.TIME_SERIES_FILE = os.path.join(self._temp_dir.name, 'time_series_history.json')
        server.VSCDB_PATH = os.path.join(self._temp_dir.name, 'nonexistent.vscdb')

    def tearDown(self):
        server.ACCOUNTS_FILE = self._orig_accounts
        server.REAL_QUOTAS_FILE = self._orig_real
        server.QUOTA_OBSERVATIONS_FILE = self._orig_obs
        server.CODEX_USAGE_FILE = self._orig_codex
        server.CODEX_MODELS_CACHE_FILE = self._orig_codex_cache
        server.CODEX_MISSION_TURNS_CACHE_FILE = self._orig_codex_mission_cache
        server.CODEX_MISSION_REVIEWS_FILE = self._orig_codex_mission_reviews
        server.TIME_SERIES_FILE = self._orig_ts
        server.VSCDB_PATH = self._orig_vscdb
        server.TRANSCRIPT_SOURCES = self._orig_sources
        server.CODEX_SESSIONS_DIR = self._orig_codex_dir
        self._temp_dir.cleanup()

    def test_multi_source_discovery_and_aggregation(self):
        ide_dir = os.path.join(self._temp_dir.name, 'ide_brain')
        cli_dir = os.path.join(self._temp_dir.name, 'cli_brain')
        os.makedirs(ide_dir, exist_ok=True)
        os.makedirs(cli_dir, exist_ok=True)

        # Create IDE conversation
        ide_conv_id = 'ide-session-001'
        ide_conv_dir = os.path.join(ide_dir, ide_conv_id, '.system_generated', 'logs')
        os.makedirs(ide_conv_dir, exist_ok=True)
        now_iso = datetime.now(timezone.utc).isoformat()
        with open(os.path.join(ide_conv_dir, 'transcript.jsonl'), 'w', encoding='utf-8') as f:
            f.write(json.dumps({
                'type': 'USER_INPUT',
                'content': '<USER_REQUEST>IDE Task</USER_REQUEST>',
                'created_at': now_iso
            }) + '\n')
            f.write(json.dumps({
                'type': 'PLANNER_RESPONSE',
                'content': 'IDE Response with some content here.',
                'created_at': now_iso
            }) + '\n')

        # Create CLI conversation
        cli_conv_id = 'cli-session-002'
        cli_conv_dir = os.path.join(cli_dir, cli_conv_id, '.system_generated', 'logs')
        os.makedirs(cli_conv_dir, exist_ok=True)
        with open(os.path.join(cli_conv_dir, 'transcript.jsonl'), 'w', encoding='utf-8') as f:
            f.write(json.dumps({
                'type': 'USER_INPUT',
                'content': '<USER_REQUEST>CLI Worker Task</USER_REQUEST>',
                'created_at': now_iso
            }) + '\n')
            f.write(json.dumps({
                'type': 'PLANNER_RESPONSE',
                'content': 'CLI Worker Response with some more content here.',
                'created_at': now_iso
            }) + '\n')

        sources = {
            'ide': {'key': 'ide', 'label': 'Antigravity IDE', 'path': ide_dir},
            'cli': {'key': 'cli', 'label': 'Antigravity CLI', 'path': cli_dir},
        }

        result = server.analyze_all_conversations(sources=sources)
        conversations = result.get('conversations', [])
        summary = result.get('summary', {})

        self.assertEqual(len(conversations), 2)
        conv_map = {c['id']: c for c in conversations}

        self.assertIn('ide-session-001', conv_map)
        self.assertEqual(conv_map['ide-session-001']['source'], 'ide')
        self.assertEqual(conv_map['ide-session-001']['source_label'], 'Antigravity IDE')

        self.assertIn('cli-session-002', conv_map)
        self.assertEqual(conv_map['cli-session-002']['source'], 'cli')
        self.assertEqual(conv_map['cli-session-002']['source_label'], 'Antigravity CLI')

        # Verify rolling tokens aggregated across both sources
        q = summary.get('quotas', {})
        g5_used = q.get('five_hour_window', {}).get('gemini', {}).get('tracked_used_tokens', 0)
        self.assertGreater(g5_used, 0)
        self.assertEqual(
            g5_used,
            conv_map['ide-session-001']['input_tokens_est'] +
            conv_map['ide-session-001']['output_tokens_est'] +
            conv_map['cli-session-002']['input_tokens_est'] +
            conv_map['cli-session-002']['output_tokens_est']
        )

        # Verify source breakdown metrics present and path omitted
        sb = summary.get('source_breakdown', {})
        self.assertIn('ide', sb)
        self.assertIn('cli', sb)
        self.assertEqual(sb['ide']['total_conversations'], 1)
        self.assertEqual(sb['cli']['total_conversations'], 1)
        self.assertGreater(sb['ide']['total_tokens'], 0)
        self.assertGreater(sb['cli']['total_tokens'], 0)
        self.assertNotIn('path', sb['ide'])
        self.assertNotIn('path', sb['cli'])

        # Check source_breakdowns alias
        sb_alias = summary.get('source_breakdowns', {})
        self.assertIn('ide', sb_alias)
        self.assertNotIn('path', sb_alias['ide'])

    def test_source_registry_injection_without_default_overrides(self):
        custom_dir = os.path.join(self._temp_dir.name, 'custom_brain')
        os.makedirs(custom_dir, exist_ok=True)
        server.TRANSCRIPT_SOURCES = {
            'custom': {'key': 'custom', 'label': 'Custom Engine', 'path': custom_dir}
        }
        loaded = server.get_transcript_sources()
        self.assertIn('custom', loaded)
        self.assertEqual(loaded['custom']['path'], custom_dir)
        self.assertEqual(loaded['custom']['label'], 'Custom Engine')

    def test_source_deduplication(self):
        shared_dir = os.path.join(self._temp_dir.name, 'shared_brain')
        conv_id = 'shared-conv-123'
        conv_logs = os.path.join(shared_dir, conv_id, '.system_generated', 'logs')
        os.makedirs(conv_logs, exist_ok=True)
        now_iso = datetime.now(timezone.utc).isoformat()
        with open(os.path.join(conv_logs, 'transcript.jsonl'), 'w', encoding='utf-8') as f:
            f.write(json.dumps({
                'type': 'USER_INPUT',
                'content': 'Test prompt',
                'created_at': now_iso
            }) + '\n')

        # Both source keys point to the exact same path
        sources = {
            'ide': {'key': 'ide', 'label': 'Antigravity IDE', 'path': shared_dir},
            'cli': {'key': 'cli', 'label': 'Antigravity CLI', 'path': shared_dir},
        }

        result = server.analyze_all_conversations(sources=sources)
        conversations = result.get('conversations', [])
        # Deduplication should ensure it is parsed only once
        self.assertEqual(len(conversations), 1)

    def test_source_aware_conversation_lookup_and_containment(self):
        ide_dir = os.path.join(self._temp_dir.name, 'ide_brain')
        cli_dir = os.path.join(self._temp_dir.name, 'cli_brain')
        os.makedirs(ide_dir, exist_ok=True)
        os.makedirs(cli_dir, exist_ok=True)

        # Create conversation in IDE
        ide_conv_id = 'conv-in-ide'
        logs_ide = os.path.join(ide_dir, ide_conv_id, '.system_generated', 'logs')
        os.makedirs(logs_ide, exist_ok=True)
        with open(os.path.join(logs_ide, 'transcript.jsonl'), 'w', encoding='utf-8') as f:
            f.write(json.dumps({'type': 'USER_INPUT', 'content': 'Hello IDE'}) + '\n')

        # Create conversation in CLI
        cli_conv_id = 'conv-in-cli'
        logs_cli = os.path.join(cli_dir, cli_conv_id, '.system_generated', 'logs')
        os.makedirs(logs_cli, exist_ok=True)
        with open(os.path.join(logs_cli, 'transcript.jsonl'), 'w', encoding='utf-8') as f:
            f.write(json.dumps({'type': 'USER_INPUT', 'content': 'Hello CLI'}) + '\n')

        server.TRANSCRIPT_SOURCES = {
            'ide': {'key': 'ide', 'label': 'Antigravity IDE', 'path': ide_dir},
            'cli': {'key': 'cli', 'label': 'Antigravity CLI', 'path': cli_dir},
        }

        # Lookup without source defaults to 'ide'
        res_ide_default = server.get_conversation_details('conv-in-ide')
        self.assertNotIn('error', res_ide_default)
        self.assertEqual(res_ide_default['id'], 'conv-in-ide')
        self.assertEqual(res_ide_default['source'], 'ide')

        # Lookup with explicit source 'cli'
        res_cli = server.get_conversation_details('conv-in-cli', source='cli')
        self.assertNotIn('error', res_cli)
        self.assertEqual(res_cli['id'], 'conv-in-cli')
        self.assertEqual(res_cli['source'], 'cli')

        # Lookup CLI conversation from IDE source fails
        res_cross = server.get_conversation_details('conv-in-cli', source='ide')
        self.assertIn('error', res_cross)

        # Invalid source key
        res_bad_source = server.get_conversation_details('conv-in-ide', source='unknown_source')
        self.assertIn('error', res_bad_source)

        # Traversal attempts rejected
        res_traversal = server.get_conversation_details('../etc/passwd')
        self.assertIn('error', res_traversal)

        res_traversal_win = server.get_conversation_details('..\\..\\windows\\system32')
        self.assertIn('error', res_traversal_win)

    def test_codex_rate_limit_normalization_one_window_and_credits_sanitization(self):
        payload = {
            'primary': {
                'window_minutes': 10080,
                'used_percent': 25.0,
                'resets_at': '2026-08-30T12:00:00+00:00',
                'used_tokens': 25000,
                'limit_tokens': 100000
            },
            'secondary': None,
            'plan_type': 'pro',
            'credits': {
                'has_credits': True,
                'remaining': 50000,
                'unlimited': False,
                'unknown_sensitive_token': 'secret_key_123',
                'nested_raw': {'foo': 'bar'}
            }
        }
        res = server.normalize_codex_rate_limits(payload, event_timestamp='2026-08-24T10:00:00+00:00')
        self.assertTrue(res['available'])
        self.assertEqual(res['plan_type'], 'pro')
        self.assertEqual(len(res['windows']), 1)
        w = res['windows'][0]
        self.assertEqual(w['window_minutes'], 10080)
        self.assertEqual(w['used_percent'], 25.0)
        self.assertEqual(w['remaining_percent'], 75.0)
        self.assertEqual(w['resets_at'], '2026-08-30T12:00:00+00:00')
        self.assertEqual(w['status'], 'Normal')

        # Check credits sanitization
        credits = res.get('credits', {})
        self.assertEqual(credits.get('has_credits'), True)
        self.assertEqual(credits.get('remaining'), 50000)
        self.assertEqual(credits.get('unlimited'), False)
        self.assertNotIn('unknown_sensitive_token', credits)
        self.assertNotIn('nested_raw', credits)

    def test_codex_rate_limit_normalization_two_windows(self):
        payload = {
            'primary': {
                'window_minutes': 300,
                'used_percent': 40.0,
                'resets_at': 1756400000
            },
            'secondary': {
                'window_minutes': 10080,
                'remaining_percent': 85.0,
                'resets_at': 1756900000
            },
            'plan_type': 'team'
        }
        res = server.normalize_codex_rate_limits(payload, event_timestamp='2026-08-24T10:00:00+00:00')
        self.assertTrue(res['available'])
        self.assertEqual(len(res['windows']), 2)
        # Windows sorted by window_minutes ascending
        w5h = res['windows'][0]
        self.assertEqual(w5h['window_minutes'], 300)
        self.assertEqual(w5h['used_percent'], 40.0)
        self.assertEqual(w5h['remaining_percent'], 60.0)

        w_wk = res['windows'][1]
        self.assertEqual(w_wk['window_minutes'], 10080)
        self.assertEqual(w_wk['used_percent'], 15.0)
        self.assertEqual(w_wk['remaining_percent'], 85.0)

    def test_codex_rate_limits_malformed_and_missing_logs(self):
        # Empty or non-dict rate limits
        res_none = server.normalize_codex_rate_limits(None)
        self.assertIsNone(res_none)

        res_empty = server.normalize_codex_rate_limits({})
        self.assertFalse(res_empty['available'])
        self.assertEqual(len(res_empty['windows']), 0)

        # Scanning non-existent directory
        res_missing = server.scan_codex_rate_limits(sessions_dir='/non/existent/path')
        self.assertFalse(res_missing['available'])
        self.assertEqual(len(res_missing['windows']), 0)
        self.assertIn('error', res_missing)

    def test_codex_scanner_bounds_and_newest_selection(self):
        sess_dir = os.path.join(self._temp_dir.name, 'codex_sessions')
        os.makedirs(sess_dir, exist_ok=True)

        # Create older log file
        old_file = os.path.join(sess_dir, 'session-2026-08-20.jsonl')
        with open(old_file, 'w', encoding='utf-8') as f:
            f.write(json.dumps({
                'type': 'event_msg',
                'timestamp': '2026-08-20T10:00:00+00:00',
                'payload': {
                    'type': 'token_count',
                    'rate_limits': {
                        'primary': {'window_minutes': 300, 'used_percent': 80.0, 'resets_at': '2026-08-20T15:00:00+00:00'}
                    }
                }
            }) + '\n')

        # Create newer log file
        new_file = os.path.join(sess_dir, 'session-2026-08-24.jsonl')
        with open(new_file, 'w', encoding='utf-8') as f:
            # Add an ignored event without token_count
            f.write(json.dumps({
                'type': 'event_msg',
                'timestamp': '2026-08-24T11:00:00+00:00',
                'payload': {
                    'type': 'other_event',
                    'rate_limits': {'primary': {'window_minutes': 300, 'used_percent': 99.0}}
                }
            }) + '\n')
            # Add valid token_count event
            f.write(json.dumps({
                'type': 'event_msg',
                'timestamp': '2026-08-24T12:00:00+00:00',
                'payload': {
                    'type': 'token_count',
                    'rate_limits': {
                        'primary': {'window_minutes': 300, 'used_percent': 10.0, 'resets_at': '2026-08-24T17:00:00+00:00'}
                    }
                }
            }) + '\n')

        res = server.scan_codex_rate_limits(sessions_dir=sess_dir)
        self.assertTrue(res['available'])
        self.assertEqual(len(res['windows']), 1)
        # Should have selected the newer valid observation (10% used, not 80% or 99%)
        self.assertEqual(res['windows'][0]['used_percent'], 10.0)

    def test_codex_scanner_newest_malformed_window_falls_back_per_duration(self):
        sess_dir = os.path.join(self._temp_dir.name, 'codex_malformed_fallback_sessions')
        os.makedirs(sess_dir, exist_ok=True)
        path = os.path.join(sess_dir, 'session.jsonl')
        events = [
            {
                'type': 'event_msg', 'timestamp': '2026-08-24T10:00:00+00:00',
                'payload': {'type': 'token_count', 'rate_limits': {
                    'primary': {'window_minutes': 300, 'used_percent': 20.0},
                    'secondary': {'window_minutes': 10080, 'used_percent': 30.0},
                }}
            },
            {
                'type': 'event_msg', 'timestamp': '2026-08-24T11:00:00+00:00',
                'payload': {'type': 'token_count', 'rate_limits': {
                    'primary': {'window_minutes': 300, 'used_percent': None},
                    'secondary': {'window_minutes': 10080, 'used_percent': 40.0},
                }}
            },
        ]
        with open(path, 'w', encoding='utf-8') as f:
            for event in events:
                f.write(json.dumps(event) + '\n')
        result = server.scan_codex_rate_limits(sessions_dir=sess_dir)
        self.assertTrue(result['available'])
        windows = {item['window_minutes']: item for item in result['windows']}
        self.assertEqual(windows[300]['used_percent'], 20.0)
        self.assertEqual(windows[10080]['used_percent'], 40.0)
        self.assertEqual(result['diagnostics']['windows_selected'], 2)

    def test_codex_rate_limit_normalization_finite_numbers_and_no_nan_inf(self):
        import math
        malformed_payload = {
            'primary': {
                'window_minutes': 300,
                'used_percent': float('nan'),
                'remaining_percent': float('inf'),
                'used_tokens': float('nan'),
                'limit_tokens': 0,
                'resets_at': 'invalid-date-string',
                'resets_in_seconds': float('inf')
            },
            'secondary': {
                'window_minutes': 'invalid',
                'window_seconds': 'NaN',
                'used_percent': 'NaN',
                'remaining_percent': 'Infinity',
            },
            'third': {
                'window_minutes': 10080,
                'used_percent': None,
                'remaining_percent': None,
                'used_tokens': 5000,
                'limit_tokens': float('inf'),
                'resets_at': 1756400000,
                'resets_in_seconds': -100
            },
            'valid_window': {
                'window_minutes': 1440,
                'used_percent': 20.0,
                'remaining_percent': 80.0,
                'resets_at': '2026-08-25T12:00:00+00:00',
                'resets_in_seconds': 3600
            },
            'plan_type': 'team',
            'credits': {
                'has_credits': True,
                'remaining': float('nan'),
                'used': float('inf'),
                'limit': 'NaN',
                'balance': 1000
            }
        }

        res = server.normalize_codex_rate_limits(malformed_payload)
        self.assertIsNotNone(res)
        self.assertTrue(res['available'])
        self.assertEqual(len(res['windows']), 1)
        self.assertEqual(res['windows'][0]['window_minutes'], 1440)

        # Serialization to JSON must succeed without ValueError or NaN / Infinity tokens
        json_str = json.dumps(res)
        self.assertNotIn('NaN', json_str)
        self.assertNotIn('Infinity', json_str)
        roundtripped = json.loads(json_str)

        for w in roundtripped['windows']:
            self.assertTrue(math.isfinite(w['used_percent']))
            self.assertTrue(math.isfinite(w['remaining_percent']))
            self.assertGreaterEqual(w['used_percent'], 0.0)
            self.assertLessEqual(w['used_percent'], 100.0)
            self.assertGreaterEqual(w['remaining_percent'], 0.0)
            self.assertLessEqual(w['remaining_percent'], 100.0)
            if w.get('used_tokens') is not None:
                self.assertTrue(math.isfinite(w['used_tokens']))
            if w.get('limit_tokens') is not None:
                self.assertTrue(math.isfinite(w['limit_tokens']))
            if w.get('resets_in_seconds') is not None:
                self.assertGreaterEqual(w['resets_in_seconds'], 0)

        # Verify credits do not contain NaN or Inf
        c = roundtripped.get('credits')
        self.assertIsNotNone(c)
        self.assertEqual(c.get('balance'), 1000)
        self.assertNotIn('remaining', c)
        self.assertNotIn('used', c)
        self.assertNotIn('limit', c)

    def test_codex_duration_only_window_omitted(self):
        duration_only_payload = {
            'primary': {
                'window_minutes': 300
                # No used_percent, remaining_percent, or valid token counts
            }
        }
        res = server.normalize_codex_rate_limits(duration_only_payload)
        self.assertIsNotNone(res)
        self.assertFalse(res['available'])
        self.assertEqual(len(res['windows']), 0)

    def test_default_sources_registry_includes_gemini_cli(self):
        sources = server.get_transcript_sources()
        self.assertIn('ide', sources)
        self.assertIn('cli', sources)
        self.assertIn('gemini_cli', sources)
        self.assertEqual(sources['ide']['label'], 'Antigravity IDE')
        self.assertEqual(sources['cli']['label'], 'Antigravity CLI')
        self.assertEqual(sources['gemini_cli']['label'], 'Gemini CLI (Official)')
        self.assertEqual(sources['gemini_cli']['format'], 'gemini_cli')

    def test_official_gemini_cli_event_sourcing_reconstruction(self):
        gemini_tmp_dir = os.path.join(self._temp_dir.name, 'gemini_tmp', 'proj_abc', 'chats')
        os.makedirs(gemini_tmp_dir, exist_ok=True)
        session_file = os.path.join(gemini_tmp_dir, 'session_official_123.jsonl')

        now_iso = datetime.now(timezone.utc).isoformat()

        # Write event-sourced records:
        # 1. Metadata
        # 2. User message msg-1
        # 3. Model message msg-2 (initial)
        # 4. Duplicate msg-2 update (replaces earlier msg-2)
        # 5. Model message msg-3 (with thinking and tools)
        # 6. Checkpoint replacing state
        # 7. Model message msg-4
        # 8. Model message msg-5
        # 9. Rewind to msg-5 (removes msg-5)
        lines = [
            json.dumps({'sessionId': 'session_official_123', 'startTime': now_iso, 'lastUpdated': now_iso}),
            json.dumps({'id': 'msg-1', 'type': 'user', 'text': '<USER_REQUEST>Solve equation</USER_REQUEST>', 'timestamp': now_iso}),
            json.dumps({'id': 'msg-2', 'type': 'gemini', 'model': 'Gemini 2.5 Pro', 'text': 'Draft response', 'tokens': {'input': 50, 'output': 20, 'total': 70}, 'timestamp': now_iso}),
            # msg-2 updated (same ID replaces without creating extra message)
            json.dumps({'id': 'msg-2', 'type': 'gemini', 'model': 'Gemini 2.5 Pro', 'text': 'Final response', 'tokens': {'input': 80, 'output': 40, 'total': 120, 'thoughts': 15}, 'timestamp': now_iso}),
            json.dumps({'id': 'msg-3', 'type': 'gemini', 'model': 'Gemini 2.5 Pro', 'text': 'Using tool', 'toolCalls': [{'name': 'run_command'}], 'tokens': {'input': 100, 'output': 50, 'total': 150}, 'timestamp': now_iso}),
            # $set Checkpoint: replaces entire message map
            json.dumps({'$set': {
                'messages': {
                    'msg-1': {'id': 'msg-1', 'type': 'user', 'text': '<USER_REQUEST>Solve equation</USER_REQUEST>', 'timestamp': now_iso},
                    'msg-2': {'id': 'msg-2', 'type': 'gemini', 'model': 'Gemini 2.5 Pro', 'text': 'Final response', 'tokens': {'input': 80, 'output': 40, 'total': 120, 'thoughts': 15}, 'timestamp': now_iso},
                    'msg-4': {'id': 'msg-4', 'type': 'gemini', 'model': 'Gemini 2.5 Pro', 'text': 'Step 4 response', 'tokens': {'input': 200, 'output': 100, 'total': 300}, 'timestamp': now_iso},
                }
            }}),
            # Add msg-5
            json.dumps({'id': 'msg-5', 'type': 'gemini', 'model': 'Gemini 2.5 Pro', 'text': 'Step 5 to be rewound', 'tokens': {'input': 50, 'output': 20, 'total': 70}, 'timestamp': now_iso}),
            # $rewindTo msg-5: removes msg-5
            json.dumps({'$rewindTo': 'msg-5'}),
        ]

        with open(session_file, 'w', encoding='utf-8') as f:
            for l in lines:
                f.write(l + '\n')

        sources = {
            'gemini_cli': {
                'key': 'gemini_cli',
                'label': 'Gemini CLI (Official)',
                'path': os.path.join(self._temp_dir.name, 'gemini_tmp'),
                'format': 'gemini_cli'
            }
        }

        result = server.analyze_all_conversations(sources=sources)
        convs = result.get('conversations', [])
        summary = result.get('summary', {})

        self.assertEqual(len(convs), 1)
        c = convs[0]
        self.assertEqual(c['id'], 'session_official_123')
        self.assertEqual(c['source'], 'gemini_cli')
        self.assertEqual(c['source_label'], 'Gemini CLI (Official)')
        self.assertEqual(c['user_messages'], 1)
        self.assertEqual(c['model_responses'], 2)  # msg-2 and msg-4 (msg-3 replaced by checkpoint, msg-5 rewound)
        self.assertEqual(c['total_steps'], 3)  # msg-1, msg-2, msg-4

        # Token accounting: msg-2 total 120 (in: 80, out: 40), msg-4 total 300 (in: 200, out: 100)
        # Total in: 280, Total out: 140, Total: 420
        self.assertEqual(c['input_tokens_est'], 280)
        self.assertEqual(c['output_tokens_est'], 140)
        self.assertEqual(c['thinking_tokens_est'], 15)

        # Quota aggregation
        q = summary.get('quotas', {})
        g5_used = q.get('five_hour_window', {}).get('gemini', {}).get('tracked_used_tokens', 0)
        self.assertEqual(g5_used, 420)

        # Source breakdown
        sb = summary.get('source_breakdowns', {})
        self.assertIn('gemini_cli', sb)
        self.assertEqual(sb['gemini_cli']['total_tokens'], 420)
        self.assertEqual(sb['gemini_cli']['gemini_5h_tokens'], 420)
        self.assertEqual(sb['gemini_cli']['gemini_weekly_tokens'], 420)

    def test_official_gemini_cli_setup_only_session_and_expired_events(self):
        gemini_tmp_dir = os.path.join(self._temp_dir.name, 'gemini_tmp2')
        os.makedirs(gemini_tmp_dir, exist_ok=True)

        now_utc = datetime.now(timezone.utc)
        old_ts = (now_utc - server.timedelta(days=10)).isoformat()
        fresh_ts = now_utc.isoformat()

        # Session 1: Setup-only session with only a user prompt and no tokens
        f1 = os.path.join(gemini_tmp_dir, 'setup_only.jsonl')
        with open(f1, 'w', encoding='utf-8') as f:
            f.write(json.dumps({'sessionId': 'setup_only', 'startTime': fresh_ts}) + '\n')
            f.write(json.dumps({'id': 'u1', 'type': 'user', 'text': 'Hello Gemini without response', 'timestamp': fresh_ts}) + '\n')

        # Session 2: Expired tokens (> 7 days ago)
        f2 = os.path.join(gemini_tmp_dir, 'expired_session.jsonl')
        with open(f2, 'w', encoding='utf-8') as f:
            f.write(json.dumps({'sessionId': 'expired_session', 'startTime': old_ts}) + '\n')
            f.write(json.dumps({'id': 'u2', 'type': 'user', 'text': 'Old query', 'timestamp': old_ts}) + '\n')
            f.write(json.dumps({'id': 'm2', 'type': 'gemini', 'text': 'Old answer', 'tokens': {'input': 500, 'output': 200, 'total': 700}, 'timestamp': old_ts}) + '\n')

        sources = {
            'gemini_cli': {
                'key': 'gemini_cli',
                'label': 'Gemini CLI (Official)',
                'path': gemini_tmp_dir,
                'format': 'gemini_cli'
            }
        }

        result = server.analyze_all_conversations(sources=sources)
        convs = result.get('conversations', [])
        summary = result.get('summary', {})

        self.assertEqual(len(convs), 2)
        q = summary.get('quotas', {})
        # Expired tokens and setup-only tokens must NOT contribute to 5h or weekly quotas
        g5_used = q.get('five_hour_window', {}).get('gemini', {}).get('tracked_used_tokens', 0)
        gw_used = q.get('weekly_window', {}).get('gemini', {}).get('tracked_used_tokens', 0)
        self.assertEqual(g5_used, 0)
        self.assertEqual(gw_used, 0)

    def test_official_gemini_cli_deduplicates_session_ids_and_ignores_info_records(self):
        gemini_tmp_dir = os.path.join(self._temp_dir.name, 'gemini_tmp_dedup')
        os.makedirs(gemini_tmp_dir, exist_ok=True)
        now_iso = datetime.now(timezone.utc).isoformat()

        records = [
            {'sessionId': 'shared-official-session', 'projectHash': 'project-a', 'startTime': now_iso},
            {'id': 'u1', 'type': 'user', 'content': [{'text': 'real prompt'}], 'timestamp': now_iso},
            {'id': 'notice', 'type': 'info', 'content': 'not a model response', 'timestamp': now_iso},
            {'id': 'm1', 'type': 'gemini', 'model': 'gemini-test', 'content': 'answer',
             'tokens': {'input': 60, 'output': 40, 'total': 100}, 'timestamp': now_iso},
        ]
        for filename in ('session-copy-a.jsonl', 'session-copy-b.jsonl'):
            with open(os.path.join(gemini_tmp_dir, filename), 'w', encoding='utf-8') as f:
                for record in records:
                    f.write(json.dumps(record) + '\n')

        sources = {
            'gemini_cli': {
                'key': 'gemini_cli', 'label': 'Gemini CLI (Official)',
                'path': gemini_tmp_dir, 'format': 'gemini_cli'
            }
        }
        result = server.analyze_all_conversations(sources=sources)
        self.assertEqual(len(result['conversations']), 1)
        conversation = result['conversations'][0]
        self.assertEqual(conversation['model_responses'], 1)
        self.assertEqual(conversation['input_tokens_est'] + conversation['output_tokens_est'], 100)
        tracked = result['summary']['quotas']['five_hour_window']['gemini']['tracked_used_tokens']
        self.assertEqual(tracked, 100)

    def test_official_gemini_cli_unknown_rewind_clears_messages(self):
        session_file = os.path.join(self._temp_dir.name, 'rewind.jsonl')
        now_iso = datetime.now(timezone.utc).isoformat()
        with open(session_file, 'w', encoding='utf-8') as f:
            f.write(json.dumps({'sessionId': 'rewind-session', 'projectHash': 'project-a', 'startTime': now_iso}) + '\n')
            f.write(json.dumps({'id': 'm1', 'type': 'gemini', 'tokens': {'total': 50}, 'timestamp': now_iso}) + '\n')
            f.write(json.dumps({'$rewindTo': 'missing-message'}) + '\n')

        parsed = server.parse_official_gemini_cli_session(session_file)
        self.assertEqual(parsed['messages'], [])

    def test_official_gemini_cli_discovery_prefers_recent_files(self):
        import time
        root = os.path.join(self._temp_dir.name, 'recent-discovery')
        os.makedirs(root, exist_ok=True)
        paths = []
        for idx in range(3):
            path = os.path.join(root, f'session-{idx}.jsonl')
            with open(path, 'w', encoding='utf-8') as f:
                f.write('{}\n')
            os.utime(path, (time.time() + idx, time.time() + idx))
            paths.append(path)

        discovered = server.discover_gemini_cli_session_files(root, max_files=2)
        self.assertEqual(discovered, [paths[2], paths[1]])

    def test_official_gemini_cli_details_lookup_and_traversal_rejection(self):
        gemini_tmp_dir = os.path.join(self._temp_dir.name, 'gemini_tmp3')
        os.makedirs(gemini_tmp_dir, exist_ok=True)

        session_file = os.path.join(gemini_tmp_dir, 'official_chat_456.jsonl')
        now_iso = datetime.now(timezone.utc).isoformat()
        with open(session_file, 'w', encoding='utf-8') as f:
            f.write(json.dumps({'sessionId': 'official_chat_456', 'startTime': now_iso}) + '\n')
            f.write(json.dumps({'id': 'msg-1', 'type': 'user', 'text': '<USER_REQUEST>Debug server</USER_REQUEST>', 'timestamp': now_iso}) + '\n')
            f.write(json.dumps({'id': 'msg-2', 'type': 'gemini', 'text': 'Inspecting code', 'tokens': {'input': 100, 'output': 50, 'total': 150, 'thoughts': 20}, 'timestamp': now_iso}) + '\n')

        server.TRANSCRIPT_SOURCES = {
            'gemini_cli': {
                'key': 'gemini_cli',
                'label': 'Gemini CLI (Official)',
                'path': gemini_tmp_dir,
                'format': 'gemini_cli'
            }
        }

        # Valid lookup by session id
        res = server.get_conversation_details('official_chat_456', source='gemini_cli')
        self.assertNotIn('error', res)
        self.assertEqual(res['id'], 'official_chat_456')
        self.assertEqual(res['source'], 'gemini_cli')
        self.assertEqual(len(res['steps']), 2)
        self.assertEqual(res['steps'][0]['type'], 'USER_INPUT')
        self.assertEqual(res['steps'][0]['preview'], 'Debug server')
        self.assertEqual(res['steps'][1]['type'], 'PLANNER_RESPONSE')
        self.assertEqual(res['steps'][1]['tokens_est'], 150)
        self.assertTrue(res['steps'][1]['has_thinking'])

        # Traversal attempts rejected
        res_traversal = server.get_conversation_details('../etc/passwd', source='gemini_cli')
        self.assertIn('error', res_traversal)

    def test_three_sources_full_aggregation(self):
        ide_dir = os.path.join(self._temp_dir.name, 'agg_ide')
        cli_dir = os.path.join(self._temp_dir.name, 'agg_cli')
        gem_dir = os.path.join(self._temp_dir.name, 'agg_gemini')
        os.makedirs(ide_dir, exist_ok=True)
        os.makedirs(cli_dir, exist_ok=True)
        os.makedirs(gem_dir, exist_ok=True)

        now_iso = datetime.now(timezone.utc).isoformat()

        # 1. IDE
        ide_c = os.path.join(ide_dir, 'ide_sess_1', '.system_generated', 'logs')
        os.makedirs(ide_c, exist_ok=True)
        with open(os.path.join(ide_c, 'transcript.jsonl'), 'w', encoding='utf-8') as f:
            f.write(json.dumps({'type': 'USER_INPUT', 'content': 'hello', 'created_at': now_iso}) + '\n')
            f.write(json.dumps({'type': 'PLANNER_RESPONSE', 'content': 'world', 'created_at': now_iso}) + '\n')

        # 2. CLI
        cli_c = os.path.join(cli_dir, 'cli_sess_1', '.system_generated', 'logs')
        os.makedirs(cli_c, exist_ok=True)
        with open(os.path.join(cli_c, 'transcript.jsonl'), 'w', encoding='utf-8') as f:
            f.write(json.dumps({'type': 'USER_INPUT', 'content': 'hello cli', 'created_at': now_iso}) + '\n')
            f.write(json.dumps({'type': 'PLANNER_RESPONSE', 'content': 'world cli', 'created_at': now_iso}) + '\n')

        # 3. Gemini CLI Official
        with open(os.path.join(gem_dir, 'gem_sess_1.jsonl'), 'w', encoding='utf-8') as f:
            f.write(json.dumps({'sessionId': 'gem_sess_1', 'startTime': now_iso}) + '\n')
            f.write(json.dumps({'id': '1', 'type': 'user', 'text': 'gemini user', 'timestamp': now_iso}) + '\n')
            f.write(json.dumps({'id': '2', 'type': 'gemini', 'text': 'gemini response', 'tokens': {'input': 100, 'output': 100, 'total': 200}, 'timestamp': now_iso}) + '\n')

        sources = {
            'ide': {'key': 'ide', 'label': 'Antigravity IDE', 'path': ide_dir, 'format': 'antigravity'},
            'cli': {'key': 'cli', 'label': 'Antigravity CLI', 'path': cli_dir, 'format': 'antigravity'},
            'gemini_cli': {'key': 'gemini_cli', 'label': 'Gemini CLI (Official)', 'path': gem_dir, 'format': 'gemini_cli'},
        }

        result = server.analyze_all_conversations(sources=sources)
        convs = result.get('conversations', [])
        summary = result.get('summary', {})

        self.assertEqual(len(convs), 3)
        sb = summary.get('source_breakdowns', {})
        self.assertIn('ide', sb)
        self.assertIn('cli', sb)
        self.assertIn('gemini_cli', sb)
        self.assertEqual(sb['ide']['label'], 'Antigravity IDE')
        self.assertEqual(sb['cli']['label'], 'Antigravity CLI')
        self.assertEqual(sb['gemini_cli']['label'], 'Gemini CLI (Official)')

        # Total 5H Gemini Quota combines all 3 sources
        g5_used = summary.get('quotas', {}).get('five_hour_window', {}).get('gemini', {}).get('tracked_used_tokens', 0)
        self.assertGreater(g5_used, 200)

    def test_codex_scanner_top_level_event_msg_filtering(self):
        sess_dir = os.path.join(self._temp_dir.name, 'codex_filter_sessions')
        os.makedirs(sess_dir, exist_ok=True)
        log_file = os.path.join(sess_dir, 'session-filtering.jsonl')

        with open(log_file, 'w', encoding='utf-8') as f:
            # Item with non-event_msg type, even if payload is token_count
            f.write(json.dumps({
                'type': 'response_item',
                'timestamp': '2026-08-24T12:00:00+00:00',
                'payload': {
                    'type': 'token_count',
                    'rate_limits': {'primary': {'window_minutes': 300, 'used_percent': 90.0}}
                }
            }) + '\n')
            # Item with event_msg type but wrong payload type
            f.write(json.dumps({
                'type': 'event_msg',
                'timestamp': '2026-08-24T12:01:00+00:00',
                'payload': {
                    'type': 'turn_context',
                    'rate_limits': {'primary': {'window_minutes': 300, 'used_percent': 85.0}}
                }
            }) + '\n')
            # Valid item
            f.write(json.dumps({
                'type': 'event_msg',
                'timestamp': '2026-08-24T12:02:00+00:00',
                'payload': {
                    'type': 'token_count',
                    'rate_limits': {'primary': {'window_minutes': 300, 'used_percent': 12.5}}
                }
            }) + '\n')

        res = server.scan_codex_rate_limits(sessions_dir=sess_dir)
        self.assertTrue(res['available'])
        self.assertEqual(len(res['windows']), 1)
        self.assertEqual(res['windows'][0]['used_percent'], 12.5)

    def test_codex_scanner_mtime_vs_event_timestamp_skew(self):
        import time
        sess_dir = os.path.join(self._temp_dir.name, 'codex_skew_sessions')
        os.makedirs(sess_dir, exist_ok=True)

        file_old_mtime = os.path.join(sess_dir, 'file_old_mtime.jsonl')
        file_new_mtime = os.path.join(sess_dir, 'file_new_mtime.jsonl')

        # file_old_mtime contains newer event timestamp
        with open(file_old_mtime, 'w', encoding='utf-8') as f:
            f.write(json.dumps({
                'type': 'event_msg',
                'timestamp': '2026-08-24T18:00:00+00:00',
                'payload': {
                    'type': 'token_count',
                    'rate_limits': {'primary': {'window_minutes': 300, 'used_percent': 5.0}}
                }
            }) + '\n')

        # file_new_mtime contains older event timestamp
        with open(file_new_mtime, 'w', encoding='utf-8') as f:
            f.write(json.dumps({
                'type': 'event_msg',
                'timestamp': '2026-08-24T10:00:00+00:00',
                'payload': {
                    'type': 'token_count',
                    'rate_limits': {'primary': {'window_minutes': 300, 'used_percent': 95.0}}
                }
            }) + '\n')

        t_now = time.time()
        os.utime(file_old_mtime, (t_now - 1000, t_now - 1000))
        os.utime(file_new_mtime, (t_now, t_now))

        res = server.scan_codex_rate_limits(sessions_dir=sess_dir)
        self.assertTrue(res['available'])
        self.assertEqual(len(res['windows']), 1)
        self.assertEqual(res['windows'][0]['used_percent'], 5.0)

    def test_codex_auto_models_distinct_attribution(self):
        sess_dir = os.path.join(self._temp_dir.name, 'codex_distinct_sessions')
        cache_path = os.path.join(self._temp_dir.name, 'test_distinct_cache.json')
        os.makedirs(sess_dir, exist_ok=True)

        now_str = datetime.now(timezone.utc).isoformat()

        # 1. Sol High session
        with open(os.path.join(sess_dir, 'sess_sol.jsonl'), 'w', encoding='utf-8') as f:
            f.write(json.dumps({'type': 'turn_context', 'payload': {'model': 'gpt-5.6-sol', 'effort': 'high'}}) + '\n')
            f.write(json.dumps({
                'type': 'event_msg',
                'timestamp': now_str,
                'payload': {
                    'type': 'token_count',
                    'info': {
                        'last_token_usage': {
                            'input_tokens': 1000,
                            'cached_input_tokens': 200,
                            'cache_write_input_tokens': 100,
                            'output_tokens': 500,
                            'reasoning_output_tokens': 100,
                            'total_tokens': 1500
                        }
                    }
                }
            }) + '\n')

        # 2. Terra High session
        with open(os.path.join(sess_dir, 'sess_terra.jsonl'), 'w', encoding='utf-8') as f:
            f.write(json.dumps({'type': 'turn_context', 'payload': {'model': 'gpt-5.6-terra', 'effort': 'high'}}) + '\n')
            f.write(json.dumps({
                'type': 'event_msg',
                'timestamp': now_str,
                'payload': {
                    'type': 'token_count',
                    'info': {
                        'last_token_usage': {
                            'input_tokens': 2000,
                            'cached_input_tokens': 400,
                            'cache_write_input_tokens': 200,
                            'output_tokens': 800,
                            'reasoning_output_tokens': 200,
                            'total_tokens': 2800
                        }
                    }
                }
            }) + '\n')

        # 3. Luna xhigh session
        with open(os.path.join(sess_dir, 'sess_luna_xhigh.jsonl'), 'w', encoding='utf-8') as f:
            f.write(json.dumps({'type': 'turn_context', 'payload': {'model': 'gpt-5.6-luna', 'effort': 'xhigh'}}) + '\n')
            f.write(json.dumps({
                'type': 'event_msg',
                'timestamp': now_str,
                'payload': {
                    'type': 'token_count',
                    'info': {
                        'last_token_usage': {
                            'input_tokens': 3000,
                            'cached_input_tokens': 1000,
                            'cache_write_input_tokens': 0,
                            'output_tokens': 1200,
                            'reasoning_output_tokens': 300,
                            'total_tokens': 4200
                        }
                    }
                }
            }) + '\n')

        # 4. Luna max session
        with open(os.path.join(sess_dir, 'sess_luna_max.jsonl'), 'w', encoding='utf-8') as f:
            f.write(json.dumps({'type': 'turn_context', 'payload': {'model': 'gpt-5.6-luna', 'effort': 'max'}}) + '\n')
            f.write(json.dumps({
                'type': 'event_msg',
                'timestamp': now_str,
                'payload': {
                    'type': 'token_count',
                    'info': {
                        'last_token_usage': {
                            'input_tokens': 4000,
                            'cached_input_tokens': 2000,
                            'cache_write_input_tokens': 500,
                            'output_tokens': 1500,
                            'reasoning_output_tokens': 500,
                            'total_tokens': 5500
                        }
                    }
                }
            }) + '\n')

        res = server.scan_codex_model_usage(sessions_dir=sess_dir, cache_file=cache_path)
        self.assertTrue(res['available'])
        models = res['models']

        self.assertIn('5.6 sol high', models)
        self.assertIn('5.6 terra high', models)
        self.assertIn('5.6 luna xhigh', models)
        self.assertIn('5.6 luna max', models)

        sol = models['5.6 sol high']
        self.assertEqual(sol['total_tokens'], 1500)
        self.assertEqual(sol['input_tokens'], 1000)
        self.assertEqual(sol['cached_input_tokens'], 200)
        self.assertEqual(sol['cache_write_input_tokens'], 100)
        self.assertEqual(sol['output_tokens'], 500)
        self.assertEqual(sol['thinking_tokens'], 100)
        self.assertEqual(sol['source_kind'], 'automatic')
        self.assertEqual(sol['source_label'], 'Automatic Codex logs')
        self.assertEqual(sol['sessions'], 1)
        self.assertEqual(sol['responses'], 1)

        terra = models['5.6 terra high']
        self.assertEqual(terra['total_tokens'], 2800)
        self.assertEqual(terra['input_tokens'], 2000)
        self.assertEqual(terra['output_tokens'], 800)

        luna_x = models['5.6 luna xhigh']
        self.assertEqual(luna_x['total_tokens'], 4200)
        self.assertEqual(luna_x['reasoning_effort'], 'xhigh')

        luna_max = models['5.6 luna max']
        self.assertEqual(luna_max['total_tokens'], 5500)
        self.assertEqual(luna_max['reasoning_effort'], 'max')

    def test_single_file_switching_model_and_effort(self):
        sess_dir = os.path.join(self._temp_dir.name, 'codex_switch_sessions')
        cache_path = os.path.join(self._temp_dir.name, 'test_switch_cache.json')
        os.makedirs(sess_dir, exist_ok=True)
        now_str = datetime.now(timezone.utc).isoformat()

        with open(os.path.join(sess_dir, 'multi_switch.jsonl'), 'w', encoding='utf-8') as f:
            # Turn 1: Sol High
            f.write(json.dumps({'type': 'turn_context', 'payload': {'model': 'gpt-5.6-sol', 'effort': 'high'}}) + '\n')
            f.write(json.dumps({
                'type': 'event_msg',
                'timestamp': now_str,
                'payload': {
                    'type': 'token_count',
                    'info': {'last_token_usage': {'input_tokens': 100, 'output_tokens': 50, 'total_tokens': 150}}
                }
            }) + '\n')
            f.write(json.dumps({
                'type': 'response_item',
                'payload': {'type': 'custom_tool_call'}
            }) + '\n')

            # Turn 2: Terra Medium (standard)
            f.write(json.dumps({'type': 'turn_context', 'payload': {'model': 'gpt-5.6-terra', 'effort': 'medium'}}) + '\n')
            f.write(json.dumps({
                'type': 'event_msg',
                'timestamp': now_str,
                'payload': {
                    'type': 'token_count',
                    'info': {'last_token_usage': {'input_tokens': 200, 'output_tokens': 100, 'total_tokens': 300}}
                }
            }) + '\n')

            # Turn 3: Sol xhigh
            f.write(json.dumps({'type': 'turn_context', 'payload': {'model': 'gpt-5.6-sol', 'effort': 'xhigh'}}) + '\n')
            f.write(json.dumps({
                'type': 'event_msg',
                'timestamp': now_str,
                'payload': {
                    'type': 'token_count',
                    'info': {'last_token_usage': {'input_tokens': 300, 'output_tokens': 150, 'total_tokens': 450}}
                }
            }) + '\n')

        res = server.scan_codex_model_usage(sessions_dir=sess_dir, cache_file=cache_path)
        self.assertTrue(res['available'])
        models = res['models']

        self.assertEqual(models['5.6 sol high']['total_tokens'], 150)
        self.assertEqual(models['5.6 sol high']['tool_calls'], 1)
        self.assertEqual(models['5.6 sol high']['responses'], 1)

        self.assertEqual(models['5.6 terra standard']['total_tokens'], 300)
        self.assertEqual(models['5.6 terra standard']['tool_calls'], 0)
        self.assertEqual(models['5.6 terra standard']['responses'], 1)

        self.assertEqual(models['5.6 sol xhigh']['total_tokens'], 450)
        self.assertEqual(models['5.6 sol xhigh']['tool_calls'], 0)
        self.assertEqual(models['5.6 sol xhigh']['responses'], 1)

    def test_incremental_cache_unchanged_append_and_truncation(self):
        sess_dir = os.path.join(self._temp_dir.name, 'codex_inc_sessions')
        cache_path = os.path.join(self._temp_dir.name, 'test_inc_cache.json')
        os.makedirs(sess_dir, exist_ok=True)
        log_file = os.path.join(sess_dir, 'growing_session.jsonl')
        now_str = datetime.now(timezone.utc).isoformat()

        # Step 1: Initial creation (100 tokens)
        with open(log_file, 'w', encoding='utf-8') as f:
            f.write(json.dumps({'type': 'turn_context', 'payload': {'model': 'gpt-5.6-sol', 'effort': 'high'}}) + '\n')
            f.write(json.dumps({
                'type': 'event_msg',
                'timestamp': now_str,
                'payload': {
                    'type': 'token_count',
                    'info': {'last_token_usage': {'input_tokens': 70, 'output_tokens': 30, 'total_tokens': 100}}
                }
            }) + '\n')

        res1 = server.scan_codex_model_usage(sessions_dir=sess_dir, cache_file=cache_path)
        self.assertEqual(res1['models']['5.6 sol high']['total_tokens'], 100)
        self.assertEqual(res1['diagnostics']['files_rebuilt'], 1)
        self.assertEqual(res1['diagnostics']['files_cached'], 0)

        # Step 2: Rescan unchanged -> cache hit
        res2 = server.scan_codex_model_usage(sessions_dir=sess_dir, cache_file=cache_path)
        self.assertEqual(res2['models']['5.6 sol high']['total_tokens'], 100)
        self.assertEqual(res2['diagnostics']['files_cached'], 1)
        self.assertEqual(res2['diagnostics']['files_rebuilt'], 0)
        self.assertEqual(res2['diagnostics']['files_appended'], 0)

        # Step 3: Append 200 tokens
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps({
                'type': 'event_msg',
                'timestamp': now_str,
                'payload': {
                    'type': 'token_count',
                    'info': {'last_token_usage': {'input_tokens': 140, 'output_tokens': 60, 'total_tokens': 200}}
                }
            }) + '\n')

        res3 = server.scan_codex_model_usage(sessions_dir=sess_dir, cache_file=cache_path)
        self.assertEqual(res3['models']['5.6 sol high']['total_tokens'], 300)
        self.assertEqual(res3['diagnostics']['files_appended'], 1)

        # Step 4: Truncate file to contain only 50 tokens
        with open(log_file, 'w', encoding='utf-8') as f:
            f.write(json.dumps({'type': 'turn_context', 'payload': {'model': 'gpt-5.6-sol', 'effort': 'high'}}) + '\n')
            f.write(json.dumps({
                'type': 'event_msg',
                'timestamp': now_str,
                'payload': {
                    'type': 'token_count',
                    'info': {'last_token_usage': {'input_tokens': 35, 'output_tokens': 15, 'total_tokens': 50}}
                }
            }) + '\n')

        res4 = server.scan_codex_model_usage(sessions_dir=sess_dir, cache_file=cache_path)
        self.assertEqual(res4['models']['5.6 sol high']['total_tokens'], 50)
        self.assertEqual(res4['diagnostics']['files_rebuilt'], 1)

    def test_rolling_cutoffs_malformed_oversized_corrupt_cache_and_bounds(self):
        sess_dir = os.path.join(self._temp_dir.name, 'codex_bounds_sessions')
        cache_path = os.path.join(self._temp_dir.name, 'test_bounds_cache.json')
        os.makedirs(sess_dir, exist_ok=True)

        now_dt = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)
        ts_10d_ago = '2026-08-14T12:00:00+00:00'
        ts_2d_ago = '2026-08-22T12:00:00+00:00'
        ts_1h_ago = '2026-08-24T11:00:00+00:00'

        log_file = os.path.join(sess_dir, 'mixed_session.jsonl')
        with open(log_file, 'w', encoding='utf-8') as f:
            f.write(json.dumps({'type': 'turn_context', 'payload': {'model': 'gpt-5.6-sol', 'effort': 'high'}}) + '\n')
            # 10 days ago (all-time only)
            f.write(json.dumps({
                'type': 'event_msg',
                'timestamp': ts_10d_ago,
                'payload': {'type': 'token_count', 'info': {'last_token_usage': {'input_tokens': 1000, 'output_tokens': 0, 'total_tokens': 1000}}}
            }) + '\n')
            # Malformed JSON line
            f.write('{"type": "event_msg", "payload": {MALFORMED JSON...\n')
            # 2 days ago (weekly + all-time)
            f.write(json.dumps({
                'type': 'event_msg',
                'timestamp': ts_2d_ago,
                'payload': {'type': 'token_count', 'info': {'last_token_usage': {'input_tokens': 500, 'output_tokens': 0, 'total_tokens': 500}}}
            }) + '\n')
            # Oversized line dummy
            f.write('{"type": "ignore", "data": "' + 'x' * 500 + '"}\n')
            # 1 hour ago (5h + weekly + all-time)
            f.write(json.dumps({
                'type': 'event_msg',
                'timestamp': ts_1h_ago,
                'payload': {'type': 'token_count', 'info': {'last_token_usage': {'input_tokens': 200, 'output_tokens': 0, 'total_tokens': 200}}}
            }) + '\n')

        # Write corrupt cache
        with open(cache_path, 'w', encoding='utf-8') as f:
            f.write('{corrupted json content...')

        res = server.scan_codex_model_usage(sessions_dir=sess_dir, cache_file=cache_path, now=now_dt)
        self.assertTrue(res['available'])
        m = res['models']['5.6 sol high']

        self.assertEqual(m['total_tokens'], 1700)
        self.assertEqual(m['weekly_tokens'], 700)     # 500 + 200
        self.assertEqual(m['five_hour_tokens'], 200)  # 200 only

    def test_automatic_supersedes_manual_and_unmatched_manual_fallback_remains(self):
        # 1. Prepare manual codex_usage data
        manual_codex_data = {
            'weekly_limit_tokens': 5_000_000,
            'models': {
                '5.6 sol high': {
                    'total_tokens': 999_999,
                    'weekly_tokens': 888_888,
                    'input_tokens': 500_000,
                    'output_tokens': 499_999,
                    'cost_usd': 20.0,
                    'sessions': 5,
                    'responses': 50,
                    'tool_calls': 10
                },
                'o3 high': {
                    'total_tokens': 5_000,
                    'weekly_tokens': 2_500,
                    'input_tokens': 3_000,
                    'output_tokens': 2_000,
                    'cost_usd': 0.05,
                    'sessions': 1,
                    'responses': 5,
                    'tool_calls': 2
                }
            }
        }

        # 2. Prepare automatic scan result with Sol High
        auto_scan_result = {
            'available': True,
            'source': 'local_session_logs',
            'source_label': 'Local Codex Session Logs',
            'observed_at': '2026-08-24T12:00:00+00:00',
            'models': {
                '5.6 sol high': {
                    'model_id': 'gpt-5.6-sol',
                    'reasoning_effort': 'high',
                    'source_kind': 'automatic',
                    'source_label': 'Automatic Codex logs',
                    'total_tokens': 1500,
                    'weekly_tokens': 1500,
                    'five_hour_tokens': 500,
                    'input_tokens': 1000,
                    'cached_input_tokens': 200,
                    'cache_write_input_tokens': 0,
                    'output_tokens': 500,
                    'thinking_tokens': 100,
                    'cost_usd': 0.014,
                    'weekly_cost_usd': 0.014,
                    'cost_known': True,
                    'weekly_cost_known': True,
                    'sessions': 1,
                    'responses': 1,
                    'tool_calls': 0,
                    'last_updated': '2026-08-24T12:00:00+00:00'
                }
            },
            'diagnostics': {'files_scanned': 1}
        }

        manual_codex_data['automatic_model_usage'] = auto_scan_result

        breakdown = server.build_models_breakdown({}, [], codex_data=manual_codex_data)

        breakdown_by_id = {row['model_id']: row for row in breakdown}

        # Verify Sol High was superseded by automatic row (total_tokens is 1500, not 999999)
        self.assertIn('5.6 sol high', breakdown_by_id)
        sol_row = breakdown_by_id['5.6 sol high']
        self.assertEqual(sol_row['source_kind'], 'automatic')
        self.assertEqual(sol_row['source_label'], 'Automatic Codex logs')
        self.assertEqual(sol_row['total_tokens'], 1500)

        # Verify o3 high remains as manual fallback
        self.assertIn('o3 high', breakdown_by_id)
        o3_row = breakdown_by_id['o3 high']
        self.assertEqual(o3_row['source_kind'], 'manual_fallback')
        self.assertEqual(o3_row['source_label'], 'Manual fallback')
        self.assertEqual(o3_row['total_tokens'], 5000)

    def test_diagnostics_contain_no_sensitive_data(self):
        sess_dir = os.path.join(self._temp_dir.name, 'codex_diag_sessions')
        os.makedirs(sess_dir, exist_ok=True)
        with open(os.path.join(sess_dir, 'secret_session.jsonl'), 'w', encoding='utf-8') as f:
            f.write(json.dumps({'type': 'turn_context', 'payload': {'model': 'gpt-5.6-sol', 'effort': 'high'}}) + '\n')
            f.write(json.dumps({
                'type': 'event_msg',
                'timestamp': '2026-08-24T12:00:00+00:00',
                'payload': {
                    'type': 'token_count',
                    'info': {'last_token_usage': {'input_tokens': 100, 'output_tokens': 50, 'total_tokens': 150}},
                    'secret_key': 'super_secret_password_123',
                    'user_prompt': 'Do not reveal this prompt in diagnostics'
                }
            }) + '\n')

        res = server.scan_codex_model_usage(sessions_dir=sess_dir)
        diag = res['diagnostics']
        diag_json = json.dumps(diag)

        self.assertNotIn('super_secret_password_123', diag_json)
        self.assertNotIn('Do not reveal this prompt', diag_json)
        self.assertNotIn(sess_dir, diag_json)

    def test_codex_scanner_max_total_bytes_enforced_and_diagnostics_truncated(self):
        sess_dir = os.path.join(self._temp_dir.name, 'codex_bytes_budget_sessions')
        cache_path = os.path.join(self._temp_dir.name, 'test_bytes_budget_cache.json')
        os.makedirs(sess_dir, exist_ok=True)

        # Create two files of ~250 bytes each
        for i in range(2):
            f_path = os.path.join(sess_dir, f'session_{i}.jsonl')
            with open(f_path, 'w', encoding='utf-8') as f:
                f.write(json.dumps({'type': 'turn_context', 'payload': {'model': 'gpt-5.6-sol', 'effort': 'high'}}) + '\n')
                f.write(json.dumps({
                    'type': 'event_msg',
                    'timestamp': '2026-08-24T12:00:00+00:00',
                    'payload': {
                        'type': 'token_count',
                        'info': {'last_token_usage': {'input_tokens': 100, 'output_tokens': 50, 'total_tokens': 150}}
                    }
                }) + '\n')

        # Limit max_total_bytes to 100 bytes (less than one file)
        res = server.scan_codex_model_usage(sessions_dir=sess_dir, cache_file=cache_path, max_total_bytes=100)
        self.assertTrue(res['diagnostics']['truncated'])
        self.assertFalse(res['diagnostics']['coverage_complete'])
        self.assertLessEqual(res['diagnostics']['bytes_read'], 100)

    def test_codex_scanner_max_files_selects_newest_candidates(self):
        import time
        sess_dir = os.path.join(self._temp_dir.name, 'codex_newest_sessions')
        cache_path = os.path.join(self._temp_dir.name, 'test_newest_cache.json')
        os.makedirs(sess_dir, exist_ok=True)

        now_t = time.time()
        # Create 4 files with different mtimes and distinct token counts
        files_config = [
            ('f_oldest.jsonl', now_t - 400, 100),
            ('f_newer.jsonl', now_t - 100, 400),
            ('f_old.jsonl', now_t - 300, 200),
            ('f_newest.jsonl', now_t, 500),
        ]

        for fname, mtime_val, toks in files_config:
            p = os.path.join(sess_dir, fname)
            with open(p, 'w', encoding='utf-8') as f:
                f.write(json.dumps({'type': 'turn_context', 'payload': {'model': 'gpt-5.6-sol', 'effort': 'high'}}) + '\n')
                f.write(json.dumps({
                    'type': 'event_msg',
                    'timestamp': '2026-08-24T12:00:00+00:00',
                    'payload': {
                        'type': 'token_count',
                        'info': {'last_token_usage': {'input_tokens': toks, 'output_tokens': 0, 'total_tokens': toks}}
                    }
                }) + '\n')
            os.utime(p, (mtime_val, mtime_val))

        # Max files = 2 should strictly select f_newest (500) and f_newer (400), total = 900
        res = server.scan_codex_model_usage(sessions_dir=sess_dir, cache_file=cache_path, max_files=2)
        self.assertTrue(res['diagnostics']['truncated'])
        self.assertFalse(res['diagnostics']['coverage_complete'])
        self.assertEqual(res['models']['5.6 sol high']['total_tokens'], 900)

    def test_codex_default_scanner_includes_archives_and_keeps_all_time_monotonic(self):
        codex_root = os.path.join(self._temp_dir.name, '.codex')
        sessions_dir = os.path.join(codex_root, 'sessions')
        archived_dir = os.path.join(codex_root, 'archived_sessions')
        os.makedirs(sessions_dir, exist_ok=True)
        os.makedirs(archived_dir, exist_ok=True)
        server.CODEX_SESSIONS_DIR = sessions_dir

        def write_session(path, model, effort, request_id, total_tokens):
            with open(path, 'w', encoding='utf-8') as f:
                f.write(json.dumps({
                    'type': 'turn_context',
                    'payload': {'model': model, 'effort': effort},
                }) + '\n')
                f.write(json.dumps({
                    'type': 'event_msg',
                    'timestamp': '2026-09-09T02:00:00+00:00',
                    'payload': {
                        'type': 'token_count',
                        'request_id': request_id,
                        'info': {'last_token_usage': {
                            'input_tokens': total_tokens,
                            'output_tokens': 0,
                            'total_tokens': total_tokens,
                        }},
                    },
                }) + '\n')

        active_path = os.path.join(sessions_dir, 'active-session.jsonl')
        archived_path = os.path.join(archived_dir, 'archived-session.jsonl')
        write_session(active_path, 'gpt-5.6-sol', 'high', 'active-1', 100)
        write_session(archived_path, 'gpt-5.6-terra', 'high', 'archived-1', 200)

        first = server.scan_codex_model_usage()
        self.assertEqual(first['models']['5.6 sol high']['total_tokens'], 100)
        self.assertEqual(first['models']['5.6 terra high']['total_tokens'], 200)
        self.assertTrue(first['diagnostics']['includes_archived_sessions'])
        self.assertEqual(first['diagnostics']['files_discovered'], 2)
        self.assertEqual(first['diagnostics']['files_in_ledger'], 2)

        # Moving a task from active to archived must reuse the basename cache
        # key and leave the all-time ledger unchanged.
        moved_path = os.path.join(archived_dir, os.path.basename(active_path))
        os.replace(active_path, moved_path)
        second = server.scan_codex_model_usage()
        self.assertEqual(second['models']['5.6 sol high']['total_tokens'], 100)
        self.assertEqual(second['models']['5.6 terra high']['total_tokens'], 200)
        self.assertEqual(second['diagnostics']['files_in_ledger'], 2)

        # If a historical file later disappears locally, the append-preserving
        # all-time ledger still must not shrink.
        os.remove(archived_path)
        third = server.scan_codex_model_usage()
        self.assertEqual(third['models']['5.6 sol high']['total_tokens'], 100)
        self.assertEqual(third['models']['5.6 terra high']['total_tokens'], 200)
        self.assertEqual(third['diagnostics']['retained_historical_files'], 1)

        with open(server.CODEX_MODELS_CACHE_FILE, 'r', encoding='utf-8') as f:
            cache = json.load(f)
        self.assertEqual(set(cache['files']), {'active-session.jsonl', 'archived-session.jsonl'})

    def test_codex_scanner_oversized_line_is_discarded_in_bounded_chunks(self):
        sess_dir = os.path.join(self._temp_dir.name, 'codex_line_bound_sessions')
        cache_path = os.path.join(self._temp_dir.name, 'test_line_bound_cache.json')
        os.makedirs(sess_dir, exist_ok=True)
        log_file = os.path.join(sess_dir, 'bounded.jsonl')
        with open(log_file, 'w', encoding='utf-8') as f:
            f.write(json.dumps({'type': 'turn_context', 'payload': {'model': 'gpt-5.6-sol', 'effort': 'high'}}) + '\n')
            f.write(json.dumps({'type': 'ignore', 'data': 'x' * 2048}) + '\n')
            f.write(json.dumps({'type': 'event_msg', 'timestamp': '2026-08-24T12:00:00+00:00',
                                'payload': {'type': 'token_count', 'info': {'last_token_usage': {
                                    'input_tokens': 60, 'output_tokens': 40, 'total_tokens': 100,
                                }}}}) + '\n')

        file_size = os.path.getsize(log_file)
        res = server.scan_codex_model_usage(
            sessions_dir=sess_dir,
            cache_file=cache_path,
            max_line_bytes=256,
            max_total_bytes=file_size,
        )
        self.assertTrue(res['diagnostics']['coverage_complete'])
        self.assertEqual(res['diagnostics']['bytes_read'], file_size)
        self.assertEqual(res['models']['5.6 sol high']['total_tokens'], 100)

    def test_codex_scanner_cached_aggregates_preserved_on_truncated_or_skipped_discovery(self):
        import time
        sess_dir = os.path.join(self._temp_dir.name, 'codex_preserve_sessions')
        cache_path = os.path.join(self._temp_dir.name, 'test_preserve_cache.json')
        os.makedirs(sess_dir, exist_ok=True)

        now_t = time.time()
        # Step 1: Create File 1 (Sol High, 100 tokens, older) and File 2 (Terra High, 200 tokens, older)
        p1 = os.path.join(sess_dir, 'file1.jsonl')
        with open(p1, 'w', encoding='utf-8') as f:
            f.write(json.dumps({'type': 'turn_context', 'payload': {'model': 'gpt-5.6-sol', 'effort': 'high'}}) + '\n')
            f.write(json.dumps({
                'type': 'event_msg',
                'timestamp': '2026-08-24T12:00:00+00:00',
                'payload': {'type': 'token_count', 'info': {'last_token_usage': {'input_tokens': 100, 'output_tokens': 0, 'total_tokens': 100}}}
            }) + '\n')
        os.utime(p1, (now_t - 200, now_t - 200))

        p2 = os.path.join(sess_dir, 'file2.jsonl')
        with open(p2, 'w', encoding='utf-8') as f:
            f.write(json.dumps({'type': 'turn_context', 'payload': {'model': 'gpt-5.6-terra', 'effort': 'high'}}) + '\n')
            f.write(json.dumps({
                'type': 'event_msg',
                'timestamp': '2026-08-24T12:00:00+00:00',
                'payload': {'type': 'token_count', 'info': {'last_token_usage': {'input_tokens': 200, 'output_tokens': 0, 'total_tokens': 200}}}
            }) + '\n')
        os.utime(p2, (now_t - 100, now_t - 100))

        res1 = server.scan_codex_model_usage(sessions_dir=sess_dir, cache_file=cache_path, max_files=10)
        self.assertEqual(res1['models']['5.6 sol high']['total_tokens'], 100)
        self.assertEqual(res1['models']['5.6 terra high']['total_tokens'], 200)

        # Step 2: Create File 3 (Luna High, 300 tokens, newest)
        p3 = os.path.join(sess_dir, 'file3.jsonl')
        with open(p3, 'w', encoding='utf-8') as f:
            f.write(json.dumps({'type': 'turn_context', 'payload': {'model': 'gpt-5.6-luna', 'effort': 'high'}}) + '\n')
            f.write(json.dumps({
                'type': 'event_msg',
                'timestamp': '2026-08-24T12:00:00+00:00',
                'payload': {'type': 'token_count', 'info': {'last_token_usage': {'input_tokens': 300, 'output_tokens': 0, 'total_tokens': 300}}}
            }) + '\n')
        os.utime(p3, (now_t, now_t))

        # Scan with max_files=1 (only file3 is visited, file1 and file2 are skipped by bounds)
        res2 = server.scan_codex_model_usage(sessions_dir=sess_dir, cache_file=cache_path, max_files=1)
        self.assertTrue(res2['diagnostics']['truncated'])
        self.assertFalse(res2['diagnostics']['coverage_complete'])

        # Cached aggregates for file1 and file2 MUST be preserved and included!
        self.assertIn('5.6 sol high', res2['models'])
        self.assertIn('5.6 terra high', res2['models'])
        self.assertIn('5.6 luna high', res2['models'])
        self.assertEqual(res2['models']['5.6 sol high']['total_tokens'], 100)
        self.assertEqual(res2['models']['5.6 terra high']['total_tokens'], 200)
        self.assertEqual(res2['models']['5.6 luna high']['total_tokens'], 300)

        # A file-size bound is also incomplete discovery and must not evict cached totals.
        res3 = server.scan_codex_model_usage(
            sessions_dir=sess_dir,
            cache_file=cache_path,
            max_file_size=1,
        )
        self.assertTrue(res3['diagnostics']['truncated'])
        self.assertFalse(res3['diagnostics']['coverage_complete'])
        self.assertEqual(res3['models']['5.6 sol high']['total_tokens'], 100)
        self.assertEqual(res3['models']['5.6 terra high']['total_tokens'], 200)
        self.assertEqual(res3['models']['5.6 luna high']['total_tokens'], 300)

    def test_codex_scanner_same_size_rewrites_prefix_mismatches_and_partial_trailing_lines(self):
        sess_dir = os.path.join(self._temp_dir.name, 'codex_fingerprint_sessions')
        cache_path = os.path.join(self._temp_dir.name, 'test_fingerprint_cache.json')
        os.makedirs(sess_dir, exist_ok=True)
        log_file = os.path.join(sess_dir, 'session.jsonl')

        # 1. Partial trailing line test
        with open(log_file, 'wb') as f:
            f.write(json.dumps({'type': 'turn_context', 'payload': {'model': 'gpt-5.6-sol', 'effort': 'high'}}).encode('utf-8') + b'\n')
            f.write(json.dumps({
                'type': 'event_msg',
                'timestamp': '2026-08-24T12:00:00+00:00',
                'payload': {'type': 'token_count', 'request_id': 'req-001', 'info': {'last_token_usage': {'input_tokens': 70, 'output_tokens': 30, 'total_tokens': 100}}}
            }).encode('utf-8') + b'\n')
            # Partial trailing line without newline (distinct request_id req-002)
            f.write(json.dumps({
                'type': 'event_msg',
                'timestamp': '2026-08-24T12:00:00+00:00',
                'payload': {'type': 'token_count', 'request_id': 'req-002', 'info': {'last_token_usage': {'input_tokens': 80, 'output_tokens': 20, 'total_tokens': 100}}}
            }).encode('utf-8'))

        res1 = server.scan_codex_model_usage(sessions_dir=sess_dir, cache_file=cache_path)
        self.assertEqual(res1['models']['5.6 sol high']['total_tokens'], 100)

        # Complete the trailing line by adding newline
        with open(log_file, 'ab') as f:
            f.write(b'\n')

        res2 = server.scan_codex_model_usage(sessions_dir=sess_dir, cache_file=cache_path)
        self.assertEqual(res2['models']['5.6 sol high']['total_tokens'], 200)
        self.assertEqual(res2['diagnostics']['files_appended'], 1)

        # Unchanged rescan assertion for exactly-once partial-line handling
        res2_cached = server.scan_codex_model_usage(sessions_dir=sess_dir, cache_file=cache_path)
        self.assertEqual(res2_cached['models']['5.6 sol high']['total_tokens'], 200)
        self.assertEqual(res2_cached['diagnostics']['files_cached'], 1)
        self.assertEqual(res2_cached['diagnostics']['files_appended'], 0)

        # 2. Same-size rewrite & prefix mismatch test
        orig_size = os.path.getsize(log_file)
        # Construct completely different content matching exact same size or prefix mismatch
        rewrite_content = json.dumps({'type': 'turn_context', 'payload': {'model': 'gpt-5.6-terra', 'effort': 'medium'}}).encode('utf-8') + b'\n'
        rewrite_content += json.dumps({
            'type': 'event_msg',
            'timestamp': '2026-08-24T12:00:00+00:00',
            'payload': {'type': 'token_count', 'request_id': 'req-003', 'info': {'last_token_usage': {'input_tokens': 30, 'output_tokens': 20, 'total_tokens': 50}}}
        }).encode('utf-8') + b'\n'
        # Pad with whitespace to match exact size
        if len(rewrite_content) < orig_size:
            rewrite_content += b' ' * (orig_size - len(rewrite_content))

        with open(log_file, 'wb') as f:
            f.write(rewrite_content)
        # Ensure the metadata changes just as it does for a normal editor rewrite.
        rewritten_mtime = os.path.getmtime(log_file) + 2
        os.utime(log_file, (rewritten_mtime, rewritten_mtime))

        res3 = server.scan_codex_model_usage(sessions_dir=sess_dir, cache_file=cache_path)
        self.assertEqual(res3['diagnostics']['files_rebuilt'], 1)
        self.assertNotIn('5.6 sol high', res3['models'])
        self.assertIn('5.6 terra standard', res3['models'])
        self.assertEqual(res3['models']['5.6 terra standard']['total_tokens'], 50)

    def test_codex_scanner_repeated_emissions_for_single_request_dedup_and_later_identical_request_counted(self):
        sess_dir = os.path.join(self._temp_dir.name, 'codex_dedup_sessions')
        cache_path = os.path.join(self._temp_dir.name, 'test_dedup_cache.json')
        os.makedirs(sess_dir, exist_ok=True)
        log_file = os.path.join(sess_dir, 'session_dedup.jsonl')

        with open(log_file, 'w', encoding='utf-8') as f:
            # Turn 1: Request 1
            f.write(json.dumps({'type': 'turn_context', 'payload': {'model': 'gpt-5.6-sol', 'effort': 'high'}}) + '\n')
            f.write(json.dumps({
                'type': 'event_msg',
                'timestamp': '2026-08-24T12:00:00+00:00',
                'payload': {
                    'type': 'token_count',
                    'request_id': 'req-001',
                    'info': {'last_token_usage': {'input_tokens': 100, 'output_tokens': 50, 'total_tokens': 150}}
                }
            }) + '\n')
            # Repeated emission for Request 1 (identical snapshot around status/tool)
            f.write(json.dumps({
                'type': 'event_msg',
                'timestamp': '2026-08-24T12:00:01+00:00',
                'payload': {
                    'type': 'token_count',
                    'request_id': 'req-001',
                    'info': {'last_token_usage': {'input_tokens': 100, 'output_tokens': 50, 'total_tokens': 150}}
                }
            }) + '\n')

            # Turn 2: Request 2 (legitimate separate request with identical token numbers)
            f.write(json.dumps({'type': 'turn_context', 'payload': {'model': 'gpt-5.6-sol', 'effort': 'high'}}) + '\n')
            f.write(json.dumps({
                'type': 'event_msg',
                'timestamp': '2026-08-24T12:01:00+00:00',
                'payload': {
                    'type': 'token_count',
                    'request_id': 'req-002',
                    'info': {'last_token_usage': {'input_tokens': 100, 'output_tokens': 50, 'total_tokens': 150}}
                }
            }) + '\n')

        res = server.scan_codex_model_usage(sessions_dir=sess_dir, cache_file=cache_path)
        sol = res['models']['5.6 sol high']
        # Request 1 (150) + Request 2 (150) = 300 tokens, exactly 2 responses
        self.assertEqual(sol['total_tokens'], 300)
        self.assertEqual(sol['responses'], 2)

    def test_codex_scanner_request_id_progress_counts_only_delta_and_one_response(self):
        sess_dir = os.path.join(self._temp_dir.name, 'codex_request_progress_sessions')
        cache_path = os.path.join(self._temp_dir.name, 'test_request_progress_cache.json')
        os.makedirs(sess_dir, exist_ok=True)
        log_file = os.path.join(sess_dir, 'session.jsonl')

        def event(total):
            return {
                'type': 'event_msg', 'timestamp': '2026-08-24T12:00:00+00:00',
                'payload': {'type': 'token_count', 'request_id': 'req-progress',
                            'info': {'last_token_usage': {
                                'input_tokens': total - 20, 'output_tokens': 20,
                                'total_tokens': total}}}
            }

        with open(log_file, 'w', encoding='utf-8') as f:
            f.write(json.dumps({'type': 'turn_context', 'payload': {
                'model': 'gpt-5.6-sol', 'effort': 'high'}}) + '\n')
            f.write(json.dumps(event(100)) + '\n')
            f.write(json.dumps(event(150)) + '\n')
            f.write(json.dumps(event(150)) + '\n')

        res = server.scan_codex_model_usage(sessions_dir=sess_dir, cache_file=cache_path)
        sol = res['models']['5.6 sol high']
        self.assertEqual(sol['total_tokens'], 150)
        self.assertEqual(sol['responses'], 1)

    def test_codex_scanner_no_id_dedup_uses_cumulative_identity_and_updates_fallback_baseline(self):
        sess_dir = os.path.join(self._temp_dir.name, 'codex_no_id_dedup_sessions')
        cache_path = os.path.join(self._temp_dir.name, 'test_no_id_dedup_cache.json')
        os.makedirs(sess_dir, exist_ok=True)
        log_file = os.path.join(sess_dir, 'session_no_id.jsonl')

        def token_event(last_usage, cumulative):
            info = {'total_token_usage': cumulative}
            if last_usage is not None:
                info['last_token_usage'] = last_usage
            return {'type': 'event_msg', 'timestamp': '2026-08-24T12:00:00+00:00',
                    'payload': {'type': 'token_count', 'info': info}}

        usage_100 = {'input_tokens': 70, 'output_tokens': 30, 'total_tokens': 100}
        cumulative_100 = {'input_tokens': 70, 'output_tokens': 30, 'total_tokens': 100}
        cumulative_200 = {'input_tokens': 140, 'output_tokens': 60, 'total_tokens': 200}
        cumulative_250 = {'input_tokens': 175, 'output_tokens': 75, 'total_tokens': 250}
        with open(log_file, 'w', encoding='utf-8') as f:
            f.write(json.dumps({'type': 'turn_context', 'payload': {'model': 'gpt-5.6-sol', 'effort': 'high'}}) + '\n')
            f.write(json.dumps(token_event(usage_100, cumulative_100)) + '\n')
            f.write(json.dumps(token_event(usage_100, cumulative_100)) + '\n')
            # A distinct later request can legitimately have the same last-usage shape.
            f.write(json.dumps(token_event(usage_100, cumulative_200)) + '\n')
            # Missing last_token_usage falls back only to the delta since cumulative_200.
            f.write(json.dumps(token_event(None, cumulative_250)) + '\n')

        res = server.scan_codex_model_usage(sessions_dir=sess_dir, cache_file=cache_path)
        sol = res['models']['5.6 sol high']
        self.assertEqual(sol['total_tokens'], 250)
        self.assertEqual(sol['input_tokens'], 175)
        self.assertEqual(sol['output_tokens'], 75)
        self.assertEqual(sol['responses'], 3)

        # Syntactically valid but malformed cache dedup entries must be ignored safely.
        with open(cache_path, 'r', encoding='utf-8') as f:
            cache_data = json.load(f)
        entry = next(iter(cache_data['files'].values()))
        entry['seen_request_ids'] = [{'bad': ['unhashable']}, ['req', ['bad']]]
        with open(cache_path, 'w', encoding='utf-8') as f:
            json.dump(cache_data, f)
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps({'type': 'event_msg', 'timestamp': '2026-08-24T12:01:00+00:00',
                                'payload': {'type': 'token_count', 'request_id': 'req-new',
                                            'info': {'last_token_usage': usage_100,
                                                     'total_token_usage': {'input_tokens': 245, 'output_tokens': 105, 'total_tokens': 350}}}}) + '\n')
        res2 = server.scan_codex_model_usage(sessions_dir=sess_dir, cache_file=cache_path)
        self.assertEqual(res2['models']['5.6 sol high']['total_tokens'], 350)

    def test_codex_scanner_no_id_cumulative_duplicate_survives_turn_boundaries_and_cache(self):
        sess_dir = os.path.join(self._temp_dir.name, 'codex_cross_turn_dedup_sessions')
        cache_path = os.path.join(self._temp_dir.name, 'test_cross_turn_dedup_cache.json')
        os.makedirs(sess_dir, exist_ok=True)
        log_file = os.path.join(sess_dir, 'session.jsonl')
        usage = {'input_tokens': 70, 'output_tokens': 30, 'total_tokens': 100}
        event = {'type': 'event_msg', 'timestamp': '2026-08-24T12:00:00+00:00',
                 'payload': {'type': 'token_count', 'info': {
                     'last_token_usage': usage, 'total_token_usage': usage}}}
        turn = {'type': 'turn_context', 'payload': {
            'model': 'gpt-5.6-sol', 'effort': 'high'}}

        with open(log_file, 'w', encoding='utf-8') as f:
            f.write(json.dumps(turn) + '\n')
            f.write(json.dumps(event) + '\n')
            f.write(json.dumps({'type': 'user_message', 'payload': {'text': 'next'}}) + '\n')
            f.write(json.dumps(turn) + '\n')
            f.write(json.dumps(event) + '\n')

        first = server.scan_codex_model_usage(sessions_dir=sess_dir, cache_file=cache_path)
        self.assertEqual(first['models']['5.6 sol high']['total_tokens'], 100)
        self.assertEqual(first['models']['5.6 sol high']['responses'], 1)

        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(turn) + '\n')
            f.write(json.dumps(event) + '\n')
        second = server.scan_codex_model_usage(sessions_dir=sess_dir, cache_file=cache_path)
        self.assertEqual(second['models']['5.6 sol high']['total_tokens'], 100)
        self.assertEqual(second['models']['5.6 sol high']['responses'], 1)

    def test_codex_scanner_malformed_cache_offset_rebuilds_and_naive_now_is_utc(self):
        sess_dir = os.path.join(self._temp_dir.name, 'codex_bad_cache_sessions')
        cache_path = os.path.join(self._temp_dir.name, 'test_bad_cache.json')
        os.makedirs(sess_dir, exist_ok=True)
        log_file = os.path.join(sess_dir, 'session.jsonl')
        with open(log_file, 'w', encoding='utf-8') as f:
            f.write(json.dumps({'type': 'turn_context', 'payload': {
                'model': 'gpt-5.6-sol', 'effort': 'high'}}) + '\n')
            f.write(json.dumps({'type': 'event_msg', 'timestamp': '2026-08-24T12:00:00+00:00',
                                'payload': {'type': 'token_count', 'request_id': 'req',
                                            'info': {'last_token_usage': {
                                                'input_tokens': 80, 'output_tokens': 20,
                                                'total_tokens': 100}}}}) + '\n')

        server.scan_codex_model_usage(sessions_dir=sess_dir, cache_file=cache_path)
        with open(cache_path, 'r', encoding='utf-8') as f:
            cache = json.load(f)
        next(iter(cache['files'].values()))['offset'] = 'bad'
        with open(cache_path, 'w', encoding='utf-8') as f:
            json.dump(cache, f)

        result = server.scan_codex_model_usage(
            sessions_dir=sess_dir, cache_file=cache_path,
            now=datetime(2026, 8, 24, 13, 0, 0))
        self.assertEqual(result['models']['5.6 sol high']['total_tokens'], 100)
        self.assertEqual(result['diagnostics']['files_rebuilt'], 1)

        for field, malformed in (('all_time', 'bad'), ('recent_events', 'bad')):
            with open(cache_path, 'r', encoding='utf-8') as f:
                cache = json.load(f)
            next(iter(cache['files'].values()))[field] = malformed
            with open(cache_path, 'w', encoding='utf-8') as f:
                json.dump(cache, f)
            rebuilt = server.scan_codex_model_usage(
                sessions_dir=sess_dir, cache_file=cache_path,
                now=datetime(2026, 8, 24, 13, 0, 0))
            self.assertEqual(rebuilt['models']['5.6 sol high']['total_tokens'], 100)
            self.assertEqual(rebuilt['diagnostics']['files_rebuilt'], 1)

        for malformed_total in (-1, float('nan'), float('inf')):
            with open(cache_path, 'r', encoding='utf-8') as f:
                cache = json.load(f)
            entry = next(iter(cache['files'].values()))
            next(iter(entry['all_time'].values()))['total_tokens'] = malformed_total
            with open(cache_path, 'w', encoding='utf-8') as f:
                json.dump(cache, f)
            rebuilt = server.scan_codex_model_usage(
                sessions_dir=sess_dir, cache_file=cache_path,
                now=datetime(2026, 8, 24, 13, 0, 0))
            self.assertEqual(rebuilt['models']['5.6 sol high']['total_tokens'], 100)
            self.assertEqual(rebuilt['diagnostics']['files_rebuilt'], 1)

    def test_codex_scanner_cumulative_fallback_per_field_deltas_and_ignores_resets_or_negative_deltas(self):
        sess_dir = os.path.join(self._temp_dir.name, 'codex_cum_sessions')
        cache_path = os.path.join(self._temp_dir.name, 'test_cum_cache.json')
        os.makedirs(sess_dir, exist_ok=True)
        log_file = os.path.join(sess_dir, 'session_cum.jsonl')

        with open(log_file, 'w', encoding='utf-8') as f:
            f.write(json.dumps({'type': 'turn_context', 'payload': {'model': 'gpt-5.6-sol', 'effort': 'high'}}) + '\n')
            # Snapshot 1: in: 100, cached: 20, write: 10, out: 50, reasoning: 10, total: 150
            f.write(json.dumps({
                'type': 'event_msg',
                'timestamp': '2026-08-24T12:00:00+00:00',
                'payload': {
                    'type': 'token_count',
                    'info': {'total_token_usage': {
                        'input_tokens': 100, 'cached_input_tokens': 20, 'cache_write_input_tokens': 10,
                        'output_tokens': 50, 'reasoning_output_tokens': 10, 'total_tokens': 150
                    }}
                }
            }) + '\n')
            # Snapshot 2: in: 250 (+150), cached: 50 (+30), write: 20 (+10), out: 120 (+70), reasoning: 30 (+20), total: 370 (+220)
            f.write(json.dumps({
                'type': 'event_msg',
                'timestamp': '2026-08-24T12:01:00+00:00',
                'payload': {
                    'type': 'token_count',
                    'info': {'total_token_usage': {
                        'input_tokens': 250, 'cached_input_tokens': 50, 'cache_write_input_tokens': 20,
                        'output_tokens': 120, 'reasoning_output_tokens': 30, 'total_tokens': 370
                    }}
                }
            }) + '\n')
            # Snapshot 3 (Reset boundary / negative delta): in drops to 50 -> must be ignored!
            f.write(json.dumps({
                'type': 'event_msg',
                'timestamp': '2026-08-24T12:02:00+00:00',
                'payload': {
                    'type': 'token_count',
                    'info': {'total_token_usage': {
                        'input_tokens': 50, 'cached_input_tokens': 0, 'cache_write_input_tokens': 0,
                        'output_tokens': 20, 'reasoning_output_tokens': 0, 'total_tokens': 70
                    }}
                }
            }) + '\n')
            # Snapshot 4: in: 100 (+50), cached: 10 (+10), write: 5 (+5), out: 40 (+20), reasoning: 5 (+5), total: 140 (+70)
            f.write(json.dumps({
                'type': 'event_msg',
                'timestamp': '2026-08-24T12:03:00+00:00',
                'payload': {
                    'type': 'token_count',
                    'info': {'total_token_usage': {
                        'input_tokens': 100, 'cached_input_tokens': 10, 'cache_write_input_tokens': 5,
                        'output_tokens': 40, 'reasoning_output_tokens': 5, 'total_tokens': 140
                    }}
                }
            }) + '\n')

        res = server.scan_codex_model_usage(sessions_dir=sess_dir, cache_file=cache_path)
        sol = res['models']['5.6 sol high']

        self.assertEqual(sol['input_tokens'], 100 + 150 + 50)           # 300
        self.assertEqual(sol['cached_input_tokens'], 20 + 30 + 10)      # 60
        self.assertEqual(sol['cache_write_input_tokens'], 10 + 10 + 5)  # 25
        self.assertEqual(sol['output_tokens'], 50 + 70 + 20)           # 140
        self.assertEqual(sol['thinking_tokens'], 10 + 20 + 5)          # 35
        self.assertEqual(sol['total_tokens'], 150 + 220 + 70)          # 440

    def test_codex_scanner_token_count_before_valid_turn_context_and_arbitrary_models_ignored(self):
        sess_dir = os.path.join(self._temp_dir.name, 'codex_causal_sessions')
        cache_path = os.path.join(self._temp_dir.name, 'test_causal_cache.json')
        os.makedirs(sess_dir, exist_ok=True)
        log_file = os.path.join(sess_dir, 'session_causal.jsonl')

        with open(log_file, 'w', encoding='utf-8') as f:
            # 1. token_count event BEFORE any turn_context, with an unverified model field
            f.write(json.dumps({
                'type': 'event_msg',
                'timestamp': '2026-08-24T12:00:00+00:00',
                'payload': {
                    'type': 'token_count',
                    'model': 'gpt-5.6-sol',
                    'info': {'last_token_usage': {'input_tokens': 500, 'output_tokens': 200, 'total_tokens': 700}}
                }
            }) + '\n')

            # 2. Valid turn_context establishing Terra High
            f.write(json.dumps({'type': 'turn_context', 'payload': {'model': 'gpt-5.6-terra', 'effort': 'high'}}) + '\n')

            # 3. Arbitrary response_item containing model: 'gpt-5.6-sol' (must NOT alter attribution)
            f.write(json.dumps({
                'type': 'response_item',
                'model': 'gpt-5.6-sol',
                'payload': {'type': 'arbitrary_event', 'model': 'gpt-5.6-sol'}
            }) + '\n')

            # 4. Valid token_count event
            f.write(json.dumps({
                'type': 'event_msg',
                'timestamp': '2026-08-24T12:01:00+00:00',
                'payload': {
                    'type': 'token_count',
                    'info': {'last_token_usage': {'input_tokens': 100, 'output_tokens': 50, 'total_tokens': 150}}
                }
            }) + '\n')

        res = server.scan_codex_model_usage(sessions_dir=sess_dir, cache_file=cache_path)
        # Event 1 must be ignored, Event 4 attributed only to Terra High
        self.assertNotIn('5.6 sol high', res['models'])
        self.assertIn('5.6 terra high', res['models'])
        self.assertEqual(res['models']['5.6 terra high']['total_tokens'], 150)

    def test_codex_scanner_malformed_numeric_values_do_not_abort_subsequent_lines(self):
        sess_dir = os.path.join(self._temp_dir.name, 'codex_malformed_sessions')
        cache_path = os.path.join(self._temp_dir.name, 'test_malformed_cache.json')
        os.makedirs(sess_dir, exist_ok=True)
        log_file = os.path.join(sess_dir, 'session_malformed.jsonl')

        with open(log_file, 'w', encoding='utf-8') as f:
            f.write(json.dumps({'type': 'turn_context', 'payload': {'model': 'gpt-5.6-sol', 'effort': 'high'}}) + '\n')
            # Malformed numeric values
            f.write(json.dumps({
                'type': 'event_msg',
                'timestamp': '2026-08-24T12:00:00+00:00',
                'payload': {
                    'type': 'token_count',
                    'info': {'last_token_usage': {
                        'input_tokens': 'not-a-number',
                        'output_tokens': None,
                        'cached_input_tokens': -100,
                        'total_tokens': 'NaN'
                    }}
                }
            }) + '\n')
            # Subsequent valid line
            f.write(json.dumps({
                'type': 'event_msg',
                'timestamp': '2026-08-24T12:01:00+00:00',
                'payload': {
                    'type': 'token_count',
                    'info': {'last_token_usage': {'input_tokens': 100, 'output_tokens': 50, 'total_tokens': 150}}
                }
            }) + '\n')

        res = server.scan_codex_model_usage(sessions_dir=sess_dir, cache_file=cache_path)
        self.assertTrue(res['available'])
        self.assertEqual(res['models']['5.6 sol high']['total_tokens'], 150)

    def test_codex_scanner_simultaneous_scans_concurrency_lock(self):
        import threading
        sess_dir = os.path.join(self._temp_dir.name, 'codex_concurrent_sessions')
        cache_path = os.path.join(self._temp_dir.name, 'test_concurrent_cache.json')
        os.makedirs(sess_dir, exist_ok=True)

        for i in range(5):
            p = os.path.join(sess_dir, f'session_{i}.jsonl')
            with open(p, 'w', encoding='utf-8') as f:
                f.write(json.dumps({'type': 'turn_context', 'payload': {'model': 'gpt-5.6-sol', 'effort': 'high'}}) + '\n')
                f.write(json.dumps({
                    'type': 'event_msg',
                    'timestamp': '2026-08-24T12:00:00+00:00',
                    'payload': {'type': 'token_count', 'info': {'last_token_usage': {'input_tokens': 100, 'output_tokens': 50, 'total_tokens': 150}}}
                }) + '\n')

        errors = []
        results = []

        def worker():
            try:
                r = server.scan_codex_model_usage(sessions_dir=sess_dir, cache_file=cache_path)
                results.append(r)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0)
        self.assertEqual(len(results), 8)
        for r in results:
            self.assertEqual(r['models']['5.6 sol high']['total_tokens'], 750)

    def test_codex_scanner_invalid_timestamps_do_not_survive_in_rolling_windows(self):
        sess_dir = os.path.join(self._temp_dir.name, 'codex_ts_sessions')
        cache_path = os.path.join(self._temp_dir.name, 'test_ts_cache.json')
        os.makedirs(sess_dir, exist_ok=True)
        log_file = os.path.join(sess_dir, 'session_ts.jsonl')

        now_dt = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)
        ts_1h_ago = '2026-08-24T11:00:00+00:00'

        with open(log_file, 'w', encoding='utf-8') as f:
            f.write(json.dumps({'type': 'turn_context', 'payload': {'model': 'gpt-5.6-sol', 'effort': 'high'}}) + '\n')
            # 1. Valid timestamp 1h ago
            f.write(json.dumps({
                'type': 'event_msg',
                'timestamp': ts_1h_ago,
                'payload': {'type': 'token_count', 'info': {'last_token_usage': {'input_tokens': 100, 'output_tokens': 50, 'total_tokens': 150}}}
            }) + '\n')
            # 2. Invalid date string (contributes to all-time, but NOT rolling windows)
            f.write(json.dumps({
                'type': 'event_msg',
                'timestamp': 'INVALID_TIMESTAMP_STRING',
                'payload': {'type': 'token_count', 'info': {'last_token_usage': {'input_tokens': 200, 'output_tokens': 100, 'total_tokens': 300}}}
            }) + '\n')
            # 3. Missing timestamp
            f.write(json.dumps({
                'type': 'event_msg',
                'payload': {'type': 'token_count', 'info': {'last_token_usage': {'input_tokens': 300, 'output_tokens': 150, 'total_tokens': 450}}}
            }) + '\n')

        res = server.scan_codex_model_usage(sessions_dir=sess_dir, cache_file=cache_path, now=now_dt)
        sol = res['models']['5.6 sol high']

        # All-time includes all 3 events (150 + 300 + 450 = 900)
        self.assertEqual(sol['total_tokens'], 900)
        # Rolling windows only include the valid 1h ago timestamp (150)
        self.assertEqual(sol['five_hour_tokens'], 150)
        self.assertEqual(sol['weekly_tokens'], 150)

        # Verify cached recent_events contains only the valid timestamp event
        with open(cache_path, 'r', encoding='utf-8') as f:
            saved_cache = json.load(f)
        cached_entry = list(saved_cache['files'].values())[0]
        self.assertEqual(len(cached_entry['recent_events']), 1)
        self.assertEqual(cached_entry['recent_events'][0]['ts'], ts_1h_ago)

    def test_codex_quota_efficiency_same_cycle_threshold_and_relative_burn(self):
        reset_at = '2026-09-07T15:00:00+00:00'

        def obs(session, model_key, ts, used, total):
            return {
                'session': session,
                'model_key': model_key,
                'model_id': model_key,
                'reasoning_effort': model_key.rsplit(' ', 1)[-1],
                'ts': ts,
                'resets_at': reset_at,
                'used_percent': used,
                'total_tokens': total,
                'input_tokens': total,
                'cached_input_tokens': 0,
                'cache_write_input_tokens': 0,
                'output_tokens': 0,
                'thinking_tokens': 0,
            }

        result = server.build_codex_quota_efficiency([
            obs('sol-session', '5.6 sol high', '2026-09-07T10:00:00+00:00', 10, 100_000),
            obs('sol-session', '5.6 sol high', '2026-09-07T10:10:00+00:00', 11, 140_000),
            obs('sol-session', '5.6 sol high', '2026-09-07T10:20:00+00:00', 14, 300_000),
            obs('astra-session', 'gpt-6 astra low', '2026-09-07T10:00:00+00:00', 20, 100_000),
            obs('astra-session', 'gpt-6 astra low', '2026-09-07T10:20:00+00:00', 24, 180_000),
        ])

        self.assertTrue(result['available'])
        rows = {row['model_key']: row for row in result['models']}
        self.assertEqual(rows['5.6 sol high']['tokens_per_quota_pct'], 50_000.0)
        self.assertEqual(rows['5.6 sol high']['relative_quota_burn_vs_sol_high'], 1.0)
        self.assertEqual(rows['gpt-6 astra low']['tokens_per_quota_pct'], 20_000.0)
        self.assertEqual(rows['gpt-6 astra low']['relative_quota_burn_vs_sol_high'], 2.5)
        self.assertEqual(result['interval_count'], 2)

    def test_codex_quota_efficiency_excludes_reset_boundary_and_model_switch(self):
        observations = [
            {'session': 'one-session', 'model_key': '5.6 sol high',
             'ts': '2026-09-07T10:00:00+00:00', 'resets_at': '2026-09-07T15:00:00+00:00',
             'used_percent': 10, 'total_tokens': 100_000},
            {'session': 'one-session', 'model_key': '5.6 sol high',
             'ts': '2026-09-07T10:20:00+00:00', 'resets_at': '2026-09-07T20:00:00+00:00',
             'used_percent': 20, 'total_tokens': 200_000},
            {'session': 'one-session', 'model_key': 'gpt-6 astra low',
             'ts': '2026-09-07T10:30:00+00:00', 'resets_at': '2026-09-07T20:00:00+00:00',
             'used_percent': 23, 'total_tokens': 260_000},
            {'session': 'one-session', 'model_key': 'gpt-6 astra low',
             'ts': '2026-09-07T10:40:00+00:00', 'resets_at': '2026-09-07T20:00:00+00:00',
             'used_percent': 24, 'total_tokens': 280_000},
        ]
        result = server.build_codex_quota_efficiency(observations)
        self.assertFalse(result['available'])
        self.assertEqual(result['interval_count'], 0)

    def test_codex_quota_efficiency_uses_median_and_reports_confidence(self):
        observations = []
        reset_at = '2026-09-07T15:00:00+00:00'
        for index, token_delta in enumerate([160_000, 200_000, 240_000]):
            session = f'session-{index}'
            observations.extend([
                {'session': session, 'model_key': '5.6 sol high',
                 'ts': f'2026-09-07T10:0{index}:00+00:00', 'resets_at': reset_at,
                 'used_percent': 10, 'total_tokens': 100_000},
                {'session': session, 'model_key': '5.6 sol high',
                 'ts': f'2026-09-07T11:0{index}:00+00:00', 'resets_at': reset_at,
                 'used_percent': 14, 'total_tokens': 100_000 + token_delta},
            ])
        row = server.build_codex_quota_efficiency(observations)['models'][0]
        self.assertEqual(row['tokens_per_quota_pct'], 50_000.0)
        self.assertEqual(row['sample_count'], 3)
        self.assertEqual(row['session_count'], 3)
        self.assertEqual(row['quota_span_pct'], 12.0)
        self.assertEqual(row['confidence'], 'medium')

    def test_codex_quota_efficiency_timeline_detects_recent_change(self):
        def pair(session, model, start, end, reset, token_delta):
            return [
                {'session': session, 'model_key': model, 'model_id': model,
                 'reasoning_effort': model.rsplit(' ', 1)[-1], 'ts': start,
                 'resets_at': reset, 'used_percent': 10, 'total_tokens': 100_000},
                {'session': session, 'model_key': model, 'model_id': model,
                 'reasoning_effort': model.rsplit(' ', 1)[-1], 'ts': end,
                 'resets_at': reset, 'used_percent': 12, 'total_tokens': 100_000 + token_delta},
            ]

        observations = []
        observations += pair('old-sol', '5.6 sol high', '2026-09-05T09:00:00+00:00',
                             '2026-09-05T10:00:00+00:00', '2026-09-05T14:00:00+00:00', 100_000)
        observations += pair('old-xh', '5.6 sol xhigh', '2026-09-05T09:00:00+00:00',
                             '2026-09-05T10:00:00+00:00', '2026-09-05T14:00:00+00:00', 120_000)
        observations += pair('new-sol', '5.6 sol high', '2026-09-09T09:00:00+00:00',
                             '2026-09-09T10:00:00+00:00', '2026-09-09T14:00:00+00:00', 100_000)
        observations += pair('new-xh', '5.6 sol xhigh', '2026-09-09T09:00:00+00:00',
                             '2026-09-09T10:00:00+00:00', '2026-09-09T14:00:00+00:00', 50_000)

        result = server.build_codex_quota_efficiency_timeline(
            observations, now=datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc),
            range_days=7, window_days_options=(1,),
        )
        rows = {row['model_key']: row for row in result['windows']['1']['models']}
        xhigh = rows['5.6 sol xhigh']
        self.assertEqual(xhigh['latest']['relative_quota_burn_vs_sol_high'], 2.0)
        self.assertEqual(xhigh['previous']['relative_quota_burn_vs_sol_high'], 0.833)
        self.assertAlmostEqual(xhigh['change_pct'], 140.1, places=1)
        self.assertEqual(xhigh['latest']['sample_count'], 1)
        self.assertEqual(xhigh['latest']['baseline_sample_count'], 1)

    def test_codex_quota_efficiency_timeline_keeps_model_without_baseline(self):
        observations = [
            {'session': 'sol-old', 'model_key': '5.6 sol high',
             'ts': '2026-09-05T09:00:00+00:00', 'resets_at': '2026-09-05T14:00:00+00:00',
             'used_percent': 10, 'total_tokens': 100_000},
            {'session': 'sol-old', 'model_key': '5.6 sol high',
             'ts': '2026-09-05T10:00:00+00:00', 'resets_at': '2026-09-05T14:00:00+00:00',
             'used_percent': 12, 'total_tokens': 200_000},
            {'session': 'astra-new', 'model_key': 'gpt-6 astra low',
             'ts': '2026-09-09T09:00:00+00:00', 'resets_at': '2026-09-09T14:00:00+00:00',
             'used_percent': 20, 'total_tokens': 100_000},
            {'session': 'astra-new', 'model_key': 'gpt-6 astra low',
             'ts': '2026-09-09T10:00:00+00:00', 'resets_at': '2026-09-09T14:00:00+00:00',
             'used_percent': 22, 'total_tokens': 140_000},
        ]
        result = server.build_codex_quota_efficiency_timeline(
            observations, now=datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc),
            range_days=7, window_days_options=(1,),
        )
        rows = {row['model_key']: row for row in result['windows']['1']['models']}
        self.assertIn('5.6 sol high', rows)
        self.assertIn('gpt-6 astra low', rows)
        latest = rows['gpt-6 astra low']['latest']
        self.assertEqual(latest['tokens_per_quota_pct'], 20000.0)
        self.assertIsNone(latest['relative_quota_burn_vs_sol_high'])
        self.assertEqual(latest['baseline_source'], 'unavailable')
        self.assertFalse(latest['comparison_estimated'])

    def test_codex_quota_efficiency_timeline_bridges_missing_sol_baseline(self):
        def pair(session, model, day, start_used, start_tokens, end_tokens):
            return [
                {'session': session, 'model_key': model,
                 'ts': f'{day}T09:00:00+00:00', 'resets_at': f'{day}T14:00:00+00:00',
                 'used_percent': start_used, 'total_tokens': start_tokens},
                {'session': session, 'model_key': model,
                 'ts': f'{day}T10:00:00+00:00', 'resets_at': f'{day}T14:00:00+00:00',
                 'used_percent': start_used + 2, 'total_tokens': end_tokens},
            ]

        observations = []
        observations += pair('sol-day5', '5.6 sol high', '2026-09-05', 10, 100_000, 200_000)
        observations += pair('luna-day5', '5.6 luna xhigh', '2026-09-05', 20, 100_000, 150_000)
        observations += pair('luna-day6', '5.6 luna xhigh', '2026-09-06', 10, 100_000, 150_000)
        observations += pair('astra-day6', 'gpt-6 astra low', '2026-09-06', 20, 100_000, 120_000)
        observations += pair('astra-new', 'gpt-6 astra low', '2026-09-09', 30, 100_000, 120_000)

        result = server.build_codex_quota_efficiency_timeline(
            observations, now=datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc),
            range_days=7, window_days_options=(1,),
        )
        rows = {row['model_key']: row for row in result['windows']['1']['models']}
        latest = rows['gpt-6 astra low']['latest']
        self.assertEqual(latest['baseline_source'], 'bridge')
        self.assertTrue(latest['comparison_estimated'])
        self.assertEqual(latest['relative_quota_burn_vs_sol_high'], 5.0)
        self.assertEqual(latest['bridge_path'], ['gpt-6 astra low', '5.6 luna xhigh', '5.6 sol high'])
        self.assertEqual(latest['bridge_hops'], 2)
        self.assertEqual(latest['confidence'], 'low')

    def test_codex_quota_efficiency_timeline_confidence_uses_weaker_side(self):
        observations = [
            {'session': 'sol', 'model_key': '5.6 sol high',
             'ts': '2026-09-09T08:00:00+00:00', 'resets_at': '2026-09-09T15:00:00+00:00',
             'used_percent': 10, 'total_tokens': 100_000},
            {'session': 'sol', 'model_key': '5.6 sol high',
             'ts': '2026-09-09T08:30:00+00:00', 'resets_at': '2026-09-09T15:00:00+00:00',
             'used_percent': 12, 'total_tokens': 200_000},
        ]
        for index in range(5):
            observations.extend([
                {'session': f'xhigh-{index}', 'model_key': '5.6 sol xhigh',
                 'ts': f'2026-09-09T09:{index:02d}:00+00:00',
                 'resets_at': '2026-09-09T15:00:00+00:00',
                 'used_percent': 20, 'total_tokens': 100_000},
                {'session': f'xhigh-{index}', 'model_key': '5.6 sol xhigh',
                 'ts': f'2026-09-09T10:{index:02d}:00+00:00',
                 'resets_at': '2026-09-09T15:00:00+00:00',
                 'used_percent': 26, 'total_tokens': 220_000},
            ])

        result = server.build_codex_quota_efficiency_timeline(
            observations, now=datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc),
            range_days=7, window_days_options=(1,),
        )
        rows = {row['model_key']: row for row in result['windows']['1']['models']}
        latest = rows['5.6 sol xhigh']['latest']
        self.assertEqual(latest['model_confidence'], 'high')
        self.assertEqual(latest['baseline_confidence'], 'low')
        self.assertEqual(latest['confidence'], 'low')

    def test_codex_quota_per_task_relative_burn(self):
        reset = '2026-09-07T15:00:00+00:00'
        def q(session, task, model, effort, ts, used, total):
            return {'session': session, 'task_id': task, 'model_key': model, 'model_id': model, 'reasoning_effort': effort, 'ts': ts, 'resets_at': reset, 'used_percent': used, 'total_tokens': total}
        obs = [
            q('sol', 't-sol', '5.6 sol high', 'high', '2026-09-07T10:00:00+00:00', 10, 100_000),
            q('sol', 't-sol', '5.6 sol high', 'high', '2026-09-07T10:20:00+00:00', 14, 300_000),
            q('xh', 't-xh', '5.6 sol xhigh', 'xhigh', '2026-09-07T10:00:00+00:00', 20, 100_000),
            q('xh', 't-xh', '5.6 sol xhigh', 'xhigh', '2026-09-07T10:20:00+00:00', 23, 300_000),
        ]
        events = [{'task_id': 't-sol', 'model_key': '5.6 sol high', 'tot': 300_000}, {'task_id': 't-xh', 'model_key': '5.6 sol xhigh', 'tot': 300_000}]
        rows = {row['model_key']: row for row in server.build_codex_quota_per_task(obs, events, ['t-sol', 't-xh'])['models']}
        self.assertEqual(rows['5.6 sol high']['estimated_quota_pct_per_task'], 6.0)
        self.assertEqual(rows['5.6 sol high']['relative_task_quota_burn_vs_sol_high'], 1.0)
        self.assertEqual(rows['5.6 sol xhigh']['estimated_quota_pct_per_task'], 4.5)
        self.assertEqual(rows['5.6 sol xhigh']['relative_task_quota_burn_vs_sol_high'], 0.75)

    def test_codex_quota_per_task_excludes_mixed_reset_and_low_coverage(self):
        def q(task, ts, used, total, reset='2026-09-07T15:00:00+00:00'):
            return {'session': task, 'task_id': task, 'model_key': '5.6 sol high', 'model_id': 'gpt-5.6-sol', 'reasoning_effort': 'high', 'ts': ts, 'resets_at': reset, 'used_percent': used, 'total_tokens': total}
        obs = [q('mixed', '2026-09-07T10:00:00+00:00', 10, 100_000), q('mixed', '2026-09-07T10:10:00+00:00', 12, 200_000), q('reset', '2026-09-07T10:00:00+00:00', 10, 100_000), q('reset', '2026-09-07T10:10:00+00:00', 12, 200_000, '2026-09-07T20:00:00+00:00'), q('low', '2026-09-07T10:00:00+00:00', 10, 100_000), q('low', '2026-09-07T10:10:00+00:00', 14, 150_000)]
        events = [{'task_id': 'mixed', 'model_key': '5.6 sol high', 'tot': 200_000}, {'task_id': 'mixed', 'model_key': '5.6 sol xhigh', 'tot': 1}, {'task_id': 'reset', 'model_key': '5.6 sol high', 'tot': 200_000}, {'task_id': 'low', 'model_key': '5.6 sol high', 'tot': 500_000}]
        result = server.build_codex_quota_per_task(obs, events, ['mixed', 'reset', 'low'])
        self.assertFalse(result['available'])
        self.assertEqual(result['excluded_mixed_model_tasks'], 1)
        self.assertEqual(result['excluded_cross_cycle_tasks'], 1)
        self.assertEqual(result['excluded_low_coverage_tasks'], 1)

    def test_codex_quota_per_task_timeline_detects_recent_change(self):
        observations = []
        events = []
        finished = []

        def add_tasks(prefix, model, day, quota_delta, count=5):
            reset = f'{day}T15:00:00+00:00'
            for index in range(count):
                task_id = f'{prefix}-{index}'
                session = f'session-{task_id}'
                start_total = 100_000
                task_tokens = 200_000
                observations.extend([
                    {'session': session, 'task_id': task_id, 'model_key': model,
                     'model_id': model, 'reasoning_effort': model.rsplit(' ', 1)[-1],
                     'ts': f'{day}T09:{index:02d}:00+00:00', 'resets_at': reset,
                     'used_percent': 10, 'total_tokens': start_total},
                    {'session': session, 'task_id': task_id, 'model_key': model,
                     'model_id': model, 'reasoning_effort': model.rsplit(' ', 1)[-1],
                     'ts': f'{day}T10:{index:02d}:00+00:00', 'resets_at': reset,
                     'used_percent': 10 + quota_delta,
                     'total_tokens': start_total + task_tokens},
                ])
                events.append({'task_id': task_id, 'model_key': model, 'tot': task_tokens})
                finished.append(task_id)

        add_tasks('old-sol', '5.6 sol high', '2026-08-20', 10)
        add_tasks('old-xh', '5.6 sol xhigh', '2026-08-20', 8)
        add_tasks('new-sol', '5.6 sol high', '2026-09-09', 10)
        add_tasks('new-xh', '5.6 sol xhigh', '2026-09-09', 20)

        result = server.build_codex_quota_per_task_timeline(
            observations, events, finished,
            now=datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc),
            range_days=30, window_days_options=(7,), min_tasks_per_model=5,
        )
        rows = {row['model_key']: row for row in result['windows']['7']['models']}
        xhigh = rows['5.6 sol xhigh']
        self.assertEqual(xhigh['latest']['relative_task_quota_burn_vs_sol_high'], 2.0)
        self.assertEqual(xhigh['previous']['relative_task_quota_burn_vs_sol_high'], 0.8)
        self.assertEqual(xhigh['change_pct'], 150.0)
        self.assertEqual(xhigh['latest']['task_count'], 5)
        self.assertEqual(xhigh['latest']['baseline_task_count'], 5)
        self.assertEqual(xhigh['latest']['tokens_per_task'], 200_000)

    def test_codex_quota_per_task_timeline_requires_same_window_minimum(self):
        observations = []
        events = []
        finished = []

        def add_tasks(prefix, model, day, count):
            for index in range(count):
                task_id = f'{prefix}-{index}'
                session = f'session-{task_id}'
                observations.extend([
                    {'session': session, 'task_id': task_id, 'model_key': model,
                     'model_id': model, 'reasoning_effort': model.rsplit(' ', 1)[-1],
                     'ts': f'{day}T09:{index:02d}:00+00:00',
                     'resets_at': f'{day}T15:00:00+00:00',
                     'used_percent': 10, 'total_tokens': 100_000},
                    {'session': session, 'task_id': task_id, 'model_key': model,
                     'model_id': model, 'reasoning_effort': model.rsplit(' ', 1)[-1],
                     'ts': f'{day}T10:{index:02d}:00+00:00',
                     'resets_at': f'{day}T15:00:00+00:00',
                     'used_percent': 15, 'total_tokens': 200_000},
                ])
                events.append({'task_id': task_id, 'model_key': model, 'tot': 100_000})
                finished.append(task_id)

        add_tasks('sol-old', '5.6 sol high', '2026-08-20', 5)
        add_tasks('sol-new', '5.6 sol high', '2026-09-09', 4)
        add_tasks('xh-new', '5.6 sol xhigh', '2026-09-09', 5)
        result = server.build_codex_quota_per_task_timeline(
            observations, events, finished,
            now=datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc),
            range_days=30, window_days_options=(7,), min_tasks_per_model=5,
        )
        keys = {row['model_key'] for row in result['windows']['7']['models']}
        self.assertIn('5.6 sol high', keys)
        self.assertNotIn('5.6 sol xhigh', keys)

    def test_extract_codex_5h_quota_observation_rejects_malformed_and_accepts_valid(self):
        cumulative = {
            'input_tokens': 90_000, 'cached_input_tokens': 50_000,
            'cache_write_input_tokens': 0, 'output_tokens': 10_000,
            'reasoning_output_tokens': 2_000, 'total_tokens': 100_000,
        }
        self.assertIsNone(server._extract_codex_5h_quota_observation(
            None, '2026-09-07T10:00:00+00:00',
            'gpt-5.6-sol', 'high', cumulative, 'session'
        ))
        self.assertIsNone(server._extract_codex_5h_quota_observation(
            {'primary': {'window_minutes': 300, 'used_percent': 10}},
            '2026-09-07T10:00:00+00:00',
            'gpt-5.6-sol', 'high', cumulative, 'session'
        ))
        valid = server._extract_codex_5h_quota_observation(
            {'primary': {'window_minutes': 300, 'used_percent': 12,
                         'resets_at': '2026-09-07T15:00:00+00:00'}},
            '2026-09-07T10:00:00+00:00',
            'gpt-5.6-sol', 'high', cumulative, 'session'
        )
        self.assertEqual(valid['model_key'], '5.6 sol high')
        self.assertEqual(valid['used_percent'], 12.0)
        self.assertEqual(valid['total_tokens'], 100_000)

    def test_codex_scanner_persists_quota_observations_and_exposes_efficiency(self):
        sess_dir = os.path.join(self._temp_dir.name, 'codex_quota_efficiency_sessions')
        cache_path = os.path.join(self._temp_dir.name, 'codex_quota_efficiency_cache.json')
        os.makedirs(sess_dir, exist_ok=True)
        reset_at = '2026-09-07T15:00:00+00:00'

        def token_event(ts, used, cumulative_total, last_total):
            return {
                'type': 'event_msg', 'timestamp': ts,
                'payload': {
                    'type': 'token_count',
                    'rate_limits': {'primary': {
                        'window_minutes': 300, 'used_percent': used, 'resets_at': reset_at,
                    }},
                    'info': {
                        'last_token_usage': {
                            'input_tokens': last_total, 'output_tokens': 0, 'total_tokens': last_total,
                        },
                        'total_token_usage': {
                            'input_tokens': cumulative_total, 'output_tokens': 0,
                            'total_tokens': cumulative_total,
                        },
                    },
                },
            }

        path = os.path.join(sess_dir, 'session.jsonl')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(json.dumps({
                'type': 'turn_context',
                'payload': {'model': 'gpt-5.6-sol', 'effort': 'high'},
            }) + '\n')
            f.write(json.dumps({
                'type': 'event_msg', 'timestamp': '2026-09-07T09:59:00+00:00',
                'payload': {'type': 'task_started', 'turn_id': 'turn-test'},
            }) + '\n')
            f.write(json.dumps(token_event(
                '2026-09-07T10:00:00+00:00', 10, 100_000, 100_000
            )) + '\n')
            f.write(json.dumps(token_event(
                '2026-09-07T10:20:00+00:00', 14, 300_000, 200_000
            )) + '\n')
            f.write(json.dumps({
                'type': 'event_msg', 'timestamp': '2026-09-07T10:21:00+00:00',
                'payload': {'type': 'task_complete', 'turn_id': 'turn-test'},
            }) + '\n')

        result = server.scan_codex_model_usage(
            sessions_dir=sess_dir, cache_file=cache_path,
            now=datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(len(result['quota_observations']), 2)
        self.assertTrue(result['quota_efficiency']['available'])
        self.assertEqual(result['quota_efficiency']['models'][0]['tokens_per_quota_pct'], 50_000.0)
        self.assertTrue(result['quota_efficiency_timeline']['available'])
        self.assertEqual(
            result['quota_efficiency_timeline']['windows']['3']['models'][0]['model_key'],
            '5.6 sol high',
        )
        self.assertTrue(result['quota_per_task']['available'])
        self.assertEqual(result['quota_per_task']['models'][0]['estimated_quota_pct_per_task'], 6.0)
        self.assertIn('quota_per_task_timeline', result)
        self.assertEqual(result['quota_per_task_timeline']['task_sample_count'], 1)
        self.assertFalse(result['quota_per_task_timeline']['available'])

        with open(cache_path, 'r', encoding='utf-8') as f:
            cache = json.load(f)
        self.assertEqual(cache['version'], server.CODEX_MODELS_CACHE_VERSION)
        cached_entry = next(iter(cache['files'].values()))
        self.assertEqual(len(cached_entry['quota_observations']), 2)
        self.assertEqual(cached_entry['finished_turn_ids'], ['turn-test'])

    def test_req010_model_breakdown_reconciles_sources_and_rejects_unlabelled_codex_denominator(self):
        def model_stats(total, weekly):
            return {
                'total_tokens': total,
                'weekly_tokens': weekly,
                'five_hour_tokens': 0,
                'input_tokens': total // 2,
                'output_tokens': total - total // 2,
                'thinking_tokens': 0,
                'cost_usd': 0.0,
                'weekly_cost_usd': 0.0,
                'sessions_count': 1,
                'model_responses': 1,
                'tool_calls': 0,
            }

        rows = server.build_models_breakdown(
            {'Gemini-REQ010': model_stats(1000, 600)}, [],
            {
                'weekly_limit_tokens': 5_000_000,
                'rate_limits': {'windows': [{
                    'window_minutes': 10080,
                    'used_percent': 12.0,
                    'limit_tokens': None,
                }]},
                'automatic_model_usage': {
                    'models': {'gpt-auto': model_stats(400, 250)}
                },
                'models': {
                    'gpt-auto': {'total_tokens': 999, 'weekly_tokens': 999},
                    'manual-only': {'total_tokens': 300, 'weekly_tokens': 200},
                },
            },
            {'weekly_window': {
                'gemini': {'limit_tokens': 100_000},
                'external': {'limit_tokens': 50_000},
            }},
        )
        self.assertEqual(
            {(row['platform'], row['model_id']): row['weekly_tokens'] for row in rows},
            {('Antigravity', 'Gemini-REQ010'): 600,
             ('Codex', 'gpt-auto'): 250,
             ('Codex', 'manual-only'): 200},
        )
        auto = next(row for row in rows if row['model_id'] == 'gpt-auto')
        self.assertEqual(auto['source_kind'], 'automatic')
        self.assertEqual(sum(row['weekly_tokens'] for row in rows), 1050)
        for row in rows:
            if row['platform'] != 'Codex':
                continue
            if row.get('weekly_quota_pct_used') is not None:
                basis = str(row.get('weekly_quota_basis') or row.get('weekly_quota_source') or '').lower()
                self.assertIn('configured', basis)

    def test_models_daily_timeline_supports_rolling_30_days(self):
        result = server.build_models_daily_timeline([], {}, {}, days=30)
        self.assertEqual(len(result['dates']), 30)
        self.assertEqual(len(result['date_keys']), 30)
        self.assertEqual(result['range']['mode'], 'rolling')
        self.assertEqual(result['range']['days'], 30)

    def test_model_colors_are_distinct_and_stable_across_timeline_rankings(self):
        names = server.MODEL_COLOR_NAMES
        colors = [server.get_model_color(name) for name in names]
        self.assertEqual(len(colors), len(set(colors)))
        self.assertNotEqual(server.get_model_color('5.6 sol xhigh'), server.get_model_color('gpt-6-sol xhigh'))
        self.assertEqual(server.get_model_color('GPT-6-SOL XHIGH'), server.get_model_color('gpt-6-sol xhigh'))

        event_dt = datetime.now(timezone.utc)
        model_a, model_b = '5.6 sol high', 'gpt-6-sol xhigh'

        def timeline(a_tokens, b_tokens):
            events = [
                {'dt': event_dt, 'model': model_a, 'tokens': a_tokens},
                {'dt': event_dt, 'model': model_b, 'tokens': b_tokens},
            ]
            return {row['name']: row['color'] for row in server.build_models_daily_timeline(events, {}, {}, days=7)['models']}

        self.assertEqual(timeline(100, 200), timeline(200, 100))

    def test_models_daily_timeline_custom_range_is_inclusive_and_sums_models(self):
        today = datetime.now().astimezone().date()
        start_day = today - timedelta(days=3)
        end_day = today - timedelta(days=1)
        event_dt = datetime.combine(start_day, datetime.min.time()).astimezone() + timedelta(hours=12)
        result = server.build_models_daily_timeline(
            [{'dt': event_dt.astimezone(timezone.utc), 'model': 'Gemini-Test', 'tokens': 120}],
            {},
            {},
            start_date=start_day.isoformat(),
            end_date=end_day.isoformat(),
        )
        self.assertEqual(
            result['date_keys'],
            [(start_day + timedelta(days=i)).isoformat() for i in range(3)],
        )
        self.assertEqual(result['range']['mode'], 'custom')
        model = next(item for item in result['models'] if item['name'] == 'Gemini-Test')
        self.assertEqual(model['daily_tokens'], [120, 0, 0])
        self.assertEqual(model['total_period_tokens'], 120)

    def test_models_daily_timeline_sums_antigravity_event_cost(self):
        today = datetime.now().astimezone().date()
        event_dt = datetime.combine(today, datetime.min.time()).astimezone() + timedelta(hours=12)
        result = server.build_models_daily_timeline(
            [{'dt': event_dt.astimezone(timezone.utc), 'model': 'Gemini 3.8 Flash', 'tokens': 1200,
              'cost_usd': 0.01234567, 'cost_known': True}],
            {}, {}, days=7,
        )
        model = next(item for item in result['models'] if item['name'] == 'Gemini 3.8 Flash')
        self.assertAlmostEqual(model['total_period_cost_usd'], 0.01234567, places=8)
        self.assertTrue(model['cost_complete'])
        self.assertEqual(model['unknown_cost_tokens'], 0)

    def test_models_daily_timeline_computes_codex_cached_input_cost(self):
        today = datetime.now().astimezone().date()
        event_dt = datetime.combine(today, datetime.min.time()).astimezone() + timedelta(hours=12)
        result = server.build_models_daily_timeline(
            [], {}, {'automatic_model_usage': {'recent_events': [{
                'ts': event_dt.astimezone(timezone.utc).isoformat(), 'model_key': 'gpt-5.6-sol',
                'in': 1000, 'cached_in': 400, 'cache_write_in': 100, 'out': 200, 'tot': 1200,
            }]}}, days=7,
        )
        model = next(item for item in result['models'] if item['name'] == 'gpt-5.6-sol')
        self.assertAlmostEqual(model['total_period_cost_usd'], 0.00666, places=8)
        self.assertTrue(model['cost_complete'])
        self.assertEqual(model['unknown_cost_tokens'], 0)

    def test_chatgpt_web_uses_sol_thinking_family_proxy_without_becoming_verified(self):
        model_name = 'chatgpt-web/high high'
        self.assertFalse(server.model_has_verified_pricing(model_name))
        self.assertTrue(server.model_has_cost_estimate(model_name))

        proxy = server.get_effective_pricing(model_name)
        self.assertTrue(proxy['pricing_estimated'])
        self.assertFalse(proxy['pricing_verified'])
        self.assertEqual(proxy['pricing_basis_model'], 'GPT-5.6 Sol Thinking')

        tokens = dict(
            input_tokens=1_000_000,
            output_tokens=100_000,
            cached_input_tokens=200_000,
            cache_write_input_tokens=50_000,
        )
        self.assertAlmostEqual(
            server.estimate_model_cost(model_name, **tokens),
            server.estimate_model_cost('5.6 sol standard', **tokens),
            places=10,
        )

    def test_codex_auto_review_uses_verified_gpt54_feature_pricing(self):
        model_name = 'codex-auto-review low'
        self.assertTrue(server.model_has_verified_pricing(model_name))
        self.assertTrue(server.model_has_cost_estimate(model_name))

        pricing = server.get_effective_pricing(model_name)
        self.assertTrue(pricing['pricing_verified'])
        self.assertFalse(pricing['pricing_estimated'])
        self.assertEqual(pricing['pricing_source'], 'official_feature_mapping')
        self.assertEqual(pricing['pricing_basis_model'], 'GPT-5.4')

        tokens = dict(
            input_tokens=1_000_000,
            output_tokens=100_000,
            cached_input_tokens=200_000,
            cache_write_input_tokens=0,
        )
        self.assertAlmostEqual(
            server.estimate_model_cost(model_name, **tokens),
            server.estimate_model_cost('gpt-5.4 low', **tokens),
            places=10,
        )

    def test_models_daily_timeline_prices_codex_auto_review(self):
        today = datetime.now().astimezone().date()
        event_dt = datetime.combine(today, datetime.min.time()).astimezone() + timedelta(hours=12)
        result = server.build_models_daily_timeline(
            [], {}, {'automatic_model_usage': {'recent_events': [{
                'ts': event_dt.astimezone(timezone.utc).isoformat(),
                'model_key': 'codex-auto-review low',
                'in': 1000, 'cached_in': 400, 'cache_write_in': 0, 'out': 200, 'tot': 1200,
            }]}}, days=7,
        )
        model = next(item for item in result['models'] if item['name'] == 'codex-auto-review low')
        expected = server.estimate_model_cost(
            'gpt-5.4 low', input_tokens=1000, output_tokens=200,
            cached_input_tokens=400, cache_write_input_tokens=0,
        )
        self.assertAlmostEqual(model['total_period_cost_usd'], expected, places=8)
        self.assertTrue(model['cost_complete'])
        self.assertEqual(model['unknown_cost_tokens'], 0)
        self.assertTrue(model['pricing_verified'])
        self.assertFalse(model['pricing_estimated'])
        self.assertEqual(model['pricing_basis_model'], 'GPT-5.4')

    def test_models_daily_timeline_prices_chatgpt_web_proxy(self):
        today = datetime.now().astimezone().date()
        event_dt = datetime.combine(today, datetime.min.time()).astimezone() + timedelta(hours=12)
        result = server.build_models_daily_timeline(
            [], {}, {'automatic_model_usage': {'recent_events': [{
                'ts': event_dt.astimezone(timezone.utc).isoformat(),
                'model_key': 'chatgpt-web/high high',
                'in': 1000, 'cached_in': 400, 'cache_write_in': 100, 'out': 200, 'tot': 1200,
            }]}}, days=7,
        )
        model = next(item for item in result['models'] if item['name'] == 'chatgpt-web/high high')
        expected = server.estimate_model_cost(
            '5.6 sol standard', input_tokens=1000, output_tokens=200,
            cached_input_tokens=400, cache_write_input_tokens=100,
        )
        self.assertAlmostEqual(model['total_period_cost_usd'], expected, places=8)
        self.assertTrue(model['cost_complete'])
        self.assertEqual(model['unknown_cost_tokens'], 0)
        self.assertTrue(model['pricing_estimated'])
        self.assertFalse(model['pricing_verified'])
        self.assertEqual(model['pricing_basis_model'], 'GPT-5.6 Sol Thinking')

    def test_models_daily_timeline_marks_unpriced_usage_unknown(self):
        today = datetime.now().astimezone().date()
        event_dt = datetime.combine(today, datetime.min.time()).astimezone() + timedelta(hours=12)
        result = server.build_models_daily_timeline(
            [{'dt': event_dt.astimezone(timezone.utc), 'model': 'Mystery Model', 'tokens': 777}],
            {}, {}, days=7,
        )
        model = next(item for item in result['models'] if item['name'] == 'Mystery Model')
        self.assertEqual(model['total_period_cost_usd'], 0.0)
        self.assertFalse(model['cost_complete'])
        self.assertEqual(model['unknown_cost_tokens'], 777)

    def test_models_daily_timeline_includes_codex_event_older_than_eight_days(self):
        today = datetime.now().astimezone().date()
        old_day = today - timedelta(days=20)
        old_event = datetime.combine(old_day, datetime.min.time()).astimezone() + timedelta(hours=12)
        result = server.build_models_daily_timeline(
            [],
            {},
            {'automatic_model_usage': {'recent_events': [{
                'ts': old_event.astimezone(timezone.utc).isoformat(),
                'model_key': 'gpt-test',
                'tot': 321,
            }]}},
            days=30,
        )
        model = next(item for item in result['models'] if item['name'] == 'gpt-test')
        self.assertEqual(model['total_period_tokens'], 321)
        self.assertEqual(model['daily_tokens'][result['date_keys'].index(old_day.isoformat())], 321)

    def test_codex_fast_is_a_separate_model_across_usage_quota_and_missions(self):
        sessions_dir = os.path.join(self._temp_dir.name, 'fast_sessions')
        os.makedirs(sessions_dir)
        log_path = os.path.join(sessions_dir, 'fast.jsonl')
        usage_cache = os.path.join(self._temp_dir.name, 'fast_usage_cache.json')
        mission_cache = os.path.join(self._temp_dir.name, 'fast_mission_cache.json')
        now = datetime.now(timezone.utc)
        rows = []

        def add(second, kind, payload):
            rows.append({
                'timestamp': (now - timedelta(minutes=2) + timedelta(seconds=second)).isoformat(),
                'type': kind, 'payload': payload,
            })

        def setting(second, tier):
            add(second, 'event_msg', {
                'type': 'thread_settings_applied', 'thread_settings': {'service_tier': tier},
            })

        def start(second, turn):
            add(second, 'event_msg', {'type': 'task_started', 'turn_id': turn})
            add(second + 0.1, 'turn_context', {
                'turn_id': turn, 'model': 'gpt-5.6-sol', 'effort': 'xhigh',
            })
            add(second + 0.2, 'response_item', {
                'type': 'message', 'role': 'user',
                'content': [{'type': 'input_text', 'text': 'Fix the web app'}],
            })

        def usage(second, request_id, tokens, cumulative):
            add(second, 'event_msg', {
                'type': 'token_count', 'request_id': request_id,
                'rate_limits': {'primary': {
                    'window_minutes': 300, 'used_percent': 10 + second,
                    'resets_at': (now + timedelta(hours=3)).isoformat(),
                }},
                'info': {
                    'last_token_usage': {
                        'input_tokens': tokens, 'output_tokens': 0, 'total_tokens': tokens,
                    },
                    'total_token_usage': {
                        'input_tokens': cumulative, 'output_tokens': 0,
                        'total_tokens': cumulative,
                    },
                },
            })

        setting(0, 'default')
        start(1, 'standard-1')
        usage(2, 's1', 100, 100)
        setting(3, 'priority')  # A change during a task applies to the next task.
        usage(4, 's1b', 20, 120)
        add(5, 'event_msg', {'type': 'task_complete', 'turn_id': 'standard-1'})
        start(6, 'fast-1')
        usage(7, 'f1', 200, 320)
        add(8, 'event_msg', {'type': 'task_complete', 'turn_id': 'fast-1'})
        setting(9, 'default')
        start(10, 'standard-2')
        usage(11, 's2', 50, 370)
        add(12, 'event_msg', {'type': 'task_complete', 'turn_id': 'standard-2'})
        with open(log_path, 'w', encoding='utf-8') as handle:
            for row in rows:
                handle.write(json.dumps(row) + '\n')

        model_usage = server.scan_codex_model_usage(
            sessions_dir=sessions_dir, cache_file=usage_cache, now=now,
        )
        standard = model_usage['models']['5.6 sol xhigh']
        fast = model_usage['models']['5.6 sol xhigh (fast)']
        self.assertEqual(standard['total_tokens'], 170)
        self.assertEqual(fast['total_tokens'], 200)
        self.assertEqual(fast['service_tier'], 'fast')
        self.assertAlmostEqual(
            fast['cost_usd'],
            2.5 * server.estimate_model_cost('5.6 sol xhigh', input_tokens=200),
            places=4,
        )
        self.assertTrue(any(
            item['model_key'] == '5.6 sol xhigh (fast)' and item['service_tier'] == 'fast'
            for item in model_usage['quota_observations']
        ))

        missions = server.scan_codex_mission_turns(
            sessions_dir=sessions_dir, cache_file=mission_cache, now=now,
        )
        by_turn = {item['turn_id']: item for item in missions['turns']}
        self.assertEqual(by_turn['standard-1']['model_key'], '5.6 sol xhigh')
        self.assertEqual(by_turn['fast-1']['model_key'], '5.6 sol xhigh (fast)')
        self.assertEqual(by_turn['standard-2']['model_key'], '5.6 sol xhigh')
        self.assertNotEqual(
            server.get_model_color('5.6 sol xhigh'),
            server.get_model_color('5.6 sol xhigh (fast)'),
        )

        # An appended setting must keep exactly-once accounting and restore
        # both the pending and active tier from the incremental cache.
        appended = [
            {'timestamp': now.isoformat(), 'type': 'event_msg',
             'payload': {'type': 'thread_settings_applied',
                         'thread_settings': {'service_tier': 'fast'}}},
            {'timestamp': now.isoformat(), 'type': 'event_msg',
             'payload': {'type': 'task_started', 'turn_id': 'fast-2'}},
            {'timestamp': now.isoformat(), 'type': 'turn_context',
             'payload': {'turn_id': 'fast-2', 'model': 'gpt-5.6-sol', 'effort': 'xhigh'}},
            {'timestamp': now.isoformat(), 'type': 'response_item',
             'payload': {'type': 'message', 'role': 'user',
                         'content': [{'type': 'input_text', 'text': 'Fix another page'}]}},
            {'timestamp': now.isoformat(), 'type': 'event_msg',
             'payload': {'type': 'token_count', 'request_id': 'f2',
                         'info': {'last_token_usage': {
                             'input_tokens': 10, 'output_tokens': 0, 'total_tokens': 10,
                         }}}},
        ]
        with open(log_path, 'a', encoding='utf-8') as handle:
            for row in appended:
                handle.write(json.dumps(row) + '\n')
        model_usage = server.scan_codex_model_usage(
            sessions_dir=sessions_dir, cache_file=usage_cache, now=now,
        )
        self.assertEqual(model_usage['models']['5.6 sol xhigh (fast)']['total_tokens'], 210)
        missions = server.scan_codex_mission_turns(
            sessions_dir=sessions_dir, cache_file=mission_cache, now=now,
        )
        self.assertEqual(
            next(item for item in missions['turns'] if item['turn_id'] == 'fast-2')['model_key'],
            '5.6 sol xhigh (fast)',
        )

    def test_models_daily_timeline_rejects_invalid_custom_ranges(self):
        today = datetime.now().astimezone().date()
        with self.assertRaises(ValueError):
            server.build_models_daily_timeline(
                [], {}, {}, start_date=today.isoformat(), end_date=(today - timedelta(days=1)).isoformat()
            )
        with self.assertRaises(ValueError):
            server.build_models_daily_timeline(
                [], {}, {}, start_date=today.isoformat(), end_date=(today + timedelta(days=1)).isoformat()
            )
        with self.assertRaises(ValueError):
            server.build_models_daily_timeline(
                [], {}, {}, start_date=(today - timedelta(days=366)).isoformat(), end_date=today.isoformat()
            )


if __name__ == '__main__':
    unittest.main()
