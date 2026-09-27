"""G11 browser-bound dual email proof lifecycle (no network transport)."""
import auth_support
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch

from webapp import email_auth, invitations, members
from webapp.access import AccessDenied
from webapp.storage import Store


class EmailAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'email.db')
        self.password='Secure-proof-2026!';self.new_password='New-verifiable-2027!'
        self.root=self.store.create_user('platform',self.password,'平台','platform_admin','platform')
        self.owner=self.store.create_user('owner',self.password,'管理员','org_admin','alpha','chief@example.com')
        members.set_quota(self.store,self.root,'alpha',5,0)
        self.browser=email_auth.browser_secret();self.other=email_auth.browser_secret()

    def issue(self,email='person@example.com',purpose='register',browser=None,**kwargs):
        return email_auth.issue(self.store,email,purpose,browser or self.browser,**kwargs)

    def member_link(self):
        return invitations.issue(self.store,self.owner,'alpha','student')

    def register(self,delivery,link='',proof='',browser=None):
        return self.store.register_with_code(delivery['email'],delivery['code'],link,self.password,
            browser_session=browser or self.browser,email_proof=proof)

    def row(self,token):
        with self.store.connect() as db:
            return dict(db.execute('SELECT * FROM email_tokens WHERE token_hash=?',(email_auth.digest(token),)).fetchone())

    def test_browser_secrets_are_unpredictable_reusable_and_format_bounded(self):
        self.assertNotEqual(self.browser,self.other);self.assertEqual(len(self.browser),43)
        self.assertEqual(email_auth.browser_secret(self.browser),self.browser)
        for value in ['',None,'fixed','A'*500,'<script>']:
            self.assertTrue(email_auth.valid_browser(email_auth.browser_secret(value)))
        with self.assertRaises(ValueError):self.issue(browser='invalid')

    def test_only_digests_persist_and_identical_codes_do_not_collide(self):
        with patch.object(email_auth.secrets,'randbelow',return_value=123456):
            first=self.issue('one@example.com');second=self.issue('two@example.com')
        self.assertEqual(first['code'],second['code']);self.assertNotEqual(first['token'],second['token'])
        self.assertNotEqual(self.row(first['token'])['code_hash'],self.row(second['token'])['code_hash'])
        with self.store.connect() as db:dump='\n'.join(db.iterdump())
        for secret in [self.browser,first['token'],second['token'],'123456']:self.assertNotIn(secret,dump)
        self.assertEqual(self.row(first['token'])['session_key'],email_auth.digest(self.browser))

    def test_registration_magic_and_code_channels_each_create_only_one_account(self):
        link=self.member_link()
        first=self.issue(invite_code=link['code'])
        proof=email_auth.redeem_magic(self.store,first['token'],self.browser)
        self.assertEqual(proof['purpose'],'register');self.assertTrue(proof['has_invite'])
        with self.assertRaises(ValueError):email_auth.redeem_magic(self.store,first['token'],self.browser)
        user,session=self.register(first,proof=proof['proof'])
        self.assertEqual((user['org_id'],user['role']),('alpha','student'))
        self.assertEqual(self.store.user_for_token(session)['id'],user['id'])
        with self.assertRaises(ValueError):self.register(first,link['code'])
        second=self.issue('second@example.com',invite_code=link['code'])
        self.register(second,link['code'])
        with self.assertRaises(ValueError):email_auth.redeem_magic(self.store,second['token'],self.browser)

    def test_copied_link_code_and_proof_rejected_in_other_browser_without_burning_attempts(self):
        link=self.member_link();delivery=self.issue(invite_code=link['code'])
        with self.assertRaises(ValueError):email_auth.redeem_magic(self.store,delivery['token'],self.other)
        with self.assertRaises(ValueError):self.register(delivery,link['code'],browser=self.other)
        self.assertEqual(self.row(delivery['token'])['attempts'],0)
        result=email_auth.redeem_magic(self.store,delivery['token'],self.browser)
        with self.assertRaises(ValueError):self.register(delivery,proof=result['proof'],browser=self.other)
        self.register(delivery,proof=result['proof'])

    def test_five_wrong_codes_invalidate_both_channels_and_survive_reopen(self):
        link=self.member_link();delivery=self.issue(invite_code=link['code'])
        wrong='000000' if delivery['code']!='000000' else '999999'
        for attempt in range(5):
            with self.assertRaises(ValueError):self.register({**delivery,'code':wrong},link['code'])
            self.store=Store(self.store.path)
            self.assertEqual(self.row(delivery['token'])['attempts'],attempt+1)
        self.assertIsNotNone(self.row(delivery['token'])['used_at'])
        with self.assertRaises(ValueError):self.register(delivery,link['code'])
        with self.assertRaises(ValueError):email_auth.redeem_magic(self.store,delivery['token'],self.browser)

    def test_resend_invalidates_old_magic_code_and_registration_proof(self):
        link=self.member_link();first=self.issue(invite_code=link['code'])
        proof=email_auth.redeem_magic(self.store,first['token'],self.browser)['proof']
        second=self.issue(invite_code=link['code'])
        with self.assertRaises(ValueError):email_auth.redeem_magic(self.store,first['token'],self.browser)
        with self.assertRaises(ValueError):self.register(first,proof=proof)
        email_auth.revoke_delivery(self.store,first['token'])  # A late old-mail failure cannot erase the new request.
        self.register(second,link['code'])

    def test_expiry_applies_to_magic_code_and_verified_proof(self):
        link=self.member_link();delivery=self.issue(invite_code=link['code'])
        proof=email_auth.redeem_magic(self.store,delivery['token'],self.browser)['proof']
        with self.store.connect() as db:db.execute("UPDATE email_tokens SET expires_at='2000-01-01T00:00:00+00:00'")
        with self.assertRaises(ValueError):self.register(delivery,proof=proof)
        with self.assertRaises(ValueError):self.register(delivery,link['code'])
        with self.assertRaises(ValueError):email_auth.redeem_magic(self.store,delivery['token'],self.browser)

    def test_invite_email_and_purpose_cannot_be_swapped(self):
        link=self.member_link();delivery=self.issue(invite_code=link['code'])
        founder=self.store.create_invite_code(self.root,'另一个机构',5)
        with self.assertRaisesRegex(ValueError,'不一致'):self.register(delivery,founder['code'])
        proof=email_auth.redeem_magic(self.store,delivery['token'],self.browser)['proof']
        with self.assertRaises(ValueError):self.register({**delivery,'email':'other@example.com'},proof=proof)
        with self.assertRaises(ValueError):email_auth.reset_password(self.store,proof,self.new_password,self.browser)
        with self.assertRaises(ValueError):email_auth.login_with_code(self.store,delivery['email'],delivery['code'],self.browser)
        self.register(delivery,proof=proof)

    def test_quota_and_weak_password_failure_preserve_verified_proof(self):
        members.set_quota(self.store,self.root,'alpha',1,1)
        link=self.member_link();delivery=self.issue(invite_code=link['code'])
        proof=email_auth.redeem_magic(self.store,delivery['token'],self.browser)['proof']
        with self.assertRaises(AccessDenied):self.register(delivery,proof=proof)
        with self.assertRaises(ValueError):self.store.register_with_code(delivery['email'],'','','short',browser_session=self.browser,email_proof=proof)
        members.set_quota(self.store,self.root,'alpha',2,2)
        self.register(delivery,proof=proof)

    def test_existing_or_inactive_email_cannot_register_and_unknown_login_is_not_issued(self):
        self.assertIsNone(self.issue('chief@example.com'))
        self.assertIsNone(self.issue('unknown@example.com','login'))
        self.assertIsNone(self.issue('unknown@example.com','reset'))
        with self.store.connect() as db:db.execute('UPDATE users SET active=0 WHERE id=?',(self.owner['id'],))
        self.assertIsNone(self.issue('chief@example.com'));self.assertIsNone(self.issue('chief@example.com','login'))
        self.assertIsNone(self.issue('chief@example.com','reset'))

    def test_passwordless_login_both_channels_return_current_identity_once(self):
        first=self.issue('chief@example.com','login')
        user,session=email_auth.login_with_code(self.store,first['email'],first['code'],self.browser)
        self.assertEqual(user['id'],self.owner['id']);self.assertEqual(self.store.user_for_token(session)['id'],user['id'])
        with self.assertRaises(ValueError):email_auth.redeem_magic(self.store,first['token'],self.browser)
        second=self.issue('chief@example.com','login')
        with self.store.connect() as db:db.execute("UPDATE users SET display_name='当前名称' WHERE id=?",(user['id'],))
        result=email_auth.redeem_magic(self.store,second['token'],self.browser)
        self.assertEqual(result['purpose'],'login');self.assertEqual(result['user']['display_name'],'当前名称')
        with self.assertRaises(ValueError):email_auth.login_with_code(self.store,second['email'],second['code'],self.browser)

    def test_wrong_login_code_attempts_persist_and_cross_browser_does_not_consume(self):
        delivery=self.issue('chief@example.com','login')
        with self.assertRaises(ValueError):email_auth.login_with_code(self.store,delivery['email'],delivery['code'],self.other)
        self.assertEqual(self.row(delivery['token'])['attempts'],0)
        for n in range(5):
            with self.assertRaises(ValueError):email_auth.login_with_code(self.store,delivery['email'],'not-a-code',self.browser)
        self.assertEqual(self.row(delivery['token'])['attempts'],5)
        with self.assertRaises(ValueError):email_auth.redeem_magic(self.store,delivery['token'],self.browser)

    def test_reset_requires_bound_proof_revokes_all_sessions_and_pending_login(self):
        _,session=self.store.authenticate(self.owner['username'],self.password)
        pending=self.issue('chief@example.com','login');reset=self.issue('chief@example.com','reset')
        with self.assertRaises(ValueError):email_auth.redeem_magic(self.store,reset['token'],self.other)
        proof=email_auth.redeem_magic(self.store,reset['token'],self.browser)['proof']
        with self.assertRaises(ValueError):email_auth.reset_password(self.store,proof,self.new_password,self.other)
        with self.assertRaises(ValueError):email_auth.reset_password(self.store,proof,'xCHIEF!8265',self.browser)
        self.assertIsNotNone(self.store.user_for_token(session))
        user=email_auth.reset_password(self.store,proof,self.new_password,self.browser)
        self.assertEqual(user['id'],self.owner['id']);self.assertIsNone(self.store.user_for_token(session))
        self.assertIsNotNone(self.store.authenticate(user['username'],self.new_password))
        with self.assertRaises(ValueError):email_auth.redeem_magic(self.store,pending['token'],self.browser)
        with self.assertRaises(ValueError):email_auth.reset_password(self.store,proof,self.password,self.browser)

    def test_unbound_legacy_rows_cannot_bypass_browser_proof(self):
        link=self.member_link();register=self.issue(invite_code=link['code'])
        with self.assertRaises(ValueError):
            self.store.register_with_code(register['email'],register['code'],link['code'],self.password)
        legacy=auth_support.legacy_token(self.store,'chief@example.com','reset')
        with self.assertRaises(ValueError):
            email_auth.reset_password(self.store,legacy,self.new_password,self.browser)
        self.assertFalse(hasattr(self.store,'redeem_password_reset'))
        self.assertFalse(hasattr(self.store,'create_register_code'))

    def test_disabling_member_invalidates_pending_proofs_even_after_restore(self):
        person=self.store.create_user('person',self.password,'成员','accountant','alpha','person@example.com')
        login=self.issue('person@example.com','login');reset=self.issue('person@example.com','reset')
        proof=email_auth.redeem_magic(self.store,reset['token'],self.browser)['proof']
        members.set_active(self.store,self.owner,'alpha',person['id'],False,True)
        members.set_active(self.store,self.owner,'alpha',person['id'],True,False)
        with self.assertRaises(ValueError):email_auth.redeem_magic(self.store,login['token'],self.browser)
        with self.assertRaises(ValueError):email_auth.reset_password(self.store,proof,self.new_password,self.browser)

    def test_magic_redemption_race_has_one_winner_across_stores(self):
        delivery=self.issue('chief@example.com','login');other=Store(self.store.path);gate=Barrier(2)
        def run(store):
            gate.wait()
            try:return email_auth.redeem_magic(store,delivery['token'],self.browser)
            except ValueError:return None
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,[self.store,other]))
        self.assertEqual(sum(row is not None for row in results),1)
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0],1)

    def test_audit_failure_rolls_back_login_proof_consumption_and_reset(self):
        login=self.issue('chief@example.com','login')
        with patch.object(members,'log',side_effect=RuntimeError('audit failure')):
            with self.assertRaises(RuntimeError):email_auth.redeem_magic(self.store,login['token'],self.browser)
        self.assertIsNone(self.row(login['token'])['used_at'])
        session=email_auth.redeem_magic(self.store,login['token'],self.browser)['session']
        reset=self.issue('chief@example.com','reset');proof=email_auth.redeem_magic(self.store,reset['token'],self.browser)['proof']
        with patch.object(members,'log',side_effect=RuntimeError('audit failure')):
            with self.assertRaises(RuntimeError):email_auth.reset_password(self.store,proof,self.new_password,self.browser)
        self.assertIsNotNone(self.store.user_for_token(session))
        self.assertIsNotNone(self.store.authenticate(self.owner['username'],self.password))
        email_auth.reset_password(self.store,proof,self.new_password,self.browser)

    def test_failed_delivery_explicitly_invalidates_its_code_and_magic(self):
        link=self.member_link();delivery=self.issue(invite_code=link['code'])
        email_auth.revoke_delivery(self.store,delivery['token'])
        with self.assertRaises(ValueError):self.register(delivery,link['code'])
        with self.assertRaises(ValueError):email_auth.redeem_magic(self.store,delivery['token'],self.browser)

    def test_backup_and_schema_reopen_preserve_bound_proof_without_plaintext(self):
        legacy_path=Path(self.tmp.name)/'legacy.db'
        with closing(sqlite3.connect(legacy_path)) as db:
            db.execute('''CREATE TABLE email_tokens (
                token_hash TEXT PRIMARY KEY,email TEXT NOT NULL,purpose TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,session_key TEXT,code_hash TEXT,
                expires_at TEXT NOT NULL,used_at TEXT,created_at TEXT NOT NULL)''')
            db.execute("INSERT INTO email_tokens(token_hash,email,purpose,expires_at,created_at) VALUES ('legacy-hash','old@example.com','register','2000','1999')")
            db.commit()
        migrated=Store(legacy_path)
        with migrated.connect() as db:
            old=db.execute("SELECT * FROM email_tokens WHERE token_hash='legacy-hash'").fetchone()
            self.assertEqual((old['email'],old['created_at']),('old@example.com','1999'))
            self.assertIsNone(old['session_key']);self.assertIsNone(old['proof_hash']);self.assertIsNone(old['magic_used_at'])
        with self.assertRaises(ValueError):email_auth.redeem_magic(migrated,'legacy-hash',self.browser)
        link=self.member_link();delivery=self.issue(invite_code=link['code'])
        proof=email_auth.redeem_magic(self.store,delivery['token'],self.browser)['proof']
        target=Path(self.tmp.name)/'restored.db'
        with self.store.connect() as source,closing(sqlite3.connect(target)) as destination:source.backup(destination)
        self.store=Store(target)
        with self.store.connect() as db:dump='\n'.join(db.iterdump())
        self.assertNotIn(proof,dump);self.assertNotIn(self.browser,dump)
        with self.assertRaises(ValueError):self.register(delivery,proof=proof,browser=self.other)
        self.register(delivery,proof=proof)
