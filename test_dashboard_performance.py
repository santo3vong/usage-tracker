import unittest

import server


class DashboardPerformanceTests(unittest.TestCase):
    def test_dashboard_payload_keeps_derived_results_without_raw_scan_ledgers(self):
        source = {
            'summary': {
                'codex_usage': {
                    'rate_limits': {'weekly': 42},
                    'automatic_model_usage': {
                        'recent_events': [{'task_id': 'one'}],
                        'quota_observations': [{'remaining': 85}],
                        'task_outcomes': {'missions': [{'id': 'one'}]},
                        'quota_efficiency_timeline': {'points': [1.2]},
                    },
                },
            },
            'conversations': [{'id': 'one'}],
        }

        dashboard = server.dashboard_payload(source)
        auto = dashboard['summary']['codex_usage']['automatic_model_usage']
        self.assertNotIn('recent_events', auto)
        self.assertNotIn('quota_observations', auto)
        self.assertEqual(auto['task_outcomes'], {'missions': [{'id': 'one'}]})
        self.assertEqual(auto['quota_efficiency_timeline'], {'points': [1.2]})
        self.assertEqual(dashboard['conversations'], source['conversations'])
        self.assertIn('recent_events', source['summary']['codex_usage']['automatic_model_usage'])
        self.assertIs(server.dashboard_payload(source, include_raw=True), source)
        self.assertIs(server.dashboard_codex_usage(source['summary']['codex_usage'], include_raw=True),
                      source['summary']['codex_usage'])

    def test_cached_repair_topics_preserve_candidate_ranking(self):
        prior = [
            {'id': 'old', 'thread_id': 'thread-a', 'title': 'Sửa biểu đồ quota tuần',
             'start_at': '2026-09-20T10:00:00Z', 'end_at': '2026-09-20T11:00:00Z',
             'categories': ['web'], 'turns': [{'prompt': 'biểu đồ quota tuần bị lệch'}]},
            {'id': 'newer', 'thread_id': 'thread-b', 'title': 'Bảng model',
             'start_at': '2026-09-21T10:00:00Z', 'end_at': '2026-09-21T11:00:00Z',
             'categories': ['web'], 'turns': [{'prompt': 'thêm model mới'}]},
        ]
        repair = {
            'id': 'repair', 'thread_id': 'thread-c',
            'title': 'Sửa lại biểu đồ quota tuần',
            'start_at': '2026-09-22T10:00:00Z',
            'categories': ['web'],
            'turns': [{'prompt': 'codex://threads/thread-a biểu đồ quota tuần vẫn lệch'}],
            'referenced_thread_ids': ['thread-a'],
        }
        uncached = server._codex_repair_link_candidates(repair, prior)
        cache = {
            id(mission): server._codex_mission_repair_topic_tokens(mission)
            for mission in [*prior, repair]
        }
        cached = server._codex_repair_link_candidates(repair, prior, topic_tokens_cache=cache)
        self.assertEqual(cached, uncached)
        self.assertEqual(cached[0]['mission_id'], 'old')


if __name__ == '__main__':
    unittest.main()
