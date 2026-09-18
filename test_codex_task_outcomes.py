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
