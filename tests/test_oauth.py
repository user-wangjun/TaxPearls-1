"""G12 storage contracts; no provider network or public OAuth endpoint yet."""
import auth_support
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import base64
import hashlib
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from webapp import email_auth, members, oauth
from webapp.storage import Store


class OAuthStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'oauth.db')
        self.password='Secure-identity-2026!'
        self.owner=self.store.create_user('owner',self.password,'机构甲','org_admin','alpha','owner@example.com')
        self.peer=self.store.create_user('peer',self.password,'机构乙','org_admin','beta','peer@example.com')
        self.session=self.store.authenticate('owner',self.password)[1]
        self.peer_session=self.store.authenticate('peer',self.password)[1]
        self.browser=email_auth.browser_secret();self.email_browser=email_auth.browser_secret()
        self.other=email_auth.browser_secret()

    def proof(self,action='bind',session=None):
        return oauth.issue_management_code(self.store,session or self.session,action,self.email_browser)

    def begin(self,provider='google',purpose='login',session=None,code=None,browser=None,client='client-1'):
        if purpose=='bind' and code is None:
            code=self.proof(session=session)['code']
        return oauth.begin(self.store,provider,client,'https://safe.example/auth/oauth/'+provider,
            browser or self.browser,purpose=purpose,session=session or self.session,
            email_code=code or '',email_browser=self.email_browser)

    def claim(self,flow,provider='google',session=None,store=None):
        return oauth.claim(store or self.store,flow['state'],self.browser,provider,'client-1',session=session or self.session)

    def complete(self,flow,subject='subject-1',provider='google',session=None,store=None):
        return oauth.complete(store or self.store,flow['state'],self.browser,provider,'client-1',subject,session=session or self.session)

    def bind(self,provider='google',subject='subject-1',session=None):
        flow=self.begin(provider,purpose='bind',session=session)
        self.claim(flow,provider,session=session)
        self.complete(flow,subject,provider,session=session)
        return flow

    def test_state_browser_and_pkce_secrets_not_persisted(self):
        flow=self.begin();claimed=self.claim(flow)
        verifier=claimed['code_verifier']
        self.assertEqual(len(flow['state']),43);self.assertEqual(len(verifier),43)
        challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        self.assertEqual(challenge,flow['code_challenge']);self.assertEqual(flow['code_challenge_method'],'S256')
        with self.store.connect() as db:dump='\n'.join(db.iterdump())
        for secret in (self.browser,self.email_browser,flow['state'],verifier,self.session):
            self.assertNotIn(secret,dump)

    def test_state_bound_to_browser_provider_client_and_exact_lifetime(self):
        flow=self.begin()
        for browser,provider,client in [(self.other,'google','client-1'),(self.browser,'github','client-1'),(self.browser,'google','client-2')]:
            with self.assertRaises(ValueError):oauth.claim(self.store,flow['state'],browser,provider,client)
        with self.store.connect() as db:
            expiry=db.execute('SELECT expires_at FROM oauth_flows').fetchone()[0]
        with patch.object(oauth.time,'time',return_value=expiry):
            with self.assertRaises(ValueError):self.claim(flow)
        self.assertEqual(self.claim(flow)['purpose'],'login')

    def test_resend_invalidates_old_state_and_pkce_changes(self):
        old=self.begin();new=self.begin()
        self.assertNotEqual(old['code_challenge'],new['code_challenge'])
        with self.assertRaises(ValueError):self.claim(old)
        self.claim(new)

    def test_claim_is_once_across_stores_and_denial_cannot_burn_other_browser(self):
        flow=self.begin();other_store=Store(self.store.path)
        with self.assertRaises(ValueError):oauth.cancel(self.store,flow['state'],self.other,'google','client-1')
        def attempt(store):
            try:self.claim(flow,store=store);return True
            except ValueError:return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(attempt,[self.store,other_store])).count(True),1)
        oauth.cancel(self.store,flow['state'],self.browser,'google','client-1')
        with self.assertRaises(ValueError):self.complete(flow)
        with self.assertRaises(ValueError):self.claim(flow)

    def test_provider_denial_consumes_pending_state(self):
        flow=self.begin()
        oauth.cancel(self.store,flow['state'],self.browser,'google','client-1')
        with self.assertRaises(ValueError):self.claim(flow)

    def test_bind_requires_live_local_session_and_purpose_specific_email_ownership(self):
        with self.assertRaises(ValueError):self.begin(purpose='bind',session='missing',code='123456')
        login=email_auth.issue(self.store,self.owner['email'],'login',self.email_browser)
        with self.assertRaises(ValueError):self.begin(purpose='bind',code=login['code'])
        proof=self.proof()
        with self.assertRaises(ValueError):email_auth.redeem_magic(self.store,proof['token'],self.email_browser)
        with self.assertRaises(ValueError):email_auth.login_with_code(self.store,self.owner['email'],proof['code'],self.email_browser)
        with self.assertRaises(ValueError):
            oauth.begin(self.store,'google','client-1','https://safe.example/cb',self.browser,purpose='bind',
                        session=self.session,email_code=proof['code'],email_browser=self.other)
        flow=self.begin(purpose='bind',code=proof['code'])
        self.claim(flow);self.complete(flow)
        with self.assertRaises(ValueError):self.begin('github',purpose='bind',code=proof['code'])

    def test_wrong_code_attempts_commit_and_other_browser_does_not_burn_attempts(self):
        delivery=self.proof()
        for _ in range(5):
            with self.assertRaises(ValueError):self.begin(purpose='bind',code='not-a-code')
        with self.assertRaises(ValueError):self.begin(purpose='bind',code=delivery['code'])
        with self.store.connect() as db:
            row=db.execute('SELECT attempts,used_at FROM email_tokens WHERE token_hash=?',(email_auth.digest(delivery['token']),)).fetchone()
            self.assertEqual(row['attempts'],5);self.assertIsNotNone(row['used_at'])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM oauth_flows').fetchone()[0],0)

    def test_binding_rechecks_exact_session_after_network_and_does_not_accept_same_user_new_session(self):
        flow=self.begin(purpose='bind');self.claim(flow)
        new=self.store.authenticate('owner',self.password)[1]
        with self.assertRaises(ValueError):self.complete(flow,session=new)
        with self.store.connect() as db:db.execute('DELETE FROM sessions WHERE token_hash=?',(email_auth.digest(self.session),))
        with self.assertRaises(ValueError):self.complete(flow)
        self.assertEqual(oauth.identities(self.store,new),[])

    def test_binding_rechecks_current_email_and_does_not_accept_other_account(self):
        flow=self.begin(purpose='bind');self.claim(flow)
        with self.assertRaises(ValueError):self.complete(flow,session=self.peer_session)
        with self.store.connect() as db:db.execute('UPDATE users SET email=? WHERE id=?',('changed@example.com',self.owner['id']))
        with self.assertRaises(ValueError):self.complete(flow)
        self.assertEqual(oauth.identities(self.store,self.session),[])

    def test_identity_unique_across_users_and_one_per_provider_without_account_merging(self):
        self.bind()
        competing=self.begin(purpose='bind',session=self.peer_session,browser=self.other)
        oauth.claim(self.store,competing['state'],self.other,'google','client-1',session=self.peer_session)
        with self.assertRaises(ValueError):
            oauth.complete(self.store,competing['state'],self.other,'google','client-1','subject-1',session=self.peer_session)
        with self.assertRaises(ValueError):self.begin(purpose='bind')
        self.assertEqual(oauth.identities(self.store,self.peer_session),[])
        self.assertEqual(self.store.get_user(self.owner['id'])['org_id'],'alpha')
        self.assertEqual(self.store.get_user(self.peer['id'])['org_id'],'beta')
        self.assertEqual(len(self.store.list_users()),2)

    def test_three_providers_map_to_same_local_account_and_identity_list_is_private(self):
        for provider in sorted(oauth.PROVIDERS):
            self.bind(provider)
            flow=self.begin(provider);self.claim(flow,provider)
            result=self.complete(flow,provider=provider)
            self.assertEqual(result['user']['id'],self.owner['id'])
            self.assertEqual(self.store.user_for_token(result['session'])['org_id'],'alpha')
            with self.assertRaises(ValueError):self.complete(flow,provider=provider)
        links=oauth.identities(self.store,self.session)
        self.assertEqual({r['provider'] for r in links},oauth.PROVIDERS)
        self.assertEqual(set(links[0]),{'provider','bound_at'})
        self.assertEqual(oauth.identities(self.store,self.peer_session),[])
        with self.assertRaises(ValueError):oauth.identities(self.store,'missing')

    def test_unbound_subject_never_selects_account_by_matching_email(self):
        flow=self.begin();self.claim(flow)
        with self.assertRaisesRegex(ValueError,'尚未绑定'):self.complete(flow,subject=self.owner['email'])
        self.assertEqual(len(self.store.list_users()),2)
        self.assertEqual(oauth.identities(self.store,self.session),[])

    def test_completion_is_atomic_across_connections(self):
        self.bind();flow=self.begin();self.claim(flow);second=Store(self.store.path)
        def attempt(store):
            try:return self.complete(flow,store=store)['session']
            except ValueError:return None
        with self.store.connect() as db:before=db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(attempt,[self.store,second]))
        self.assertEqual(sum(r is not None for r in results),1)
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0],before+1)

    def test_binding_and_login_log_failures_roll_back_identity_and_session(self):
        flow=self.begin(purpose='bind');self.claim(flow)
        with patch.object(members,'log',side_effect=RuntimeError('test log failure')):
            with self.assertRaises(RuntimeError):self.complete(flow)
        self.assertEqual(oauth.identities(self.store,self.session),[])
        self.complete(flow)
        login=self.begin();self.claim(login)
        with self.store.connect() as db:before=db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]
        with patch.object(members,'log',side_effect=RuntimeError('test log failure')):
            with self.assertRaises(RuntimeError):self.complete(login)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0],before)
            self.assertNotIn('subject-1',str([tuple(r) for r in db.execute('SELECT * FROM audit_log')]))
        self.complete(login)

    def test_unlink_requires_email_ownership_rotates_sessions_and_keeps_email_fallback(self):
        self.bind();flow=self.begin();self.claim(flow)
        old_login=self.complete(flow)['session']
        bind_code=self.proof()['code']
        with self.assertRaises(ValueError):oauth.unlink(self.store,self.session,'google',bind_code,self.email_browser)
        proof=self.proof('unbind')
        with self.assertRaises(ValueError):oauth.unlink(self.store,self.session,'google',proof['code'],self.other)
        result=oauth.unlink(self.store,self.session,'google',proof['code'],self.email_browser)
        self.assertEqual(oauth.identities(self.store,result['session']),[])
        self.assertIsNone(self.store.user_for_token(old_login));self.assertIsNone(self.store.user_for_token(self.session))
        self.assertIsNotNone(self.store.authenticate('owner',self.password))
        email=email_auth.issue(self.store,self.owner['email'],'login',self.email_browser)
        self.assertIsNotNone(email_auth.login_with_code(self.store,email['email'],email['code'],self.email_browser))

    def test_unlink_wrong_attempts_commit_and_audit_failure_preserves_binding_sessions_proof(self):
        self.bind();proof=self.proof('unbind')
        with self.assertRaises(ValueError):oauth.unlink(self.store,self.session,'google','wrong',self.email_browser)
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT attempts FROM email_tokens WHERE token_hash=?',(email_auth.digest(proof['token']),)).fetchone()[0],1)
        with patch.object(members,'log',side_effect=RuntimeError('test log failure')):
            with self.assertRaises(RuntimeError):oauth.unlink(self.store,self.session,'google',proof['code'],self.email_browser)
        self.assertEqual(len(oauth.identities(self.store,self.session)),1)
        self.assertIsNotNone(self.store.user_for_token(self.session))
        oauth.unlink(self.store,self.session,'google',proof['code'],self.email_browser)

    def test_unlink_rebind_does_not_revive_prior_inflight_login(self):
        self.bind();old=self.begin();self.claim(old)
        proof=self.proof('unbind')
        self.session=oauth.unlink(self.store,self.session,'google',proof['code'],self.email_browser)['session']
        self.bind()
        with self.assertRaises(ValueError):self.complete(old)
        fresh=self.begin();self.claim(fresh);self.complete(fresh)

    def test_password_reset_revokes_inflight_login_and_bind_but_keeps_binding(self):
        self.bind();old=self.begin();self.claim(old)
        bind=self.begin('github',purpose='bind');self.claim(bind,'github')
        reset=email_auth.issue(self.store,self.owner['email'],'reset',self.email_browser)
        proof=email_auth.redeem_magic(self.store,reset['token'],self.email_browser)['proof']
        email_auth.reset_password(self.store,proof,'Fresh-identity-2027!',self.email_browser)
        with self.assertRaises(ValueError):self.complete(old)
        with self.assertRaises(ValueError):self.complete(bind,provider='github')
        new=self.begin();self.claim(new);self.assertEqual(self.complete(new)['user']['id'],self.owner['id'])

    def test_bound_password_reset_invalidates_pending_provider_login(self):
        self.bind();old=self.begin();self.claim(old)
        reset=auth_support.reset_proof(self.store,self.owner['email'])
        auth_support.reset_password(self.store,reset,'Fresh-identity-2027!')
        with self.assertRaises(ValueError):self.complete(old)

    def test_member_disable_restore_cannot_revive_old_oauth_requests(self):
        root=self.store.create_user('root',self.password,'平台','platform_admin','platform','chief@example.com')
        # A second admin permits disabling the first admin under member rules.
        self.store.create_user('second',self.password,'备用','org_admin','alpha','second@example.com')
        members.set_quota(self.store,root,'alpha',5,0)
        self.bind();old=self.begin();self.claim(old)
        members.set_active(self.store,root,'alpha',self.owner['id'],False,True)
        with self.assertRaises(ValueError):self.complete(old)
        during_disable=self.begin();self.claim(during_disable)
        members.set_active(self.store,root,'alpha',self.owner['id'],True,False)
        with self.assertRaises(ValueError):self.complete(old)
        with self.assertRaises(ValueError):self.complete(during_disable)
        fresh=self.begin();self.claim(fresh);self.complete(fresh)

    def test_unlink_or_disable_during_exchange_cannot_create_new_session(self):
        self.bind();flow=self.begin();self.claim(flow)
        proof=self.proof('unbind')
        oauth.unlink(self.store,self.session,'google',proof['code'],self.email_browser)
        with self.assertRaises(ValueError):self.complete(flow)

    def test_capacity_expiry_cleanup_and_failed_code_do_not_consume_valid_proof(self):
        self.begin();proof=self.proof()
        with patch.object(oauth,'MAX_FLOWS',1):
            with self.assertRaisesRegex(ValueError,'容量'):self.begin('github',purpose='bind',code=proof['code'])
        with self.store.connect() as db:db.execute('UPDATE oauth_flows SET expires_at=0')
        with patch.object(oauth,'MAX_FLOWS',1):flow=self.begin('github',purpose='bind',code=proof['code'])
        self.claim(flow,'github');self.complete(flow,provider='github')

    def test_invalid_providers_subjects_and_missing_email_fail_closed(self):
        for provider in ['unknown','../google','GOOGLE']:
            with self.assertRaises(ValueError):self.begin(provider)
        self.store.create_user('no-email',self.password,'无邮箱','accountant','alpha')
        session=self.store.authenticate('no-email',self.password)[1]
        with self.assertRaisesRegex(ValueError,'未绑定邮箱'):self.proof(session=session)
        flow=self.begin();self.claim(flow)
        for subject in ['',None,123,True,'x'*513,'bad\nsubject']:
            with self.assertRaises(ValueError):self.complete(flow,subject=subject)

    def test_schema_migration_backup_reopen_preserves_bound_identity_and_pending_state(self):
        self.bind();flow=self.begin()
        target=Path(self.tmp.name)/'restored.db'
        with self.store.connect() as source,closing(sqlite3.connect(target)) as destination:source.backup(destination)
        self.store=Store(target)
        self.assertEqual(len(oauth.identities(self.store,self.session)),1)
        self.claim(flow);result=self.complete(flow)
        self.assertEqual(result['user']['id'],self.owner['id'])
        with self.store.connect() as db:
            dump='\n'.join(db.iterdump())
            self.assertNotIn('subject-1',dump)
            db.execute('DROP TABLE oauth_flows');db.execute('DROP TABLE oauth_identities')
        reopened=Store(target)
        self.assertEqual(len(reopened.list_users()),2)
        self.assertIsNotNone(reopened.user_for_token(self.session))
        self.assertEqual(oauth.identities(reopened,self.session),[])


if __name__=='__main__':unittest.main()
