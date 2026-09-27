"""H01 contract tests. Synthetic adapters are not real IM integrations."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import tempfile
from threading import Barrier, Thread
import time
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

from fastapi.testclient import TestClient
from src import engine, loader
from webapp import app as app_module
from webapp.access import AccessDenied
from webapp.notifications import (ChannelAdapter, DeliveryError, NotificationWorker,
    Recipient, channel_registry, deliver_pending, frozen_payload, migrate_deliveries,
    whitelisted_summary)
from webapp.storage import Store

ROOT = Path(__file__).resolve().parents[1]


class NotificationChannelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dataset = loader.load(ROOT / 'samples' / '样例企业-审计材料.xlsx')
        cls.findings = engine.run(engine.load_rules(ROOT / 'rules'), cls.dataset)

    def setUp(self):
        self.env = patch.dict('os.environ', {'TAXPEARLS_NOTIFICATION_EMAIL_ENABLED':'0', 'TAXPEARLS_AI_ENABLED':'0'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.tmp = tempfile.TemporaryDirectory(prefix='taxpearls-channels-')
        self.addCleanup(self.tmp.cleanup)
        self.enabled = True
        self.sent = []
        self.rendered = []
        self.errors = {}
        self.adapters = {name:self.adapter(name) for name in ('alpha_bot','beta_bot')}
        self.store = Store(Path(self.tmp.name)/'channels.db', notification_adapters=self.adapters)
        self.password = 'Notify-channel-2026!'
        self.admin = self.store.create_user('channeladmin',self.password,'管理员','org_admin','org-a','admin@example.test')
        self.user = self.store.create_user('channelacct',self.password,'会计','accountant','org-a','acct@example.test')
        self.other = self.store.create_user('channelother',self.password,'其他机构','org_admin','org-b','other@example.test')
        self.client = self.store.upsert_client(self.admin,self.dataset.company.name,self.dataset.company.taxpayer_id,self.user['id'])
        self.store.set_notification_preferences(self.user,self.user['id'],True,False,True)
        # Test-only fixture: production identity verification belongs to H04.
        with self.store.connect() as db:
            db.execute('CREATE TABLE test_notification_routes(user_id TEXT,channel TEXT,recipient TEXT,revision TEXT,enabled INTEGER)')
            db.executemany('INSERT INTO test_notification_routes VALUES (?,?,?,?,1)',
                [(self.user['id'],name,name+'-private-target','generation-1') for name in self.adapters])

    def adapter(self, name):
        def recipient(db,user):
            row = db.execute('SELECT * FROM test_notification_routes WHERE user_id=? AND channel=? AND enabled=1',
                             (user['id'],name)).fetchone()
            return Recipient(row['recipient'],row['revision']) if row else None
        def render(summary):
            self.rendered.append((name,summary))
            return {'kind':name,'summary':summary}
        def send(recipient,payload,key,timeout):
            self.sent.append((name,recipient,payload,key,timeout))
            if name in self.errors:
                raise self.errors[name]
            return name+'-receipt'
        return ChannelAdapter(name,recipient,render,send,lambda:self.enabled)

    def enqueue(self, audit_id='channel-audit'):
        self.store.save_audit(audit_id,self.admin,self.client['id'],self.dataset,self.findings,{},
                              datetime.now(UTC).isoformat(timespec='seconds'))
        return self.store.list_notifications(self.user)[0]['id']

    def rows(self):
        with self.store.connect() as db:
            return {r['channel']:dict(r) for r in db.execute('SELECT * FROM notification_deliveries')}

    def reopen(self):
        return Store(self.store.path,notification_adapters=self.adapters)

    def test_fanout_one_web_notice_no_recipient_leak_and_default_email_compatibility(self):
        notice_id = self.enqueue()
        notices = self.store.list_notifications(self.user)
        self.assertEqual(len(notices),1)
        self.assertEqual(notices[0]['email_status'],'pending')
        self.assertEqual({r['channel'] for r in notices[0]['deliveries']},set(self.adapters)|{'email'})
        self.assertNotIn('private-target',json.dumps(notices))
        self.assertNotIn('generation-1',json.dumps(notices))
        self.assertEqual(deliver_pending(self.store),2)
        self.assertEqual(self.rows()['email']['status'],'pending')
        emails = []
        self.assertEqual(deliver_pending(self.store,sender=lambda **kw:emails.append(kw) or 'mail-receipt'),1)
        self.assertEqual(emails[0]['idempotency_key'],'audit-notification/'+notice_id)
        for name,target,payload,key,timeout in self.sent:
            self.assertEqual(target,name+'-private-target')
            self.assertEqual(payload['kind'],name)
            self.assertEqual(key,'audit-notification/'+name+'/'+notice_id)
            self.assertEqual(timeout,10)
        self.assertEqual(deliver_pending(self.reopen()),0)

    def test_renderers_only_receive_scalar_whitelist(self):
        self.enqueue()
        raw = self.rendered[0][1]
        summary = dict(raw,company='private-company',dataset={'secret':123},evidence=['private'])
        summary['risks'] = [dict(item,measured='private',calculation='private') for item in raw['risks']]
        filtered = whitelisted_summary(summary)
        self.assertEqual(filtered,raw)
        self.assertNotIn(self.dataset.company.taxpayer_id,json.dumps(filtered,ensure_ascii=False))
        for bad in [dict(summary,hit_count=True),dict(summary,audit_id={'secret':'value'}),
                    dict(summary,risks=[{'id':'R-1','name':{'secret':'value'},'severity':'high'}])]:
            with self.assertRaises(ValueError):whitelisted_summary(bad)
        for payload in [[],{'x':float('nan')},{'x':'a'*(128*1024)}]:
            with self.assertRaises(ValueError):
                frozen_payload(replace(self.adapters['alpha_bot'],render=lambda s:payload),summary)

    def test_cross_store_concurrent_claims_and_channel_scoped_receipts(self):
        self.enqueue()
        other = self.reopen()
        with ThreadPoolExecutor(max_workers=6) as pool:
            claims = list(pool.map(lambda i:(self.store if i%2 else other).claim_notification_delivery(),range(6)))
        claims = [c for c in claims if c]
        self.assertEqual(len(claims),3)
        self.assertEqual(len({(c['notification_id'],c['channel']) for c in claims}),3)
        for claim in claims:
            wrong = 'email' if claim['channel']!='email' else 'alpha_bot'
            self.assertFalse(self.store.finish_notification_delivery(claim['notification_id'],claim['claim_token'],'accepted','wrong',channel=wrong))
            self.assertTrue(self.store.finish_notification_delivery(claim['notification_id'],claim['claim_token'],'accepted','right',channel=claim['channel']))
        self.assertEqual({r['attempts'] for r in self.rows().values()},{1})

    def test_failed_channel_retry_preserves_payload_and_key_independently(self):
        notice_id = self.enqueue()
        self.errors['alpha_bot'] = DeliveryError(rejected=True)
        deliver_pending(self.store)
        self.assertEqual(self.rows()['alpha_bot']['status'],'failed')
        self.assertEqual(self.rows()['beta_bot']['status'],'accepted')
        original = self.sent[0]
        self.store.retry_notification_delivery(self.user,notice_id,channel='alpha_bot')
        self.adapters['alpha_bot'] = replace(self.adapters['alpha_bot'],render=lambda s: {'changed':True})
        deliver_pending(self.reopen())
        self.assertEqual(self.sent[-1],original)
        self.assertEqual(self.rows()['beta_bot']['attempts'],1)
        self.assertEqual(self.rows()['email']['attempts'],0)

    def test_disabled_channels_remain_pending(self):
        self.enqueue()
        self.enabled = False
        self.assertEqual(deliver_pending(self.store),0)
        self.assertEqual({r['status'] for r in self.rows().values()},{'pending'})
        self.assertIsNone(self.store.claim_notification_delivery(channels=[]))

    def test_changed_target_generation_or_consent_suppresses_only_affected_channel(self):
        for field,value in [('recipient','new-target'),('revision','generation-2'),('enabled',0)]:
            with self.subTest(field=field):
                self.enqueue('change-'+field)
                with self.store.connect() as db:
                    db.execute(f'UPDATE test_notification_routes SET {field}=? WHERE channel=?',(value,'alpha_bot'))
                claim = self.store.claim_notification_delivery(channels=['alpha_bot'])
                self.assertIsNone(claim)
                with self.store.connect() as db:
                    db.execute("UPDATE test_notification_routes SET recipient='alpha_bot-private-target',revision='generation-1',enabled=1 WHERE channel='alpha_bot'")
        self.assertEqual(deliver_pending(self.store),3)
        self.assertEqual({entry[0] for entry in self.sent},{'beta_bot'})

    def test_email_unsubscribe_does_not_cancel_bots_event_unsubscribe_cancels_all(self):
        self.enqueue()
        self.store.set_notification_preferences(self.user,self.user['id'],True,False,False)
        self.assertEqual(self.rows()['email']['status'],'suppressed')
        self.assertEqual(self.rows()['alpha_bot']['status'],'pending')
        self.store.set_notification_preferences(self.user,self.user['id'],False,False,False)
        self.assertEqual({r['status'] for r in self.rows().values()},{'suppressed'})
        self.assertEqual(deliver_pending(self.store),0)

    def test_retry_rechecks_binding_consent(self):
        notice_id = self.enqueue()
        self.errors['alpha_bot'] = DeliveryError(rejected=True)
        deliver_pending(self.store)
        with self.store.connect() as db:
            db.execute("UPDATE test_notification_routes SET enabled=0 WHERE channel='alpha_bot'")
        self.assertTrue(self.store.retry_notification_delivery(self.user,notice_id,channel='alpha_bot'))
        self.assertEqual(deliver_pending(self.store),0)
        self.assertEqual(self.rows()['alpha_bot']['status'],'suppressed')

    def test_revoked_customer_access_suppresses_all_channels_and_hides_notice(self):
        notice_id = self.enqueue()
        with self.store.connect() as db:
            db.execute('UPDATE clients SET accountant_id=NULL WHERE id=?',(self.client['id'],))
        self.assertEqual(self.store.list_notifications(self.user),[])
        self.assertFalse(self.store.retry_notification_delivery(self.user,notice_id,channel='alpha_bot'))
        self.assertIsNone(self.store.claim_notification_delivery())
        self.assertEqual({r['status'] for r in self.rows().values()},{'suppressed'})

    def test_inactive_actor_cannot_read_retry_or_receive(self):
        notice_id = self.enqueue()
        with self.store.connect() as db:
            db.execute('UPDATE users SET active=0 WHERE id=?',(self.user['id'],))
        with self.assertRaises(AccessDenied):self.store.list_notifications(self.user)
        with self.assertRaises(AccessDenied):self.store.retry_notification_delivery(self.user,notice_id,channel='alpha_bot')
        self.assertIsNone(self.store.claim_notification_delivery())

    def test_unknown_channel_never_falls_back_to_email(self):
        self.enqueue()
        with self.store.connect() as db:
            db.execute("UPDATE notification_deliveries SET channel='removed_adapter' WHERE channel='alpha_bot'")
        self.assertEqual(deliver_pending(self.store),1)
        self.assertEqual(self.rows()['removed_adapter']['status'],'pending')
        with self.assertRaises(ValueError):self.store.claim_notification_delivery(channels=['removed_adapter'])
        for name,adapter in [('email',self.adapters['alpha_bot']),('Alpha!',self.adapters['alpha_bot']),('alpha_bot',None)]:
            with self.assertRaises(ValueError):channel_registry({name:adapter})
        with self.assertRaises(TypeError):self.store.notification_adapters['new']=self.adapters['alpha_bot']

    def test_invalid_external_recipient_rolls_back_whole_audit(self):
        with self.store.connect() as db:
            db.execute("UPDATE test_notification_routes SET revision='' WHERE channel='beta_bot'")
        with self.assertRaises(ValueError):self.enqueue()
        with self.store.connect() as db:
            for table in ['audits','notifications','notification_deliveries']:
                self.assertEqual(db.execute('SELECT count(*) FROM '+table).fetchone()[0],0)

    def test_invalid_binding_after_enqueue_does_not_block_other_channels(self):
        self.enqueue()
        with self.store.connect() as db:
            db.execute("UPDATE test_notification_routes SET revision='' WHERE channel='alpha_bot'")
        self.assertEqual(deliver_pending(self.store),1)
        self.assertEqual(self.rows()['alpha_bot']['status'],'failed')
        self.assertEqual(self.rows()['alpha_bot']['error_code'],'invalid_recipient')
        self.assertEqual(self.rows()['alpha_bot']['attempts'],0)
        self.assertEqual(self.rows()['beta_bot']['status'],'accepted')
        self.assertEqual(deliver_pending(self.store,sender=lambda **kw:'mail-receipt'),1)

    def test_binding_database_failure_is_not_misclassified_as_invalid_recipient(self):
        self.enqueue()
        def unavailable(db,user):
            raise sqlite3.OperationalError('synthetic database unavailable')
        self.adapters['alpha_bot'] = replace(self.adapters['alpha_bot'],recipient=unavailable)
        with self.assertRaises(sqlite3.OperationalError):deliver_pending(self.reopen())
        self.assertEqual(self.sent,[])
        self.assertEqual({r['status'] for r in self.rows().values()},{'pending'})

    def test_malformed_payload_and_summary_do_not_stall_other_channels_or_audits(self):
        self.enqueue()
        with self.store.connect() as db:
            db.execute("UPDATE notification_deliveries SET payload_json='[]' WHERE channel='alpha_bot'")
        self.assertEqual(deliver_pending(self.store),1)
        self.assertEqual(self.rows()['alpha_bot']['error_code'],'invalid_payload')
        self.enqueue('broken-summary')
        with self.store.connect() as db:
            db.execute("UPDATE notifications SET summary_json='not-json' WHERE audit_id='broken-summary'")
        self.assertEqual(deliver_pending(self.store),0)
        with self.store.connect() as db:
            statuses = db.execute("SELECT d.status FROM notification_deliveries d JOIN notifications n ON n.id=d.notification_id WHERE n.audit_id='broken-summary' AND channel!='email'").fetchall()
        self.assertEqual([r[0] for r in statuses],['failed','failed'])

    def test_unknown_send_outcome_redacts_error_and_never_auto_retries(self):
        self.enqueue()
        self.errors['alpha_bot'] = RuntimeError('SECRET-provider-token-and-private-body')
        self.assertEqual(deliver_pending(self.store),2)
        self.assertEqual(self.rows()['alpha_bot']['status'],'uncertain')
        self.assertEqual(self.rows()['alpha_bot']['error_code'],'send_unknown')
        self.assertNotIn('SECRET',json.dumps(self.rows()))
        self.assertEqual(deliver_pending(self.reopen()),0)

    def test_non_email_retry_attempt_and_age_limits(self):
        notice_id = self.enqueue()
        self.errors['alpha_bot'] = DeliveryError()
        for attempt in range(3):
            deliver_pending(self.store)
            if attempt<2:self.store.retry_notification_delivery(self.user,notice_id,channel='alpha_bot')
        with self.assertRaisesRegex(ValueError,'23 小时'):
            self.store.retry_notification_delivery(self.user,notice_id,channel='alpha_bot')
        with self.store.connect() as db:
            db.execute("UPDATE notification_deliveries SET attempts=1,created_at='2020-01-01' WHERE channel='alpha_bot'")
        with self.assertRaisesRegex(ValueError,'23 小时'):
            self.store.retry_notification_delivery(self.user,notice_id,channel='alpha_bot')

    def test_backup_restore_claimed_becomes_uncertain_accepted_never_resends(self):
        notice_id = self.enqueue()
        claim = self.store.claim_notification_delivery(channels=['alpha_bot'])
        deliver_pending(self.store)
        with self.store.connect() as db:
            db.execute("UPDATE notification_deliveries SET claimed_at='2020-01-01' WHERE channel='alpha_bot'")
        with self.store.connect() as db:
            with closing(sqlite3.connect(Path(self.tmp.name)/'restored.db')) as dest:db.backup(dest)
        restored = Store(Path(self.tmp.name)/'restored.db',notification_adapters=self.adapters)
        self.assertEqual(restored.recover_notification_claims(),1)
        self.assertEqual(deliver_pending(restored),0)
        restored.retry_notification_delivery(self.user,notice_id,channel='alpha_bot')
        self.assertEqual(deliver_pending(restored),1)
        self.assertEqual(self.sent[-1][2],claim['payload'])
        self.assertEqual(self.sent[-1][3],'audit-notification/alpha_bot/'+notice_id)

    def test_api_channel_retry_ownership_and_legacy_response(self):
        notice_id = self.enqueue()
        self.enabled = False
        with self.store.connect() as db:db.execute("UPDATE notification_deliveries SET status='failed'")
        with patch.object(app_module,'store',self.store), TestClient(app_module.app) as client:
            client.cookies.set(app_module.COOKIE_NAME,self.store.authenticate(self.user['username'],self.password)[1])
            self.assertEqual(len(client.get('/api/notifications').json()),1)
            result = client.post('/api/notifications/'+notice_id+'/retry?channel=alpha_bot')
            self.assertEqual(result.status_code,200,result.text)
            self.assertEqual(result.json()['channel'],'alpha_bot')
            self.assertNotIn('email_status',result.json())
            self.assertEqual(client.post('/api/notifications/'+notice_id+'/retry').json()['email_status'],'pending')
            self.assertEqual(client.post('/api/notifications/'+notice_id+'/retry?channel=unknown').status_code,409)
            client.cookies.set(app_module.COOKIE_NAME,self.store.authenticate(self.other['username'],self.password)[1])
            self.assertEqual(client.get('/api/notifications').json(),[])
            self.assertEqual(client.post('/api/notifications/'+notice_id+'/retry?channel=beta_bot').status_code,404)

    def test_non_email_worker_local_http_restart_no_duplicate(self):
        received = []
        class Sink(BaseHTTPRequestHandler):
            def do_POST(inner):
                received.append((inner.headers['Idempotency-Key'],json.loads(inner.rfile.read(int(inner.headers['Content-Length'])))))
                inner.send_response(200)
                inner.end_headers()
                inner.wfile.write(b'{"id":"local-receipt"}')
            def log_message(self,*args):pass
        sink = ThreadingHTTPServer(('127.0.0.1',0),Sink)
        thread = Thread(target=sink.serve_forever,daemon=True)
        thread.start()
        def send(recipient,payload,key,timeout):
            request = Request(f'http://127.0.0.1:{sink.server_port}/test-channel',
                data=json.dumps({'recipient':recipient,'payload':payload}).encode(),
                headers={'Content-Type':'application/json','Idempotency-Key':key},method='POST')
            with urlopen(request,timeout=timeout) as response:return json.load(response)['id']
        try:
            self.enqueue()
            self.adapters['alpha_bot'] = replace(self.adapters['alpha_bot'],send=send)
            self.adapters['beta_bot'] = replace(self.adapters['beta_bot'],enabled=lambda:False)
            for _ in range(2):
                restored = self.reopen()
                worker = NotificationWorker(lambda:restored,interval=.01)
                worker.start()
                try:
                    deadline = time.monotonic()+5
                    while self.rows()['alpha_bot']['status']!='accepted' and time.monotonic()<deadline:time.sleep(.02)
                    self.assertEqual(self.rows()['alpha_bot']['status'],'accepted')
                finally:worker.stop()
            self.assertEqual(len(received),1)
            self.assertEqual(received[0][1]['recipient'],'alpha_bot-private-target')
            self.assertEqual(self.rows()['email']['status'],'pending')
        finally:
            sink.shutdown()
            sink.server_close()
            thread.join(timeout=2)


class NotificationChannelMigrationTests(unittest.TestCase):
    def legacy(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.execute('''CREATE TABLE notification_deliveries (
            notification_id TEXT PRIMARY KEY,recipient_email TEXT NOT NULL,status TEXT,
            attempts INTEGER,claim_token TEXT,provider_id TEXT,error_code TEXT,claimed_at TEXT,
            payload_json TEXT,created_at TEXT,updated_at TEXT)''')
        db.execute('CREATE INDEX idx_notification_deliveries_status ON notification_deliveries(status,created_at)')
        for i,status in enumerate(['pending','claimed','accepted','failed','uncertain','suppressed']):
            db.execute('INSERT INTO notification_deliveries VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (str(i),'legacy@example.test',status,i,'claim-'+str(i),'receipt-'+str(i),'original',
                 '2026-09-27',json.dumps({'frozen':i}),'2026-09-26','2026-09-27'))
        db.commit()
        return db

    def test_all_legacy_states_claims_and_frozen_payloads_preserved(self):
        db = self.legacy()
        old = [dict(r) for r in db.execute('SELECT * FROM notification_deliveries ORDER BY notification_id')]
        migrate_deliveries(db)
        migrate_deliveries(db)
        new = [dict(r) for r in db.execute('SELECT * FROM notification_deliveries ORDER BY notification_id')]
        for before,after in zip(old,new):
            self.assertEqual(before,{key:after[key] for key in before})
            self.assertEqual(after['channel'],'email')
            self.assertEqual(after['recipient_key'],before['recipient_email'])
            self.assertEqual(after['recipient_revision'],'')
        db.execute("INSERT INTO notification_deliveries(notification_id,channel,created_at,updated_at) VALUES ('0','alpha_bot','now','now')")
        self.assertEqual(db.execute('SELECT count(*) FROM notification_deliveries').fetchone()[0],7)

    def test_copy_failure_rolls_back_schema_and_all_data(self):
        db = self.legacy()
        old = list(db.execute('SELECT * FROM notification_deliveries'))
        class FailingCopy:
            def execute(self,sql,*args):
                if 'INSERT INTO notification_deliveries' in sql:raise sqlite3.OperationalError('synthetic-copy-failure')
                return db.execute(sql,*args)
        with self.assertRaises(sqlite3.OperationalError):migrate_deliveries(FailingCopy())
        self.assertEqual(list(db.execute('SELECT * FROM notification_deliveries')),old)
        self.assertNotIn('channel',{r['name'] for r in db.execute('PRAGMA table_info(notification_deliveries)')})
        self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='notification_deliveries_h01_old'").fetchone())
        migrate_deliveries(db)
        self.assertEqual(db.execute('SELECT count(*) FROM notification_deliveries').fetchone()[0],6)

    def test_concurrent_migration_checks_schema_under_write_lock(self):
        legacy = self.legacy()
        with tempfile.TemporaryDirectory(prefix='taxpearls-channel-migrate-') as tmp:
            path = Path(tmp)/'concurrent.db'
            with closing(sqlite3.connect(path)) as target:legacy.backup(target)
            ready = Barrier(2)
            def migrate_and_enqueue(_):
                with closing(sqlite3.connect(path,timeout=10)) as db:
                    db.row_factory = sqlite3.Row
                    ready.wait(timeout=5)
                    migrate_deliveries(db)
                    db.execute("INSERT OR IGNORE INTO notification_deliveries(notification_id,channel,created_at,updated_at) VALUES ('0','alpha_bot','now','now')")
                    db.commit()
            with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(migrate_and_enqueue,range(2)))
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM notification_deliveries').fetchone()[0],7)
                self.assertEqual(db.execute("SELECT status FROM notification_deliveries WHERE notification_id='2' AND channel='email'").fetchone()[0],'accepted')
