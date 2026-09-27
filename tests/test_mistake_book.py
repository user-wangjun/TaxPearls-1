"""E11 persistence, isolated practice, access revocation and atomicity."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from scripts.ops_db import create_backup, restore_backup
from webapp import app as module, mistake_book
from webapp.storage import Store


class MistakeBookTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'TAXPEARLS_AI_ENABLED': '0', 'TAXPEARLS_NOTIFICATION_EMAIL_ENABLED': '0'})
        self.env.start()
        self.tmp = TemporaryDirectory()
        self.old = module.store
        self.store = Store(Path(self.tmp.name) / 'mistakes.db')
        module.store = self.store
        self.password = 'Mistake-book-2026!'
        self.teacher = self.person('teacher', 'teacher')
        self.student = self.person('student', 'student')
        self.other = self.person('other-student', 'student')
        self.client = TestClient(module.app)
        self.client.__enter__()
        self.login(self.teacher)
        result = self.client.post('/api/exercises', json={'rule_id': 'R-020', 'seed': 17, 'expected_version': '2.0'})
        self.assertEqual(result.status_code, 200, result.text)
        self.audit = result.json()['audit_id']
        with self.store.connect() as db:
            findings = json.loads(db.execute('SELECT findings_json FROM audits WHERE id=?', (self.audit,)).fetchone()[0])[:3]
            for finding, status in zip(findings, ('hit', 'pass', 'skipped')):
                finding['status'] = status
            db.execute('UPDATE audits SET findings_json=? WHERE id=?', (json.dumps(findings), self.audit))
        self.hit, self.passed, self.skipped = [f['rule']['id'] for f in findings]
        self.cid = self.client.post('/api/classes', json={'name': '错题班', 'student_ids': [self.student['id']]}).json()['id']
        response = self.client.post('/api/assignments', json={'title': '<img src=x onerror=alert(1)>错题案例',
            'audit_id': self.audit, 'class_id': self.cid, 'published': True})
        self.assertEqual(response.status_code, 200, response.text)
        self.aid = response.json()['id']
        self.login(self.student)

    def tearDown(self):
        self.client.__exit__(None, None, None)
        module.store = self.old
        self.tmp.cleanup()
        self.env.stop()

    def person(self, name, role, org='mistake-school'):
        return self.store.create_user(name, self.password, name, role, org)

    def login(self, person):
        self.client.cookies.clear()
        self.client.cookies.set(module.COOKIE_NAME, self.store.authenticate(person['username'], self.password)[1])

    def submit(self, answers):
        response = self.client.post(f'/api/assignments/{self.aid}/submit', json={'selected_rule_ids': answers})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def listing(self):
        response = self.client.get('/api/training/mistakes')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers['cache-control'], 'private, no-store')
        return response.json()['cases']

    def detail(self, case=None):
        case = case or self.listing()[0]
        response = self.client.get('/api/training/mistakes/' + case['id'])
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def practice(self, answers, case=None, key='practice-request-001'):
        case = case or self.listing()[0]
        return self.client.post(f'/api/training/mistakes/{case["id"]}/practice', json={
            'revision': case['revision'], 'request_id': key, 'selected_rule_ids': answers})

    def formal_rows(self):
        with self.store.connect() as db:
            return [tuple(row) for row in db.execute('SELECT * FROM submissions ORDER BY id')]

    def test_empty_then_automatic_capture_and_skipped_separation(self):
        self.assertEqual(self.listing(), [])
        self.submit([self.passed, self.skipped])
        case = self.detail()
        self.assertEqual({e['kind'] for e in case['errors']}, {'missed', 'false_positive', 'unsupported'})
        self.assertTrue(all(e['formal_count'] == 1 and e['practice_count'] == 0 for e in case['errors']))
        self.assertEqual(case['practice_count'], 0)
        self.assertEqual(len(case['rules']), 3)
        self.assertNotIn('status', case['rules'][0])
        self.assertTrue(case['material']['accounts'])
        self.assertEqual({e['rule_id'] for e in case['review']}, {self.hit, self.passed, self.skipped})

    def test_resubmission_retains_mistakes_and_review_does_not_change_them(self):
        self.submit([])
        original = self.listing()[0]
        self.submit([self.hit])
        current = self.listing()[0]
        self.assertEqual(current['id'], original['id'])
        self.assertEqual(current['errors'], original['errors'])
        self.assertEqual(current['status'], 'pending')
        sub = self.store.get_submission(self.aid, self.student['id'])
        self.store.review_submission(sub['id'], self.teacher['id'], 77, '教师复核保留')
        self.assertEqual(self.client.post('/api/training/mistakes/sync').json()['updated'], 0)
        self.assertEqual(self.listing()[0], current)

    def test_practice_preserves_formal_grade_review_stats_and_profile(self):
        self.submit([self.passed])
        sub = self.store.get_submission(self.aid, self.student['id'])
        self.store.review_submission(sub['id'], self.teacher['id'], 88, '不可覆盖')
        formal = self.formal_rows()
        profile = self.client.get('/api/training/profile').json()
        self.login(self.teacher)
        stats = self.client.get(f'/api/classes/{self.cid}/statistics').json()
        self.login(self.student)
        result = self.practice([self.hit])
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['score'], 100)
        self.assertEqual(self.listing()[0]['status'], 'corrected')
        self.assertTrue(all(e['correct_in_latest_practice'] for e in self.listing()[0]['errors']))
        self.assertEqual(self.formal_rows(), formal)
        self.assertEqual(self.client.get('/api/training/profile').json(), profile)
        self.login(self.teacher)
        self.assertEqual(self.client.get(f'/api/classes/{self.cid}/statistics').json(), stats)

    def test_new_practice_errors_accumulate_and_formal_recurrence_resets_status(self):
        self.submit([])
        self.assertEqual(self.practice([self.hit, self.skipped]).status_code, 200)
        case = self.listing()[0]
        unsupported = next(e for e in case['errors'] if e['kind'] == 'unsupported')
        self.assertEqual((unsupported['formal_count'], unsupported['practice_count']), (0, 1))
        self.assertEqual(self.practice([self.hit], key='practice-request-002').status_code, 200)
        self.assertEqual(self.listing()[0]['status'], 'corrected')
        self.submit([])
        case = self.listing()[0]
        self.assertEqual((case['status'], case['practice_count'], case['latest_practice']), ('pending', 2, None))
        self.assertEqual(next(e for e in case['errors'] if e['kind'] == 'missed')['formal_count'], 2)

    def test_explicit_legacy_import_is_idempotent_not_a_get_side_effect(self):
        with patch('webapp.mistake_book.capture', return_value='unchanged'):
            self.submit([self.passed])
        formal = self.formal_rows()
        self.assertEqual(self.listing(), [])
        first = self.client.post('/api/training/mistakes/sync')
        self.assertEqual(first.json()['added'], 1)
        case = self.listing()[0]
        second = self.client.post('/api/training/mistakes/sync')
        self.assertEqual(second.json(), {'added': 0, 'updated': 0, 'unchanged': 1, 'invalid': 0})
        self.assertEqual(self.listing()[0], case)
        self.assertEqual(self.formal_rows(), formal)

    def test_corrupt_legacy_rows_are_unavailable_not_mistakes(self):
        with patch('webapp.mistake_book.capture', return_value='unchanged'):
            self.submit([])
        for column, value in [('answers_json', 'broken'), ('answers_json', '["unknown-rule"]'), ('submitted_at', 'unknown')]:
            with self.store.connect() as db:
                db.execute(f'UPDATE submissions SET {column}=? WHERE assignment_id=?', (value, self.aid))
            self.assertEqual(self.client.post('/api/training/mistakes/sync').json()['invalid'], 1)
            self.assertEqual(self.listing(), [])

    def test_authentication_role_self_org_and_target_boundaries(self):
        self.submit([])
        case = self.listing()[0]
        for person in (self.other, self.person('foreign', 'student', 'foreign-school')):
            self.login(person)
            self.assertEqual(self.listing(), [])
            self.assertEqual(self.client.get('/api/training/mistakes/' + case['id']).status_code, 404)
            self.assertEqual(self.practice([], case).status_code, 404)
        for person in (self.teacher, self.person('admin', 'org_admin')):
            self.login(person)
            for path in ('/api/training/mistakes', '/api/training/mistakes/' + case['id']):
                self.assertEqual(self.client.get(path).status_code, 403)
            self.assertEqual(self.client.post('/api/training/mistakes/sync').status_code, 403)
            self.assertEqual(self.practice([], case).status_code, 403)
        self.client.cookies.clear()
        self.assertEqual(self.client.get('/api/training/mistakes').status_code, 401)
        self.login(self.student)
        with self.store.connect() as db:
            db.execute('UPDATE assignments SET target_student_id=? WHERE id=?', (self.other['id'], self.aid))
        self.assertEqual(self.listing(), [])
        self.assertEqual(self.practice([], case).status_code, 404)

    def test_revocation_hides_read_and_write_but_restoration_preserves_history(self):
        self.submit([])
        case = self.listing()[0]
        for revoke, restore, args in [
            ('UPDATE assignments SET published=0 WHERE id=?', 'UPDATE assignments SET published=1 WHERE id=?', (self.aid,)),
            ('DELETE FROM training_class_members WHERE class_id=? AND student_id=?', 'INSERT INTO training_class_members VALUES (?,?)', (self.cid, self.student['id']))]:
            with self.store.connect() as db:
                db.execute(revoke, args)
            self.assertEqual(self.listing(), [])
            self.assertEqual(self.client.get('/api/training/mistakes/' + case['id']).status_code, 404)
            self.assertEqual(self.practice([], case).status_code, 404)
            with self.store.connect() as db:
                db.execute(restore, args)
            self.assertEqual(self.listing()[0], case)

    def test_deadline_blocks_formal_submission_but_not_authorized_practice(self):
        self.submit([])
        with self.store.connect() as db:
            db.execute("UPDATE training_assignment_settings SET deadline_at='2000-01-01T00:00:00+00:00' WHERE assignment_id=?", (self.aid,))
        self.assertEqual(self.client.post(f'/api/assignments/{self.aid}/submit', json={'selected_rule_ids': [self.hit]}).status_code, 409)
        self.assertEqual(self.practice([self.hit]).status_code, 200)

    def test_material_or_grading_change_refuses_old_practice_without_leaking_answers(self):
        self.submit([])
        case = self.listing()[0]
        with self.store.connect() as db:
            db.execute('UPDATE assignments SET false_positive_penalty=9 WHERE id=?', (self.aid,))
        self.assertEqual(self.listing()[0]['status'], 'unavailable')
        self.assertEqual(self.listing()[0]['errors'], [])
        self.assertEqual(self.client.get('/api/training/mistakes/' + case['id']).status_code, 409)
        self.assertEqual(self.practice([self.hit], case).status_code, 409)
        self.assertEqual(self.client.post('/api/training/mistakes/sync').json()['invalid'], 1)

    def test_damaged_mistake_record_is_not_silently_repaired_or_scored(self):
        self.submit([])
        case = self.listing()[0]
        with self.store.connect() as db:
            db.execute("UPDATE training_mistake_cases SET errors_json='[]' WHERE id=?", (case['id'],))
        self.assertFalse(self.listing()[0]['available'])
        self.assertEqual(self.practice([self.hit], case).status_code, 409)
        self.assertEqual(self.client.get('/api/training/mistakes/' + case['id']).status_code, 409)
        self.assertEqual(self.client.post('/api/training/mistakes/sync').json()['invalid'], 1)
        with self.store.connect() as db:
            db.execute("UPDATE training_mistake_cases SET errors_json='{}' WHERE id=?", (case['id'],))
        self.submit([])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT errors_json FROM training_mistake_cases WHERE id=?', (case['id'],)).fetchone()[0], '{}')
        self.assertFalse(self.listing()[0]['available'])

    def test_read_permission_snapshot_and_next_request_revocation(self):
        self.submit([])
        case = self.listing()[0]
        original = mistake_book.checked_snapshot
        def revoke_after_authorization(db, item, record):
            with self.store.connect() as concurrent:
                concurrent.execute('DELETE FROM training_class_members WHERE class_id=?', (self.cid,))
            return original(db, item, record)
        with patch('webapp.mistake_book.checked_snapshot', side_effect=revoke_after_authorization):
            self.assertEqual(self.client.get('/api/training/mistakes/' + case['id']).status_code, 200)
        self.assertEqual(self.client.get('/api/training/mistakes/' + case['id']).status_code, 404)
        self.assertEqual(self.practice([], case).status_code, 404)

    def test_current_engine_is_not_used_and_replay_still_checks_current_access(self):
        self.submit([])
        case = self.listing()[0]
        with patch('webapp.app._audit_rules', side_effect=AssertionError('must use frozen findings')):
            self.assertEqual(self.practice([self.hit], case).status_code, 200)
        with self.store.connect() as db:
            db.execute('UPDATE assignments SET published=0 WHERE id=?', (self.aid,))
        self.assertEqual(self.practice([self.hit], case).status_code, 404)
        with self.store.connect() as db:
            db.execute('UPDATE assignments SET published=1 WHERE id=?', (self.aid,))
            db.execute('UPDATE users SET active=0 WHERE id=?', (self.student['id'],))
        self.assertIn(self.practice([self.hit], case).status_code, (401, 404))

    def test_ui_assets_and_stale_response_guards(self):
        html = self.client.get('/').text
        script = self.client.get('/mistake-book.js')
        self.assertEqual(script.status_code, 200)
        for identifier in ('mistakeBook', 'mistakeState', 'mistakeKind', 'mistakeDetail', 'btnSyncMistakes'):
            self.assertIn('id="' + identifier + '"', html)
        self.assertIn('version === request', script.text)
        self.assertIn('uid === getUser()?.id', script.text)
        self.assertIn('training-stats-scroll', script.text)
        self.assertNotIn('innerHTML', script.text)

    def test_practice_idempotency_revision_validation_and_concurrency(self):
        self.submit([])
        case = self.listing()[0]
        first = self.practice([self.hit], case)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(self.practice([self.hit], case).json(), first.json())
        self.assertEqual(self.practice([], case).status_code, 409)
        self.assertEqual(self.practice([], case, key='practice-request-002').status_code, 409)
        self.assertEqual(self.listing()[0]['practice_count'], 1)
        case = self.listing()[0]
        self.assertEqual(self.practice([self.hit, self.hit], case).status_code, 422)
        self.assertEqual(self.practice(['nonexistent'], case).status_code, 422)
        cookies = dict(self.client.cookies)
        def send(key):
            with TestClient(module.app) as client:
                client.cookies.update(cookies)
                return client.post(f'/api/training/mistakes/{case["id"]}/practice', json={
                    'revision': case['revision'], 'request_id': key, 'selected_rule_ids': []}).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(send, ['parallel-request-001', 'parallel-request-002'])), [200, 409])
        self.assertEqual(self.listing()[0]['practice_count'], 2)

    def test_capture_and_practice_storage_failures_roll_back_atomically(self):
        with patch('webapp.mistake_book.capture', side_effect=RuntimeError('test write failure')):
            with self.assertRaisesRegex(RuntimeError, 'test write failure'):
                self.client.post(f'/api/assignments/{self.aid}/submit', json={'selected_rule_ids': []})
        self.assertEqual(self.formal_rows(), [])
        self.submit([])
        case = self.listing()[0]
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER fail_practice BEFORE UPDATE OF latest_attempt_id ON training_mistake_cases BEGIN SELECT RAISE(ABORT, 'test rollback'); END")
        with self.assertRaisesRegex(Exception, 'test rollback'):
            self.practice([self.hit], case)
        self.assertEqual(self.listing()[0], case)

    def test_reopen_backup_original_session_and_additive_migration(self):
        self.submit([self.passed])
        self.assertEqual(self.practice([self.hit]).status_code, 200)
        before, formal, cookies = self.detail(), self.formal_rows(), dict(self.client.cookies)
        backup, restored = Path(self.tmp.name) / 'backup.db', Path(self.tmp.name) / 'restored.db'
        create_backup(self.store.path, backup)
        restore_backup(backup, restored)
        for path in (self.store.path, restored):
            module.store = Store(path)
            self.client.cookies.clear()
            self.client.cookies.update(cookies)
            self.assertEqual(self.detail(), before)
            with module.store.connect() as db:
                self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM submissions ORDER BY id')], formal)
        module.store = self.store
        with self.store.connect() as db:
            db.execute('DROP TABLE training_practice_attempts')
            db.execute('DROP TABLE training_mistake_cases')
        module.store = Store(self.store.path)
        self.assertEqual(self.listing(), [])
        self.assertEqual(self.formal_rows(), formal)
        self.assertEqual(self.client.post('/api/training/mistakes/sync').json()['added'], 1)


if __name__ == '__main__':
    unittest.main()
