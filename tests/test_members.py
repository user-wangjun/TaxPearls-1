"""G10/G13 membership and F12 serialized active-seat checks; no external mail."""
import auth_support
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import os
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from webapp import app as module, members
from webapp.access import AccessDenied
from webapp.storage import Store


class MemberTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.old=module.store
        self.env=patch.dict(os.environ,{'TAXPEARLS_AI_ENABLED':'0','TAXPEARLS_NOTIFICATION_EMAIL_ENABLED':'0'})
        self.env.start()
        self.store=Store(Path(self.tmp.name)/'members.db');module.store=self.store
        self.password='Member-review-2026!'
        self.root=self.person('platform','platform_admin','platform')
        self.owner=self.person('owner','org_admin','alpha')
        self.other=self.person('other-owner','org_admin','beta')
        self.member=self.person('member','accountant','alpha')
        self.client=TestClient(module.app);self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None,None,None);module.store=self.old
        self.env.stop();self.tmp.cleanup()

    def person(self,name,role,org):
        return self.store.create_user(name,self.password,name,role,org)

    def login(self,who):
        self.client.cookies.clear()
        self.client.cookies.set(module.COOKIE_NAME,self.store.authenticate(who['username'],self.password)[1])

    def configure(self,seats=3):
        current=members.overview(self.store,self.root,'alpha')['quota']
        return members.set_quota(self.store,self.root,'alpha',seats,current['revision'])

    def create(self,name,repo=None):
        return (repo or self.store).create_user(name,self.password,name,'accountant','alpha',actor=self.owner)

    def test_platform_manual_creation_denied_before_hash_or_write_and_owner_kept(self):
        self.configure();self.login(self.root)
        body={'username':'created','password':self.password,'display_name':'会计','role':'accountant','org_id':'alpha'}
        before=len(self.store.list_users())
        with patch.object(self.store,'create_user') as create:
            for role in ['platform_admin','org_admin','accountant','teacher','student']:
                self.assertEqual(self.client.post('/api/users',json={**body,'role':role}).status_code,403)
            create.assert_not_called()
        self.assertEqual(len(self.store.list_users()),before)
        with self.assertRaises(AccessDenied):
            self.store.create_user('bypass',self.password,'禁止平台建号','accountant','alpha',actor=self.root)
        self.assertEqual(self.client.get('/api/users').status_code,200)
        self.assertEqual(self.client.post('/api/invites',json={'org_name':'新机构','seats':5}).status_code,200)
        self.login(self.owner)
        for fields in [{'role':'teacher'},{'role':'org_admin'},{'org_id':'beta'}]:
            self.assertEqual(self.client.post('/api/users',json={**body,**fields}).status_code,403)
        created=self.client.post('/api/users',json=body)
        self.assertEqual(created.status_code,200,created.text)
        row=next(r for r in members.overview(self.store,self.owner,'alpha')['members'] if r['id']==created.json()['id'])
        self.assertEqual(row['source_kind'],'admin_created');self.assertEqual(row['actor_id'],self.owner['id'])
        self.assertEqual(members.overview(self.store,self.owner,'alpha')['quota']['remaining'],0)

    def test_missing_quota_is_explicit_and_existing_accounts_remain_valid(self):
        state=members.overview(self.store,self.owner,'alpha')
        self.assertIsNone(state['quota']['seats']);self.assertEqual(state['quota']['used'],2)
        self.assertEqual({r['source_kind'] for r in state['members']},{'legacy'})
        self.assertIsNotNone(self.store.authenticate(self.member['username'],self.password))
        with self.assertRaisesRegex(AccessDenied,'尚未配置'):self.create('missing-quota')
        self.assertEqual(len(self.store.list_users('alpha')),2)
        self.configure(3);self.create('configured-user')
        self.assertEqual(members.overview(self.store,self.owner,'alpha')['quota']['remaining'],0)

    def test_member_and_quota_api_role_org_and_body_matrix(self):
        teacher=self.person('teacher','teacher','alpha');student=self.person('student','student','alpha')
        self.configure(5)
        for who,status in [(self.root,200),(self.owner,200),(self.other,404),(self.member,403),(teacher,403),(student,403)]:
            self.login(who)
            response=self.client.get('/api/members/alpha')
            self.assertEqual(response.status_code,status,who['role'])
            self.assertIn('no-store',response.headers['cache-control'])
            if status!=200:self.assertNotIn(self.member['id'],response.text)
        self.login(self.owner)
        self.assertEqual(self.client.get('/api/members/organizations').json(),[{'org_id':'alpha','name':'owner'}])
        self.assertEqual(self.client.put('/api/members/alpha/quota',json={'seats':9,'revision':1}).status_code,403)
        self.assertEqual(self.client.put('/api/members/beta/'+self.other['id']+'/active',json={'active':False,'expected_active':True}).status_code,404)
        self.assertEqual(self.client.put('/api/members/alpha/'+self.member['id']+'/active',json={'active':'false','expected_active':True}).status_code,422)
        self.login(self.root)
        self.assertEqual({r['org_id'] for r in self.client.get('/api/members/organizations').json()},{'alpha','beta'})
        self.assertEqual(self.client.put('/api/members/alpha/quota',json={'seats':5,'revision':1,'password':'NEVER-ECHO'}).status_code,422)
        self.client.cookies.clear();self.assertEqual(self.client.get('/api/members/alpha').status_code,401)

    def test_quota_revision_limits_and_no_silent_deactivation(self):
        self.configure(3)
        for seats,revision in [(1,1),(4,0),(True,1),(201,1)]:
            with self.assertRaises(AccessDenied):members.set_quota(self.store,self.root,'alpha',seats,revision)
        state=members.overview(self.store,self.root,'alpha')
        self.assertEqual((state['quota']['seats'],state['quota']['revision']),(3,1))
        self.assertTrue(all(r['active'] for r in state['members']))
        with self.assertRaises(AccessDenied):members.set_quota(self.store,self.root,'missing',5,0)
        with self.store.connect() as db:db.execute("UPDATE org_quota SET seats=1 WHERE org_id='alpha'")
        self.assertTrue(members.overview(self.store,self.owner,'alpha')['quota']['over_quota'])
        self.assertIsNotNone(self.store.authenticate(self.member['username'],self.password))

    def test_disable_releases_seat_restore_requires_space_and_never_revives_cookie(self):
        self.configure(2)
        token=self.store.authenticate(self.member['username'],self.password)[1]
        customer=self.store.upsert_client(self.owner,'历史客户','SYNTHETIC-CLIENT',self.member['id'])
        members.set_active(self.store,self.owner,'alpha',self.member['id'],False,True)
        self.assertIsNone(self.store.user_for_token(token));self.assertIsNone(self.store.authenticate(self.member['username'],self.password))
        replacement=self.create('replacement')
        with self.assertRaisesRegex(AccessDenied,'席位已满'):
            members.set_active(self.store,self.owner,'alpha',self.member['id'],True,False)
        members.set_active(self.store,self.owner,'alpha',replacement['id'],False,True)
        members.set_active(self.store,self.owner,'alpha',self.member['id'],True,False)
        self.assertIsNone(self.store.user_for_token(token))
        self.assertIsNotNone(self.store.authenticate(self.member['username'],self.password))
        self.assertEqual(self.store.get_client(customer['id'])['accountant_id'],self.member['id'])
        actions={r['action'] for r in self.store.list_logs(self.owner)}
        self.assertTrue({'activate_member','deactivate_member'}<=actions)

    def test_last_admin_self_other_admin_and_platform_protected(self):
        self.configure(4)
        for actor,target,status in [(self.owner,self.owner,403),(self.root,self.owner,409),(self.root,self.root,404)]:
            with self.assertRaises(AccessDenied) as raised:
                members.set_active(self.store,actor,'alpha',target['id'],False,True)
            self.assertEqual(raised.exception.status,status)
        second=self.person('second-owner','org_admin','alpha')
        with self.assertRaises(AccessDenied):members.set_active(self.store,self.owner,'alpha',second['id'],False,True)
        members.set_active(self.store,self.root,'alpha',second['id'],False,True)
        with self.assertRaisesRegex(AccessDenied,'状态已变化'):
            members.set_active(self.store,self.root,'alpha',second['id'],True,True)

    def test_stale_manager_cannot_create_configure_or_mutate(self):
        self.configure()
        with self.store.connect() as db:db.execute("UPDATE users SET role='accountant' WHERE id=?",(self.owner['id'],))
        for call in [lambda:self.create('stale-write'),lambda:members.overview(self.store,self.owner,'alpha'),
                     lambda:members.set_active(self.store,self.owner,'alpha',self.member['id'],False,True)]:
            with self.assertRaises(AccessDenied):call()
        with self.store.connect() as db:db.execute('UPDATE users SET active=0 WHERE id=?',(self.root['id'],))
        with self.assertRaises(AccessDenied):members.set_quota(self.store,self.root,'alpha',5,1)
        self.assertEqual(len(self.store.list_users('alpha')),2)

    def test_concurrent_creations_across_stores_never_overbook(self):
        self.configure(3)
        second=Store(self.store.path);gate=Barrier(2)
        def create(index,repo):
            gate.wait(timeout=5)
            try:self.create('race-'+str(index),repo);return 'created'
            except AccessDenied as exc:return exc.status
        with ThreadPoolExecutor(2) as executor:
            futures=[executor.submit(create,1,self.store),executor.submit(create,2,second)]
            self.assertCountEqual([f.result(timeout=10) for f in futures],['created',409])
        self.assertEqual(members.overview(self.store,self.owner,'alpha')['quota']['used'],3)
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM member_origins').fetchone()[0],1)

    def test_restore_races_creation_for_same_last_slot(self):
        self.configure(3)
        spare=self.person('inactive-member','accountant','alpha')
        members.set_active(self.store,self.owner,'alpha',spare['id'],False,True)
        second=Store(self.store.path);gate=Barrier(2)
        def call(restore):
            gate.wait(timeout=5)
            try:
                if restore:members.set_active(second,self.owner,'alpha',spare['id'],True,False)
                else:self.create('race-restore')
                return 200
            except AccessDenied as exc:return exc.status
        with ThreadPoolExecutor(2) as executor:
            futures=[executor.submit(call,True),executor.submit(call,False)]
            self.assertCountEqual([f.result(timeout=10) for f in futures],[200,409])
        self.assertEqual(members.overview(self.store,self.owner,'alpha')['quota']['used'],3)

    def test_login_and_disable_serialization_revokes_racing_cookie(self):
        from webapp import storage
        self.configure()
        entered,release=Event(),Event()
        original=storage._passwords.verify
        def verify(*args,**kwargs):
            entered.set()
            if not release.wait(5):raise AssertionError('test handshake timed out')
            return original(*args,**kwargs)
        second=Store(self.store.path)
        with patch.object(storage,'_passwords',wraps=storage._passwords) as hasher,ThreadPoolExecutor(2) as executor:
            hasher.verify.side_effect=verify
            login=executor.submit(self.store.authenticate,self.member['username'],self.password)
            self.assertTrue(entered.wait(5))
            stop=executor.submit(members.set_active,second,self.owner,'alpha',self.member['id'],False,True)
            release.set();token=login.result(timeout=10)[1];stop.result(timeout=10)
        members.set_active(self.store,self.owner,'alpha',self.member['id'],True,False)
        self.assertIsNone(self.store.user_for_token(token))

    def test_founder_provenance_quota_and_legacy_upgrade_preserve_data(self):
        invite=self.store.create_invite_code(self.root,'开户机构',5)
        code=auth_support.register_code(self.store,'founder@example.test')
        owner,token=self.store.register_with_code('founder@example.test',code,invite['code'],self.password, browser_session=auth_support.BROWSER)
        state=members.overview(self.store,owner,owner['org_id'])
        self.assertEqual(state['members'][0]['source_kind'],'founder')
        self.assertEqual((state['quota']['used'],state['quota']['remaining']),(1,4))
        reopened=Store(self.store.path)
        self.assertEqual(members.overview(reopened,owner,owner['org_id']),state)
        self.assertIsNotNone(reopened.user_for_token(token))
        self.assertNotIn(invite['code'],str(state))
        self.assertNotIn('password_hash',str(state))

    def test_disabling_suppresses_queued_notices_and_restore_does_not_resend(self):
        from src import loader
        self.configure()
        with self.store.connect() as db:db.execute('UPDATE users SET email=? WHERE id=?',('member@example.test',self.member['id']))
        self.store.set_notification_preferences(self.member,self.member['id'],True,False,True)
        data=loader.load(Path(__file__).resolve().parents[1]/'samples'/'样例企业-审计材料.xlsx')
        customer=self.store.upsert_client(self.owner,data.company.name,data.company.taxpayer_id,self.member['id'])
        module._save_audit(data,self.owner,customer['id'])
        notice=self.store.list_notifications(self.member)[0]
        self.assertEqual(notice['email_status'],'pending')
        members.set_active(self.store,self.owner,'alpha',self.member['id'],False,True)
        members.set_active(self.store,self.owner,'alpha',self.member['id'],True,False)
        self.assertEqual(self.store.list_notifications(self.member)[0]['email_status'],'suppressed')
        self.assertIsNone(self.store.claim_notification_delivery())

    def test_failed_provenance_or_audit_write_rolls_back_account_and_status(self):
        self.configure(3)
        token=self.store.authenticate(self.member['username'],self.password)[1]
        with patch.object(members,'log',side_effect=RuntimeError('simulated audit failure')):
            with self.assertRaises(RuntimeError):
                members.set_active(self.store,self.owner,'alpha',self.member['id'],False,True)
        self.assertTrue(self.store.get_user(self.member['id'])['active'])
        self.assertIsNotNone(self.store.user_for_token(token))
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER reject_origin BEFORE INSERT ON member_origins BEGIN SELECT RAISE(ABORT,'simulated origin failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):self.create('rollback-member')
        self.assertEqual(members.overview(self.store,self.owner,'alpha')['quota']['used'],2)
        self.assertNotIn('rollback-member',{r['username'] for r in self.store.list_users()})

    def test_creation_and_founder_issue_roll_back_if_log_write_fails(self):
        self.configure(3)
        before_users = self.store.list_users()
        before_invites = self.store.list_invite_codes()
        with patch.object(self.store, '_log', side_effect=RuntimeError('test log failure')):
            with self.assertRaises(RuntimeError):
                self.create('log-rollback-member')
            with self.assertRaises(RuntimeError):
                self.store.create_invite_code(self.root, '回滚机构', 5)
        self.assertEqual(self.store.list_users(), before_users)
        self.assertEqual(self.store.list_invite_codes(), before_invites)
        self.assertEqual(members.overview(self.store,self.owner,'alpha')['quota']['used'], 2)
        self.login(self.owner)
        response = self.client.post('/api/users', json={'username':'logged-member','password':self.password,
            'display_name':'新成员','role':'accountant'})
        self.assertEqual(response.status_code, 200, response.text)
        self.login(self.root)
        response = self.client.post('/api/invites', json={'org_name':'已记录机构','seats':5})
        self.assertEqual(response.status_code, 200, response.text)
        with self.store.connect() as db:
            for action in ('create_user', 'invite_created'):
                rows = db.execute('SELECT * FROM audit_log WHERE action=?', (action,)).fetchall()
                self.assertEqual(len(rows), 1)
                self.assertNotIn(response.json()['code'], str([tuple(row) for row in rows]))

    def test_backup_preserves_member_provenance_quota_and_revoked_session(self):
        self.configure(3);created=self.create('backup-member')
        token=self.store.authenticate(created['username'],self.password)[1]
        members.set_active(self.store,self.owner,'alpha',created['id'],False,True)
        before=members.overview(self.store,self.owner,'alpha')
        backup=Path(self.tmp.name)/'restored.db'
        with self.store.connect() as source,closing(sqlite3.connect(backup)) as target:source.backup(target)
        restored=Store(backup)
        self.assertEqual(members.overview(restored,self.owner,'alpha'),before)
        self.assertIsNone(restored.user_for_token(token))
        self.assertFalse(restored.get_user(created['id'])['active'])

    def test_member_frontend_contains_real_controls_and_explicit_missing_quota(self):
        page=self.client.get('/').text + self.client.get('/console.js').text
        for element in ['memberOrg','memberQuotaStatus','memberSeats','btnSaveMemberQuota','btnRefreshMembers']:
            self.assertIn('id="'+element+'"',page)
        self.assertIn('expected_active:!!person.active',page)
        self.assertIn('request!==memberRequest',page)
        self.assertIn('席位尚未配置',page)


if __name__=='__main__':unittest.main()
