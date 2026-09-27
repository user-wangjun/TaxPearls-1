"""E10: cross-assignment observations, not invented retake history or diagnoses."""
from copy import deepcopy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from scripts.ops_db import create_backup, restore_backup
from webapp import app as module, training_profiles
from webapp.storage import Store


class TrainingProfileTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'TAXPEARLS_AI_ENABLED': '0', 'TAXPEARLS_NOTIFICATION_EMAIL_ENABLED': '0'})
        self.env.start()
        self.tmp = TemporaryDirectory()
        self.old = module.store
        self.store = Store(Path(self.tmp.name) / 'profiles.db')
        module.store = self.store
        self.password = 'Training-profile-2026!'
        self.teacher = self.person('profile-teacher', 'teacher')
        self.student = self.person('profile-student', 'student')
        self.other = self.person('other-student', 'student')
        self.client = TestClient(module.app)
        self.client.__enter__()
        self.login(self.teacher)
        response = self.client.post('/api/exercises', json={'rule_id': 'R-020', 'seed': 17, 'expected_version': '2.0'})
        self.assertEqual(response.status_code, 200, response.text)
        self.audit = response.json()['audit_id']
        with self.store.connect() as db:
            self.findings = json.loads(db.execute('SELECT findings_json FROM audits WHERE id=?', (self.audit,)).fetchone()[0])[:3]
            for finding, status in zip(self.findings, ('hit', 'pass', 'skipped')):
                finding['status'] = status
            # Controlled category: exercise both miss and false-positive rates
            # in one bucket, independently of production rule classification.
            self.findings[1]['rule']['category'] = self.findings[0]['rule']['category']
            db.execute('UPDATE audits SET findings_json=? WHERE id=?', (json.dumps(self.findings), self.audit))
        self.hit, self.passed, self.skipped = [f['rule']['id'] for f in self.findings]
        self.category = self.findings[0]['rule']['category']
        self.cid = self.client.post('/api/classes', json={'name': '能力画像班', 'student_ids': [self.student['id'], self.other['id']]}).json()['id']

    def tearDown(self):
        self.client.__exit__(None, None, None)
        module.store = self.old
        self.tmp.cleanup()
        self.env.stop()

    def person(self, name, role, org='profile-school'):
        return self.store.create_user(name, self.password, name, role, org)

    def login(self, person):
        self.client.cookies.clear()
        self.client.cookies.set(module.COOKIE_NAME, self.store.authenticate(person['username'], self.password)[1])

    def assignment(self, **fields):
        self.login(self.teacher)
        body = {'title': '跨次作业', 'audit_id': self.audit, 'class_id': self.cid, 'published': True}
        body.update(fields)
        response = self.client.post('/api/assignments', json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['id']

    def submit(self, aid, answers, stamp='2026-01-01T00:00:00+00:00', person=None):
        self.login(person or self.student)
        with patch('webapp.storage._now', return_value=stamp):
            response = self.client.post('/api/assignments/' + aid + '/submit', json={'selected_rule_ids': answers})
        self.assertEqual(response.status_code, 200, response.text)

    def lesson(self, answers, stamp='2026-01-01T00:00:00+00:00', **fields):
        aid = self.assignment(**fields)
        self.submit(aid, answers, stamp)
        return aid

    def profile(self, teacher=False, **params):
        self.login(self.teacher if teacher else self.student)
        url = f'/api/classes/{self.cid}/students/{self.student["id"]}/profile' if teacher else '/api/training/profile'
        response = self.client.get(url, params=params)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers['cache-control'], 'private, no-store')
        return response.json()

    def clone_audit(self, findings):
        with self.store.connect() as db:
            row = dict(db.execute('SELECT * FROM audits WHERE id=?', (self.audit,)).fetchone())
            row['id'] = 'profile-cloned-audit'
            row['findings_json'] = json.dumps(findings)
            db.execute(f"INSERT INTO audits ({','.join(row)}) VALUES ({','.join('?' for _ in row)})", list(row.values()))
        return row['id']

    def category_point(self, result, index):
        return next(c for c in result['timeline'][index]['categories'] if c['category'] == self.category)

    def test_cross_assignment_rates_recurring_weaknesses_and_same_basis_change(self):
        self.lesson([self.passed])
        self.lesson([self.hit], '2026-01-02T00:00:00+00:00')
        self.lesson([self.passed], '2026-01-03T00:00:00+00:00')
        result = self.profile()
        self.assertEqual((result['totals']['valid'], result['totals']['mean_score']), (3, 33.33))
        self.assertEqual((result['totals']['unique_cases'], result['totals']['repeated_cases']), (1, 2))
        category = next(c for c in result['categories'] if c['category'] == self.category)
        self.assertEqual((category['missed_cases'], category['false_positive_cases'], category['case_count']), (2, 2, 3))
        self.assertEqual((category['miss_rate'], category['false_positive_rate']), (66.67, 66.67))
        first, second, third = [self.category_point(result, i) for i in range(3)]
        self.assertEqual((first['comparison'], second['comparison'], third['comparison']), ('first', 'same_basis', 'same_basis'))
        self.assertEqual((second['miss_rate_change'], second['false_positive_rate_change']), (-100, -100))
        self.assertEqual((third['miss_rate_change'], third['false_positive_rate_change']), (100, 100))

    def test_retake_replaces_latest_point_and_adjusted_grade_not_rates(self):
        aid = self.lesson([])
        sub = self.store.get_submission(aid, self.student['id'])
        self.store.review_submission(sub['id'], self.teacher['id'], 90, '人工鼓励分')
        result = self.profile()
        self.assertEqual((result['totals']['mean_score'], result['totals']['automatic_mean_score']), (90, 0))
        self.assertEqual(self.category_point(result, 0)['miss_rate'], 100)
        self.submit(aid, [self.hit], '2026-02-01T00:00:00+00:00')
        result = self.profile()
        self.assertEqual((result['totals']['submitted'], len(result['timeline']), result['totals']['adjusted']), (1, 1, 0))
        self.assertEqual(result['timeline'][0]['submitted_at'], '2026-02-01T00:00:00+00:00')
        self.assertEqual(self.category_point(result, 0)['miss_rate'], 0)
        self.assertEqual(self.category_point(result, 0)['comparison'], 'first')

    def test_percentage_point_changes_use_exact_counts_before_rounding(self):
        for day, misses in ((1, 1), (2, 2)):
            for index in range(6):
                self.lesson([] if index < misses else [self.hit], f'2026-01-{day:02}T00:00:00+00:00')
        result = self.profile()
        first, second = self.category_point(result, 0), self.category_point(result, 1)
        self.assertEqual((first['miss_rate'], second['miss_rate']), (16.67, 33.33))
        self.assertEqual(second['comparison'], 'same_basis')
        self.assertEqual(second['miss_rate_change'], 16.67)  # Not 33.33 - 16.67 = 16.66.

    def test_equal_instants_normalized_and_grouped_without_arbitrary_order(self):
        self.lesson([])
        self.lesson([self.hit], '2026-01-01T08:00:00+08:00')
        self.lesson([self.hit], '2026-01-02T00:00:00+00:00')
        result = self.profile()
        self.assertEqual(len(result['timeline']), 2)
        self.assertEqual(len(result['timeline'][0]['records']), 2)
        self.assertEqual(self.category_point(result, 0)['miss_rate'], 50)
        # Different opportunity counts do not get silently connected.
        self.assertEqual(self.category_point(result, 1)['comparison'], 'changed_basis')
        self.assertIsNone(self.category_point(result, 1)['miss_rate_change'])

    def test_rule_version_and_threshold_change_break_comparison_not_regrade(self):
        self.lesson([])
        changed = deepcopy(self.findings)
        changed[0]['rule']['version'] = '3.0'
        changed[1]['threshold_desc'] += '（新版冻结阈值）'
        new_audit = self.clone_audit(changed)
        self.lesson([self.hit], '2026-01-02T00:00:00+00:00', audit_id=new_audit)
        with patch('src.engine.evaluate', side_effect=AssertionError('must not evaluate current rules')):
            with patch('src.training.score_submission', side_effect=AssertionError('must not regrade')):
                result = self.profile()
        self.assertEqual(len(result['rules']), 5)
        self.assertEqual(self.category_point(result, 1)['comparison'], 'changed_basis')
        self.assertEqual(result['totals']['unique_cases'], 2)

    def test_risk_status_change_with_same_rule_breaks_comparison(self):
        self.lesson([])
        changed = deepcopy(self.findings)
        changed[0]['status'], changed[1]['status'] = 'pass', 'hit'
        new_audit = self.clone_audit(changed)
        self.lesson([self.passed], '2026-01-02T00:00:00+00:00', audit_id=new_audit)
        self.assertEqual(self.category_point(self.profile(), 1)['comparison'], 'changed_basis')

    def test_skipped_is_not_correct_exclusion_and_denominators_can_be_empty(self):
        self.lesson([self.hit, self.skipped])
        result = self.profile()
        rule = next(r for r in result['rules'] if r['rule_id'] == self.skipped)
        self.assertEqual((rule['skipped'], rule['unsupported'], rule['executed_decisions']), (1, 1, 0))
        self.assertIsNone(rule['miss_rate'])
        self.assertIsNone(rule['false_positive_rate'])
        self.assertIsNone(rule['accuracy'])

    def test_empty_and_unsubmitted_profile_does_not_expose_answer_key(self):
        result = self.profile()
        self.assertEqual(result['totals']['assignments'], 0)
        self.assertIsNone(result['totals']['mean_score'])
        self.assignment()
        result = self.profile()
        self.assertEqual((result['totals']['assignments'], result['totals']['unsubmitted']), (1, 1))
        for key in ('rules', 'categories', 'records', 'timeline'):
            self.assertEqual(result[key], [])
        self.assertNotIn(self.hit, json.dumps(result))

    def test_student_only_self_and_teacher_only_owned_class_current_member(self):
        self.lesson([self.hit])
        result = self.profile(student_id=self.other['id'])
        self.assertEqual(result['student']['id'], self.student['id'])
        self.login(self.other)
        self.assertEqual(self.client.get('/api/training/profile').json()['totals']['submitted'], 0)
        url = f'/api/classes/{self.cid}/students/{self.student["id"]}/profile'
        self.assertEqual(self.client.get(url).status_code, 403)
        for role in ('teacher', 'org_admin', 'platform_admin', 'accountant'):
            person = self.person('new-' + role, role)
            self.login(person)
            self.assertEqual(self.client.get('/api/training/profile').status_code, 403)
            self.assertEqual(self.client.get(url).status_code, 404 if role == 'teacher' else 403)
        self.login(self.person('foreign-teacher', 'teacher', 'foreign'))
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.cookies.clear()
        self.assertEqual(self.client.get('/api/training/profile').status_code, 401)

    def test_teacher_profile_excludes_public_other_class_and_other_teacher_work(self):
        own = self.lesson([])
        self.lesson([self.hit], '2026-01-02T00:00:00+00:00', class_id=None)
        other_teacher = self.person('second-teacher', 'teacher')
        self.login(other_teacher)
        other_class = self.client.post('/api/classes', json={'name': '另一班', 'student_ids': [self.student['id']]}).json()['id']
        entry = self.store.get_audit(self.audit)
        other_audit = 'other-teacher-audit'
        self.store.save_audit(other_audit, other_teacher, None, entry['dataset'], entry['findings'], entry['summary'], entry['audited_at'])
        aid = self.store.create_assignment(other_teacher, '另一教师作业', other_audit, None, {}, 5, True, other_class)
        self.submit(aid, [self.hit], '2026-01-03T00:00:00+00:00')
        result = self.profile(teacher=True)
        self.assertEqual([r['id'] for r in result['records']], [own])
        self.assertEqual(self.profile()['totals']['submitted'], 3)

    def test_withdrawal_membership_revocation_and_inactive_account(self):
        aid = self.lesson([])
        self.login(self.teacher)
        self.assertEqual(self.client.put('/api/assignments/' + aid + '/settings', json={'revision': 1, 'published': False, 'deadline_at': None}).status_code, 200)
        self.assertEqual(self.profile(include_withdrawn=True)['records'], [])
        self.assertEqual(self.profile(teacher=True)['records'], [])
        self.assertEqual(len(self.profile(teacher=True, include_withdrawn=True)['records']), 1)
        url = f'/api/classes/{self.cid}/students/{self.student["id"]}/profile'
        self.login(self.teacher)
        with self.store.connect() as db:
            db.execute('UPDATE users SET active=0 WHERE id=?', (self.student['id'],))
        self.assertEqual(self.client.get(url).status_code, 404)
        with self.store.connect() as db:
            db.execute('UPDATE users SET active=1 WHERE id=?', (self.student['id'],))
            db.execute('UPDATE assignments SET published=1 WHERE id=?', (aid,))
            db.execute('DELETE FROM training_class_members WHERE student_id=?', (self.student['id'],))
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.profile()['records'], [])
        self.assertIsNotNone(self.store.get_submission(aid, self.student['id']))

    def test_target_student_filter_and_zero_submission_teacher_draft(self):
        self.assignment(target_student_id=self.other['id'])
        self.assignment(published=False)
        self.lesson([self.hit], target_student_id=self.student['id'])
        result = self.profile()
        self.assertEqual((result['totals']['assignments'], result['totals']['submitted']), (1, 1))
        self.assertEqual(self.profile(teacher=True, include_withdrawn=True)['totals']['assignments'], 1)

    def test_invalid_snapshot_answers_and_time_are_not_success_and_break_lines(self):
        self.lesson([self.hit])
        bad = self.lesson([self.hit], '2026-01-02T00:00:00+00:00')
        self.lesson([], '2026-01-03T00:00:00+00:00')
        with self.store.connect() as db:
            db.execute("UPDATE submissions SET answers_json='[1]' WHERE assignment_id=?", (bad,))
        result = self.profile()
        self.assertEqual((result['totals']['valid'], result['totals']['invalid']), (2, 1))
        self.assertEqual(result['timeline'][1]['invalid'], 1)
        self.assertEqual(self.category_point(result, 2)['comparison'], 'incomplete')
        for stamp in ('not-a-time', '2026-01-02T00:00:00'):
            with self.store.connect() as db:
                db.execute('UPDATE submissions SET submitted_at=? WHERE assignment_id=?', (stamp, bad))
            result = self.profile()
            self.assertEqual(result['totals']['undated'], 1)
            self.assertEqual(self.category_point(result, 1)['comparison'], 'incomplete')
        with self.store.connect() as db:
            db.execute("UPDATE audits SET org_id='foreign' WHERE id=?", (self.audit,))
        result = self.profile()
        self.assertEqual(result['rules'], [])
        self.assertEqual(result['totals']['invalid'], 3)

    def test_nonfinite_score_and_unknown_answer_excluded(self):
        aid = self.lesson([self.hit])
        for sql, value in [('score', float('inf')), ('answers_json', '["unknown-rule"]')]:
            with self.store.connect() as db:
                db.execute('UPDATE submissions SET score=100,answers_json=? WHERE assignment_id=?', (json.dumps([self.hit]), aid))
                db.execute('UPDATE submissions SET ' + sql + '=? WHERE assignment_id=?', (value, aid))
            result = self.profile()
            self.assertEqual((result['totals']['submitted'], result['totals']['invalid'], result['totals']['unsubmitted']), (1, 1, 0))
            self.assertIsNone(result['totals']['mean_score'])

    def test_consistent_snapshot_while_membership_is_removed(self):
        self.lesson([self.hit])
        self.login(self.teacher)
        original = training_profiles.collect
        def concurrent(db, student, classroom, include_withdrawn):
            with self.store.connect() as writer:
                writer.execute('DELETE FROM training_class_members WHERE student_id=?', (self.student['id'],))
            return original(db, student, classroom, include_withdrawn)
        url = f'/api/classes/{self.cid}/students/{self.student["id"]}/profile'
        with patch.object(training_profiles, 'collect', side_effect=concurrent):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['totals']['valid'], 1)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_category_gap_is_not_interpolated(self):
        self.lesson([])
        separate = [deepcopy(self.findings[2])]
        separate[0]['status'] = 'hit'
        aid = self.clone_audit(separate)
        self.lesson([self.skipped], '2026-01-02T00:00:00+00:00', audit_id=aid)
        self.lesson([self.hit], '2026-01-03T00:00:00+00:00')
        self.assertEqual(self.category_point(self.profile(), 2)['comparison'], 'gap')

    def test_read_only_schema_and_backup_reopen_original_session(self):
        self.lesson([])
        self.lesson([self.hit], '2026-01-02T00:00:00+00:00')
        before = self.profile()
        cookies = dict(self.client.cookies)
        with self.store.connect() as db:
            schema = list(map(tuple, db.execute('SELECT * FROM sqlite_master ORDER BY name')))
            submissions = list(map(tuple, db.execute('SELECT * FROM submissions ORDER BY id')))
        backup, restored = Path(self.tmp.name) / 'backup.db', Path(self.tmp.name) / 'restored.db'
        create_backup(self.store.path, backup)
        restore_backup(backup, restored)
        for path in (self.store.path, restored):
            module.store = Store(path)
            self.client.cookies.clear()
            self.client.cookies.update(cookies)
            self.assertEqual(self.client.get('/api/training/profile').json(), before)
            with module.store.connect() as db:
                self.assertEqual(list(map(tuple, db.execute('SELECT * FROM sqlite_master ORDER BY name'))), schema)
                self.assertEqual(list(map(tuple, db.execute('SELECT * FROM submissions ORDER BY id'))), submissions)
        html = self.client.get('/').text
        script = self.client.get('/classroom.js').text
        for identifier in ('studentProfile', 'profileClass', 'profileStudent', 'profileWithdrawn', 'studentProfileResult'):
            self.assertIn('id="' + identifier + '"', html)
        self.assertIn('request !== profileRequest', script)
        self.assertIn('entry.comparison === "same_basis"', script)
        self.assertNotIn('innerHTML', script)


if __name__ == '__main__':
    unittest.main()
