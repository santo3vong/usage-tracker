import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

import server


def turn(turn_id, thread_id, at, prompt, model='5.6 sol high', parent='', subagent=False,
         completed=True, assistant_messages=1, cwd=r'C:\work'):
    return {
        'turn_id': turn_id,
        'thread_id': thread_id,
        'parent_thread_id': parent,
        'is_subagent': subagent,
        'started_at': at,
        'completed_at': at,
        'completed': completed,
        'model_key': model,
        'cwd': cwd,
        'user_text': prompt,
        'user_tokens_est': server.estimate_tokens(prompt),
        'user_message_count': 1,
        'assistant_message_count': assistant_messages,
    }


def usage(turn_id, model, total, at):
    return {
        'task_id': turn_id,
        'model_key': model,
        'ts': at,
        'in': max(0, total - 10),
        'cached_in': 0,
        'cache_write_in': 0,
        'out': 10,
        'think': 2,
        'tot': total,
    }


class CodexTaskOutcomeTests(unittest.TestCase):
    def test_aborted_no_response_turn_does_not_contaminate_model_route(self):
        turns = [
            turn('ghost', 'thread-a', '2026-09-05T01:08:32+00:00',
                 'Kiểm tra toàn bộ quy trình setup giao dịch.', model='5.6 sol high',
                 completed=False, assistant_messages=0),
            turn('astra', 'thread-a', '2026-09-05T01:08:52+00:00',
                 'Kiểm tra toàn bộ quy trình setup giao dịch.', model='gpt-6-astra high'),
            turn('git', 'thread-a', '2026-09-05T01:35:20+00:00',
                 'git là sao nhỉ, với công việc của tôi có cần thiết phải có git ko',
                 model='gpt-6-astra high'),
        ]
        events = [
            usage('astra', 'gpt-6-astra high', 120, '2026-09-05T01:20:00+00:00'),
            usage('git', 'gpt-6-astra high', 30, '2026-09-05T01:36:00+00:00'),
        ]

        result = server.build_codex_task_outcomes(
            {'turns': turns, 'diagnostics': {}}, events,
            reviews={'version': 1, 'turn_boundaries': {}, 'missions': {}},
            now=datetime(2026, 9, 11, tzinfo=timezone.utc),
        )

        first = next(m for m in result['missions'] if m['anchor_turn_id'] == 'astra')
        self.assertEqual(first['route'], ['gpt-6-astra high'])
        self.assertTrue(first['pure_model'])
        self.assertEqual(first['turn_count'], 1)
        self.assertNotIn('ghost', first['turn_ids'])

    def test_independent_git_and_model_reasoning_prompts_form_new_missions(self):
        turns = [
            turn('setup', 'thread-a', '2026-09-05T01:08:52+00:00',
                 'Kiểm tra toàn bộ quy trình setup giao dịch.', model='gpt-6-astra high'),
            turn('git-q', 'thread-a', '2026-09-05T01:35:20+00:00',
                 'git là sao nhỉ, với công việc của tôi có cần thiết phải có git ko',
                 model='gpt-6-astra high'),
            turn('git-follow', 'thread-a', '2026-09-05T01:42:26+00:00',
                 'vậy giờ tôi tạo github và đưa lên cloud được ko, tôi chưa làm điều này bao giờ',
                 model='gpt-6-astra high'),
            turn('reasoning', 'thread-a', '2026-09-05T05:36:01+00:00',
                 'Tôi thấy model Astra mạnh hơn. Dùng mức suy luận high có thừa không?',
                 model='gpt-6-astra high'),
        ]
        events = [usage(item['turn_id'], item['model_key'], 40, item['started_at']) for item in turns]

        result = server.build_codex_task_outcomes(
            {'turns': turns, 'diagnostics': {}}, events,
            reviews={'version': 1, 'turn_boundaries': {}, 'missions': {}},
            now=datetime(2026, 9, 11, tzinfo=timezone.utc),
        )

        missions = {m['anchor_turn_id']: m for m in result['missions']}
        self.assertEqual(set(missions), {'setup', 'git-q', 'reasoning'})
        self.assertEqual(missions['git-q']['turn_ids'], ['git-q', 'git-follow'])
        self.assertEqual(missions['git-q']['category'], 'software_debugging')
        self.assertEqual(missions['reasoning']['category'], 'research')
        self.assertTrue(missions['setup']['accepted'])
        self.assertTrue(missions['git-q']['accepted'])

    def test_genuine_multi_model_followup_remains_mixed(self):
        turns = [
            turn('a', 'thread-a', '2026-09-01T10:00:00+00:00',
                 'Tôi muốn làm web usage tracker.', model='gpt-6-astra high'),
            turn('b', 'thread-a', '2026-09-01T10:05:00+00:00',
                 'làm tiếp phần này đi', model='5.6 sol high'),
            turn('ok', 'thread-a', '2026-09-01T10:10:00+00:00', 'Ok tốt rồi.'),
        ]
        events = [
            usage('a', 'gpt-6-astra high', 100, '2026-09-01T10:02:00+00:00'),
            usage('b', '5.6 sol high', 80, '2026-09-01T10:08:00+00:00'),
        ]

        result = server.build_codex_task_outcomes(
            {'turns': turns, 'diagnostics': {}}, events,
            reviews={'version': 1, 'turn_boundaries': {}, 'missions': {}},
            now=datetime(2026, 9, 11, tzinfo=timezone.utc),
        )

        mission = result['missions'][0]
        self.assertFalse(mission['pure_model'])
        self.assertEqual(mission['route'], ['gpt-6-astra high', '5.6 sol high'])

    def test_groups_corrections_until_explicit_acceptance(self):
        turns = [
            turn('t1', 'thread-a', '2026-09-01T10:00:00+00:00', 'Tôi muốn học setup giao dịch bằng ví dụ này.'),
            turn('t2', 'thread-a', '2026-09-01T10:10:00+00:00', 'Chưa đúng, tôi giảng lại quy tắc setup cho bạn.'),
            turn('t3', 'thread-a', '2026-09-01T10:20:00+00:00', 'Ok tốt rồi.'),
            turn('t4', 'thread-a', '2026-09-01T10:30:00+00:00', 'Giờ tôi muốn làm web theo dõi usage tracker.', model='5.6 luna xhigh'),
        ]
        events = [
            usage('t1', '5.6 sol high', 100, '2026-09-01T10:05:00+00:00'),
            usage('t2', '5.6 sol high', 80, '2026-09-01T10:15:00+00:00'),
            usage('t4', '5.6 luna xhigh', 50, '2026-09-01T10:35:00+00:00'),
        ]

        result = server.build_codex_task_outcomes(
            {'turns': turns, 'diagnostics': {}}, events,
            reviews={'version': 1, 'turn_boundaries': {}, 'missions': {}},
            now=datetime(2026, 9, 11, tzinfo=timezone.utc),
        )

        self.assertEqual(len(result['missions']), 2)
        learned = next(m for m in result['missions'] if m['anchor_turn_id'] == 't1')
        self.assertEqual(learned['status'], 'accepted_explicit')
        self.assertEqual(learned['category'], 'trading_setup')
        self.assertEqual(learned['turn_count'], 2)
        self.assertEqual(learned['correction_turns'], 1)
        self.assertEqual(learned['total_tokens'], 180)
        self.assertGreater(learned['added_guidance_tokens_est'], 0)
        self.assertTrue(learned['pure_model'])

    def test_matrix_uses_same_category_sol_high_baseline(self):
        missions = []
        for index, tokens in enumerate((90, 100, 110)):
            missions.append({
                'accepted': True, 'pure_model': True, 'category': 'trading_setup',
                'model_key': '5.6 sol high', 'total_tokens': tokens, 'cost_usd': 1.0,
                'cost_known': True, 'correction_turns': 1, 'added_guidance_tokens_est': 20,
                'first_pass_success': False, 'status': 'accepted_explicit',
                'start_at': f'2026-09-0{index + 1}T00:00:00+00:00',
            })
        for index, tokens in enumerate((60, 70, 80)):
            missions.append({
                'accepted': True, 'pure_model': True, 'category': 'trading_setup',
                'model_key': '5.6 luna xhigh', 'total_tokens': tokens, 'cost_usd': 0.5,
                'cost_known': True, 'correction_turns': 0, 'added_guidance_tokens_est': 0,
                'first_pass_success': True, 'status': 'accepted_manual',
                'start_at': f'2026-09-0{index + 1}T01:00:00+00:00',
            })

        matrix = server._codex_task_matrix(
            missions, None, datetime(2026, 9, 11, tzinfo=timezone.utc)
        )
        trading = next(row for row in matrix['rows'] if row['category'] == 'trading_setup')
        self.assertEqual(trading['cells']['5.6 sol high']['relative_tokens_vs_sol_high'], 1.0)
        self.assertEqual(trading['cells']['5.6 luna xhigh']['relative_tokens_vs_sol_high'], 0.7)
        documents = next(row for row in matrix['rows'] if row['category'] == 'documents')
        self.assertEqual(documents['cells']['5.6 luna xhigh']['sample_status'], 'no_data')

    def test_review_file_controls_category_outcome_and_boundaries(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, 'reviews.json')
            server.update_codex_mission_review({
                'action': 'review', 'anchor_turn_id': 'turn-a',
                'category': 'web', 'outcome': 'accepted',
            }, path)
            server.update_codex_mission_review({
                'action': 'merge_previous', 'anchor_turn_id': 'turn-b',
            }, path)
            server.update_codex_mission_review({
                'action': 'split_last', 'turn_id': 'turn-c',
            }, path)

            saved = server.load_codex_mission_reviews(path)
            self.assertEqual(saved['missions']['turn-a']['category'], 'web')
            self.assertEqual(saved['missions']['turn-a']['outcome'], 'accepted')
            self.assertEqual(saved['turn_boundaries']['turn-b'], 'continue')
            self.assertEqual(saved['turn_boundaries']['turn-c'], 'new')

    def test_subagent_tokens_attach_to_parent_and_leave_matrix(self):
        turns = [
            turn('parent-turn', 'parent-thread', '2026-09-01T10:00:00+00:00', 'Tôi muốn làm web usage tracker.'),
            turn('worker-turn', 'worker-thread', '2026-09-01T10:05:00+00:00', 'Audit the frontend.',
                 model='5.6 luna xhigh', parent='parent-thread', subagent=True),
            turn('ack-turn', 'parent-thread', '2026-09-01T10:20:00+00:00', 'Ok tốt rồi.'),
        ]
        events = [
            usage('parent-turn', '5.6 sol high', 100, '2026-09-01T10:03:00+00:00'),
            usage('worker-turn', '5.6 luna xhigh', 40, '2026-09-01T10:08:00+00:00'),
        ]
        result = server.build_codex_task_outcomes(
            {'turns': turns, 'diagnostics': {}}, events,
            reviews={'version': 1, 'turn_boundaries': {}, 'missions': {}},
            now=datetime(2026, 9, 11, tzinfo=timezone.utc),
        )

        self.assertEqual(len(result['missions']), 1)
        mission = result['missions'][0]
        self.assertEqual(mission['total_tokens'], 140)
        self.assertEqual(mission['delegated_turn_count'], 1)
        self.assertFalse(mission['pure_model'])
        self.assertEqual(result['diagnostics']['delegated_turns_attached'], 1)

    def test_turn_scanner_uses_cache_and_skips_injected_context(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sessions = os.path.join(temp_dir, 'sessions')
            os.makedirs(sessions)
            cache = os.path.join(temp_dir, 'turn-cache.json')
            log_path = os.path.join(sessions, 'rollout-a.jsonl')
            records = [
                {'type': 'session_meta', 'timestamp': '2026-09-01T10:00:00Z',
                 'payload': {'id': 'thread-a', 'cwd': r'C:\work\usage-tracker'}},
                {'type': 'event_msg', 'timestamp': '2026-09-01T10:00:01Z',
                 'payload': {'type': 'task_started', 'turn_id': 'turn-a', 'started_at': 1788256801}},
                {'type': 'response_item', 'timestamp': '2026-09-01T10:00:02Z',
                 'payload': {'type': 'message', 'role': 'user',
                             'content': [{'type': 'input_text', 'text': '<environment_context>hidden</environment_context>'}]}},
                {'type': 'response_item', 'timestamp': '2026-09-01T10:00:02Z',
                 'payload': {'type': 'message', 'role': 'user',
                             'content': [{'type': 'input_text', 'text': '<in-app-browser-context source="ambient-ui-state">hidden</in-app-browser-context>'}]}},
                {'type': 'turn_context', 'timestamp': '2026-09-01T10:00:03Z',
                 'payload': {'turn_id': 'turn-a', 'model': 'gpt-5.6-sol', 'effort': 'high'}},
                {'type': 'response_item', 'timestamp': '2026-09-01T10:00:04Z',
                 'payload': {'type': 'message', 'role': 'user',
                             'content': [{'type': 'input_text', 'text': 'Làm web usage tracker.'}]}},
                {'type': 'event_msg', 'timestamp': '2026-09-01T10:05:00Z',
                 'payload': {'type': 'task_complete', 'turn_id': 'turn-a', 'completed_at': 1788257100}},
            ]
            with open(log_path, 'w', encoding='utf-8') as handle:
                for record in records:
                    handle.write(json.dumps(record, ensure_ascii=False) + '\n')

            first = server.scan_codex_mission_turns(sessions_dir=sessions, cache_file=cache)
            second = server.scan_codex_mission_turns(sessions_dir=sessions, cache_file=cache)
            self.assertEqual(len(first['turns']), 1)
            self.assertEqual(first['turns'][0]['user_text'], 'Làm web usage tracker.')
            self.assertEqual(first['turns'][0]['thread_id'], 'thread-a')
            self.assertTrue(first['turns'][0]['completed'])
            self.assertEqual(second['diagnostics']['files_cached'], 1)
            self.assertEqual(second['diagnostics']['bytes_read'], 0)


if __name__ == '__main__':
    unittest.main()
