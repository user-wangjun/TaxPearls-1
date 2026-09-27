"""Pre-confirmation enterprise input analysis is not risk evaluation."""
from copy import deepcopy
from io import BytesIO
from pathlib import Path
import unittest
from unittest.mock import patch

from src import config, engine, materials
from src.input_errors import InputError
from src.snapshots import deserialize_dataset
from tests.test_materials import accounts, workbook, COMPANY, KEYS
from tests.test_related_graph import graph_workbook
from webapp.enterprise_analysis import analyze, prefill

ROOT = Path(__file__).resolve().parents[1]


class EnterpriseAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.rules = engine.load_rules(ROOT / 'rules')
        self.scope = {'name': COMPANY['name'], 'taxpayer_id': COMPANY['taxpayer_id'], 'industry': '服务业',
                      'period_start': '2026-01-01', 'period_end': '2026-01-31'}

    def docs(self, company=COMPANY, amount=100000):
        return materials.preview([('账.xlsx', accounts(company, amount))], KEYS, allow_incomplete_company=True)

    def test_unambiguous_prefill_does_not_use_filename_or_upload_date(self):
        docs = self.docs()
        self.assertEqual(prefill(docs), self.scope)
        docs[0]['company']['period'] = ''
        docs[0]['name'] = '2026-01账.xlsx'
        self.assertEqual(prefill(docs)['period_start'], '')
        two = self.docs({**COMPANY, 'period': '2025-01'})
        self.assertEqual(prefill(self.docs() + two)['period_start'], '')

    def test_partial_materials_can_continue_but_no_risk_rule_runs(self):
        with patch('src.engine.evaluate', side_effect=AssertionError('risk run')), \
             patch('src.engine.run', side_effect=AssertionError('risk run')), \
             patch('src.related_graph.run', side_effect=AssertionError('graph run')):
            result = analyze(self.docs(), self.scope, {}, self.rules)
        self.assertTrue(result['can_confirm'])
        self.assertTrue(result['feedback']['limited'])
        self.assertNotIn('findings', result)
        self.assertFalse(any('status' in c for c in result['checks']))
        data = deserialize_dataset(result['dataset'])
        self.assertEqual(data.get('营业收入'), 100000)
        self.assertIsNone(data.get('增值税.销售额'))
        graph = next(c for c in result['checks'] if c['rule_id'] == 'G-001')
        self.assertFalse(graph['ready'])
        self.assertIn('未提供关联方', graph['reasons'][0])

    def test_graph_readiness_is_in_scope_without_executing_a_finding(self):
        for reviewed in (True, False):
            with self.subTest(reviewed=reviewed):
                book = graph_workbook()
                if not reviewed:
                    book['关联交易']['G2'] = '待复核'
                stream = BytesIO()
                book.save(stream)
                book.close()
                docs = materials.preview([('关联.xlsx', stream.getvalue())], KEYS)
                with patch('src.related_graph.run', side_effect=AssertionError('must not execute')):
                    result = analyze(docs, prefill(docs), {}, self.rules)
                check = next(c for c in result['checks'] if c['rule_id'] == 'G-001')
                self.assertEqual(check['ready'], reviewed)
                self.assertNotIn('status', check)
                self.assertTrue(result['can_confirm'])
                if not reviewed:
                    self.assertIn('未复核', check['reasons'][0])

    def test_required_scope_and_invalid_dates_are_blocking(self):
        for key in ('name', 'taxpayer_id', 'period_start', 'period_end'):
            with self.subTest(key=key):
                result = analyze(self.docs(), {**self.scope, key: ''}, {}, self.rules)
                self.assertFalse(result['can_confirm'])
                self.assertIsNone(result['dataset'])
        result = analyze(self.docs(), {**self.scope, 'period_end': '2026-02-30'}, {}, self.rules)
        self.assertEqual(result['feedback']['blocking'][0]['code'], 'invalid_period')

    def test_tax_identity_conflict_cannot_be_erased_by_user_scope(self):
        docs = self.docs({**COMPANY, 'taxpayer_id': 'OTHER-CUSTOMER'})
        result = analyze(docs, self.scope, {}, self.rules)
        self.assertFalse(result['can_confirm'])
        self.assertEqual(result['feedback']['blocking'][0]['code'], 'identity_conflict')
        self.assertEqual(result['files'][0]['material_company']['taxpayer_id'], 'OTHER-CUSTOMER')

    def test_original_identity_matrix_never_treats_user_tax_as_source_proof(self):
        cases = [
            ('其他企业', '', False, 'identity_unresolved'),
            ('其他企业', 'OTHER-TAX', False, 'identity_conflict'),
            ('其他企业', COMPANY['taxpayer_id'], True, 'name_difference'),
            (COMPANY['name'], '', True, 'scope_user_supplied'),
            ('', '', True, 'scope_user_supplied'),
            (COMPANY['name'], 'OTHER-TAX', False, 'identity_conflict'),
        ]
        for name, tax, ready, code in cases:
            with self.subTest(name=name, tax=tax):
                docs = self.docs({**COMPANY, 'name': name, 'taxpayer_id': tax})
                before = deepcopy(docs)
                result = analyze(docs, self.scope, {}, self.rules)
                self.assertEqual(result['can_confirm'], ready, result['feedback'])
                level = 'suggested' if ready else 'blocking'
                self.assertIn(code, [n['code'] for n in result['feedback'][level]])
                self.assertEqual(docs, before)
                if not ready:
                    self.assertIsNone(result['dataset'])

    def test_unidentified_other_enterprise_blocked_for_current_and_history_until_excluded(self):
        for purpose, period in [('current', '2026-01'), ('history', '2025-01')]:
            with self.subTest(purpose=purpose):
                docs = materials.preview([('本期.xlsx', accounts()), ('其他.xlsx', accounts({
                    **COMPANY, 'name': '其他企业', 'taxpayer_id': '', 'period': period}))], KEYS,
                    allow_incomplete_company=True)
                result = analyze(docs, self.scope, {'1': {'purpose': purpose}}, self.rules)
                self.assertFalse(result['can_confirm'])
                self.assertIn('identity_unresolved', [n['code'] for n in result['feedback']['blocking']])
                result = analyze(docs, self.scope, {'1': {'purpose': 'excluded'}}, self.rules)
                self.assertTrue(result['can_confirm'])
                self.assertEqual(deserialize_dataset(result['dataset']).get('营业收入'), 100000)

    def test_explicit_current_period_conflict_blocks_and_alias_is_normalized(self):
        docs = self.docs({**COMPANY, 'period': '2025-01'})
        result = analyze(docs, self.scope, {}, self.rules)
        self.assertEqual(result['feedback']['blocking'][0]['code'], 'period_conflict')
        result = analyze(self.docs({**COMPANY, 'period': '2026年1月'}), self.scope, {}, self.rules)
        self.assertTrue(result['can_confirm'])
        self.assertTrue(any(e['origin'] == 'normalized' for e in result['edits']))

    def test_history_reference_never_replaces_current_amount(self):
        # Accounting credit turnover and profit-statement revenue are not
        # interchangeable. Trend input uses its declared statement metric.
        docs = materials.preview([('本期.xlsx', workbook('利润表', [config.COL_STATEMENT, ['营业收入', 100000]], COMPANY)),
                                  ('上年.xlsx', workbook('利润表', [config.COL_STATEMENT, ['营业收入', 300000]],
                                                       {**COMPANY, 'period': '2025-01'}))], KEYS)
        original = deepcopy(docs)
        result = analyze(docs, self.scope, {'1': {'purpose': 'history'}}, self.rules)
        self.assertTrue(result['can_confirm'], result['feedback'])
        data = deserialize_dataset(result['dataset'])
        self.assertEqual(data.get('利润表.营业收入'), 100000)
        self.assertEqual(data.get('趋势.营业收入.上年同期'), 300000)
        self.assertFalse(data.accounts)
        self.assertEqual(docs, original)
        self.assertTrue(any(f['purpose'] == 'history' for f in result['files']))

    def test_future_or_undated_history_and_history_only_are_blocking(self):
        for period in ('', '2026-01', '2027-01'):
            docs = materials.preview([('本期.xlsx', accounts()),
                                      ('历史.xlsx', accounts({**COMPANY, 'period': period}))], KEYS,
                                     allow_incomplete_company=True)
            result = analyze(docs, self.scope, {'1': {'purpose': 'history'}}, self.rules)
            self.assertTrue(any(n['code'] == 'history_period' for n in result['feedback']['blocking']))
        docs = self.docs({**COMPANY, 'period': '2025-01'})
        result = analyze(docs, self.scope, {'0': {'purpose': 'history'}}, self.rules)
        self.assertTrue(any(n['code'] == 'no_current_material' for n in result['feedback']['blocking']))

    def test_read_failure_requires_explicit_exclusion(self):
        docs = self.docs()
        failed = deepcopy(docs[0])
        failed.update(id='1', name='读取失败.xlsx', error='文件无法读取')
        docs.append(failed)
        result = analyze(docs, self.scope, {}, self.rules)
        self.assertFalse(result['can_confirm'])
        self.assertEqual(result['feedback']['blocking'][0]['code'], 'read_failed')
        result = analyze(docs, self.scope, {'1': {'purpose': 'excluded'}}, self.rules)
        self.assertTrue(result['can_confirm'])
        self.assertEqual(len(result['files']), 2)

    def test_missing_industry_is_not_made_mandatory_or_invented(self):
        scope = {**self.scope, 'industry': ''}
        docs = self.docs({**COMPANY, 'industry': ''})
        result = analyze(docs, scope, {}, self.rules)
        self.assertTrue(result['can_confirm'])
        self.assertEqual(result['dataset']['company']['industry'], '未提供')

    def test_conflicting_amounts_block_without_summation(self):
        docs = materials.preview([('账A.xlsx', accounts(COMPANY, 100000)),
                                  ('账B.xlsx', accounts(COMPANY, 200000))], KEYS)
        result = analyze(docs, self.scope, {}, self.rules)
        self.assertFalse(result['can_confirm'])
        self.assertEqual(result['feedback']['blocking'][0]['code'], 'input_conflict')

    def test_zero_denominator_limited_even_with_complete_inputs(self):
        rule = next(r for r in self.rules if r.id == 'R-001')
        from src.models import Company, Dataset, Metric
        data = Dataset(Company(**COMPANY), [], {}, {'营业收入': Metric('营业收入', 100, '账表'),
                                                   '增值税.销售额': Metric('增值税.销售额', 0, '申报')})
        self.assertTrue(engine.readiness(rule, data))
        data.metrics['增值税.销售额'].value = 100
        self.assertEqual(engine.readiness(rule, data), [])
        data.metrics['增值税.销售额'].source = ''
        self.assertTrue(engine.readiness(rule, data))

    def test_unknown_selection_and_forged_per_file_scope_rejected(self):
        with self.assertRaises(InputError):
            analyze(self.docs(), self.scope, {'foreign': {}}, self.rules)
        with self.assertRaises(InputError):
            analyze(self.docs(), self.scope, {'0': {'company': {'taxpayer_id': 'FORGED'}}}, self.rules)
        with self.assertRaises(InputError):
            analyze(self.docs(), self.scope, {'0': {'rows': []}}, self.rules)


if __name__ == '__main__':
    unittest.main()
