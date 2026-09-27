"""E09 denominators, frozen answers, privacy, snapshot consistency and recovery."""
from copy import deepcopy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from scripts.ops_db import create_backup, restore_backup
from webapp import app as module, training_stats
from webapp.storage import Store


class TrainingStatisticsTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'TAXPEARLS_AI_ENABLED': '0', 'TAXPEARLS_NOTIFICATION_EMAIL_ENABLED': '0'})
        self.env.start()
        self.tmp = TemporaryDirectory()
        self.old = module.store
        self.store = Store(Path(self.tmp.name) / 'statistics.db')
        module.store = self.store
        self.password = 'Class-statistics-2026!'
        self.teacher = self.person('teacher', 'teacher')
        self.students = [self.person('student-' + str(i), 'student') for i in range(3)]
        self.client = TestClient(module.app)
        self.client.__enter__()
        self.login(self.teacher)
        response = self.client.post('/api/exercises', json={'rule_id': 'R-020', 'seed': 17, 'expected_version': '2.0'})
        self.assertEqual(response.status_code, 200, response.text)
        self.audit = response.json()['audit_id']
        # Controlled synthetic snapshot: one hit, one pass and one skipped rule.
        # These fixture statuses make each numerator independently testable.
        with self.store.connect() as db:
            raw = json.loads(db.execute('SELECT findings_json FROM audits WHERE id=?', (self.audit,)).fetchone()[0])
        self.findings = deepcopy(raw[:3])
        for finding, status in zip(self.findings, ('hit', 'pass', 'skipped')):
            finding['status'] = status
        self.hit, self.passed, self.skipped = [f['rule']['id'] for f in self.findings]
        self.set_findings(self.findings)
        response = self.client.post('/api/classes', json={'name': '学情测试班', 'student_ids': [s['id'] for s in self.students]})
        self.assertEqual(response.status_code, 200, response.text)
        self.cid = response.json()['id']

    def tearDown(self):
        self.client.__exit__(None, None, None)
        module.store = self.old
        self.tmp.cleanup()
        self.env.stop()

    def person(self, name, role, org='statistics-school'):
        return self.store.create_user(name, self.password, name, role, org)

    def login(self, person):
        self.client.cookies.clear()
        token = self.store.authenticate(person['username'], self.password)[1]
        self.client.cookies.set(module.COOKIE_NAME, token)

    def set_findings(self, items):
        with self.store.connect() as db:
            db.execute('UPDATE audits SET findings_json=? WHERE id=?', (json.dumps(items), self.audit))

    def assignment(self, **fields):
        self.login(self.teacher)
        body = {'title': '案例题', 'audit_id': self.audit, 'class_id': self.cid, 'published': True}
        body.update(fields)
        response = self.client.post('/api/assignments', json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['id']

    def submit(self, aid, person, answers):
        self.login(person)
        response = self.client.post('/api/assignments/' + aid + '/submit', json={'selected_rule_ids': answers})
        self.assertEqual(response.status_code, 200, response.text)

    def stats(self, **params):
        self.login(self.teacher)
        response = self.client.get('/api/classes/' + self.cid + '/statistics', params=params)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers['cache-control'], 'private, no-store')
        return response.json()

    def test_denominators_missing_misses_false_positives_and_distribution(self):
        aid = self.assignment()
        self.submit(aid, self.students[0], [self.hit])
        self.submit(aid, self.students[1], [self.passed])
        result = self.stats()
        q = result['questions'][0]
        self.assertEqual((q['expected'], q['submitted'], q['unsubmitted'], q['valid'], q['exact_correct']), (3, 2, 1, 2, 1))
        self.assertEqual((q['rank'], q['accuracy'], q['mean_score']), (1, 50, 50))
        rules = {r['rule_id']: r for r in result['rules']}
        self.assertEqual((rules[self.hit]['missed'], rules[self.hit]['hit_opportunities'], rules[self.hit]['miss_rate']), (1, 2, 50))
        self.assertEqual((rules[self.passed]['false_positive'], rules[self.passed]['pass_opportunities'], rules[self.passed]['false_positive_rate']), (1, 2, 50))
        self.assertEqual(sum(c['errors'] for c in result['categories']), 2)
        self.assertEqual(sum(c['error_share'] for c in result['categories']), 100)

    def test_skipped_is_not_true_negative_and_unsupported_is_separate(self):
        aid = self.assignment()
        self.submit(aid, self.students[0], [self.hit, self.skipped])
        self.submit(aid, self.students[1], [self.hit])
        result = self.stats()
        rule = next(r for r in result['rules'] if r['rule_id'] == self.skipped)
        self.assertEqual((rule['unsupported'], rule['skipped'], rule['errors']), (1, 2, 1))
        self.assertEqual(rule['executed_decisions'], 0)
        self.assertIsNone(rule['accuracy'])
        self.assertIsNone(rule['false_positive_rate'])
        self.assertEqual(result['questions'][0]['accuracy'], 50)

    def test_adjustments_change_mean_not_correctness_and_retake_replaces(self):
        aid = self.assignment()
        self.submit(aid, self.students[0], [])
        sub = self.store.get_submission(aid, self.students[0]['id'])
        self.login(self.teacher)
        self.assertEqual(self.client.put('/api/submissions/' + sub['id'] + '/review', json={'adjusted_score': 100, 'feedback': '教学调整'}).status_code, 200)
        q = self.stats()['questions'][0]
        self.assertEqual((q['accuracy'], q['mean_score'], q['automatic_mean_score'], q['adjusted_count']), (0, 100, 0, 1))
        self.submit(aid, self.students[0], [self.hit])
        q = self.stats()['questions'][0]
        self.assertEqual((q['submitted'], q['accuracy'], q['mean_score'], q['adjusted_count']), (1, 100, 100, 0))

    def test_ranking_ties_unsubmitted_and_rounding(self):
        a, b, c, empty = [self.assignment(title=title) for title in ('全对', '薄弱一', '薄弱二', '未提交')]
        self.submit(a, self.students[0], [self.hit])
        self.submit(b, self.students[0], [])
        self.submit(c, self.students[0], [])
        self.submit(c, self.students[1], [])
        questions = self.stats()['questions']
        self.assertEqual([q['rank'] for q in questions], [1, 1, 3, None])
        self.assertEqual([q['accuracy'] for q in questions], [0, 0, 100, None])
        self.assertEqual(questions[-1]['id'], empty)
        self.assertIsNone(questions[-1]['mean_score'])
        self.assertEqual(training_stats.percent(1, 3), 33.33)
        self.assertEqual(training_stats.percent(2, 3), 66.67)

    def test_current_roster_active_accounts_and_target_intersection(self):
        aid = self.assignment()
        targeted = self.assignment(target_student_id=self.students[0]['id'])
        for student in self.students:
            self.submit(aid, student, [self.hit])
        self.submit(targeted, self.students[0], [self.hit])
        self.login(self.teacher)
        self.assertEqual(self.client.put('/api/classes/' + self.cid, json={'name': '新名册', 'student_ids': [s['id'] for s in self.students[1:]], 'revision': 1}).status_code, 200)
        with self.store.connect() as db:
            db.execute('UPDATE users SET active=0 WHERE id=?', (self.students[1]['id'],))
        result = self.stats()
        self.assertEqual(result['active_students'], 1)
        self.assertEqual((result['totals']['expected'], result['totals']['submitted'], result['totals']['excluded_submissions']), (1, 1, 3))
        self.assertIsNotNone(self.store.get_submission(targeted, self.students[0]['id']))
        self.assertIsNone(next(q for q in result['questions'] if q['id'] == targeted)['accuracy'])

    def test_drafts_withdrawn_history_and_expired_cases(self):
        self.assignment(published=False)
        withdrawn, expired, empty_withdrawn = self.assignment(), self.assignment(), self.assignment()
        self.submit(withdrawn, self.students[0], [])
        self.submit(expired, self.students[0], [self.hit])
        self.login(self.teacher)
        self.assertEqual(self.client.put('/api/assignments/' + empty_withdrawn + '/settings', json={'published': False, 'deadline_at': None, 'revision': 1}).status_code, 200)
        self.assertEqual(self.client.put('/api/assignments/' + withdrawn + '/settings', json={'published': False, 'deadline_at': None, 'revision': 1}).status_code, 200)
        with self.store.connect() as db:
            db.execute("UPDATE training_assignment_settings SET deadline_at='2020-01-01T00:00:00+00:00' WHERE assignment_id=?", (expired,))
        result = self.stats()
        self.assertEqual([q['id'] for q in result['questions']], [expired])
        self.assertEqual((result['totals']['excluded_unpublished_empty'], result['totals']['excluded_withdrawn']), (2, 1))
        result = self.stats(include_withdrawn=True)
        self.assertEqual(len(result['questions']), 2)
        self.assertEqual(result['questions'][0]['id'], withdrawn)
        self.assertFalse(result['questions'][0]['published'])

    def test_own_class_only_no_cross_tenant_student_or_admin_read(self):
        self.assignment()
        url = '/api/classes/' + self.cid + '/statistics'
        for role in ('student', 'accountant', 'org_admin', 'platform_admin'):
            self.login(self.person('role-' + role, role))
            self.assertEqual(self.client.get(url).status_code, 403)
        for org in ('statistics-school', 'another-school'):
            self.login(self.person('teacher-' + org, 'teacher', org))
            self.assertEqual(self.client.get(url).status_code, 404)
        self.client.cookies.clear()
        self.assertEqual(self.client.get(url).status_code, 401)

    def test_institution_other_class_and_unpublished_without_submissions_excluded(self):
        public = self.assignment(class_id=None)
        self.submit(public, self.students[0], [self.hit])
        self.login(self.teacher)
        other = self.client.post('/api/classes', json={'name': '其他班', 'student_ids': [self.students[0]['id']]}).json()['id']
        aid = self.assignment(class_id=other)
        self.submit(aid, self.students[0], [self.hit])
        self.assignment(published=False)
        result = self.stats()
        self.assertEqual(result['questions'], [])
        self.assertEqual(result['rules'], [])
        self.assertEqual(result['totals']['expected'], 0)
        self.assertEqual(result['totals']['excluded_unpublished_empty'], 1)

    def test_frozen_rule_versions_and_thresholds_not_merged_or_regraded(self):
        a = self.assignment()
        self.submit(a, self.students[0], [self.hit])
        # Duplicate the fixture as a distinct frozen audit; do not change a's answer.
        with self.store.connect() as db:
            row = dict(db.execute('SELECT * FROM audits WHERE id=?', (self.audit,)).fetchone())
            row['id'] = 'separate-frozen-audit'
            changed = deepcopy(self.findings)
            changed[0]['rule']['version'] = '3.0'
            changed[1]['threshold_desc'] += '，另一期冻结阈值'
            row['findings_json'] = json.dumps(changed)
            db.execute(f"INSERT INTO audits ({','.join(row)}) VALUES ({','.join('?' for _ in row)})", list(row.values()))
        b = self.assignment(audit_id=row['id'])
        self.submit(b, self.students[0], [self.hit])
        with patch('src.training.score_submission', side_effect=AssertionError('must not regrade')):
            with patch('src.engine.evaluate', side_effect=AssertionError('must not run rules')):
                result = self.stats()
        self.assertEqual(len(result['rules']), 5)
        self.assertEqual({r['version'] for r in result['rules'] if r['rule_id'] == self.hit}, {self.findings[0]['rule']['version'], '3.0'})

    def test_bad_answers_and_scores_are_not_silent_success_or_missing(self):
        aid = self.assignment()
        for student in self.students[:2]:
            self.submit(aid, student, [self.hit])
        for bad in ('not json', '{}', '[1]', '["unknown"]', json.dumps([self.hit, self.hit])):
            with self.subTest(bad=bad):
                with self.store.connect() as db:
                    db.execute('UPDATE submissions SET answers_json=? WHERE assignment_id=? AND student_id=?', (bad, aid, self.students[0]['id']))
                q = self.stats()['questions'][0]
                self.assertEqual((q['submitted'], q['unsubmitted'], q['valid'], q['invalid'], q['accuracy']), (2, 1, 1, 1, 100))
                self.assertTrue(q['warnings'])
        with self.store.connect() as db:
            db.execute('UPDATE submissions SET score=? WHERE assignment_id=?', (float('inf'), aid))
        q = self.stats()['questions'][0]
        self.assertIsNone(q['accuracy'])
        self.assertIsNone(q['mean_score'])
        self.assertEqual(q['invalid'], 2)

    def test_corrupt_or_wrong_org_snapshot_does_not_expose_or_fabricate_statistics(self):
        aid = self.assignment()
        self.submit(aid, self.students[0], [self.hit])
        for bad in (None, {}, [], [self.findings[0], self.findings[0]], [{'rule': None}], [{'rule': self.findings[0]['rule'], 'status': 'bogus', 'threshold_desc': ''}]):
            self.set_findings(bad)
            q = self.stats()['questions'][0]
            self.assertEqual(q['invalid'], 1)
            self.assertIsNone(q['accuracy'])
            self.assertTrue(q['warnings'])
        self.set_findings(self.findings)
        with self.store.connect() as db:
            db.execute("UPDATE audits SET org_id='foreign' WHERE id=?", (self.audit,))
        self.assertEqual(self.stats()['rules'], [])

    def test_consistent_read_snapshot_during_concurrent_membership_change(self):
        aid = self.assignment()
        self.submit(aid, self.students[0], [self.hit])
        self.login(self.teacher)
        original = training_stats.collect
        def concurrent(db, classroom, include_withdrawn):
            # owned_class has already established the read snapshot in the route.
            with self.store.connect() as other:
                other.execute('DELETE FROM training_class_members WHERE class_id=?', (self.cid,))
                other.execute('UPDATE training_classes SET revision=revision+1 WHERE id=?', (self.cid,))
            return original(db, classroom, include_withdrawn)
        with patch.object(training_stats, 'collect', side_effect=concurrent):
            result = self.client.get('/api/classes/' + self.cid + '/statistics').json()
        self.assertEqual((result['class']['revision'], result['active_students'], result['totals']['submitted']), (1, 3, 1))
        later = self.stats()
        self.assertEqual((later['class']['revision'], later['active_students'], later['totals']['submitted']), (2, 0, 0))

    def test_reopen_and_backup_restore_same_session_identical_result_no_schema_changes(self):
        aid = self.assignment()
        self.submit(aid, self.students[0], [self.hit])
        before = self.stats()
        cookies = dict(self.client.cookies)
        with self.store.connect() as db:
            schema = list(map(tuple, db.execute('SELECT * FROM sqlite_master ORDER BY name')))
        backup = Path(self.tmp.name) / 'backup.db'
        create_backup(self.store.path, backup)
        restored = Path(self.tmp.name) / 'restored.db'
        restore_backup(backup, restored)
        for path in (self.store.path, restored):
            module.store = Store(path)
            self.client.cookies.clear()
            self.client.cookies.update(cookies)
            self.assertEqual(self.client.get('/api/classes/' + self.cid + '/statistics').json(), before)
            with module.store.connect() as db:
                self.assertEqual(list(map(tuple, db.execute('SELECT * FROM sqlite_master ORDER BY name'))), schema)

    def test_paper_cases_and_teacher_interface_assets(self):
        self.login(self.teacher)
        response = self.client.post('/api/papers', json={'title': '综合试卷', 'class_id': self.cid, 'published': True, 'items': [{'audit_id': self.audit, 'points': 30}]})
        self.assertEqual(response.status_code, 200, response.text)
        paper = self.client.get('/api/papers/' + response.json()['id']).json()
        self.submit(paper['items'][0]['id'], self.students[0], [self.hit])
        q = self.stats()['questions'][0]
        self.assertEqual((q['paper_id'], q['points'], q['accuracy']), (paper['id'], 30, 100))
        html = self.client.get('/').text
        script = self.client.get('/classroom.js').text
        for identifier in ('classStatistics', 'statisticsClass', 'statisticsWithdrawn', 'btnRefreshClassStatistics', 'classStatisticsResult'):
            self.assertIn('id="' + identifier + '"', html)
        self.assertIn('request !== statisticsRequest', script)
        self.assertNotIn('innerHTML', script)


if __name__ == '__main__':
    unittest.main()
