import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

import server


def turn(turn_id, thread_id, at, prompt, model='5.6 sol high', parent='', subagent=False,
         completed=True, assistant_messages=1, cwd=r'C:\work', assistant_text='',
         tool_calls=0, tool_successes=0, tool_failures=0, task_error=''):
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
        'assistant_text': assistant_text,
        'tool_call_count': tool_calls,
        'tool_success_count': tool_successes,
        'tool_failure_count': tool_failures,
        'task_error': task_error,
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
    def test_tool_output_failure_parser_uses_explicit_result_evidence(self):
        self.assertFalse(server._codex_tool_output_failed('Process exited with code 0'))
        self.assertTrue(server._codex_tool_output_failed('Process exited with code 1'))
        self.assertTrue(server._codex_tool_output_failed({'isError': True}))
        self.assertTrue(server._codex_tool_output_failed({'exit_code': 2}))

    def test_delegation_envelope_is_recognized_as_subagent_from_cached_turn(self):
        raw = turn(
            'delegated', 'worker-thread', '2026-09-10T10:00:00+00:00',
            '<codex_delegation><source_thread_id>parent-thread</source_thread_id>'
            '<input>continue the task</input></codex_delegation>',
        )
        logical = server._expand_codex_mission_turn(raw)
        self.assertEqual(len(logical), 1)
        self.assertTrue(logical[0]['is_subagent'])
        self.assertEqual(logical[0]['thread_source'], 'subagent')
        self.assertEqual(logical[0]['parent_thread_id'], 'parent-thread')

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

    def test_usage_only_turn_without_model_response_is_excluded_from_model_evaluation(self):
        turns = [
            turn(
                'quota-stop', 'thread-a', '2026-09-16T19:15:20+00:00',
                'Sửa lỗi Excel Mobile rồi lưu lại file.', model='chatgpt-web/high high',
                completed=False, assistant_messages=0,
            ),
        ]
        result = server.build_codex_task_outcomes(
            {'turns': turns, 'diagnostics': {}},
            [usage('quota-stop', 'chatgpt-web/high high', 120, '2026-09-16T19:15:30+00:00')],
            reviews={'version': 1, 'turn_boundaries': {}, 'missions': {}},
            now=datetime(2026, 9, 17, tzinfo=timezone.utc),
        )

        mission = result['missions'][0]
        self.assertEqual(mission['status'], 'excluded_no_response_auto')
        self.assertEqual(mission['infrastructure_exclusion_reason'], 'no_model_response')
        self.assertFalse(server._codex_mission_usable_for_matrix(mission))

    def test_explicit_zero_tool_infrastructure_block_is_excluded(self):
        turns = [
            turn(
                'bridge-stop', 'thread-a', '2026-09-16T20:00:00+00:00',
                'Bạn sửa trực tiếp workbook đi.', model='chatgpt-web/high high',
                assistant_text=(
                    'Hiện tại tôi chưa có kênh công cụ thao tác file nên không thể sửa trực tiếp. '
                    'Bạn cần mở lại task có quyền workspace.'
                ),
                task_error='stream disconnected before completion: launcher browser control channel failed',
            ),
        ]
        result = server.build_codex_task_outcomes(
            {'turns': turns, 'diagnostics': {}}, [],
            reviews={'version': 1, 'turn_boundaries': {}, 'missions': {}},
            now=datetime(2026, 9, 17, tzinfo=timezone.utc),
        )

        mission = result['missions'][0]
        self.assertEqual(mission['status'], 'excluded_infrastructure_auto')
        self.assertEqual(mission['infrastructure_exclusion_reason'], 'tool_or_bridge_unavailable')

    def test_successful_tool_work_is_not_auto_excluded_by_later_harness_wording(self):
        turns = [
            turn(
                'worked', 'thread-a', '2026-09-16T20:10:00+00:00',
                'Sửa trực tiếp workbook rồi kiểm tra.', model='chatgpt-web/high high',
                assistant_text='Tôi đã đọc file, nhưng lượt sau chưa có kênh công cụ thao tác để hoàn tất.',
                tool_calls=1, tool_successes=1,
            ),
        ]
        result = server.build_codex_task_outcomes(
            {'turns': turns, 'diagnostics': {}},
            [usage('worked', 'chatgpt-web/high high', 80, '2026-09-16T20:10:10+00:00')],
            reviews={'version': 1, 'turn_boundaries': {}, 'missions': {}},
            now=datetime(2026, 9, 17, tzinfo=timezone.utc),
        )

        mission = result['missions'][0]
        self.assertFalse(mission['status'].startswith('excluded'))
        self.assertFalse(mission['infrastructure_blocked'])

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

    def test_explicit_acceptance_without_model_response_is_still_used(self):
        turns = [
            turn(
                'answer', 'thread-a', '2026-09-01T10:00:00+00:00',
                'lần trước tôi đã huấn luyện chọn TP2 tới tuần nào',
                assistant_text='Mốc đúng là W30; W31 đã phân tích nhưng chưa được xác nhận.',
            ),
            turn(
                'confirmation', 'thread-a', '2026-09-01T10:05:00+00:00',
                'ok đúng rồi đấy', completed=False, assistant_messages=0,
            ),
        ]
        result = server.build_codex_task_outcomes(
            {'turns': turns, 'diagnostics': {}},
            [usage('answer', '5.6 sol high', 100, '2026-09-01T10:01:00+00:00')],
            reviews={'version': 1, 'turn_boundaries': {}, 'missions': {}},
            now=datetime(2026, 9, 11, tzinfo=timezone.utc),
        )
        self.assertEqual(len(result['missions']), 1)
        self.assertEqual(result['missions'][0]['status'], 'accepted_explicit')

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

    def test_matrix_bridges_missing_baseline_and_sparse_model_cells(self):
        missions = []

        def add_samples(category, model, tokens, day_prefix):
            for index, total_tokens in enumerate(tokens):
                missions.append({
                    'accepted': True,
                    'pure_model': True,
                    'category': category,
                    'model_key': model,
                    'total_tokens': total_tokens,
                    'cost_usd': 0.0,
                    'cost_known': False,
                    'correction_turns': 0,
                    'added_guidance_tokens_est': 0,
                    'first_pass_success': True,
                    'status': 'accepted_explicit',
                    'start_at': f'2026-09-{day_prefix + index:02d}T00:00:00+00:00',
                })

        # Historical bridge: Sol ↔ Luna in one category, then Luna ↔ Astra in another.
        add_samples('trading_setup', '5.6 sol high', (90, 100, 110), 1)
        add_samples('trading_setup', '5.6 luna xhigh', (45, 50, 55), 1)
        add_samples('documents', '5.6 luna xhigh', (54, 60, 66), 4)
        add_samples('documents', 'gpt-6 astra low', (27, 30, 33), 4)

        # Current range has only Astra in this category.
        add_samples('software_debugging', 'gpt-6 astra low', (24, 25, 26), 10)
        # A single current Astra sample should still support a low-confidence bridged comparison.
        add_samples('research', 'gpt-6 astra low', (25,), 11)

        matrix = server._codex_task_matrix(
            missions, 2, datetime(2026, 9, 12, tzinfo=timezone.utc)
        )
        self.assertEqual(matrix['baseline_model'], '5.6 sol high')
        self.assertIn('5.6 luna xhigh', matrix['models'])

        debugging = next(row for row in matrix['rows'] if row['category'] == 'software_debugging')
        astra = debugging['cells']['gpt-6 astra low']
        self.assertEqual(astra['sample_status'], 'estimated')
        self.assertEqual(astra['baseline_source'], 'bridge')
        self.assertEqual(astra['relative_tokens_vs_sol_high'], 0.25)
        self.assertEqual(
            astra['baseline_bridge_path'],
            ['gpt-6 astra low', '5.6 luna xhigh', '5.6 sol high'],
        )
        self.assertEqual(astra['baseline_bridge_hops'], 2)
        self.assertEqual(astra['confidence'], 'low')

        luna = debugging['cells']['5.6 luna xhigh']
        self.assertEqual(luna['sample_count'], 0)
        self.assertEqual(luna['sample_status'], 'estimated')
        self.assertEqual(luna['estimated_total_tokens'], 50)
        self.assertEqual(luna['relative_tokens_vs_sol_high'], 0.5)
        self.assertEqual(luna['bridge_path'], ['gpt-6 astra low', '5.6 luna xhigh'])

        research = next(row for row in matrix['rows'] if row['category'] == 'research')
        sparse_astra = research['cells']['gpt-6 astra low']
        self.assertEqual(sparse_astra['sample_count'], 1)
        self.assertEqual(sparse_astra['model_value_source'], 'sparse_direct')
        self.assertEqual(sparse_astra['effective_total_tokens'], 25)
        self.assertEqual(sparse_astra['sample_status'], 'estimated')
        self.assertEqual(sparse_astra['baseline_source'], 'bridge')
        self.assertEqual(sparse_astra['baseline_bridge_anchor_sample_count'], 1)
        self.assertEqual(sparse_astra['relative_tokens_vs_sol_high'], 0.25)
        self.assertEqual(sparse_astra['confidence'], 'low')

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

    def test_review_file_accepts_multiple_categories_and_keeps_legacy_primary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, 'reviews.json')
            server.update_codex_mission_review({
                'action': 'review', 'anchor_turn_id': 'turn-mixed',
                'categories': ['documents', 'system_diagnostics', 'software_debugging'],
            }, path)

            saved = server.load_codex_mission_reviews(path)
            review = saved['missions']['turn-mixed']
            self.assertEqual(review['category'], 'documents')
            self.assertEqual(
                review['categories'],
                ['documents', 'system_diagnostics', 'software_debugging'],
            )

    def test_manual_category_review_clears_low_confidence_turn_review(self):
        turns = [turn(
            'vague', 'thread-vague', '2026-09-10T10:00:00+00:00',
            'ok bạn làm tiếp đi',
        )]
        reviews = {
            'version': 1, 'turn_boundaries': {},
            'missions': {'vague': {'categories': ['documents'], 'category': 'documents'}},
        }
        result = server.build_codex_task_outcomes(
            {'turns': turns, 'diagnostics': {}},
            [usage('vague', '5.6 sol high', 100, '2026-09-10T10:01:00+00:00')],
            reviews=reviews,
            now=datetime(2026, 9, 11, tzinfo=timezone.utc),
        )
        mission = result['missions'][0]
        self.assertTrue(mission['category_reviewed'])
        self.assertFalse(mission['needs_category_review'])
        self.assertEqual(result['summary']['review_case_count'], 0)

    def test_vague_new_thread_uses_final_assistant_conclusion_for_category(self):
        item = turn(
            'vague', 'thread-vague', '2026-09-10T10:00:00+00:00',
            'ok bạn làm đi',
            assistant_text='Đã sửa xong Usage Tracker dashboard và kiểm tra HTML, CSS, JavaScript.',
        )
        result = server.build_codex_task_outcomes(
            {'turns': [item], 'diagnostics': {}},
            [usage('vague', '5.6 sol high', 100, '2026-09-10T10:01:00+00:00')],
            reviews={'version': 1, 'turn_boundaries': {}, 'missions': {}},
            now=datetime(2026, 9, 11, tzinfo=timezone.utc),
        )
        mission = result['missions'][0]
        self.assertEqual(mission['category'], 'web')
        self.assertEqual(mission['category_confidence'], 'medium')
        self.assertTrue(mission['turns'][0]['category_from_assistant'])
        self.assertFalse(mission['needs_category_review'])

    def test_vague_next_mission_inherits_previous_same_thread_category(self):
        turns = [
            turn('web', 'thread-one', '2026-09-10T10:00:00+00:00', 'sửa web usage tracker'),
            turn('accept', 'thread-one', '2026-09-10T10:05:00+00:00', 'ok tốt rồi'),
            turn('next', 'thread-one', '2026-09-10T10:10:00+00:00', 'ok bạn làm tiếp đi'),
        ]
        result = server.build_codex_task_outcomes(
            {'turns': turns, 'diagnostics': {}},
            [
                usage('web', '5.6 sol high', 100, '2026-09-10T10:01:00+00:00'),
                usage('next', '5.6 sol high', 50, '2026-09-10T10:11:00+00:00'),
            ],
            reviews={'version': 1, 'turn_boundaries': {}, 'missions': {}},
            now=datetime(2026, 9, 11, tzinfo=timezone.utc),
        )
        missions = {mission['anchor_turn_id']: mission for mission in result['missions']}
        self.assertEqual(missions['next']['category'], 'web')
        self.assertTrue(missions['next']['turns'][0]['category_inherited'])
        self.assertFalse(missions['next']['needs_category_review'])

    def test_vague_continuation_uses_previous_category_over_assistant_process_text(self):
        turns = [
            turn('cockpit', 'thread-cockpit', '2026-09-10T10:00:00+00:00',
                 'xây dựng mô phỏng 3d buồng lái'),
            turn('accept', 'thread-cockpit', '2026-09-10T10:05:00+00:00', 'ok tốt rồi'),
            turn(
                'next', 'thread-cockpit', '2026-09-10T10:10:00+00:00',
                'bạn test gì mà lâu thế',
                assistant_text='Tôi đang kiểm tra quy trình và sẽ báo cáo kết quả nghiên cứu.',
            ),
        ]
        result = server.build_codex_task_outcomes(
            {'turns': turns, 'diagnostics': {}},
            [
                usage('cockpit', '5.6 sol high', 100, '2026-09-10T10:01:00+00:00'),
                usage('next', '5.6 sol high', 50, '2026-09-10T10:11:00+00:00'),
            ],
            reviews={'version': 1, 'turn_boundaries': {}, 'missions': {}},
            now=datetime(2026, 9, 11, tzinfo=timezone.utc),
        )
        missions = {mission['anchor_turn_id']: mission for mission in result['missions']}
        self.assertEqual(missions['next']['categories'], ['simulation_3d'])
        self.assertTrue(missions['next']['turns'][0]['category_inherited'])
        self.assertFalse(missions['next']['needs_category_review'])

    def test_vague_opening_turn_backfills_from_later_concrete_work(self):
        turns = [
            turn('vague', 'thread-forward', '2026-09-10T10:00:00+00:00',
                 'bạn có xem được nội dung đoạn chat đó không'),
            turn('concrete', 'thread-forward', '2026-09-10T10:05:00+00:00',
                 'hãy phân tích bộ filter GBPUSD trong đoạn chat đó'),
        ]
        result = server.build_codex_task_outcomes(
            {'turns': turns, 'diagnostics': {}},
            [
                usage('vague', '5.6 sol high', 50, '2026-09-10T10:01:00+00:00'),
                usage('concrete', '5.6 sol high', 100, '2026-09-10T10:06:00+00:00'),
            ],
            reviews={'version': 1, 'turn_boundaries': {}, 'missions': {}},
            now=datetime(2026, 9, 11, tzinfo=timezone.utc),
        )
        mission = result['missions'][0]
        self.assertNotIn('other', mission['categories'])
        self.assertEqual(mission['turns'][0]['category'], 'trading_setup')
        self.assertTrue(mission['turns'][0]['category_inherited'])
        self.assertFalse(mission['needs_category_review'])

    def test_review_file_can_exclude_non_task_from_matrix_and_review_queues(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, 'reviews.json')
            server.update_codex_mission_review({
                'action': 'review', 'anchor_turn_id': 'greeting-turn',
                'outcome': 'excluded',
            }, path)
            reviews = server.load_codex_mission_reviews(path)
            self.assertEqual(reviews['missions']['greeting-turn']['outcome'], 'excluded')

            turns = [
                turn('greeting-turn', 'thread-greeting', '2026-09-10T10:00:00+00:00',
                     'ghi chú thử không thuộc nhiệm vụ cần so sánh'),
            ]
            events = [usage(
                'greeting-turn', '5.6 sol high', 100,
                '2026-09-10T10:01:00+00:00',
            )]
            result = server.build_codex_task_outcomes(
                {'turns': turns, 'diagnostics': {}}, events,
                reviews=reviews,
                now=datetime(2026, 9, 11, tzinfo=timezone.utc),
            )
            mission = result['missions'][0]
            self.assertEqual(mission['status'], 'excluded_manual')
            self.assertTrue(mission['outcome_reviewed'])
            self.assertFalse(mission['accepted'])
            self.assertFalse(mission['needs_review'])
            self.assertFalse(mission['audit_needed'])
            self.assertEqual(result['summary']['excluded_count'], 1)
            self.assertEqual(result['summary']['unresolved_count'], 0)
            self.assertEqual(result['summary']['review_case_count'], 0)
            self.assertEqual(result['matrices']['all']['eligible_missions'], 0)

    def test_review_file_keeps_inferred_acceptance_distinct_from_direct_confirmation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, 'reviews.json')
            server.update_codex_mission_review({
                'action': 'review', 'anchor_turn_id': 'old-question',
                'outcome': 'inferred',
            }, path)
            reviews = server.load_codex_mission_reviews(path)
            self.assertEqual(reviews['missions']['old-question']['outcome'], 'inferred')

            result = server.build_codex_task_outcomes(
                {'turns': [turn(
                    'old-question', 'thread-question', '2026-09-10T10:00:00+00:00',
                    'hạn mức codex go so với codex free có khác nhau không',
                )], 'diagnostics': {}},
                [usage(
                    'old-question', '5.6 sol high', 100,
                    '2026-09-10T10:01:00+00:00',
                )],
                reviews=reviews,
                now=datetime(2026, 9, 11, tzinfo=timezone.utc),
            )
            mission = result['missions'][0]
            self.assertEqual(mission['status'], 'accepted_inferred_review')
            self.assertEqual(mission['status_confidence'], 'medium')
            self.assertTrue(mission['accepted'])
            self.assertTrue(mission['outcome_reviewed'])
            self.assertEqual(result['summary']['accepted_count'], 1)
            self.assertEqual(result['summary']['unresolved_count'], 0)

    def test_completed_informational_answer_is_inferred_without_manual_review(self):
        question = turn(
            'question', 'thread-question', '2026-09-10T10:00:00+00:00',
            'tôi muốn hỏi cache hit là gì và có tốn hạn mức không',
            assistant_text='Cache hit là phần ngữ cảnh đã có trong bộ nhớ đệm. Nó vẫn được tính theo cơ chế của dịch vụ.',
        )
        result = server.build_codex_task_outcomes(
            {'turns': [question], 'diagnostics': {}},
            [usage('question', '5.6 sol high', 100, '2026-09-10T10:01:00+00:00')],
            reviews={'version': 1, 'turn_boundaries': {}, 'missions': {}},
            now=datetime(2026, 9, 11, tzinfo=timezone.utc),
        )
        mission = result['missions'][0]
        self.assertEqual(mission['status'], 'accepted_inferred')
        self.assertTrue(mission['accepted'])
        self.assertFalse(mission['outcome_reviewed'])

    def test_completion_claim_with_required_real_device_check_stays_unresolved(self):
        item = turn(
            'mobile-fix', 'thread-mobile', '2026-09-10T10:00:00+00:00',
            'sửa công thức Excel để chạy trên điện thoại',
            assistant_text=(
                'Đã sửa xong công thức trên file. Tuy nhiên chưa trực tiếp kiểm tra trên điện thoại; '
                'cần thử trên điện thoại để xác nhận.'
            ),
        )
        result = server.build_codex_task_outcomes(
            {'turns': [item], 'diagnostics': {}},
            [usage('mobile-fix', '5.6 sol high', 100, '2026-09-10T10:01:00+00:00')],
            reviews={'version': 1, 'turn_boundaries': {}, 'missions': {}},
            now=datetime(2026, 9, 11, tzinfo=timezone.utc),
        )
        self.assertEqual(result['missions'][0]['status'], 'unresolved')

    def test_explicit_user_manual_repair_marks_model_failure(self):
        turns = [
            turn(
                'model-attempt', 'thread-manual', '2026-09-10T10:00:00+00:00',
                'sửa nội dung public GitHub cho tôi',
                assistant_text='Tôi đang tìm file cần sửa.',
            ),
            turn(
                'user-repair', 'thread-manual', '2026-09-10T10:10:00+00:00',
                'về sau bị mất harness nên tôi đã tự sửa bằng tay luôn rồi',
                assistant_text='Đã hiểu.',
            ),
        ]
        result = server.build_codex_task_outcomes(
            {'turns': turns, 'diagnostics': {}},
            [usage('model-attempt', '5.6 sol high', 100, '2026-09-10T10:01:00+00:00')],
            reviews={'version': 1, 'turn_boundaries': {}, 'missions': {}},
            now=datetime(2026, 9, 11, tzinfo=timezone.utc),
        )
        self.assertEqual(len(result['missions']), 1)
        mission = result['missions'][0]
        self.assertEqual(mission['status'], 'failed_user_repaired_auto')
        self.assertTrue(mission['user_repaired'])
        self.assertFalse(mission['accepted'])
        self.assertEqual(result['summary']['abandoned_count'], 1)
        self.assertEqual(result['summary']['unresolved_count'], 0)

    def test_manual_review_can_record_user_repaired_outcome(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, 'reviews.json')
            server.update_codex_mission_review({
                'action': 'review', 'anchor_turn_id': 'manual-failure',
                'outcome': 'user_repaired',
            }, path)
            reviews = server.load_codex_mission_reviews(path)
            result = server.build_codex_task_outcomes(
                {'turns': [turn(
                    'manual-failure', 'thread-manual', '2026-09-10T10:00:00+00:00',
                    'sửa bài viết GitHub',
                )], 'diagnostics': {}},
                [usage('manual-failure', '5.6 sol high', 100, '2026-09-10T10:01:00+00:00')],
                reviews=reviews,
                now=datetime(2026, 9, 11, tzinfo=timezone.utc),
            )
            mission = result['missions'][0]
            self.assertEqual(mission['status'], 'failed_user_repaired_manual')
            self.assertTrue(mission['outcome_reviewed'])
            self.assertEqual(result['summary']['abandoned_count'], 1)

    def test_review_file_can_set_clear_and_suppress_repair_link(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, 'reviews.json')
            server.update_codex_mission_review({
                'action': 'review',
                'anchor_turn_id': 'repair-turn',
                'repair_of_anchor_turn_id': 'original-turn',
            }, path)
            saved = server.load_codex_mission_reviews(path)
            self.assertEqual(
                saved['missions']['repair-turn']['repair_of_anchor_turn_id'],
                'original-turn',
            )

            server.update_codex_mission_review({
                'action': 'review',
                'anchor_turn_id': 'repair-turn',
                'repair_of_anchor_turn_id': 'auto',
            }, path)
            saved = server.load_codex_mission_reviews(path)
            self.assertNotIn('repair-turn', saved['missions'])

            server.update_codex_mission_review({
                'action': 'review',
                'anchor_turn_id': 'repair-turn',
                'repair_of_anchor_turn_id': 'none',
            }, path)
            saved = server.load_codex_mission_reviews(path)
            self.assertIsNone(
                saved['missions']['repair-turn']['repair_of_anchor_turn_id']
            )

    def test_unresolved_mission_is_usable_matrix_sample_with_lower_confidence(self):
        mission = {
            'id': 'pending-1', 'accepted': False, 'pure_model': True,
            'category': 'documents', 'categories': ['documents'],
            'model_key': '5.6 sol high', 'total_tokens': 100,
            'cost_usd': 0.0, 'cost_known': False, 'correction_turns': 0,
            'added_guidance_tokens_est': 0, 'first_pass_success': False,
            'status': 'unresolved', 'start_at': '2026-09-10T00:00:00+00:00',
        }
        matrix = server._codex_task_matrix(
            [mission], None, datetime(2026, 9, 11, tzinfo=timezone.utc)
        )
        documents = next(row for row in matrix['rows'] if row['category'] == 'documents')
        cell = documents['cells']['5.6 sol high']
        self.assertEqual(matrix['eligible_missions'], 1)
        self.assertEqual(cell['sample_count'], 1)
        self.assertEqual(cell['pending_review_count'], 0)
        self.assertEqual(cell['unconfirmed_count'], 1)
        self.assertEqual(matrix['pending_review_samples'], 0)
        self.assertEqual(matrix['unconfirmed_samples'], 1)
        self.assertEqual(cell['confidence'], 'low')

    def test_genuine_review_case_is_counted_separately_from_unconfirmed_status(self):
        mission = {
            'id': 'review-1', 'accepted': False, 'pure_model': True,
            'category': 'documents', 'categories': ['documents'],
            'model_key': '5.6 sol high', 'total_tokens': 100,
            'cost_usd': 0.0, 'cost_known': False, 'correction_turns': 0,
            'added_guidance_tokens_est': 0, 'first_pass_success': False,
            'status': 'unresolved', 'needs_review': True,
            'start_at': '2026-09-10T00:00:00+00:00',
        }
        matrix = server._codex_task_matrix(
            [mission], None, datetime(2026, 9, 11, tzinfo=timezone.utc)
        )
        documents = next(row for row in matrix['rows'] if row['category'] == 'documents')
        cell = documents['cells']['5.6 sol high']
        self.assertEqual(cell['sample_count'], 1)
        self.assertEqual(cell['pending_review_count'], 1)
        self.assertEqual(cell['unconfirmed_count'], 1)
        self.assertEqual(matrix['pending_review_samples'], 1)
        self.assertEqual(matrix['unconfirmed_samples'], 1)
        self.assertEqual(cell['confidence'], 'low')

    def test_mixed_category_turn_tokens_are_split_without_double_counting(self):
        mission = {
            'id': 'mixed-1', 'accepted': False, 'category': 'documents',
            'categories': ['documents', 'system_diagnostics'],
            'category_reviewed': False, 'status': 'unresolved', 'total_tokens': 300,
            'correction_turns': 1, 'added_guidance_tokens_est': 0,
            'first_pass_success': False, 'start_at': '2026-09-10T00:00:00+00:00',
            'turns': [
                {
                    'turn_id': 'a', 'categories': ['documents'], 'category': 'documents',
                    'total_tokens': 200, 'model_key': '5.6 sol high', 'cost_known': False,
                    'model_totals': [{'model_key': '5.6 sol high', 'total_tokens': 200, 'cost_known': False}],
                },
                {
                    'turn_id': 'b', 'categories': ['documents', 'system_diagnostics'],
                    'category': 'documents', 'total_tokens': 100, 'model_key': '5.6 sol high',
                    'cost_known': False,
                    'model_totals': [{'model_key': '5.6 sol high', 'total_tokens': 100, 'cost_known': False}],
                },
            ],
        }
        samples = server._codex_matrix_attributed_samples([mission])
        self.assertEqual(len(samples), 2)
        self.assertEqual(round(sum(item['total_tokens'] for item in samples)), 300)
        documents = sum(item['total_tokens'] for item in samples if item['category'] == 'documents')
        diagnostics = sum(item['total_tokens'] for item in samples if item['category'] == 'system_diagnostics')
        self.assertEqual(round(documents), 250)
        self.assertEqual(round(diagnostics), 50)
        matrix = server._codex_task_matrix(
            [mission], None, datetime(2026, 9, 11, tzinfo=timezone.utc)
        )
        documents_cell = next(
            row for row in matrix['rows'] if row['category'] == 'documents'
        )['cells']['5.6 sol high']
        diagnostics_cell = next(
            row for row in matrix['rows'] if row['category'] == 'system_diagnostics'
        )['cells']['5.6 sol high']
        self.assertEqual(documents_cell['sample_count'], 1)
        self.assertEqual(documents_cell['median_total_tokens'], 250)
        self.assertEqual(diagnostics_cell['sample_count'], 1)
        self.assertEqual(diagnostics_cell['median_total_tokens'], 50)

    def test_short_followups_inherit_previous_turn_category_inside_mixed_mission(self):
        turns = [
            turn('t1', 'thread-mixed', '2026-09-10T10:00:00+00:00',
                 'file word ho so dong chi Tu, cap hoc trung cap, que quan'),
            turn('t2', 'thread-mixed', '2026-09-10T10:05:00+00:00',
                 'tai sao quyen doc bi chan, con dong chi Tu thi chua dung'),
            turn('t3', 'thread-mixed', '2026-09-10T10:10:00+00:00',
                 'con may cho luc nay toi noi ban da sua chua'),
            turn('t4', 'thread-mixed', '2026-09-10T10:15:00+00:00',
                 'the ban sua di'),
            turn('t5', 'thread-mixed', '2026-09-10T10:20:00+00:00',
                 'sao no lai bi chan quyen, kiem tra quyen doc'),
        ]
        events = [usage(item['turn_id'], item['model_key'], 100, item['started_at']) for item in turns]

        result = server.build_codex_task_outcomes(
            {'turns': turns, 'diagnostics': {}}, events,
            reviews={'version': 1, 'turn_boundaries': {}, 'missions': {}},
            now=datetime(2026, 9, 11, tzinfo=timezone.utc),
        )

        self.assertEqual(len(result['missions']), 1)
        mission = result['missions'][0]
        self.assertEqual(mission['categories'], ['documents', 'system_diagnostics'])
        self.assertEqual(mission['turns'][2]['categories'], ['documents'])
        self.assertTrue(mission['turns'][2]['category_inherited'])
        self.assertEqual(mission['turns'][3]['categories'], ['documents'])
        self.assertTrue(mission['turns'][3]['category_inherited'])
        self.assertEqual(mission['turns'][4]['categories'], ['system_diagnostics'])
        self.assertFalse(mission['needs_category_review'])
        self.assertEqual(result['summary']['review_case_count'], 0)
        self.assertFalse(mission['audit_needed'])
        self.assertFalse(mission['soft_audit_needed'])
        self.assertEqual(mission['audit_reasons'], [])
        self.assertEqual(result['summary']['audit_case_count'], 0)
        self.assertEqual(result['summary']['soft_audit_case_count'], 0)
        self.assertEqual(result['summary']['audit_pattern_count'], 0)

    def test_unknown_low_confidence_turn_still_requires_review(self):
        turns = [
            turn('unknown', 'thread-unknown', '2026-09-10T10:00:00+00:00',
                 'xem giup toi cai nay nhe'),
        ]
        events = [usage('unknown', '5.6 sol high', 100, '2026-09-10T10:05:00+00:00')]

        result = server.build_codex_task_outcomes(
            {'turns': turns, 'diagnostics': {}}, events,
            reviews={'version': 1, 'turn_boundaries': {}, 'missions': {}},
            now=datetime(2026, 9, 11, tzinfo=timezone.utc),
        )

        self.assertEqual(len(result['missions']), 1)
        mission = result['missions'][0]
        self.assertEqual(mission['category'], 'other')
        self.assertTrue(mission['needs_category_review'])
        self.assertEqual(result['summary']['review_case_count'], 1)
        self.assertIn('other_category', mission['category_review_reasons'])
        self.assertIn('low_category_confidence', mission['category_review_reasons'])
        self.assertTrue(mission['audit_needed'])
        self.assertFalse(mission['soft_audit_needed'])
        self.assertEqual(mission['audit_pattern_key'], '')
        self.assertEqual(result['summary']['soft_audit_case_count'], 0)

    def test_pure_greetings_are_not_tasks_or_matrix_samples(self):
        turns = [
            turn('hello', 'thread-hello', '2026-09-10T10:00:00+00:00', 'hello'),
            turn('xin-chao', 'thread-xin-chao', '2026-09-10T11:00:00+00:00', 'xin chào'),
        ]
        events = [
            usage('hello', '5.6 sol high', 100, '2026-09-10T10:01:00+00:00'),
            usage('xin-chao', '5.6 sol high', 100, '2026-09-10T11:01:00+00:00'),
        ]
        result = server.build_codex_task_outcomes(
            {'turns': turns, 'diagnostics': {}}, events,
            reviews={'version': 1, 'turn_boundaries': {}, 'missions': {}},
            now=datetime(2026, 9, 11, tzinfo=timezone.utc),
        )
        self.assertEqual(result['missions'], [])
        self.assertEqual(result['summary']['mission_count'], 0)
        self.assertEqual(result['matrices']['all']['eligible_missions'], 0)

    def test_positive_acknowledgement_closes_prior_task(self):
        turns = [
            turn('work', 'thread-ack', '2026-09-10T10:00:00+00:00',
                 'sửa bảng usage tracker trên web'),
            turn('ack', 'thread-ack', '2026-09-10T10:05:00+00:00',
                 'ồ được rồi, hay quá'),
        ]
        events = [usage('work', '5.6 sol high', 100, '2026-09-10T10:03:00+00:00')]
        result = server.build_codex_task_outcomes(
            {'turns': turns, 'diagnostics': {}}, events,
            reviews={'version': 1, 'turn_boundaries': {}, 'missions': {}},
            now=datetime(2026, 9, 11, tzinfo=timezone.utc),
        )
        self.assertEqual(len(result['missions']), 1)
        self.assertEqual(result['missions'][0]['status'], 'accepted_explicit')

    def test_explicit_new_feature_wins_over_generic_followup_words(self):
        turns = [
            turn('first', 'thread-features', '2026-09-10T10:00:00+00:00',
                 'sửa biểu đồ token trong usage tracker'),
            turn('second', 'thread-features', '2026-09-10T11:00:00+00:00',
                 'thêm nữa là tôi có ý tưởng tiếp theo, tôi muốn thêm tính năng lưu độ rộng cột'),
        ]
        events = [
            usage('first', '5.6 sol high', 100, '2026-09-10T10:30:00+00:00'),
            usage('second', '5.6 sol high', 100, '2026-09-10T11:30:00+00:00'),
        ]

        result = server.build_codex_task_outcomes(
            {'turns': turns, 'diagnostics': {}}, events,
            reviews={'version': 1, 'turn_boundaries': {}, 'missions': {}},
            now=datetime(2026, 9, 11, tzinfo=timezone.utc),
        )

        self.assertEqual(len(result['missions']), 2)
        first = next(item for item in result['missions'] if item['anchor_turn_id'] == 'first')
        second = next(item for item in result['missions'] if item['anchor_turn_id'] == 'second')
        self.assertEqual(first['status'], 'accepted_inferred')
        self.assertEqual(second['status'], 'unresolved')

    def test_ok_start_work_continues_plan_instead_of_accepting_it(self):
        turns = [
            turn('plan', 'thread-plan', '2026-09-10T10:00:00+00:00',
                 'hãy lập kế hoạch viết lại tài liệu cho tôi'),
            turn('execute', 'thread-plan', '2026-09-10T10:05:00+00:00',
                 'ok bạn bắt đầu làm đi'),
        ]
        events = [
            usage('plan', '5.6 sol high', 100, '2026-09-10T10:01:00+00:00'),
            usage('execute', '5.6 sol high', 100, '2026-09-10T10:06:00+00:00'),
        ]

        result = server.build_codex_task_outcomes(
            {'turns': turns, 'diagnostics': {}}, events,
            reviews={'version': 1, 'turn_boundaries': {}, 'missions': {}},
            now=datetime(2026, 9, 11, tzinfo=timezone.utc),
        )

        self.assertEqual(len(result['missions']), 1)
        self.assertEqual(result['missions'][0]['anchor_turn_id'], 'plan')
        self.assertEqual(result['missions'][0]['turn_count'], 2)
        self.assertEqual(result['missions'][0]['status'], 'unresolved')

    def test_ok_execute_on_github_continues_same_objective(self):
        turns = [
            turn('draft', 'thread-github', '2026-09-10T10:00:00+00:00',
                 'viết lại tài liệu trong repo cho giống văn phong của tôi'),
            turn('execute', 'thread-github', '2026-09-10T10:05:00+00:00',
                 'ok thế bạn sửa trên github cho tôi đi'),
        ]
        events = [
            usage('draft', '5.6 sol high', 100, '2026-09-10T10:01:00+00:00'),
            usage('execute', '5.6 sol high', 100, '2026-09-10T10:06:00+00:00'),
        ]

        result = server.build_codex_task_outcomes(
            {'turns': turns, 'diagnostics': {}}, events,
            reviews={'version': 1, 'turn_boundaries': {}, 'missions': {}},
            now=datetime(2026, 9, 11, tzinfo=timezone.utc),
        )

        self.assertEqual(len(result['missions']), 1)
        self.assertEqual(result['missions'][0]['anchor_turn_id'], 'draft')
        self.assertEqual(result['missions'][0]['turn_count'], 2)

    def test_cross_model_concrete_action_after_answer_starts_new_mission(self):
        turns = [
            turn(
                'cleanup', 'thread-public', '2026-09-21T02:43:00+00:00',
                'kiểm tra và dọn các bản usage tracker cũ', model='5.6 sol xhigh',
            ),
            turn(
                'path-question', 'thread-public', '2026-09-21T02:58:00+00:00',
                'folder cuối của usage tracker giờ là đây đúng ko', model='5.6 sol xhigh',
            ),
            turn(
                'sync-public', 'thread-public', '2026-09-21T04:08:00+00:00',
                'ok giờ bạn đem bản cập nhật mới nhất sang folder public đi, '
                'nhưng vẫn chưa đưa lên github nhé, rồi sửa phần tiếng Anh',
                model='5.6 sol high',
            ),
        ]
        events = [
            usage('cleanup', '5.6 sol xhigh', 100, '2026-09-21T02:44:00+00:00'),
            usage('path-question', '5.6 sol xhigh', 30, '2026-09-21T02:59:00+00:00'),
            usage('sync-public', '5.6 sol high', 80, '2026-09-21T04:09:00+00:00'),
        ]

        result = server.build_codex_task_outcomes(
            {'turns': turns, 'diagnostics': {}}, events,
            reviews={'version': 1, 'turn_boundaries': {}, 'missions': {}},
            now=datetime(2026, 9, 21, tzinfo=timezone.utc),
        )

        missions = {item['anchor_turn_id']: item for item in result['missions']}
        self.assertEqual(set(missions), {'cleanup', 'sync-public'})
        self.assertEqual(missions['cleanup']['turn_ids'], ['cleanup', 'path-question'])
        self.assertEqual(missions['sync-public']['route'], ['5.6 sol high'])
        self.assertEqual(
            missions['sync-public']['inferred_boundary_reason'],
            'cross_model_new_action',
        )
        self.assertIsNone(missions['sync-public']['repair_of_anchor_turn_id'])

    def test_cross_model_missing_ui_complaint_becomes_repair_mission(self):
        turns = [
            turn(
                'original', 'thread-ui', '2026-09-21T04:08:00+00:00',
                'đồng bộ bản mới sang public và sửa phần tiếng Anh', model='5.6 sol high',
            ),
            turn(
                'repair', 'thread-ui', '2026-09-21T05:12:00+00:00',
                'ủa phần chọn tiếng Anh với tiếng Việt đâu mất tiêu rồi, bạn bị làm sao vậy',
                model='5.6 sol xhigh',
            ),
        ]
        events = [
            usage('original', '5.6 sol high', 100, '2026-09-21T04:09:00+00:00'),
            usage('repair', '5.6 sol xhigh', 40, '2026-09-21T05:13:00+00:00'),
        ]

        result = server.build_codex_task_outcomes(
            {'turns': turns, 'diagnostics': {}}, events,
            reviews={'version': 1, 'turn_boundaries': {}, 'missions': {}},
            now=datetime(2026, 9, 21, tzinfo=timezone.utc),
        )

        missions = {item['anchor_turn_id']: item for item in result['missions']}
        self.assertEqual(set(missions), {'original', 'repair'})
        self.assertEqual(missions['original']['status'], 'abandoned_auto_repaired')
        self.assertEqual(missions['repair']['repair_of_anchor_turn_id'], 'original')
        self.assertEqual(missions['repair']['repair_penalty_tokens'], 40)
        self.assertEqual(missions['original']['repair_penalty_received_tokens'], 40)
        self.assertEqual(missions['repair']['inferred_boundary_reason'], 'cross_model_repair')

    def test_instruction_constraints_are_not_misread_as_repairs(self):
        for prompt in (
            'không dùng dữ liệu cũ trong file Excel',
            'tôi thấy bạn chưa dùng hết sức mạnh CPU và RAM',
            'nhưng vẫn chưa đưa lên GitHub nhé',
            'một số file PDF vẫn chưa có, hãy tạo các file còn thiếu',
            'tôi vừa làm lại hai ảnh do chúng chưa xóa background, bạn sử dụng lại hình nhé',
        ):
            with self.subTest(prompt=prompt):
                self.assertFalse(server._codex_is_repair_prompt(prompt))

        self.assertTrue(server._codex_is_repair_prompt('kết quả này chưa đúng, sửa lại cho tôi'))
        self.assertTrue(server._codex_is_repair_prompt('phần chọn ngôn ngữ đâu mất tiêu rồi'))
        self.assertTrue(server._codex_is_repair_prompt('nếu vẫn chưa tìm ra được cách tái tạo thì kiểm tra lại'))

    def test_quota_accounting_question_does_not_create_repair_link(self):
        question = (
            'ví dụ công việc sửa web vừa rồi dùng Sol High rồi sửa lại bằng XHigh, '
            'nên khoản hạn mức này cũng bị trừ vào Sol High nhỉ'
        )
        self.assertTrue(server._codex_is_repair_prompt(question))
        self.assertTrue(server._codex_is_informational_question(question))
        turns = [
            turn('original', 'thread-quota', '2026-09-21T04:00:00+00:00',
                 'sửa web usage tracker', model='5.6 sol high'),
            turn('question', 'thread-quota', '2026-09-21T05:00:00+00:00',
                 question, model='5.6 sol xhigh'),
        ]
        result = server.build_codex_task_outcomes(
            {'turns': turns, 'diagnostics': {}},
            [
                usage('original', '5.6 sol high', 100, '2026-09-21T04:01:00+00:00'),
                usage('question', '5.6 sol xhigh', 40, '2026-09-21T05:01:00+00:00'),
            ],
            reviews={'version': 1, 'turn_boundaries': {}, 'missions': {}},
            now=datetime(2026, 9, 21, tzinfo=timezone.utc),
        )
        missions = {item['anchor_turn_id']: item for item in result['missions']}
        self.assertIsNone(missions['question']['repair_of_anchor_turn_id'])
        self.assertEqual(
            missions['question']['inferred_boundary_reason'],
            'quota_accounting_question',
        )
        self.assertEqual(missions['original']['repair_penalty_received_tokens'], 0)

        self.assertFalse(server._codex_is_quota_accounting_question(
            'bạn làm tiếp đi, thêm nữa hệ số hạn mức có phải thay đổi đúng ko'
        ))

    def test_cross_thread_repair_without_explicit_reference_is_not_auto_linked(self):
        turns = [
            turn('original', 'thread-one', '2026-09-21T04:00:00+00:00',
                 'sửa web usage tracker và biểu đồ model', model='5.6 sol high'),
            turn('possible-repair', 'thread-two', '2026-09-21T05:00:00+00:00',
                 'web usage tracker và biểu đồ model chưa đúng, sửa lại giúp tôi',
                 model='5.6 sol xhigh'),
        ]
        result = server.build_codex_task_outcomes(
            {'turns': turns, 'diagnostics': {}},
            [
                usage('original', '5.6 sol high', 100, '2026-09-21T04:01:00+00:00'),
                usage('possible-repair', '5.6 sol xhigh', 40, '2026-09-21T05:01:00+00:00'),
            ],
            reviews={'version': 1, 'turn_boundaries': {}, 'missions': {}},
            now=datetime(2026, 9, 21, tzinfo=timezone.utc),
        )
        missions = {item['anchor_turn_id']: item for item in result['missions']}
        self.assertIsNone(missions['possible-repair']['repair_of_anchor_turn_id'])
        self.assertEqual(missions['original']['repair_penalty_received_tokens'], 0)

    def test_immediate_same_thread_cross_model_repair_is_separate_and_high_confidence(self):
        turns = [
            turn('first', 'thread-repair', '2026-09-10T10:00:00+00:00',
                 'sửa công thức nhóm tag trong file Excel'),
            turn('second', 'thread-repair', '2026-09-10T10:10:00+00:00',
                 'nhiệm vụ tiếp theo là cập nhật bảng khác'),
            turn('repair', 'thread-repair', '2026-09-10T10:20:00+00:00',
                 'ok thế bạn sửa lại đi'),
        ]
        events = [
            usage('first', '5.6 sol high', 100, '2026-09-10T10:05:00+00:00'),
            usage('second', '5.6 sol high', 100, '2026-09-10T10:15:00+00:00'),
            usage('repair', '5.6 sol xhigh', 100, '2026-09-10T10:25:00+00:00'),
        ]

        result = server.build_codex_task_outcomes(
            {'turns': turns, 'diagnostics': {}}, events,
            reviews={'version': 1, 'turn_boundaries': {}, 'missions': {}},
            now=datetime(2026, 9, 11, tzinfo=timezone.utc),
        )

        repaired_mission = next(
            item for item in result['missions'] if item['anchor_turn_id'] == 'repair'
        )
        self.assertEqual(repaired_mission['turn_count'], 1)
        self.assertEqual(repaired_mission['repair_of_anchor_turn_id'], 'second')
        self.assertEqual(repaired_mission['repair_link_confidence'], 'high')
        self.assertEqual(repaired_mission['inferred_boundary_reason'], 'cross_model_repair')
        self.assertFalse(repaired_mission['soft_audit_needed'])

    def test_delayed_same_category_request_starts_new_mission(self):
        turns = [
            turn('first', 'thread-gap', '2026-09-10T01:00:00+00:00',
                 'phân tích bộ filter giao dịch hiện tại'),
            turn('later', 'thread-gap', '2026-09-10T09:00:00+00:00',
                 'so sánh hiệu quả của bộ filter một cột và hai cột'),
        ]
        events = [
            usage('first', '5.6 sol high', 100, '2026-09-10T01:30:00+00:00'),
            usage('later', '5.6 sol high', 100, '2026-09-10T09:30:00+00:00'),
        ]

        result = server.build_codex_task_outcomes(
            {'turns': turns, 'diagnostics': {}}, events,
            reviews={'version': 1, 'turn_boundaries': {}, 'missions': {}},
            now=datetime(2026, 9, 11, tzinfo=timezone.utc),
        )

        self.assertEqual({item['anchor_turn_id'] for item in result['missions']}, {'first', 'later'})

    def test_delayed_explicit_continuation_stays_in_same_mission(self):
        turns = [
            turn('first', 'thread-gap-followup', '2026-09-10T01:00:00+00:00',
                 'phân tích bộ filter giao dịch hiện tại'),
            turn('later', 'thread-gap-followup', '2026-09-10T09:00:00+00:00',
                 'bạn làm tiếp phần phân tích vừa rồi đi'),
        ]
        events = [
            usage('first', '5.6 sol high', 100, '2026-09-10T01:30:00+00:00'),
            usage('later', '5.6 sol high', 100, '2026-09-10T09:30:00+00:00'),
        ]

        result = server.build_codex_task_outcomes(
            {'turns': turns, 'diagnostics': {}}, events,
            reviews={'version': 1, 'turn_boundaries': {}, 'missions': {}},
            now=datetime(2026, 9, 11, tzinfo=timezone.utc),
        )

        self.assertEqual(len(result['missions']), 1)
        self.assertEqual(result['missions'][0]['turn_count'], 2)
        self.assertTrue(server._codex_is_explicit_continuation_prompt(
            'bạn đang dở ấy, bạn làm tiếp đi'
        ))
        self.assertTrue(server._codex_is_explicit_continuation_prompt(
            'ok giờ tôi có hạn mức lại rồi, bạn làm tiếp các công việc chưa hoàn thành nhé'
        ))
        self.assertTrue(server._codex_is_explicit_continuation_prompt(
            'bạn có làm tiếp nhiệm vụ này được ko'
        ))
        self.assertTrue(server._codex_is_explicit_continuation_prompt(
            '<codex_internal_context source="goal">Continue working toward the active goal.'
            '</codex_internal_context>'
        ))

    def test_generic_followup_word_inside_new_prompt_does_not_bridge_long_gap(self):
        turns = [
            turn('old', 'thread-gap-generic', '2026-08-30T10:00:00+00:00',
                 'sửa lỗi tải lịch tin Forex Factory'),
            turn('new', 'thread-gap-generic', '2026-09-21T10:00:00+00:00',
                 'zoom in zoom out làm mất vị trí theo dõi, thêm nữa thanh công cụ bị dịch chuyển'),
        ]
        events = [
            usage('old', '5.6 sol high', 100, '2026-08-30T10:01:00+00:00'),
            usage('new', '5.6 sol high', 100, '2026-09-21T10:01:00+00:00'),
        ]
        result = server.build_codex_task_outcomes(
            {'turns': turns, 'diagnostics': {}}, events,
            reviews={'version': 1, 'turn_boundaries': {}, 'missions': {}},
            now=datetime(2026, 9, 21, tzinfo=timezone.utc),
        )
        missions = {item['anchor_turn_id']: item for item in result['missions']}
        self.assertEqual(set(missions), {'old', 'new'})
        self.assertEqual(missions['new']['inferred_boundary_reason'], 'long_gap_followup')

    def test_unconfirmed_groups_are_review_only_and_group_similar_topics(self):
        missions = [
            {
                'anchor_turn_id': 'usage-1', 'status': 'unresolved',
                'categories': ['web'], 'title': 'sửa usage tracker',
                'turn_count': 1,
                'turns': [{'prompt': 'sửa biểu đồ token trong usage tracker'}],
            },
            {
                'anchor_turn_id': 'usage-2', 'status': 'unresolved',
                'categories': ['research'], 'title': 'hỏi quota',
                'turn_count': 1,
                'turns': [{'prompt': 'tại sao quota hạn mức Codex thay đổi?'}],
            },
            {
                'anchor_turn_id': 'done', 'status': 'accepted_explicit',
                'categories': ['web'], 'title': 'đã xong', 'turn_count': 1,
                'turns': [{'prompt': 'sửa usage tracker'}],
            },
        ]
        groups = server._codex_collect_unconfirmed_groups(missions)
        self.assertEqual(sum(group['mission_count'] for group in groups), 2)
        self.assertTrue(all(group['topic_key'] == 'usage_tracker' for group in groups))
        self.assertEqual(missions[0]['unconfirmed_group_key'], 'usage_tracker:action')
        self.assertEqual(missions[1]['unconfirmed_group_key'], 'usage_tracker:question')
        self.assertEqual(missions[2]['unconfirmed_group_key'], '')

    def test_unconfirmed_group_does_not_treat_generic_token_as_usage_tracker(self):
        missions = [
            {
                'anchor_turn_id': 'su30-token', 'status': 'unresolved',
                'categories': ['documents'], 'title': 'chuyển tài liệu',
                'turn_count': 1,
                'turns': [{'prompt': 'chuyển tài liệu SU-30 này sao cho ít tốn token'}],
            },
            {
                'anchor_turn_id': 'model-advice', 'status': 'unresolved',
                'categories': ['research'], 'title': 'chọn model',
                'turn_count': 1,
                'turns': [{'prompt': 'Astra hay 5.6 Sol phù hợp hơn cho công việc của tôi?'}],
            },
        ]
        server._codex_collect_unconfirmed_groups(missions)
        self.assertEqual(missions[0]['unconfirmed_group_key'], 'su30:action')
        self.assertEqual(missions[1]['unconfirmed_group_key'], 'product_model:question')

    def test_explicit_usage_tracker_reference_wins_over_model_name(self):
        mission = {
            'anchor_turn_id': 'tracker-astra', 'status': 'unresolved',
            'categories': ['web'], 'title': 'sửa biểu đồ', 'turn_count': 1,
            'turns': [{
                'prompt': 'sửa biểu đồ Astra trong Usage Tracker cho tôi',
            }],
        }
        server._codex_collect_unconfirmed_groups([mission])
        self.assertEqual(mission['unconfirmed_group_key'], 'usage_tracker:action')

    def test_clean_high_confidence_mission_is_not_flagged_for_audit(self):
        turns = [
            turn('diag', 'thread-clean', '2026-09-10T10:00:00+00:00',
                 'sao no lai bi chan quyen, kiem tra quyen doc'),
        ]
        events = [usage('diag', '5.6 sol high', 100, '2026-09-10T10:05:00+00:00')]

        result = server.build_codex_task_outcomes(
            {'turns': turns, 'diagnostics': {}}, events,
            reviews={'version': 1, 'turn_boundaries': {}, 'missions': {}},
            now=datetime(2026, 9, 11, tzinfo=timezone.utc),
        )

        mission = result['missions'][0]
        self.assertEqual(mission['category_confidence'], 'high')
        self.assertFalse(mission['needs_category_review'])
        self.assertFalse(mission['audit_needed'])
        self.assertFalse(mission['soft_audit_needed'])
        self.assertEqual(result['summary']['audit_case_count'], 0)

    def test_long_gap_is_not_a_soft_audit_reason_after_boundary_pass(self):
        mission = {
            'id': 'mission-long-gap',
            'anchor_turn_id': 'turn-a',
            'title': 'Repeated actions in one old thread',
            'status': 'unresolved',
            'category': 'system_diagnostics',
            'categories': ['system_diagnostics'],
            'category_confidence': 'high',
            'category_reviewed': False,
            'outcome_reviewed': False,
            'needs_review': False,
            'needs_category_review': False,
            'pure_model': True,
            'turns': [
                {
                    'turn_id': 'turn-a',
                    'started_at': '2026-09-10T01:00:00+00:00',
                    'category': 'system_diagnostics',
                    'categories': ['system_diagnostics'],
                    'prompt': 'Run the first completed action.',
                },
                {
                    'turn_id': 'turn-b',
                    'started_at': '2026-09-10T09:30:00+00:00',
                    'category': 'system_diagnostics',
                    'categories': ['system_diagnostics'],
                    'prompt': 'Run a later action in the same thread.',
                },
            ],
        }

        cases, groups, patterns = server._codex_collect_audit_cases([mission])

        self.assertEqual(cases, [])
        self.assertEqual(groups, [])
        self.assertEqual(patterns, [])
        self.assertFalse(mission['audit_needed'])
        self.assertFalse(mission['soft_audit_needed'])

    def test_fully_reviewed_long_gap_is_not_reopened_as_soft_audit(self):
        mission = {
            'id': 'mission-reviewed-gap',
            'anchor_turn_id': 'turn-a',
            'title': 'Human-reviewed long mission',
            'status': 'accepted_manual',
            'category': 'system_diagnostics',
            'categories': ['system_diagnostics'],
            'category_confidence': 'high',
            'category_reviewed': True,
            'outcome_reviewed': True,
            'needs_review': False,
            'needs_category_review': False,
            'pure_model': True,
            'turns': [
                {
                    'turn_id': 'turn-a',
                    'started_at': '2026-09-10T01:00:00+00:00',
                    'category': 'system_diagnostics',
                    'categories': ['system_diagnostics'],
                    'prompt': 'Run the first completed action.',
                },
                {
                    'turn_id': 'turn-b',
                    'started_at': '2026-09-10T09:30:00+00:00',
                    'category': 'system_diagnostics',
                    'categories': ['system_diagnostics'],
                    'prompt': 'Run a later action in the same thread.',
                },
            ],
        }

        cases, groups, patterns = server._codex_collect_audit_cases([mission])

        self.assertEqual(cases, [])
        self.assertEqual(groups, [])
        self.assertEqual(patterns, [])
        self.assertFalse(mission['audit_needed'])
        self.assertFalse(mission['soft_audit_needed'])

    def test_automatic_multi_category_alone_is_not_soft_audit(self):
        mission = {
            'id': 'mission-mixed-valid',
            'anchor_turn_id': 'turn-a',
            'title': 'Edit a document and diagnose its font',
            'status': 'accepted_inferred',
            'category': 'documents',
            'categories': ['documents', 'system_diagnostics'],
            'category_confidence': 'medium',
            'category_reviewed': False,
            'outcome_reviewed': False,
            'needs_review': False,
            'needs_category_review': False,
            'pure_model': True,
            'turns': [{
                'turn_id': 'turn-a',
                'started_at': '2026-09-10T01:00:00+00:00',
                'category': 'documents',
                'categories': ['documents', 'system_diagnostics'],
                'category_ambiguous': True,
                'prompt': 'Repair this document and diagnose the broken font.',
            }],
        }

        cases, groups, patterns = server._codex_collect_audit_cases([mission])

        self.assertEqual(cases, [])
        self.assertEqual(groups, [])
        self.assertEqual(patterns, [])
        self.assertFalse(mission['audit_needed'])
        self.assertFalse(mission['soft_audit_needed'])

    def test_vietnamese_d_stroke_and_blocked_permission_are_classified(self):
        categories, primary, confidence, ambiguous = server._codex_task_categories(
            'tại sao quyền đọc lại bị chặn nhỉ, còn chỗ đồng chí Tú thì sửa tài liệu chưa đúng'
        )
        self.assertEqual(primary, 'documents')
        self.assertEqual(categories, ['documents', 'system_diagnostics'])
        self.assertEqual(confidence, 'medium')
        self.assertTrue(ambiguous)

        categories, primary, confidence, ambiguous = server._codex_task_categories(
            'sao nó lại chặn quyền nhỉ, bạn kiểm tra lại được chưa nè'
        )
        self.assertEqual(primary, 'system_diagnostics')
        self.assertEqual(categories, ['system_diagnostics'])
        self.assertEqual(confidence, 'high')
        self.assertFalse(ambiguous)

    def test_su30_path_is_domain_context_not_automatic_3d_work(self):
        categories, primary, confidence, ambiguous = server._codex_task_categories(
            'chuyển file PDF này sang Unicode và giữ nguyên bố cục',
            r'D:\tài liệu đồng bộ\tai lieu su30',
        )
        self.assertEqual(categories, ['documents'])
        self.assertEqual(primary, 'documents')
        self.assertEqual(confidence, 'high')
        self.assertFalse(ambiguous)

        categories, primary, confidence, ambiguous = server._codex_task_categories(
            'xây buồng lái 3D bằng Three.js',
            r'D:\tài liệu đồng bộ\tai lieu su30',
        )
        self.assertIn('simulation_3d', categories)
        self.assertEqual(primary, 'simulation_3d')
        self.assertEqual(confidence, 'high')

        categories, primary, confidence, ambiguous = server._codex_task_categories(
            'bạn làm tiếp đi',
            r'C:\work\cockpit360',
        )
        self.assertEqual(categories, ['simulation_3d'])
        self.assertEqual(primary, 'simulation_3d')
        self.assertFalse(ambiguous)

    def test_category_keywords_do_not_match_inside_unrelated_words(self):
        categories, primary, confidence, ambiguous = server._codex_task_categories(
            'thôi hướng dẫn tôi thực hiện wifi debug đi'
        )
        self.assertEqual(categories, ['system_diagnostics'])
        self.assertEqual(primary, 'system_diagnostics')
        self.assertFalse(ambiguous)

        categories, primary, confidence, ambiguous = server._codex_task_categories(
            'trước tôi có cài chương trình đó và có hẳn file Python rồi'
        )
        self.assertEqual(categories, ['software_debugging'])
        self.assertNotIn('documents', categories)

        categories, primary, confidence, ambiguous = server._codex_task_categories(
            'sửa trình độ học vấn trong hồ sơ nhân sự'
        )
        self.assertEqual(categories, ['documents'])
        self.assertEqual(primary, 'documents')

    def test_usage_tracker_classifier_examples_do_not_become_task_labels(self):
        categories, primary, confidence, ambiguous = server._codex_task_categories(
            'sửa phần phân loại trong usage tracker; ví dụ tài liệu bị chặn quyền đọc '
            'hoặc công việc mô phỏng 3D phải được gom đúng nhóm'
        )
        self.assertEqual(categories, ['web'])
        self.assertEqual(primary, 'web')
        self.assertEqual(confidence, 'high')
        self.assertFalse(ambiguous)

    def test_embedded_review_example_inherits_usage_tracker_work(self):
        turns = [
            turn('tracker', 'thread-meta', '2026-09-10T10:00:00+00:00',
                 'sửa biểu đồ trong usage tracker'),
            turn('example', 'thread-meta', '2026-09-10T10:05:00+00:00',
                 'review lại từng mẫu quá lâu, ví dụ lượt 1 sửa hồ sơ, lượt 2 bị chặn quyền đọc'),
        ]
        events = [usage(item['turn_id'], item['model_key'], 100, item['started_at']) for item in turns]
        result = server.build_codex_task_outcomes(
            {'turns': turns, 'diagnostics': {}}, events,
            reviews={'version': 1, 'turn_boundaries': {}, 'missions': {}},
            now=datetime(2026, 9, 11, tzinfo=timezone.utc),
        )
        mission = result['missions'][0]
        self.assertEqual(mission['categories'], ['web'])
        self.assertEqual(mission['turns'][1]['categories'], ['web'])
        self.assertFalse(mission['turns'][1]['category_ambiguous'])

    def test_python_mention_requires_programming_context(self):
        categories, primary, confidence, ambiguous = server._codex_task_categories(
            'ổ đĩa đang nặng, có nên xóa không vì Codex cũng dùng Python'
        )
        self.assertEqual(categories, ['system_diagnostics'])
        self.assertEqual(primary, 'system_diagnostics')

        categories, primary, confidence, ambiguous = server._codex_task_categories(
            'chương trình này có file Python, sửa script giúp tôi'
        )
        self.assertEqual(categories, ['software_debugging'])
        self.assertEqual(primary, 'software_debugging')

    def test_codex_context_configuration_is_software_debugging(self):
        categories, primary, confidence, ambiguous = server._codex_task_categories(
            'tôi tăng giới hạn context lên 240k rồi, nhờ bạn chỉnh sửa lại mấy file đó'
        )
        self.assertEqual(primary, 'software_debugging')
        self.assertIn('software_debugging', categories)
        self.assertEqual(confidence, 'high')
        self.assertFalse(ambiguous)

    def test_cross_model_repair_keeps_raw_usage_and_penalizes_original_model(self):
        turns = [
            turn('original', 'thread-repair', '2026-09-10T10:00:00+00:00',
                 'lam web usage tracker', model='5.6 sol high'),
            turn('repair', 'thread-repair', '2026-09-10T10:10:00+00:00',
                 'chua dung, sua lai cho toi', model='5.6 luna xhigh'),
        ]
        events = [
            usage('original', '5.6 sol high', 100, '2026-09-10T10:05:00+00:00'),
            usage('repair', '5.6 luna xhigh', 40, '2026-09-10T10:15:00+00:00'),
        ]
        reviews = {
            'version': 1,
            'turn_boundaries': {'repair': 'new'},
            'missions': {},
        }

        result = server.build_codex_task_outcomes(
            {'turns': turns, 'diagnostics': {}}, events, reviews=reviews,
            now=datetime(2026, 9, 11, tzinfo=timezone.utc),
        )
        missions = {mission['anchor_turn_id']: mission for mission in result['missions']}
        original = missions['original']
        repair = missions['repair']

        self.assertEqual(original['total_tokens'], 100)
        self.assertEqual(repair['total_tokens'], 40)
        self.assertEqual(repair['repair_original_model'], '5.6 sol high')
        self.assertEqual(repair['repair_tokens'], 40)
        self.assertEqual(repair['repair_penalty_tokens'], 40)
        self.assertEqual(original['repair_penalty_received_tokens'], 40)
        self.assertEqual(original['penalized_total_tokens'], 140)
        self.assertEqual(result['repair_accounting']['raw_tokens'], 140)
        self.assertEqual(result['repair_accounting']['repair_penalty_tokens'], 40)
        self.assertEqual(result['repair_accounting']['effective_tokens'], 180)
        accounting = {item['model_key']: item for item in result['repair_accounting']['by_model']}
        self.assertEqual(accounting['5.6 sol high']['raw_tokens'], 100)
        self.assertEqual(accounting['5.6 sol high']['repair_penalty_tokens'], 40)
        self.assertEqual(accounting['5.6 sol high']['effective_tokens'], 140)
        self.assertEqual(accounting['5.6 luna xhigh']['raw_tokens'], 40)
        self.assertEqual(accounting['5.6 luna xhigh']['repair_penalty_tokens'], 0)

        matrix = result['matrices']['all']
        web = next(row for row in matrix['rows'] if row['category'] == 'web')
        sol = web['cells']['5.6 sol high']
        luna = web['cells']['5.6 luna xhigh']
        self.assertEqual(matrix['eligible_missions'], 1)
        self.assertEqual(sol['sample_count'], 1)
        self.assertEqual(sol['median_raw_total_tokens'], 100)
        self.assertEqual(sol['median_repair_penalty_tokens'], 40)
        self.assertEqual(sol['median_total_tokens'], 140)
        self.assertEqual(sol['repair_attribution_count'], 1)
        self.assertIsNone(sol['median_cost_usd'])
        self.assertEqual(luna['sample_count'], 1)
        self.assertEqual(luna['median_total_tokens'], 40)

    def test_same_model_repair_does_not_duplicate_penalty(self):
        missions = [
            {
                'id': 'original', 'thread_id': 'thread-repair', 'start_at': '2026-09-10T10:00:00+00:00',
                'model_key': '5.6 sol high', 'total_tokens': 100,
                'model_totals': [{'model_key': '5.6 sol high', 'total_tokens': 100}],
                'turns': [{'turn_id': 'a', 'prompt': 'lam web', 'model_key': '5.6 sol high',
                           'total_tokens': 100,
                           'model_totals': [{'model_key': '5.6 sol high', 'total_tokens': 100}]}],
            },
            {
                'id': 'repair', 'thread_id': 'thread-repair', 'start_at': '2026-09-10T10:10:00+00:00',
                'model_key': '5.6 sol high', 'total_tokens': 40,
                'model_totals': [{'model_key': '5.6 sol high', 'total_tokens': 40}],
                'turns': [{'turn_id': 'b', 'prompt': 'chua dung, sua lai', 'model_key': '5.6 sol high',
                           'total_tokens': 40,
                           'model_totals': [{'model_key': '5.6 sol high', 'total_tokens': 40}]}],
            },
        ]

        server._codex_apply_repair_penalties(missions)
        self.assertEqual(missions[1]['repair_tokens'], 40)
        self.assertEqual(missions[1]['repair_penalty_tokens'], 0)
        self.assertEqual(missions[0]['repair_penalty_received_tokens'], 0)
        self.assertEqual(missions[0]['penalized_total_tokens'], 100)

        matrix = server._codex_task_matrix(
            missions, None, datetime(2026, 9, 11, tzinfo=timezone.utc)
        )
        other = next(row for row in matrix['rows'] if row['category'] == 'other')
        cell = other['cells']['5.6 sol high']
        self.assertEqual(matrix['eligible_missions'], 1)
        self.assertEqual(cell['sample_count'], 1)
        self.assertEqual(cell['median_total_tokens'], 140)
        self.assertEqual(cell['median_repair_penalty_tokens'], 0)

    def test_abandoned_same_model_attempt_stays_in_accepted_repair_chain(self):
        turns = [
            turn('failed', 'old-thread', '2026-09-10T10:00:00+00:00',
                 'sửa rule usage tracker', model='5.6 sol high'),
            turn('repair', 'new-thread', '2026-09-10T11:00:00+00:00',
                 'sửa tiếp rule usage tracker', model='5.6 sol high'),
        ]
        events = [
            usage('failed', '5.6 sol high', 100, '2026-09-10T10:05:00+00:00'),
            usage('repair', '5.6 sol high', 200, '2026-09-10T11:05:00+00:00'),
        ]
        reviews = {
            'version': 1, 'turn_boundaries': {},
            'missions': {
                'failed': {'outcome': 'abandoned'},
                'repair': {
                    'outcome': 'accepted',
                    'repair_of_anchor_turn_id': 'failed',
                },
            },
        }
        result = server.build_codex_task_outcomes(
            {'turns': turns, 'diagnostics': {}}, events,
            reviews=reviews,
            now=datetime(2026, 9, 11, tzinfo=timezone.utc),
        )
        samples = server._codex_matrix_attributed_samples(result['missions'])
        tracker_samples = [
            sample for sample in samples
            if sample['model_key'] == '5.6 sol high' and sample['category'] == 'web'
        ]
        self.assertEqual(len(tracker_samples), 1)
        self.assertEqual(tracker_samples[0]['total_tokens'], 300)
        self.assertTrue(tracker_samples[0]['accepted'])
        self.assertFalse(tracker_samples[0]['unconfirmed'])

    def test_abandoned_mixed_route_is_kept_when_later_repair_completes_goal(self):
        turns = [
            turn('failed-high', 'old-thread', '2026-09-10T10:00:00+00:00',
                 'sửa rule usage tracker', model='5.6 sol high'),
            turn('failed-medium', 'old-thread', '2026-09-10T10:10:00+00:00',
                 'làm tiếp rule usage tracker', model='5.6 sol standard'),
            turn('repair', 'new-thread', '2026-09-10T11:00:00+00:00',
                 'sửa tiếp rule usage tracker', model='5.6 sol high'),
        ]
        events = [
            usage('failed-high', '5.6 sol high', 100, '2026-09-10T10:05:00+00:00'),
            usage('failed-medium', '5.6 sol standard', 50, '2026-09-10T10:15:00+00:00'),
            usage('repair', '5.6 sol high', 200, '2026-09-10T11:05:00+00:00'),
        ]
        reviews = {
            'version': 1, 'turn_boundaries': {},
            'missions': {
                'failed-high': {'outcome': 'abandoned'},
                'repair': {
                    'outcome': 'accepted',
                    'repair_of_anchor_turn_id': 'failed-high',
                },
            },
        }
        result = server.build_codex_task_outcomes(
            {'turns': turns, 'diagnostics': {}}, events,
            reviews=reviews,
            now=datetime(2026, 9, 11, tzinfo=timezone.utc),
        )
        samples = server._codex_matrix_attributed_samples(result['missions'])
        tracker = {
            sample['model_key']: sample for sample in samples
            if sample['category'] == 'web'
        }
        self.assertEqual(tracker['5.6 sol high']['total_tokens'], 300)
        self.assertEqual(tracker['5.6 sol standard']['raw_total_tokens'], 50)
        self.assertTrue(tracker['5.6 sol high']['accepted'])
        self.assertTrue(tracker['5.6 sol standard']['accepted'])

    def test_non_contiguous_manual_repair_chain_uses_cumulative_quota_penalty(self):
        turns = [
            turn('a', 'thread-chain', '2026-09-10T10:00:00+00:00',
                 'lam web usage tracker', model='5.6 sol high'),
            turn('unrelated-1', 'thread-chain', '2026-09-10T10:05:00+00:00',
                 'viet tai lieu huong dan rieng', model='5.6 sol high'),
            turn('b', 'thread-chain', '2026-09-10T10:10:00+00:00',
                 'usage tracker chua dung, sua lai', model='5.6 sol xhigh'),
            turn('unrelated-2', 'thread-chain', '2026-09-10T10:15:00+00:00',
                 'kiem tra wifi cho toi', model='5.6 sol high'),
            turn('c', 'thread-chain', '2026-09-10T10:20:00+00:00',
                 'usage tracker van sai, sua lai tiep', model='gpt-6-astra low'),
        ]
        events = [
            usage('a', '5.6 sol high', 100, '2026-09-10T10:01:00+00:00'),
            usage('unrelated-1', '5.6 sol high', 20, '2026-09-10T10:06:00+00:00'),
            usage('b', '5.6 sol xhigh', 50, '2026-09-10T10:11:00+00:00'),
            usage('unrelated-2', '5.6 sol high', 20, '2026-09-10T10:16:00+00:00'),
            usage('c', 'gpt-6-astra low', 25, '2026-09-10T10:21:00+00:00'),
        ]
        reviews = {
            'version': 1,
            'turn_boundaries': {
                'unrelated-1': 'new', 'b': 'new', 'unrelated-2': 'new', 'c': 'new',
            },
            'missions': {
                'b': {'repair_of_anchor_turn_id': 'a'},
                'c': {'repair_of_anchor_turn_id': 'b'},
            },
        }
        quota_efficiency = {'models': [
            {'model_key': '5.6 sol high', 'tokens_per_quota_pct': 100, 'confidence': 'high', 'sample_count': 10},
            {'model_key': '5.6 sol xhigh', 'tokens_per_quota_pct': 50, 'confidence': 'high', 'sample_count': 10},
            {'model_key': 'gpt-6-astra low', 'tokens_per_quota_pct': 25, 'confidence': 'high', 'sample_count': 10},
        ]}

        result = server.build_codex_task_outcomes(
            {'turns': turns, 'diagnostics': {}}, events, reviews=reviews,
            now=datetime(2026, 9, 11, tzinfo=timezone.utc),
            quota_efficiency=quota_efficiency,
        )
        missions = {mission['anchor_turn_id']: mission for mission in result['missions']}
        original = missions['a']
        second = missions['b']
        third = missions['c']

        self.assertEqual(second['repair_of_anchor_turn_id'], 'a')
        self.assertEqual(third['repair_of_anchor_turn_id'], 'b')
        self.assertEqual(third['repair_root_mission_id'], original['id'])
        self.assertEqual(
            third['repair_chain_models'],
            ['5.6 sol high', '5.6 sol xhigh', 'gpt-6-astra low'],
        )
        self.assertAlmostEqual(original['quota_pct_5h'], 1.0)
        self.assertAlmostEqual(original['repair_penalty_received_quota_pct_5h'], 2.0)
        self.assertAlmostEqual(original['penalized_quota_pct_5h'], 3.0)
        self.assertAlmostEqual(second['quota_pct_5h'], 1.0)
        self.assertAlmostEqual(second['repair_penalty_received_quota_pct_5h'], 1.0)
        self.assertAlmostEqual(second['penalized_quota_pct_5h'], 2.0)
        self.assertAlmostEqual(third['quota_pct_5h'], 1.0)
        self.assertAlmostEqual(third['repair_penalty_received_quota_pct_5h'], 0.0)
        self.assertAlmostEqual(third['penalized_quota_pct_5h'], 1.0)

        # Account burn is 3%; the model comparison scores deliberately total
        # 6% because later repairs are also charged as failure penalties.
        self.assertAlmostEqual(
            sum(missions[key]['quota_pct_5h'] for key in ('a', 'b', 'c')), 3.0
        )
        self.assertAlmostEqual(
            sum(missions[key]['penalized_quota_pct_5h'] for key in ('a', 'b', 'c')), 6.0
        )

        matrix = result['matrices']['all']
        web = next(row for row in matrix['rows'] if row['category'] == 'web')
        sol = web['cells']['5.6 sol high']
        xhigh = web['cells']['5.6 sol xhigh']
        astra = web['cells']['gpt-6-astra low']
        self.assertEqual(sol['quota_sample_status'], 'sparse_direct')
        self.assertEqual(sol['pending_review_count'], 0)
        self.assertEqual(sol['unconfirmed_count'], 1)
        self.assertEqual(sol['median_raw_quota_pct_5h'], 1.0)
        self.assertEqual(sol['median_repair_penalty_quota_pct_5h'], 2.0)
        self.assertEqual(sol['effective_quota_pct_5h'], 3.0)
        self.assertEqual(sol['relative_quota_vs_sol_high'], 1.0)
        self.assertEqual(xhigh['effective_quota_pct_5h'], 2.0)
        self.assertEqual(xhigh['relative_quota_vs_sol_high'], 0.667)
        self.assertEqual(astra['effective_quota_pct_5h'], 1.0)
        self.assertEqual(astra['relative_quota_vs_sol_high'], 0.333)

    def test_explicit_thread_link_makes_cross_thread_repair_candidate_high_confidence(self):
        turns = [
            turn(
                'original', '11111111-1111-7111-8111-111111111111',
                '2026-09-10T10:00:00+00:00', 'làm trang public GitHub',
                model='5.6 sol high',
            ),
            turn(
                'repair', '22222222-2222-7222-8222-222222222222',
                '2026-09-10T11:00:00+00:00',
                'codex://threads/11111111-1111-7111-8111-111111111111 '
                'phần trong task này chưa đúng, sửa lại giúp tôi',
                model='gpt-6-astra low',
            ),
        ]
        events = [
            usage('original', '5.6 sol high', 100, '2026-09-10T10:01:00+00:00'),
            usage('repair', 'gpt-6-astra low', 50, '2026-09-10T11:01:00+00:00'),
        ]
        result = server.build_codex_task_outcomes(
            {'turns': turns, 'diagnostics': {}}, events,
            reviews={'version': 1, 'turn_boundaries': {}, 'missions': {}},
            now=datetime(2026, 9, 11, tzinfo=timezone.utc),
        )
        missions = {mission['anchor_turn_id']: mission for mission in result['missions']}
        repair = missions['repair']
        self.assertEqual(repair['repair_of_anchor_turn_id'], 'original')
        self.assertEqual(repair['repair_link_confidence'], 'high')
        self.assertTrue(repair['repair_link_candidates'][0]['explicit_thread_reference'])

    def test_unknown_quota_rate_stays_unavailable_instead_of_using_tokens(self):
        missions = [{
            'id': 'unknown',
            'anchor_turn_id': 'unknown',
            'thread_id': 'thread-unknown',
            'start_at': '2026-09-10T10:00:00+00:00',
            'status': 'unresolved',
            'category': 'web',
            'categories': ['web'],
            'model_key': 'unknown-model',
            'pure_model': True,
            'total_tokens': 100,
            'model_totals': [{'model_key': 'unknown-model', 'total_tokens': 100}],
            'turns': [{
                'turn_id': 'unknown',
                'usage_task_id': 'unknown',
                'prompt': 'lam web',
                'model_key': 'unknown-model',
                'total_tokens': 100,
                'categories': ['web'],
                'model_totals': [{'model_key': 'unknown-model', 'total_tokens': 100}],
            }],
        }]
        server._codex_apply_repair_penalties(missions)
        server._codex_apply_mission_quota_estimates(missions, quota_efficiency={'models': []})
        self.assertFalse(missions[0]['quota_known'])
        self.assertIsNone(missions[0]['quota_pct_5h'])

        matrix = server._codex_task_matrix(
            missions, None, datetime(2026, 9, 11, tzinfo=timezone.utc)
        )
        web = next(row for row in matrix['rows'] if row['category'] == 'web')
        cell = web['cells']['unknown-model']
        self.assertEqual(cell['sample_count'], 1)
        self.assertEqual(cell['median_total_tokens'], 100)
        self.assertEqual(cell['quota_sample_status'], 'no_data')
        self.assertIsNone(cell['effective_quota_pct_5h'])

    def test_quota_bridge_remains_estimated_when_baseline_is_sparse(self):
        def mission(key, at, category, model, tokens, quota):
            model_usage = {
                'model_key': model,
                'total_tokens': tokens,
                'quota_known': True,
                'quota_pct_5h': quota,
                'quota_source': 'calibrated_model',
            }
            return {
                'id': key,
                'anchor_turn_id': key,
                'thread_id': key,
                'start_at': at,
                'status': 'accepted_explicit',
                'accepted': True,
                'category': category,
                'categories': [category],
                'model_key': model,
                'pure_model': True,
                'total_tokens': tokens,
                'model_totals': [dict(model_usage)],
                'turns': [{
                    'turn_id': key,
                    'prompt': category,
                    'model_key': model,
                    'total_tokens': tokens,
                    'categories': [category],
                    'model_totals': [dict(model_usage)],
                }],
            }

        missions = []
        for index in range(3):
            missions.append(mission(
                f'h-sol-{index}', f'2026-01-0{index + 1}T10:00:00+00:00',
                'documents', '5.6 sol high', 100, 2.0,
            ))
            missions.append(mission(
                f'h-astra-{index}', f'2026-01-0{index + 1}T11:00:00+00:00',
                'documents', 'gpt-6-astra low', 50, 1.0,
            ))
        missions.append(mission(
            'current-sol', '2026-09-10T10:00:00+00:00',
            'web', '5.6 sol high', 200, 4.0,
        ))

        matrix = server._codex_task_matrix(
            missions, 30, datetime(2026, 9, 11, tzinfo=timezone.utc)
        )
        web = next(row for row in matrix['rows'] if row['category'] == 'web')
        astra = web['cells']['gpt-6-astra low']
        self.assertEqual(astra['sample_count'], 0)
        self.assertEqual(astra['quota_value_source'], 'bridge')
        self.assertEqual(astra['quota_baseline_source'], 'sparse_direct')
        self.assertEqual(astra['quota_sample_status'], 'estimated')
        self.assertEqual(astra['effective_quota_pct_5h'], 2.0)
        self.assertEqual(astra['relative_quota_vs_sol_high'], 0.5)

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
        self.assertEqual(result['matrices']['all']['eligible_missions'], 0)
        self.assertEqual(result['matrices']['all']['eligible_samples'], 0)

    def test_manually_reviewed_parentless_subagent_is_promoted_to_mission(self):
        orphan = turn(
            'reviewed-orphan', 'direct-thread', '2026-09-17T05:17:26+00:00',
            'Sửa công thức Excel để chạy đúng trên mobile.',
            model='gpt-6-astra low', subagent=True,
        )
        orphan['outer_turn_id'] = 'reviewed-orphan'
        result = server.build_codex_task_outcomes(
            {'turns': [orphan], 'diagnostics': {}},
            [usage('reviewed-orphan', 'gpt-6-astra low', 120, '2026-09-17T05:20:00+00:00')],
            reviews={
                'version': 1,
                'turn_boundaries': {},
                'missions': {
                    'reviewed-orphan': {
                        'categories': ['documents'],
                        'outcome': 'accepted',
                    },
                },
            },
            now=datetime(2026, 9, 18, tzinfo=timezone.utc),
        )

        self.assertEqual(len(result['missions']), 1)
        mission = result['missions'][0]
        self.assertEqual(mission['anchor_turn_id'], 'reviewed-orphan')
        self.assertEqual(mission['status'], 'accepted_manual')
        self.assertEqual(mission['total_tokens'], 120)
        self.assertEqual(result['diagnostics']['delegated_turns_orphaned'], 0)

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

    def test_turn_scanner_counts_custom_tool_calls_and_outputs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sessions = os.path.join(temp_dir, 'sessions')
            os.makedirs(sessions)
            cache = os.path.join(temp_dir, 'turn-cache.json')
            log_path = os.path.join(sessions, 'rollout-custom-tools.jsonl')
            metadata = {'turn_id': 'turn-custom'}
            records = [
                {'type': 'session_meta', 'timestamp': '2026-09-01T10:00:00Z',
                 'payload': {'id': 'thread-custom', 'cwd': r'C:\work\usage-tracker'}},
                {'type': 'event_msg', 'timestamp': '2026-09-01T10:00:01Z',
                 'payload': {'type': 'task_started', 'turn_id': 'turn-custom'}},
                {'type': 'turn_context', 'timestamp': '2026-09-01T10:00:02Z',
                 'payload': {'turn_id': 'turn-custom', 'model': 'gpt-6-astra', 'effort': 'low'}},
                {'type': 'response_item', 'timestamp': '2026-09-01T10:00:03Z',
                 'payload': {'type': 'message', 'role': 'user',
                             'content': [{'type': 'input_text', 'text': 'Sửa workbook.'}]}},
                {'type': 'response_item', 'timestamp': '2026-09-01T10:00:04Z',
                 'payload': {'type': 'custom_tool_call', 'name': 'exec',
                             'internal_chat_message_metadata_passthrough': metadata}},
                {'type': 'response_item', 'timestamp': '2026-09-01T10:00:05Z',
                 'payload': {'type': 'custom_tool_call_output', 'output': {'exit_code': 0},
                             'internal_chat_message_metadata_passthrough': metadata}},
                {'type': 'response_item', 'timestamp': '2026-09-01T10:00:06Z',
                 'payload': {'type': 'custom_tool_call', 'name': 'exec',
                             'internal_chat_message_metadata_passthrough': metadata}},
                {'type': 'response_item', 'timestamp': '2026-09-01T10:00:07Z',
                 'payload': {'type': 'custom_tool_call_output', 'output': {'exit_code': 1},
                             'internal_chat_message_metadata_passthrough': metadata}},
                {'type': 'event_msg', 'timestamp': '2026-09-01T10:00:08Z',
                 'payload': {'type': 'task_complete', 'turn_id': 'turn-custom'}},
            ]
            with open(log_path, 'w', encoding='utf-8') as handle:
                for record in records:
                    handle.write(json.dumps(record, ensure_ascii=False) + '\n')

            result = server.scan_codex_mission_turns(sessions_dir=sessions, cache_file=cache)
            self.assertEqual(len(result['turns']), 1)
            scanned = result['turns'][0]
            self.assertEqual(scanned['tool_call_count'], 2)
            self.assertEqual(scanned['tool_success_count'], 1)
            self.assertEqual(scanned['tool_failure_count'], 1)

    def test_single_outer_task_splits_human_messages_and_usage_without_double_counting(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sessions = os.path.join(temp_dir, 'sessions')
            os.makedirs(sessions)
            cache = os.path.join(temp_dir, 'turn-cache.json')
            log_path = os.path.join(sessions, 'rollout-mixed.jsonl')
            prompts = [
                'file word ho so dong chi Tu, cap hoc trung cap, que quan',
                'tai sao quyen doc bi chan, con dong chi Tu thi chua dung',
                'con may cho luc nay toi noi ban da sua chua',
                'the ban sua di',
                'sao no lai bi chan quyen, kiem tra quyen doc',
            ]
            records = [
                {'type': 'session_meta', 'timestamp': '2026-09-10T10:00:00Z',
                 'payload': {'id': 'thread-mixed-outer', 'cwd': r'C:\work'}},
                {'type': 'event_msg', 'timestamp': '2026-09-10T10:00:01Z',
                 'payload': {'type': 'task_started', 'turn_id': 'outer-turn', 'started_at': '2026-09-10T10:00:01Z'}},
                {'type': 'turn_context', 'timestamp': '2026-09-10T10:00:02Z',
                 'payload': {'turn_id': 'outer-turn', 'model': 'gpt-5.6-sol', 'effort': 'high'}},
            ]
            for index, prompt in enumerate(prompts):
                minute = index * 5
                records.extend([
                    {'type': 'response_item', 'timestamp': f'2026-09-10T10:{minute:02d}:03Z',
                     'payload': {'type': 'message', 'role': 'user',
                                 'content': [{'type': 'input_text', 'text': prompt}]}},
                    {'type': 'response_item', 'timestamp': f'2026-09-10T10:{minute:02d}:30Z',
                     'payload': {'type': 'message', 'role': 'assistant',
                                 'content': [{'type': 'output_text', 'text': 'done'}]}},
                ])
            records.append(
                {'type': 'event_msg', 'timestamp': '2026-09-10T10:25:00Z',
                 'payload': {'type': 'task_complete', 'turn_id': 'outer-turn',
                             'completed_at': '2026-09-10T10:25:00Z'}}
            )
            with open(log_path, 'w', encoding='utf-8') as handle:
                for record in records:
                    handle.write(json.dumps(record, ensure_ascii=False) + '\n')

            scanned = server.scan_codex_mission_turns(sessions_dir=sessions, cache_file=cache)
            self.assertEqual(len(scanned['turns']), 5)
            self.assertEqual(scanned['turns'][0]['turn_id'], 'outer-turn')
            self.assertEqual(scanned['turns'][1]['turn_id'], 'outer-turn__u2')
            self.assertTrue(all(item['outer_turn_id'] == 'outer-turn' for item in scanned['turns']))
            self.assertEqual([item['user_text'] for item in scanned['turns']], prompts)
            self.assertEqual([item['assistant_message_count'] for item in scanned['turns']], [1, 1, 1, 1, 1])
            self.assertEqual([item['assistant_text'] for item in scanned['turns']], ['done'] * 5)

            token_totals = [100, 200, 300, 400, 500]
            events = [
                usage('outer-turn', '5.6 sol high', total, f'2026-09-10T10:{index * 5 + 1:02d}:00+00:00')
                for index, total in enumerate(token_totals)
            ]
            result = server.build_codex_task_outcomes(
                scanned, events,
                reviews={'version': 1, 'turn_boundaries': {}, 'missions': {}},
                now=datetime(2026, 9, 11, tzinfo=timezone.utc),
            )

            self.assertEqual(len(result['missions']), 1)
            mission = result['missions'][0]
            self.assertEqual(mission['anchor_turn_id'], 'outer-turn')
            self.assertEqual(mission['turn_count'], 5)
            self.assertEqual(mission['total_tokens'], sum(token_totals))
            self.assertEqual([item['total_tokens'] for item in mission['turns']], token_totals)
            self.assertEqual(mission['categories'], ['documents', 'system_diagnostics'])
            self.assertEqual(mission['turns'][2]['categories'], ['documents'])
            self.assertTrue(mission['turns'][2]['category_inherited'])
            self.assertEqual(mission['turns'][3]['categories'], ['documents'])
            self.assertTrue(mission['turns'][3]['category_inherited'])
            self.assertEqual(mission['turns'][4]['categories'], ['system_diagnostics'])


if __name__ == '__main__':
    unittest.main()
