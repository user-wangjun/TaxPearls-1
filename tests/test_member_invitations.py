"""Full-hash invitation registration, lifecycle, policy and concurrency gates."""
import auth_support
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from webapp import app as module, invitations, members
from webapp.access import AccessDenied
from webapp.login_guard import RateLimiter
from webapp.storage import Store, _hash_token


class MemberInvitationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name)/'invitations.db')
        self.password = 'Secure-welcome-2026!'
        self.root = self.store.create_user('root',self.password,'平台','platform_admin','platform')
        self.owner = self.store.create_user('owner',self.password,'机构甲','org_admin','alpha')
        self.other = self.store.create_user('other',self.password,'机构乙','org_admin','beta')
        members.set_quota(self.store,self.root,'alpha',10,0)
        self.patches = patch.multiple(module,store=self.store,
            register_complete_limiter=RateLimiter({'email':(10,900),'ip':(60,3600)}))
        self.patches.start(); self.addCleanup(self.patches.stop)
        self.client = TestClient(module.app)
        self.client.__enter__(); self.addCleanup(self.client.__exit__,None,None,None)

    def issue(self,role='accountant'):
        return invitations.issue(self.store,self.owner,'alpha',role)

    def redeem(self,link,email='newperson@example.com',store=None):
        store = store or self.store
        return store.register_with_code(email,auth_support.register_code(store,email),link['code'],self.password, browser_session=auth_support.BROWSER)

    def login(self,actor):
        self.client.cookies.clear()
        self.client.cookies.set(module.COOKIE_NAME,self.store.authenticate(actor['username'],self.password)[1])

    def test_multi_use_roles_sources_and_no_plaintext_in_database_or_ledger(self):
        for role in sorted(invitations.MEMBER_ROLES):
            link = self.issue(role)
            self.assertRegex(link['code'],r'^[0-9A-HJKMNP-TV-Z]{32}$')
            self.assertEqual(link['path'],'/p/'+link['code'])
            for n in range(2):
                user,session = self.redeem(link,f'{role}{n}@example.com')
                self.assertEqual((user['org_id'],user['role']),('alpha',role))
                self.assertEqual(self.store.user_for_token(session)['id'],user['id'])
            state = invitations.overview(self.store,self.owner,'alpha')
            record = next(row for row in state['links'] if row['id']==link['id'])
            self.assertEqual(record['used_count'],2); self.assertIsNotNone(record['last_used_at'])
            self.assertNotIn(link['code'],json.dumps(state)); self.assertNotIn('token_hash',json.dumps(state))
            with self.store.connect() as db:
                dump = '\n'.join(db.iterdump())
            self.assertNotIn(link['code'],dump)
        state = members.overview(self.store,self.owner,'alpha')
        self.assertEqual(state['quota']['used'],7)
        for row in state['members']:
            if row['id']!=self.owner['id']:
                self.assertEqual(row['source_kind'],'member_invitation')
                self.assertTrue(row['credential_id']); self.assertEqual(row['invitation_role'],row['role'])

    def test_refresh_stable_alias_disable_and_revision_conflict(self):
        first = self.issue(); second = self.issue('student')
        self.assertEqual(first['code'][:8],second['code'][:8])
        disabled = invitations.change(self.store,self.owner,'alpha',first['id'],1,active=False)
        with self.assertRaisesRegex(ValueError,'停用'): self.redeem(first)
        rotated = invitations.change(self.store,self.owner,'alpha',first['id'],disabled['revision'])
        self.assertFalse(rotated['active'])
        self.assertEqual(first['code'][:8],rotated['code'][:8]); self.assertNotEqual(first['code'][8:],rotated['code'][8:])
        with self.assertRaisesRegex(AccessDenied,'变化'):
            invitations.change(self.store,self.owner,'alpha',first['id'],1,active=True)
        with self.assertRaises(ValueError): self.redeem(first)
        invitations.change(self.store,self.owner,'alpha',first['id'],rotated['revision'],active=True)
        self.redeem(rotated)
        self.assertEqual(invitations.overview(self.store,self.owner,'alpha')['links'][0]['used_count'],1)

    def test_full_hash_only_not_length_or_prefix_and_normalization(self):
        link = self.issue()
        unusual = 'A10123456789'  # Member credential deliberately has founder-code length.
        with self.store.connect() as db:
            db.execute('UPDATE member_invitations SET token_hash=? WHERE id=?',(_hash_token(unusual),link['id']))
        user,_ = self.redeem({'code':' aI-o1-2345 6789 '})
        self.assertEqual(user['role'],'accountant')
        with self.assertRaises(ValueError): self.redeem({'code':link['code'][:8]},'another@example.com')
        founder = self.store.create_invite_code(self.root,'另一个机构',5)
        with self.store.connect() as db:
            db.execute('UPDATE invite_codes SET token_hash=? WHERE token_hash=?',(_hash_token(unusual),_hash_token(founder['code'])))
        with self.assertRaisesRegex(ValueError,'无效'): self.redeem({'code':unusual},'ambiguous@example.com')

    def test_optional_domain_policy_exact_match_idna_and_revision(self):
        link = self.issue()
        self.assertEqual(invitations.overview(self.store,self.owner,'alpha')['policy'],{'enabled':False,'domains':[],'revision':1})
        state = invitations.set_policy(self.store,self.owner,'alpha',True,['Example.COM','学校.example'],1)
        self.assertEqual(state['domains'],['example.com','xn--48s290a.example'])
        for email in ['someone@sub.example.com','someone@example.com.evil.test','someone@notexample.com']:
            with self.assertRaisesRegex(ValueError,'域名'): self.redeem(link,email)
        self.redeem(link,'Allowed@EXAMPLE.COM')
        self.redeem(link,'student@学校.example')
        with self.assertRaisesRegex(AccessDenied,'变化'):
            invitations.set_policy(self.store,self.owner,'alpha',False,[],1)
        for bad in ['*.example.com','@example.com','https://example.com','example.com:443','example.com.','bad_domain.com']:
            with self.assertRaises(AccessDenied): invitations.set_policy(self.store,self.owner,'alpha',True,[bad],2)
        with self.assertRaises(AccessDenied): invitations.set_policy(self.store,self.owner,'alpha',True,[],2)
        invitations.set_policy(self.store,self.owner,'alpha',False,[],2)
        self.redeem(link,'anywhere@else.test')

    def test_policy_before_issue_and_reopen_does_not_change_alias_or_policy(self):
        state = invitations.set_policy(self.store,self.owner,'alpha',True,['example.com'],0)
        self.assertEqual(state['revision'],1)
        link = self.issue()
        reopened = Store(self.store.path)
        rotated = invitations.change(reopened,self.owner,'alpha',link['id'],1)
        self.assertEqual(link['code'][:8],rotated['code'][:8])
        self.assertEqual(invitations.overview(reopened,self.owner,'alpha')['policy'],state)
        self.redeem(rotated,store=reopened)

    def test_api_role_org_matrix_and_immutable_credential_role(self):
        link = self.issue()
        teacher = self.store.create_user('teacher',self.password,'教师','teacher','alpha')
        student = self.store.create_user('student',self.password,'学生','student','alpha')
        accountant = self.store.create_user('accountant',self.password,'会计','accountant','alpha')
        for actor,status in [(self.root,200),(self.owner,200),(self.other,404),(teacher,403),(student,403),(accountant,403)]:
            self.login(actor)
            result = self.client.get('/api/members/alpha/invitations')
            self.assertEqual(result.status_code,status); self.assertIn('no-store',result.headers['cache-control'])
            if status!=200:
                self.assertNotIn(link['id'],result.text)
                self.assertEqual(self.client.post('/api/members/alpha/invitations',json={'role':'student'}).status_code,status)
                self.assertEqual(self.client.post(f'/api/members/alpha/invitations/{link["id"]}/refresh',json={'revision':1}).status_code,status)
                self.assertEqual(self.client.put(f'/api/members/alpha/invitations/{link["id"]}/state',json={'revision':1,'active':False}).status_code,status)
                self.assertEqual(self.client.put('/api/members/alpha/invitation-policy',json={'enabled':False,'domains':[],'revision':1}).status_code,status)
        self.login(self.owner)
        for body in [{'role':'org_admin'},{'role':'platform_admin'},{'role':'student','org_id':'beta'}]:
            self.assertEqual(self.client.post('/api/members/alpha/invitations',json=body).status_code,422)
        self.assertEqual(self.client.post('/api/members/alpha/invitations',json={'role':'accountant'}).status_code,409)
        self.assertEqual(self.client.put(f'/api/members/beta/invitations/{link["id"]}/state',json={'revision':1,'active':False}).status_code,404)
        self.assertEqual(self.client.post(f'/api/members/alpha/invitations/{link["id"]}/refresh',json={'revision':True}).status_code,422)
        self.client.cookies.clear()
        self.assertEqual(self.client.get('/api/members/alpha/invitations').status_code,401)

    def test_registration_rejects_caller_role_org_and_sets_session(self):
        link = self.issue('teacher'); email='newteacher@example.com'
        browser=module.email_auth.browser_secret();self.client.cookies.set(module.EMAIL_COOKIE_NAME,browser)
        delivery=module.email_auth.issue(self.store,email,'register',browser)
        body = {'email':email,'code':delivery['code'],'invite_code':link['code'],'password':self.password}
        for extra in [{'role':'platform_admin'},{'org_id':'beta'}]:
            result=self.client.post('/api/register/complete',json={**body,**extra})
            self.assertEqual(result.status_code,422); self.assertNotIn(self.password,result.text)
        result=self.client.post('/api/register/complete',json=body)
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['user']['role'],'teacher')
        self.assertEqual(self.client.get('/api/me').json()['org_id'],'alpha')
        landing=self.client.get(link['path'])
        self.assertEqual(landing.status_code,200)
        self.assertEqual(landing.headers['referrer-policy'],'no-referrer')
        self.assertIn('no-store',landing.headers['cache-control']);self.assertNotIn(link['code'],landing.text)

    def test_quota_failure_preserves_email_code_then_disable_releases_slot(self):
        members.set_quota(self.store,self.root,'alpha',2,1)
        link=self.issue();user,_=self.redeem(link)
        email='waiting@example.com';code=auth_support.register_code(self.store,email)
        with self.assertRaisesRegex(AccessDenied,'已满'):
            self.store.register_with_code(email,code,link['code'],self.password, browser_session=auth_support.BROWSER)
        with self.store.connect() as db:
            self.assertIsNone(db.execute('SELECT used_at FROM email_tokens WHERE email=?',(email,)).fetchone()[0])
        members.set_active(self.store,self.owner,'alpha',user['id'],False,True)
        self.store.register_with_code(email,code,link['code'],self.password, browser_session=auth_support.BROWSER)
        self.assertEqual(invitations.overview(self.store,self.owner,'alpha')['links'][0]['used_count'],2)
        with self.assertRaisesRegex(ValueError,'已注册'):self.redeem(link)

    def test_missing_quota_and_stale_manager_rejected(self):
        owned=self.issue()
        link=invitations.issue(self.store,self.other,'beta','student')
        with self.assertRaisesRegex(AccessDenied,'尚未配置'): self.redeem(link)
        with self.store.connect() as db: db.execute('UPDATE users SET active=0 WHERE id=?',(self.owner['id'],))
        for operation in [lambda: self.issue(),lambda: invitations.overview(self.store,self.owner,'alpha'),
                          lambda: invitations.change(self.store,self.owner,'alpha',owned['id'],1),
                          lambda: invitations.set_policy(self.store,self.owner,'alpha',False,[],0)]:
            with self.assertRaises(AccessDenied): operation()

    def test_atomic_log_failure_rolls_back_link_policy_and_registration(self):
        with patch.object(members,'log',side_effect=RuntimeError('log unavailable')):
            with self.assertRaises(RuntimeError):self.issue()
        self.assertEqual(invitations.overview(self.store,self.owner,'alpha')['policy']['revision'],0)
        link=self.issue();email='rollback@example.com';code=auth_support.register_code(self.store,email)
        with patch.object(members,'log',side_effect=RuntimeError('log unavailable')):
            with self.assertRaises(RuntimeError):invitations.change(self.store,self.owner,'alpha',link['id'],1)
            with self.assertRaises(RuntimeError):invitations.set_policy(self.store,self.owner,'alpha',True,['example.com'],1)
            with self.assertRaises(RuntimeError):self.store.register_with_code(email,code,link['code'],self.password, browser_session=auth_support.BROWSER)
        self.assertIsNone(self.store.get_user_by_email(email))
        state=invitations.overview(self.store,self.owner,'alpha')
        self.assertEqual(state['links'][0]['revision'],1);self.assertEqual(state['links'][0]['used_count'],0)
        self.assertFalse(state['policy']['enabled'])
        self.store.register_with_code(email,code,link['code'],self.password, browser_session=auth_support.BROWSER)

    def test_two_stores_register_last_slot_once(self):
        members.set_quota(self.store,self.root,'alpha',2,1)
        link=self.issue();other=Store(self.store.path);barrier=Barrier(2)
        codes={email:auth_support.register_code(self.store,email) for email in ['raceone@example.com','racetwo@example.com']}
        def run(store,email):
            barrier.wait()
            try: return store.register_with_code(email,codes[email],link['code'],self.password, browser_session=auth_support.BROWSER)[0]['id']
            except AccessDenied:return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda args:run(*args),[(self.store,'raceone@example.com'),(other,'racetwo@example.com')]))
        self.assertEqual(sum(value is not None for value in results),1)
        state=invitations.overview(self.store,self.owner,'alpha')
        self.assertEqual(state['quota']['used'],2);self.assertEqual(state['links'][0]['used_count'],1)

    def test_redemption_races_refresh_serializes_to_valid_outcome(self):
        link=self.issue();other=Store(self.store.path);barrier=Barrier(2)
        email='racer@example.com';code=auth_support.register_code(self.store,email)
        def redeem():
            barrier.wait()
            try:return self.store.register_with_code(email,code,link['code'],self.password, browser_session=auth_support.BROWSER)[0]
            except ValueError:return None
        def rotate():
            barrier.wait();return invitations.change(other,self.owner,'alpha',link['id'],1)
        with ThreadPoolExecutor(max_workers=2) as pool:
            future=pool.submit(redeem);rotation=pool.submit(rotate)
            user=future.result();newlink=rotation.result()
        self.assertEqual(invitations.overview(self.store,self.owner,'alpha')['links'][0]['used_count'],int(user is not None))
        with self.assertRaises(ValueError): self.redeem(link,'expired@example.com')
        self.redeem(newlink,'valid@example.com')

    def test_backup_restore_preserves_provenance_usage_and_rotated_secret(self):
        link=self.issue();self.redeem(link)
        rotated=invitations.change(self.store,self.owner,'alpha',link['id'],1)
        expected=invitations.overview(self.store,self.owner,'alpha')
        backup=Path(self.tmp.name)/'backup.db'
        with self.store.connect() as source,closing(sqlite3.connect(backup)) as target:source.backup(target)
        restored=Store(backup)
        self.assertEqual(invitations.overview(restored,self.owner,'alpha'),expected)
        self.assertEqual(members.overview(restored,self.owner,'alpha'),members.overview(self.store,self.owner,'alpha'))
        with self.assertRaises(ValueError): self.redeem(link,'oldtoken@example.com',restored)
        self.redeem(rotated,'restored@example.com',restored)

    def test_no_expiry_founder_long_token_and_failed_origin_atomicity(self):
        link=self.issue()
        with self.store.connect() as db:
            db.execute("UPDATE member_invitations SET created_at='2000-01-01T00:00:00+00:00',updated_at=created_at")
        self.redeem(link)  # No implicit expiry even on old invitation rows.
        founder=self.store.create_invite_code(self.root,'长凭证兼容',5)
        long_code='Z'*32
        with self.store.connect() as db:
            db.execute('UPDATE invite_codes SET token_hash=? WHERE token_hash=?',(_hash_token(long_code),_hash_token(founder['code'])))
        user,_=self.redeem({'code':long_code},'founder@example.com')
        self.assertEqual(user['role'],'org_admin');self.assertNotEqual(user['org_id'],'alpha')
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER fail_origin BEFORE INSERT ON member_origins BEGIN SELECT RAISE(ABORT,'origin unavailable'); END")
        email='originfail@example.com';code=auth_support.register_code(self.store,email)
        with self.assertRaises(sqlite3.IntegrityError):self.store.register_with_code(email,code,link['code'],self.password, browser_session=auth_support.BROWSER)
        self.assertIsNone(self.store.get_user_by_email(email))
        self.assertEqual(invitations.overview(self.store,self.owner,'alpha')['links'][0]['used_count'],1)
        with self.store.connect() as db:db.execute('DROP TRIGGER fail_origin')
        self.store.register_with_code(email,code,link['code'],self.password, browser_session=auth_support.BROWSER)

    def test_registration_vs_member_restore_share_last_slot(self):
        link=self.issue();person,_=self.redeem(link)
        members.set_active(self.store,self.owner,'alpha',person['id'],False,True)
        members.set_quota(self.store,self.root,'alpha',2,1)
        other=Store(self.store.path);barrier=Barrier(2)
        email='slotrace@example.com';code=auth_support.register_code(self.store,email)
        def register():return self.store.register_with_code(email,code,link['code'],self.password, browser_session=auth_support.BROWSER)
        def restore():return members.set_active(other,self.owner,'alpha',person['id'],True,False)
        def attempt(operation):
            barrier.wait()
            try:operation();return True
            except AccessDenied:return False
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(attempt,[register,restore]))
        self.assertEqual(sum(results),1)
        self.assertEqual(members.overview(self.store,self.owner,'alpha')['quota']['used'],2)

    def test_registration_ui_send_guard_and_invitation_contract(self):
        html=''.join((Path(__file__).parents[1]/'webapp/static'/name).read_text(encoding='utf-8') for name in ('index.html','console.js'))
        for marker in ['updateSignupSendButton','signupSendBusy=true','captchaAnswer").required = false',
                       '$("signupCode").value = inviteCode','name="referrer" content="no-referrer"',
                       'request!==memberRequest','clearMemberInvitations','memberSnapshot.invitations.policy.revision']:
            self.assertIn(marker,html)
