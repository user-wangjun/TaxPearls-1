"""F07: business tenant boundaries, delegated clients and transient result scopes."""
import os
import base64
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

import httpx2 as httpx
from fastapi.testclient import TestClient

from src import loader
from src.settings import AISettings
from webapp import app as module
from webapp.access import AccessDenied
from webapp.storage import Store
from tests.enterprise_support import audit as enterprise_audit, confirm, material_key


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / 'samples' / '样例企业-审计材料.xlsx'


@material_key
class TenantIsolationTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'TAXPEARLS_AI_ENABLED': '0', 'TAXPEARLS_NOTIFICATION_EMAIL_ENABLED': '0'})
        self.env.start()
        self.tmp = TemporaryDirectory()
        self.old = module.store
        self.store = Store(Path(self.tmp.name) / 'tenant.db')
        module.store = self.store
        self.password = 'Tenant-isolation-2026!'
        self.admin = self.person('alpha-admin', 'org_admin', 'alpha')
        self.accountant = self.person('alpha-accountant', 'accountant', 'alpha')
        self.other = self.person('alpha-other-accountant', 'accountant', 'alpha')
        self.foreign = self.person('beta-admin', 'org_admin', 'beta')
        self.root = self.person('platform', 'platform_admin', 'platform')
        self.data = loader.load(SAMPLE)
        self.customer = self.store.upsert_client(self.admin, self.data.company.name, self.data.company.taxpayer_id, self.accountant['id'])
        self.audit = module._save_audit(self.data, self.admin, self.customer['id'])['audit_id']
        self.client = TestClient(module.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        module.store = self.old
        self.tmp.cleanup()
        self.env.stop()

    def person(self, name, role, org):
        return self.store.create_user(name, self.password, name, role, org)

    def test_stale_identity_cannot_list_users_invites_or_class_candidates(self):
        teacher = self.person('list-teacher', 'teacher', 'alpha')
        for actor, urls in ((self.admin, ['/api/users']), (self.root, ['/api/users', '/api/invites']),
                            (teacher, ['/api/classes/students'])):
            with self.store.connect() as db:
                db.execute('UPDATE users SET active=0 WHERE id=?', (actor['id'],))
            with patch.object(module, '_user', return_value=actor), patch.object(self.store, 'user_for_token', return_value=actor):
                for url in urls:
                    with self.subTest(role=actor['role'], url=url):
                        self.assertEqual(self.client.get(url).status_code, 403)

    def login(self, person):
        self.client.cookies.clear()
        self.client.cookies.set(module.COOKIE_NAME, self.store.authenticate(person['username'], self.password)[1])

    def upload(self, **fields):
        return enterprise_audit(self.client, data=fields, files={'file': ('sample.xlsx', SAMPLE.read_bytes())})

    def preview(self):
        response = self.client.post('/api/enterprise/materials', data={'client_id': self.customer['id']},
                                    files={'files': ('sample.xlsx', SAMPLE.read_bytes())})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_five_roles_existing_entry_matrix_and_scoped_audit_reads(self):
        teacher=self.person('matrix-teacher','teacher','alpha')
        student=self.person('matrix-student','student','alpha')
        teaching=module._save_audit(self.data,teacher)['audit_id']
        aid=self.store.create_assignment(teacher,'角色矩阵作业',teaching,None,{},5,True)
        self.store.log(teacher,'create_assignment','assignment',aid)
        roles=[self.root,self.admin,self.accountant,teacher,student]
        entries={
            '/api/dashboard':{'org_admin','accountant','teacher'},
            '/api/clients':{'org_admin','accountant'},
            '/api/users':{'platform_admin','org_admin'},
            '/api/audit-log':{'platform_admin','org_admin','teacher'},
            '/api/assignments':{'teacher','student'},
            '/api/classes':{'teacher','student'},
            '/api/papers':{'teacher','student'},
            '/api/submissions':{'teacher'},
            '/api/notifications':{'org_admin','accountant','teacher'},
            '/api/rules/R-001/versions':{'platform_admin'},
        }
        for actor in roles:
            self.login(actor)
            for path,allowed in entries.items():
                with self.subTest(role=actor['role'],path=path):
                    self.assertEqual(self.client.get(path).status_code,200 if actor['role'] in allowed else 403)
            for audit_id,allowed in [(self.audit,{'org_admin','accountant'}),(teaching,{'org_admin','teacher'})]:
                for path in [f'/api/audits/{audit_id}',f'/api/report/{audit_id}/html']:
                    expected=200 if actor['role'] in allowed else (403 if actor['role'] in {'platform_admin','student'} else 404)
                    self.assertEqual(self.client.get(path).status_code,expected,(actor['role'],path))
            self.assertEqual(self.client.put('/api/rules/R-001/state',json={'enabled':True}).status_code,
                             200 if actor['role']=='platform_admin' else 403)
            self.assertEqual(self.client.post('/api/assignments',json={'title':'发布权限','audit_id':teaching}).status_code,
                             200 if actor['role']=='teacher' else 403)
        self.login(student)
        self.assertEqual(self.client.get('/api/assignments/'+aid).status_code,200)
        self.assertEqual(self.client.post('/api/assignments/'+aid+'/submit',json={'selected_rule_ids':[]}).status_code,200)
        self.login(self.accountant)
        self.assertEqual(self.client.get('/api/report/'+self.audit).status_code,409)
        self.login(teacher)
        review=self.client.get('/api/submissions').json()[0]
        for actor in roles:
            self.login(actor)
            self.assertEqual(self.client.put('/api/submissions/'+review['id']+'/review',json={'adjusted_score':75,'feedback':'矩阵验证'}).status_code,
                             200 if actor['role']=='teacher' else 403)
        # New invitation/member endpoints are intentionally covered with G10/G13,
        # not counted as implemented merely because existing role checks pass.

    def test_platform_cannot_bypass_business_org_by_audit_id(self):
        self.login(self.root)
        self.assertEqual(self.client.get('/api/audits').status_code, 403)
        self.assertEqual(self.client.get('/api/audits/' + self.audit).status_code, 403)

    def test_implicit_upload_cannot_take_over_another_accountants_customer(self):
        self.login(self.other)
        response = self.upload()
        self.assertIn(response.status_code, (403, 404))
        self.assertEqual(self.store.get_client(self.customer['id'])['accountant_id'], self.accountant['id'])
        self.assertEqual(self.client.get('/api/audits').json(), [])

    def test_material_result_replay_rechecks_current_client_assignment(self):
        self.login(self.accountant)
        body = self.preview()
        committed = confirm(self.client, body)
        self.assertEqual(committed.status_code, 200, committed.text)
        self.assertIn('audit_id', committed.json())
        self.store.upsert_client(self.admin, self.customer['name'], self.customer['taxpayer_id'], self.other['id'])
        replay = confirm(self.client, body)
        self.assertIn(replay.status_code, (403, 404))

    def test_material_preview_cannot_follow_account_into_different_org(self):
        self.login(self.accountant)
        body = self.preview()
        with self.store.connect() as db:
            db.execute("UPDATE users SET org_id='beta' WHERE id=?", (self.accountant['id'],))
        response = confirm(self.client, body)
        self.assertIn(response.status_code, (403, 404, 422))
        self.assertEqual(self.client.get('/api/audits').json(), [])

    def test_business_route_matrix_denies_foreign_ids_before_processing(self):
        entry = self.store.get_audit(self.audit)
        hit = next(f.rule.id for f in entry['findings'] if f.hit)
        routes = [
            ('GET', '/api/audits/' + self.audit, None),
            ('GET', f'/api/audits/{self.audit}/changes', None),
            ('GET', f'/api/audits/{self.audit}/report-versions', None),
            ('POST', f'/api/audits/{self.audit}/report-versions', None),
            ('GET', f'/api/report/{self.audit}/html', None),
            ('GET', f'/api/report/{self.audit}?confirm=true', None),
            ('GET', f'/api/knowledge/graph?audit_id={self.audit}', None),
            ('POST', '/api/knowledge/ask', {'audit_id': self.audit, 'node_id': 'company', 'question': '解释证据'}),
            ('POST', f'/api/audits/{self.audit}/findings/{hit}/interpretation', None),
            ('POST', f'/api/audits/{self.audit}/narrative', None),
        ]
        with patch.object(module, 'ask_graph') as ask, patch.object(module, 'interpret_finding') as interpret, \
                patch.object(module, 'generate_audit_narrative') as narrative:
            for actor in (self.foreign, self.root, self.other):
                self.login(actor)
                for method, url, body in routes:
                    with self.subTest(actor=actor['username'], url=url):
                        response = self.client.request(method, url, json=body)
                        self.assertEqual(response.status_code, 403 if actor == self.root else 404, url)
                        self.assertNotIn(self.data.company.taxpayer_id, response.text)
                        self.assertIn('no-store', response.headers['cache-control'])
                        self.assertIn('cookie', response.headers['vary'].lower())
            ask.assert_not_called(); interpret.assert_not_called(); narrative.assert_not_called()
        self.login(self.accountant)
        for method, url, body in routes:
            if method == 'GET' and not url.endswith('?confirm=true'):
                self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_same_taxpayer_is_independent_per_org_and_platform_management_remains(self):
        self.login(self.foreign)
        uploaded = self.upload()
        self.assertEqual(uploaded.status_code, 200)
        foreign_id = uploaded.json()['audit_id']
        foreign_client = self.client.get('/api/clients').json()[0]
        self.assertNotEqual(foreign_client['id'], self.customer['id'])
        self.assertEqual(foreign_client['taxpayer_id'], self.customer['taxpayer_id'])
        self.assertEqual({a['id'] for a in self.client.get('/api/audits').json()}, {foreign_id})
        self.assertEqual(self.client.get('/api/audits/' + self.audit).status_code, 404)
        self.login(self.admin)
        self.assertEqual({a['id'] for a in self.client.get('/api/audits').json()}, {self.audit})
        self.assertEqual(self.client.get('/api/audits/' + foreign_id).status_code, 404)
        self.login(self.root)
        self.assertEqual(self.client.get('/api/audits').status_code, 403)
        self.assertEqual(self.client.get('/api/clients').status_code, 403)
        self.assertIn(self.foreign['id'], {u['id'] for u in self.client.get('/api/users').json()})
        self.assertEqual(self.client.get('/api/audit-log').status_code, 200)

    def test_accountant_owned_upload_works_but_foreign_assignment_is_rejected(self):
        self.login(self.accountant)
        self.assertEqual(self.upload().status_code, 200)
        self.login(self.admin)
        foreign_accountant = self.person('beta-accountant', 'accountant', 'beta')
        body = {'name': self.customer['name'], 'taxpayer_id': self.customer['taxpayer_id'],
                'accountant_id': foreign_accountant['id']}
        self.assertEqual(self.client.post('/api/clients', json=body).status_code, 422)
        self.assertEqual(self.store.get_client(self.customer['id'])['accountant_id'], self.accountant['id'])

    def test_confirmation_repeat_is_idempotent_and_rechecks_current_role(self):
        self.login(self.accountant)
        body = self.preview()
        first = confirm(self.client, body)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(confirm(self.client, body).json(), first.json())
        self.assertEqual(len(self.client.get('/api/audits').json()), 2)
        with self.store.connect() as db:
            db.execute("UPDATE users SET role='org_admin' WHERE id=?", (self.accountant['id'],))
        # A current org admin still has this client's permission. A teacher
        # cannot replay the enterprise result, even with the old session.
        self.assertEqual(confirm(self.client, body).status_code, 200)
        with self.store.connect() as db:
            db.execute("UPDATE users SET role='teacher' WHERE id=?", (self.accountant['id'],))
        self.assertEqual(confirm(self.client, body).status_code, 403)

    def test_async_material_job_is_bound_to_owner_org_and_role(self):
        teacher = self.person('job-teacher', 'teacher', 'alpha')
        other_teacher = self.person('other-job-teacher', 'teacher', 'alpha')
        self.login(teacher)
        settings = AISettings(enabled=True, api_key='synthetic-test-only')
        with patch('webapp.material_upload.AISettings.from_env', return_value=settings), \
                patch('webapp.material_upload.materials.preview', return_value=[]):
            started = self.client.post('/api/materials/preview', data={'extraction': 'ai'},
                                       files={'files': ('sample.xlsx', SAMPLE.read_bytes())})
        self.assertEqual(started.status_code, 202)
        url = '/api/materials/jobs/' + started.json()['job_id']
        self.assertEqual(self.client.get(url).json()['state'], 'done')
        self.login(other_teacher)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.login(teacher)
        for field, value in [('org_id', 'beta'), ('role', 'org_admin')]:
            with self.subTest(field=field):
                with self.store.connect() as db:
                    db.execute(f'UPDATE users SET {field}=? WHERE id=?', (value, teacher['id']))
                self.assertEqual(self.client.get(url).status_code, 404 if field == 'org_id' else 403)
                with self.store.connect() as db:
                    db.execute(f'UPDATE users SET {field}=? WHERE id=?', (teacher[field], teacher['id']))

    def test_late_reassignment_prevents_atomic_audit_and_ai_cache_writes(self):
        entry = self.store.get_audit(self.audit)
        self.store.upsert_client(self.admin, self.customer['name'], self.customer['taxpayer_id'], self.other['id'])
        with self.assertRaises(AccessDenied):
            self.store.save_audit('late-write', self.accountant, self.customer['id'], self.data,
                                  entry['findings'], entry['summary'], entry['audited_at'])
        for operation in (
            lambda: self.store.save_audit_narrative(self.audit, 'hash', {'model': 'mock'}, self.accountant),
            lambda: self.store.save_finding_interpretation(self.audit, 'R-001', 'hash', {'model': 'mock'}, self.accountant),
        ):
            with self.assertRaises(AccessDenied):
                operation()
        self.assertIsNone(self.store.get_audit('late-write'))
        self.assertIsNone(self.store.get_audit_narrative(self.audit, 'hash'))
        self.assertIsNone(self.store.get_finding_interpretation(self.audit, 'R-001', 'hash'))

    def test_graph_response_discarded_if_access_revoked_during_model_call(self):
        self.login(self.accountant)
        def revoke(*args):
            self.store.upsert_client(self.admin, self.customer['name'], self.customer['taxpayer_id'], self.other['id'])
            return {'answer': 'private answer must not escape'}
        with patch.object(module, 'ask_graph', side_effect=revoke):
            response = self.client.post('/api/knowledge/ask', json={
                'audit_id': self.audit, 'node_id': 'company', 'question': '解释证据'})
        self.assertEqual(response.status_code, 404)
        self.assertNotIn('private answer', response.text)

    def test_moved_or_disabled_actor_cannot_use_stale_snapshot(self):
        for field, value in [('org_id', 'beta'), ('role', 'student'), ('active', 0)]:
            with self.subTest(field=field):
                with self.store.connect() as db:
                    db.execute(f'UPDATE users SET {field}=? WHERE id=?', (value, self.admin['id']))
                for operation in (
                    lambda: self.store.get_audit_for_user(self.audit, self.admin),
                    lambda: self.store.list_clients(self.admin),
                    lambda: self.store.org_report_sources(self.admin),
                    lambda: self.store.upsert_client(self.admin, 'must not write', 'NEW-TAX'),
                ):
                    with self.assertRaises(AccessDenied):
                        operation()
                with self.store.connect() as db:
                    db.execute(f'UPDATE users SET {field}=? WHERE id=?', (self.admin[field], self.admin['id']))

    def test_inconsistent_legacy_client_link_fails_closed_and_survives_backup(self):
        foreign_client = self.store.upsert_client(self.foreign, 'foreign name', 'BETA-TAX')
        with self.store.connect() as db:
            db.execute('UPDATE audits SET client_id=? WHERE id=?', (foreign_client['id'], self.audit))
        for store in (self.store, Store(self.store.path)):
            self.assertIsNone(store.get_audit_for_user(self.audit, self.admin))
            self.assertEqual(store.list_audits(self.admin), [])
        backup = Store(Path(self.tmp.name) / 'backup.db')
        with self.store.connect() as source, backup.connect() as target:
            source.backup(target)
        self.assertIsNone(backup.get_audit_for_user(self.audit, self.admin))
        self.assertEqual(backup.search_audits(self.admin)['items'], [])

    def test_classroom_and_submissions_do_not_expose_moved_student_identity(self):
        teacher = self.person('alpha-teacher', 'teacher', 'alpha')
        student = self.person('alpha-student', 'student', 'alpha')
        foreign_teacher = self.person('beta-teacher', 'teacher', 'beta')
        foreign_student = self.person('beta-student', 'student', 'beta')
        self.login(teacher)
        created = self.client.post('/api/classes', json={'name': 'alpha-only class', 'student_ids': [student['id']]})
        self.assertEqual(created.status_code, 200)
        class_id = created.json()['id']
        teaching = module._save_audit(self.data, teacher)['audit_id']
        assignment = self.store.create_assignment(teacher, 'alpha-only assignment', teaching, None, {}, 5, True, class_id)
        self.login(student)
        submitted = self.client.post(f'/api/assignments/{assignment}/submit', json={'selected_rule_ids': []})
        self.assertEqual(submitted.status_code, 200)
        self.login(teacher)
        self.assertEqual(len(self.client.get('/api/submissions').json()), 1)
        for actor in (foreign_student, foreign_teacher):
            self.login(actor)
            self.assertEqual(self.client.get('/api/assignments').json(), [])
            self.assertEqual(self.client.get('/api/classes').json(), [])
            self.assertEqual(self.client.get(f'/api/assignments/{assignment}').status_code, 404)
        self.assertEqual(self.client.get('/api/submissions').json(), [])
        with self.store.connect() as db:
            db.execute("UPDATE users SET org_id='beta',display_name='private beta identity' WHERE id=?", (student['id'],))
        self.login(teacher)
        self.assertEqual(self.client.get('/api/classes').json()[0]['students'], [])
        self.assertEqual(self.client.get('/api/submissions').json(), [])
        self.login(student)
        self.assertEqual(self.client.get(f'/api/assignments/{assignment}').status_code, 404)

    def test_org_archives_protection_and_trial_share_the_tenant_boundary(self):
        self.login(self.admin)
        response = self.client.post('/api/org/reports', json={'client_ids': [self.customer['id']]})
        self.assertEqual(response.status_code, 200)
        report = response.json()
        version = self.store.get_report_version(self.audit, 1)
        identifiers = [version['manifest']['protection']['id'], report['snapshot']['protection']['id']]
        for identifier in identifiers:
            self.assertEqual(self.client.get('/api/report-verification/' + identifier).status_code, 200)
        for actor in (self.foreign, self.root):
            self.login(actor)
            expected = 403 if actor == self.root else 404
            if actor == self.root:
                self.assertEqual(self.client.get('/api/org/reports').status_code, 403)
            else:
                self.assertEqual(self.client.get('/api/org/reports').json(), [])
            for suffix in ('', '/html', '/pdf'):
                self.assertEqual(self.client.get('/api/org/reports/' + report['id'] + suffix).status_code, expected)
            for identifier in identifiers:
                self.assertEqual(self.client.get('/api/report-verification/' + identifier).status_code, expected)
                self.assertEqual(self.client.post('/api/report-verification/' + identifier,
                    json={'format': 'html', 'sha256': '0' * 64}).status_code, expected)
            self.assertEqual(self.client.post('/api/rules/R-001/trial', json={
                'audit_id': self.audit, 'expected_version': '2.0', 'new_version': '2.1',
                'logic': {}, 'threshold_basis': 'synthetic test'}).status_code, expected)

    def test_platform_same_org_denied_entire_business_route_matrix_before_side_effects(self):
        # Same organization is intentional: tenant filtering alone was insufficient.
        with self.store.connect() as db:
            db.execute("UPDATE users SET org_id='alpha' WHERE id=?", (self.root['id'],))
        self.root = self.store.get_user(self.root['id'])
        self.login(self.admin)
        org_report = self.client.post('/api/org/reports', json={'client_ids':[self.customer['id']]}).json()
        identifier = self.store.get_report_version(self.audit, 1)['manifest']['protection']['id']
        self.login(self.root)
        routes = [('GET', p, None) for p in (
            '/api/dashboard', '/api/audits', '/api/archive', '/api/clients',
            '/api/audits/'+self.audit, '/api/audits/'+self.audit+'/changes',
            '/api/audits/'+self.audit+'/report-versions', '/api/report/'+self.audit+'/html',
            '/api/report/'+self.audit+'?confirm=true', '/api/knowledge/graph?audit_id='+self.audit,
            '/api/org/overview', '/api/org/reports', '/api/org/reports/'+org_report['id'],
            '/api/org/reports/'+org_report['id']+'/html', '/api/org/reports/'+org_report['id']+'/pdf',
            '/api/org/settings', '/api/org/logo', '/api/report-verification/'+identifier,
            '/api/materials/config', '/api/materials/jobs/nonexistent', '/api/notifications',
            '/api/notifications/preferences')]
        routes.extend([
            ('POST','/api/clients',{'name':'forbidden','taxpayer_id':'SYNTHETIC'}),
            ('POST','/api/org/reports',{'client_ids':[self.customer['id']]}),
            ('PUT','/api/org/settings',{'display_name':'forbidden','report_title':'forbidden','footer_text':''}),
            ('DELETE','/api/org/logo',None),
            ('POST','/api/audits/'+self.audit+'/report-versions',None),
            ('POST','/api/audits/'+self.audit+'/narrative',None),
            ('POST','/api/audits/'+self.audit+'/findings/R-001/interpretation',None),
            ('POST','/api/knowledge/ask',{'audit_id':self.audit,'node_id':'company','question':'forbidden'}),
            ('POST','/api/rules/R-001/trial',{'audit_id':self.audit,'expected_version':'2.0','new_version':'2.1','logic':{},'threshold_basis':'test'}),
            ('POST','/api/report-verification/'+identifier,{'format':'html','sha256':'0'*64}),
            ('POST','/api/materials/audit',{'token':'unused'}),
        ])
        with patch.object(module, 'ask_graph') as ask, patch.object(module, 'interpret_finding') as interpret, \
             patch.object(module, 'generate_audit_narrative') as narrative, \
             patch.object(module.render, 'export_pdf') as pdf:
            for method, url, body in routes:
                with self.subTest(method=method, url=url):
                    response = self.client.request(method, url, json=body)
                    self.assertEqual(response.status_code, 403, response.text)
                    self.assertNotIn(self.data.company.taxpayer_id, response.text)
                    self.assertIn('no-store', response.headers['cache-control'])
            self.assertEqual(self.upload().status_code, 403)
            self.assertEqual(self.client.post('/api/materials/preview', files={'files':('sample.xlsx',SAMPLE.read_bytes())}).status_code, 403)
            self.assertEqual(self.client.post('/api/org/logo', files={'file':('logo.png',b'not-an-image')}).status_code, 403)
            for spy in (ask, interpret, narrative, pdf): spy.assert_not_called()
        self.assertEqual(len(self.store.list_audits(self.admin)), 1)
        self.assertEqual(len(self.store.list_clients(self.admin)), 1)
        self.assertEqual(len(self.store.list_org_reports(self.admin)), 1)

    def test_platform_same_org_storage_guards_and_control_plane_remain(self):
        with self.store.connect() as db:
            db.execute("UPDATE users SET org_id='alpha' WHERE id=?", (self.root['id'],))
        self.root = self.store.get_user(self.root['id'])
        self.assertEqual(self.store.list_audits(self.root), [])
        self.assertEqual(self.store.search_audits(self.root)['total'], 0)
        from webapp.access import audit_row
        with self.store.connect() as db:
            self.assertIsNone(audit_row(db, self.audit, self.root))
        for action in (
            lambda: self.store.upsert_client(self.root, 'forbidden','SYNTHETIC'),
            lambda: self.store.list_clients(self.root),
            lambda: self.store.save_audit('forbidden',self.root,None,self.data,[],{},'2026-09-27'),
            lambda: self.store.archive_report(self.audit,self.root,{}),
            lambda: self.store.org_report_sources(self.root),
            lambda: self.store.list_org_reports(self.root),
            lambda: self.store.get_org_report('missing',self.root),
            lambda: self.store.save_org_report(self.root,{'org_id':'alpha'},''),
            lambda: self.store.report_protection_target('missing',self.root),
        ):
            with self.assertRaises(PermissionError): action()
        self.login(self.root)
        for path in ('/api/users','/api/invites','/api/rules','/api/rules/R-001/versions',
                     '/api/audit-log','/api/knowledge','/api/knowledge/graph'):
            self.assertEqual(self.client.get(path).status_code, 200, path)
        self.assertEqual(self.client.put('/api/rules/R-001/state',json={'enabled':False}).status_code, 200)
        self.assertEqual(self.client.put('/api/rules/R-001/state',json={'enabled':True}).status_code, 200)
        self.assertEqual(self.client.post('/api/invites',json={'org_name':'Synthetic school','seats':5}).status_code, 200)
        # Platform may manage subscriptions, but must not receive business summaries itself.
        self.assertNotIn(self.root['id'],{r['id'] for r in self.client.get('/api/notifications/recipients').json()})
        with self.assertRaises(ValueError):
            self.store.set_notification_preferences(self.root,self.root['id'],True,True,False)

    def test_role_change_to_platform_revokes_same_cookie_business_access(self):
        self.login(self.admin)
        self.assertEqual(self.client.get('/api/audits/'+self.audit).status_code, 200)
        with self.store.connect() as db:
            db.execute("UPDATE users SET role='org_admin' WHERE id=?", (self.root['id'],))
            db.execute("UPDATE users SET role='platform_admin' WHERE id=?", (self.admin['id'],))
        self.assertEqual(self.client.get('/api/audits/'+self.audit).status_code, 403)
        self.assertEqual(self.client.get('/api/users').status_code, 200)
        with self.assertRaises(AccessDenied):
            self.store.save_audit('late',self.admin,None,self.data,[],{},'2026-09-27')

    def test_real_http_sessions_keep_isolation_after_restart_and_encrypted_restore(self):
        from scripts.ops_db import create_encrypted_backup, restore_encrypted_backup
        # Separate server process: exercise actual cookies, HTTP and persisted sessions.
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        env = {**os.environ, 'TAXPEARLS_DB': str(self.store.path),
               'TAXPEARLS_AI_ENABLED': '0', 'TAXPEARLS_NOTIFICATION_EMAIL_ENABLED': '0',
               'TAXPEARLS_COOKIE_SECURE': '0', 'PYTHONUTF8': '1'}
        with httpx.Client(base_url=base, trust_env=False, timeout=3) as own, \
                httpx.Client(base_url=base, trust_env=False, timeout=3) as foreign, \
                httpx.Client(base_url=base, trust_env=False, timeout=3) as platform:
            for cycle in range(2):
                if cycle == 1:
                    # First process is stopped. Restore into an isolated new database,
                    # then start the real app against it with original HTTP cookies.
                    sealed = Path(self.tmp.name) / 'http-state.tpbackup'
                    restored = Path(self.tmp.name) / 'http-restored.db'
                    with patch.dict(os.environ, {'TAXPEARLS_BACKUP_KEY': base64.b64encode(b'T' * 32).decode()}):
                        create_encrypted_backup(self.store.path, sealed, retention_days=30)
                        restore_encrypted_backup(sealed, restored, safety_retention_days=30)
                    env['TAXPEARLS_DB'] = str(restored)
                process = subprocess.Popen([sys.executable, '-m', 'webapp', '--host', '127.0.0.1', '--port', str(port)],
                    cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                try:
                    until = time.monotonic() + 25
                    while time.monotonic() < until:
                        if process.poll() is not None:
                            self.fail('Test server exited: ' + process.stderr.read().decode('utf-8', errors='replace'))
                        try:
                            if own.get('/healthz').status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        time.sleep(.1)
                    else:
                        self.fail('Test server did not become healthy')
                    if cycle == 0:
                        for client, actor in ((own, self.accountant), (foreign, self.foreign), (platform, self.root)):
                            response = client.post('/api/login', json={'username': actor['username'], 'password': self.password})
                            self.assertEqual(response.status_code, 200)
                    self.assertEqual(own.get('/api/audits/' + self.audit).status_code, 200)
                    self.assertEqual({a['id'] for a in own.get('/api/audits').json()}, {self.audit})
                    report = own.get('/api/report/' + self.audit + '/html')
                    self.assertEqual(report.status_code, 200)
                    if cycle == 0:
                        original_report = report.content
                    else:
                        self.assertEqual(report.content, original_report)
                    self.assertEqual(foreign.get('/api/audits').json(), [])
                    self.assertEqual(platform.get('/api/audits').status_code, 403)
                    for client, status in ((foreign, 404), (platform, 403)):
                        self.assertEqual(client.get('/api/audits/' + self.audit).status_code, status)
                        self.assertEqual(client.get('/api/report/' + self.audit + '/html').status_code, status)
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill(); process.wait(timeout=10)
                    process.stderr.close()


if __name__ == '__main__':
    unittest.main()
