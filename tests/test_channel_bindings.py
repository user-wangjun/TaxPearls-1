"""H04 local/verified-adapter contract; no real provider identity is claimed."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch

from src import engine, loader
from webapp import channel_bindings as bindings, email_auth, members
from webapp.access import AccessDenied
from webapp.notifications import ChannelAdapter, deliver_pending
from webapp.storage import Store

ROOT = Path(__file__).resolve().parents[1]


class ChannelBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(prefix='taxpearls-binding-')
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict('os.environ',{'TAXPEARLS_AI_ENABLED':'0','TAXPEARLS_NOTIFICATION_EMAIL_ENABLED':'0'})
        env.start()
        self.addCleanup(env.stop)
        self.store = Store(Path(self.tmp.name)/'bindings.db')
        self.password = 'Binding-safe-2026!'
        self.owner = self.person('chief','org_admin','org-a')
        self.user = self.person('worker','accountant','org-a')
        self.other = self.person('outsider','accountant','org-b')
        self.session = self.login(self.user)
        self.other_session = self.login(self.other)
        self.scope = bindings.Scope('feishu','test-app','test-tenant')
        self.second = bindings.Scope('dingtalk','test-app','test-tenant')

    def person(self,name,role,org):
        return self.store.create_user(name,self.password,name,role,org,name+'@example.test')

    def login(self,person):
        return self.store.authenticate(person['username'],self.password)[1]

    def begin(self,session=None,scope=None):
        return bindings.begin(self.store,session or self.session,scope or self.scope)

    def claim(self,flow,subject='subject-a',event='event-a',scope=None):
        return bindings.accept_code(self.store,scope or self.scope,subject,flow['code'],event,private_message=True)

    def bind(self,*,session=None,scope=None,subject='subject-a',event='event-a',notifications=False):
        session = session or self.session
        scope = scope or self.scope
        flow = self.begin(session,scope)
        self.assertTrue(self.claim(flow,subject,event,scope))
        return bindings.confirm(self.store,session,flow['id'],scope,notifications=notifications)

    def test_requires_im_proof_then_original_session_confirmation(self):
        flow = self.begin()
        with self.assertRaises(AccessDenied):bindings.confirm(self.store,self.session,flow['id'],self.scope)
        self.assertTrue(self.claim(flow))
        self.assertEqual(bindings.list_bindings(self.store,self.session),[])
        self.assertIsNone(bindings.actor_for_subject(self.store,self.scope,'subject-a'))
        status = bindings.status(self.store,self.session,flow['id'])
        self.assertEqual(status['status'],'candidate')
        self.assertEqual(len(status['candidate_fingerprint']),12)
        self.assertNotIn('subject-a',json.dumps(status))
        result = bindings.confirm(self.store,self.session,flow['id'],self.scope)
        self.assertFalse(result['notifications_enabled'])
        self.assertEqual(bindings.actor_for_subject(self.store,self.scope,'subject-a')['id'],self.user['id'])
        with self.assertRaises(AccessDenied):bindings.confirm(self.store,self.session,flow['id'],self.scope)

    def test_no_code_or_session_plaintext_and_no_external_identity_in_logs_or_lists(self):
        flow = self.begin()
        self.claim(flow)
        bindings.confirm(self.store,self.session,flow['id'],self.scope)
        with self.store.connect() as db:
            dump = '\n'.join(db.iterdump())
            flow_row = dict(db.execute('SELECT * FROM channel_binding_flows').fetchone())
            logs = json.dumps([dict(r) for r in db.execute('SELECT * FROM audit_log')])
        self.assertNotIn(flow['code'].replace('-',''),dump)
        self.assertNotIn(self.session,dump)
        self.assertIsNone(flow_row['recipient_key'])
        self.assertNotIn('subject-a',logs)
        public = json.dumps(bindings.list_bindings(self.store,self.session))
        self.assertNotIn('subject-a',public)
        self.assertNotIn('scope_key',public)
        self.assertNotIn('revision',public)

    def test_rejects_group_message_wrong_channel_app_tenant_without_consuming_code(self):
        flow = self.begin()
        self.assertFalse(bindings.accept_code(self.store,self.scope,'subject-a',flow['code'],'group',private_message=False))
        for scope in [self.second,bindings.Scope('feishu','wrong-app','test-tenant'),bindings.Scope('feishu','test-app','wrong-tenant')]:
            self.assertFalse(self.claim(flow,scope=scope))
        self.assertEqual(bindings.status(self.store,self.session,flow['id'])['status'],'pending')
        self.assertTrue(self.claim(flow))

    def test_duplicate_event_acknowledged_but_other_sender_or_event_cannot_replace_candidate(self):
        flow = self.begin()
        self.assertTrue(self.claim(flow))
        self.assertTrue(self.claim(flow))
        self.assertFalse(self.claim(flow,subject='attacker'))
        self.assertFalse(self.claim(flow,event='different-event'))
        other = self.begin(self.other_session)
        self.assertFalse(self.claim(other,subject='different-subject'))
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM audit_log WHERE action='channel_binding_candidate'").fetchone()[0],1)

    def test_same_account_different_session_and_other_account_cannot_confirm_or_cancel(self):
        flow = self.begin()
        self.claim(flow)
        for session in [self.login(self.user),self.other_session]:
            for action in [lambda:bindings.status(self.store,session,flow['id']),
                           lambda:bindings.confirm(self.store,session,flow['id'],self.scope),
                           lambda:bindings.cancel(self.store,session,flow['id'])]:
                with self.assertRaises(AccessDenied):action()
        self.assertEqual(bindings.status(self.store,self.session,flow['id'])['status'],'candidate')

    def test_local_account_org_role_and_live_session_rechecked(self):
        flow = self.begin()
        with self.store.connect() as db:db.execute('UPDATE users SET org_id=? WHERE id=?',('moved',self.user['id']))
        self.assertFalse(self.claim(flow))
        with self.assertRaises(AccessDenied):bindings.status(self.store,self.session,flow['id'])
        with self.store.connect() as db:db.execute('UPDATE users SET org_id=?,role=? WHERE id=?',('org-a','teacher',self.user['id']))
        self.assertFalse(self.claim(flow))
        with self.store.connect() as db:
            db.execute('UPDATE users SET role=? WHERE id=?',('accountant',self.user['id']))
            db.execute('DELETE FROM sessions WHERE token_hash=?',(bindings._digest(self.session),))
        self.assertFalse(self.claim(flow))
        with self.assertRaises(AccessDenied):bindings.confirm(self.store,self.session,flow['id'],self.scope)

    def test_expiration_resend_cancel_and_persistent_cooldown(self):
        with patch.object(bindings.time,'time',return_value=1000):
            flow = self.begin()
            with self.assertRaises(AccessDenied):bindings.begin(Store(self.store.path),self.session,self.scope)
        with patch.object(bindings.time,'time',return_value=1031):
            later = self.begin()
            self.assertFalse(self.claim(flow))
            self.assertTrue(self.claim(later))
            bindings.cancel(self.store,self.session,later['id'])
            self.assertFalse(self.claim(later))
        with patch.object(bindings.time,'time',return_value=2000):
            expired = self.begin()
        with patch.object(bindings.time,'time',return_value=2600):
            self.assertFalse(self.claim(expired))
            with self.assertRaises(AccessDenied):bindings.status(self.store,self.session,expired['id'])

    def test_capacity_never_evicts_another_live_flow_and_expiry_recovers(self):
        with patch.object(bindings,'CAPACITY',1),patch.object(bindings.time,'time',return_value=1000):
            flow = self.begin()
            with self.assertRaises(AccessDenied):self.begin(self.other_session)
            self.assertEqual(bindings.status(self.store,self.session,flow['id'])['status'],'pending')
        with patch.object(bindings,'CAPACITY',1),patch.object(bindings.time,'time',return_value=1600):
            self.assertEqual(self.begin(self.other_session)['status'],'pending')

    def test_identity_unique_across_accounts_and_one_binding_per_channel(self):
        self.bind()
        flow = self.begin(self.other_session)
        self.assertFalse(self.claim(flow,event='new-event'))
        with self.assertRaises(AccessDenied):self.begin()
        self.bind(scope=self.second,event='second-event')
        self.assertEqual(len(bindings.list_bindings(self.store,self.session)),2)
        self.assertEqual(bindings.list_bindings(self.store,self.other_session),[])

    def test_different_app_or_tenant_ids_are_not_cross_matched(self):
        self.bind()
        for scope in [self.second,bindings.Scope('feishu','another-app','test-tenant'),bindings.Scope('feishu','test-app','another-tenant')]:
            self.assertIsNone(bindings.actor_for_subject(self.store,scope,'subject-a'))
        foreign_scope = bindings.Scope('feishu','another-app','another-tenant')
        self.bind(session=self.other_session,scope=foreign_scope,event='other-event')
        self.assertEqual(bindings.actor_for_subject(self.store,foreign_scope,'subject-a')['id'],self.other['id'])

    def test_concurrent_confirmation_one_winner_across_stores(self):
        flow = self.begin()
        self.claim(flow)
        stores = [self.store,Store(self.store.path)]
        gate = Barrier(2)
        def confirm(store):
            gate.wait(timeout=5)
            try:return bindings.confirm(store,self.session,flow['id'],self.scope)
            except AccessDenied:return None
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(confirm,stores))
        self.assertEqual(sum(r is not None for r in results),1)

    def test_two_candidate_flows_cannot_bind_same_external_identity(self):
        first = self.begin()
        second = self.begin(self.other_session)
        self.claim(first)
        self.assertTrue(self.claim(second,event='event-b'))
        bindings.confirm(self.store,self.session,first['id'],self.scope)
        with self.assertRaises(AccessDenied):bindings.confirm(self.store,self.other_session,second['id'],self.scope)

    def test_binding_and_subscription_mutations_require_owner_not_org_admin(self):
        result = self.bind()
        for session in [self.other_session,self.login(self.owner)]:
            with self.assertRaises(AccessDenied):bindings.set_notifications(self.store,session,result['id'],True)
            with self.assertRaises(AccessDenied):bindings.unlink(self.store,session,result['id'])
        self.assertEqual(len(bindings.list_bindings(self.store,self.session)),1)

    def test_subscription_generations_change_and_unlink_relink_cannot_revive_old_delivery(self):
        stamp = [1000]
        with patch.object(bindings.time,'time',side_effect=lambda:stamp[0]):
            result = self.bind(notifications=True)
            with self.store.connect() as db:original=bindings.recipient(db,self.user,self.scope)
            bindings.set_notifications(self.store,self.session,result['id'],False)
            with self.store.connect() as db:self.assertIsNone(bindings.recipient(db,self.user,self.scope))
            bindings.set_notifications(self.store,self.session,result['id'],True)
            with self.store.connect() as db:changed=bindings.recipient(db,self.user,self.scope)
            self.assertEqual(original.key,changed.key)
            self.assertNotEqual(original.revision,changed.revision)
            bindings.unlink(self.store,self.session,result['id'])
            self.assertIsNone(bindings.actor_for_subject(self.store,self.scope,'subject-a'))
            stamp[0] += 31
            self.bind(event='fresh-event',notifications=True)
            with self.store.connect() as db:relinked=bindings.recipient(db,self.user,self.scope)
            self.assertNotEqual(relinked.revision,changed.revision)

    def test_student_binding_never_grants_audit_subscription(self):
        student = self.person('learner','student','org-a')
        session = self.login(student)
        flow = self.begin(session)
        self.claim(flow)
        with self.assertRaises(AccessDenied):bindings.confirm(self.store,session,flow['id'],self.scope,notifications=True)
        result = bindings.confirm(self.store,session,flow['id'],self.scope)
        with self.assertRaises(AccessDenied):bindings.set_notifications(self.store,session,result['id'],True)
        with self.store.connect() as db:self.assertIsNone(bindings.recipient(db,student,self.scope))

    def test_audit_failure_rolls_back_begin_candidate_confirm_subscription_and_unlink(self):
        with patch.object(members,'log',side_effect=RuntimeError('log unavailable')):
            with self.assertRaises(RuntimeError):self.begin()
        flow = self.begin()
        with patch.object(members,'log',side_effect=RuntimeError('log unavailable')):
            with self.assertRaises(RuntimeError):self.claim(flow)
        self.assertEqual(bindings.status(self.store,self.session,flow['id'])['status'],'pending')
        self.claim(flow)
        with patch.object(members,'log',side_effect=RuntimeError('log unavailable')):
            with self.assertRaises(RuntimeError):bindings.confirm(self.store,self.session,flow['id'],self.scope)
        self.assertEqual(bindings.list_bindings(self.store,self.session),[])
        result = bindings.confirm(self.store,self.session,flow['id'],self.scope)
        with patch.object(members,'log',side_effect=RuntimeError('log unavailable')):
            with self.assertRaises(RuntimeError):bindings.set_notifications(self.store,self.session,result['id'],True)
            with self.assertRaises(RuntimeError):bindings.unlink(self.store,self.session,result['id'])
        self.assertFalse(bindings.list_bindings(self.store,self.session)[0]['notifications_enabled'])

    def test_member_disable_restore_revokes_flows_sessions_and_opt_in(self):
        self.bind(notifications=True)
        flow = self.begin(scope=self.second)
        self.claim(flow,scope=self.second)
        with self.store.connect() as db:db.execute('INSERT INTO org_quota VALUES (?,?,?,?,?)',('org-a',10,self.owner['id'],members.now(),1))
        members.set_active(self.store,self.owner,'org-a',self.user['id'],False,True)
        self.assertIsNone(bindings.actor_for_subject(self.store,self.scope,'subject-a'))
        members.set_active(self.store,self.owner,'org-a',self.user['id'],True,False)
        self.session = self.login(self.user)
        with self.assertRaises(AccessDenied):bindings.confirm(self.store,self.session,flow['id'],self.second)
        self.assertFalse(bindings.list_bindings(self.store,self.session)[0]['notifications_enabled'])

    def test_password_reset_revokes_pending_and_requires_renewed_notification_consent(self):
        self.bind(notifications=True)
        flow = self.begin(scope=self.second)
        browser = 'B'*43
        delivery = email_auth.issue(self.store,'worker@example.test','reset',browser)
        proof = email_auth.redeem_magic(self.store,delivery['token'],browser)['proof']
        email_auth.reset_password(self.store,proof,'Renewed-safepass-2026!',browser)
        self.assertFalse(self.claim(flow,scope=self.second))
        with self.store.connect() as db:
            self.assertIsNone(bindings.recipient(db,self.user,self.scope))
            self.assertEqual(db.execute('SELECT count(*) FROM channel_bindings').fetchone()[0],1)

    def test_h01_uses_current_verified_binding_consent_and_global_event_subscription(self):
        binding = self.bind()
        self.store.set_notification_preferences(self.user,self.user['id'],True,False,False)
        sent = []
        adapter = ChannelAdapter('feishu',lambda db,user:bindings.recipient(db,user,self.scope),
            lambda summary:{'summary':summary},lambda *args:sent.append(args) or 'synthetic-accepted',lambda:True)
        store = Store(self.store.path,notification_adapters={'feishu':adapter})
        dataset = loader.load(ROOT/'samples'/'样例企业-审计材料.xlsx')
        findings = engine.run(engine.load_rules(ROOT/'rules'),dataset)
        client = store.upsert_client(self.owner,dataset.company.name,dataset.company.taxpayer_id,self.user['id'])
        def audit(name):store.save_audit(name,self.owner,client['id'],dataset,findings,{},datetime.now(UTC).isoformat())
        audit('no-consent')
        self.assertEqual(deliver_pending(store),0)
        bindings.set_notifications(store,self.session,binding['id'],True)
        audit('consented')
        self.assertEqual(deliver_pending(store),1)
        self.assertEqual(sent[0][0],'subject-a')
        audit('cancelled')
        bindings.unlink(store,self.session,binding['id'])
        self.assertEqual(deliver_pending(store),0)
        with store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM notification_deliveries WHERE channel='feishu' AND status='suppressed'").fetchone()[0],1)

    def test_backup_reopen_migration_preserves_binding_pending_flow_and_session(self):
        self.bind()
        flow = self.begin(scope=self.second)
        with self.store.connect() as db:
            with closing(sqlite3.connect(Path(self.tmp.name)/'copy.db')) as dest:db.backup(dest)
        restored = Store(Path(self.tmp.name)/'copy.db')
        self.assertEqual(len(bindings.list_bindings(restored,self.session)),1)
        self.assertTrue(bindings.accept_code(restored,self.second,'subject-b',flow['code'],'event-b',private_message=True))
        bindings.confirm(restored,self.session,flow['id'],self.second)
        self.assertEqual(len(bindings.list_bindings(restored,self.session)),2)

    def test_invalid_inputs_do_not_create_or_consume_proofs(self):
        for scope in [bindings.Scope('email','a','b'),bindings.Scope('feishu','','b'),bindings.Scope('feishu','a','b\n')]:
            with self.assertRaises(ValueError):self.begin(scope=scope)
        flow = self.begin()
        for code in ['',123,'x'*100,flow['code']+'X']:
            self.assertFalse(bindings.accept_code(self.store,self.scope,'subject-a',code,'event-a',private_message=True))
        self.assertTrue(self.claim(flow))
        with self.assertRaises(ValueError):bindings.confirm(self.store,self.session,flow['id'],self.scope,notifications='yes')
