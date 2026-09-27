"""Controlled original storage is a foundation, not upload-flow acceptance."""
import base64
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from webapp import material_batches as batches
from webapp.access import AccessDenied
from webapp.storage import Store
from scripts.ops_db import create_encrypted_backup, restore_encrypted_backup


class MaterialBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {'TAXPEARLS_MATERIAL_KEY': base64.b64encode(b'x' * 32).decode()})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = Store(Path(self.tmp.name) / 'materials.db')
        self.admin = self.person('admin', 'org_admin')
        self.accountant = self.person('accountant', 'accountant')
        self.other = self.person('other-accountant', 'accountant')
        self.foreign = self.person('foreign', 'org_admin', 'foreign-org')
        self.teacher = self.person('teacher', 'teacher')
        self.platform = self.person('platform', 'platform_admin')
        self.raw = b'PRIVATE-FINANCIAL-ORIGINAL-2026'

    def person(self, name, role, org='batch-org'):
        return self.store.create_user(name, 'Material-batch-2026!', name, role, org)

    def create(self, actor=None):
        return batches.create(self.store, actor or self.accountant, [('secret-customer.xlsx', self.raw)])

    def counts(self):
        with self.store.connect() as db:
            return [db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] for table in
                    ('material_batches', 'material_originals', 'material_revisions', 'material_events', 'audit_log')]

    def test_encrypted_original_versions_and_restart(self):
        item = self.create()
        bid, fid = item['id'], item['files'][0]['id']
        payload = {'old': 'sensitive-old-value', 'new': 'sensitive-new-value', 'source': 'sheet1!A3'}
        self.assertEqual(batches.append_revision(self.store, self.accountant, bid, 0, 'edit', payload), 1)
        with self.store.connect() as db:
            for table in ('material_originals', 'material_revisions', 'material_events', 'audit_log'):
                for row in db.execute('SELECT * FROM ' + table):
                    self.assertNotIn('sensitive-old-value', str(tuple(row)))
                    self.assertNotIn('secret-customer.xlsx', str(tuple(row)))
                    self.assertNotIn(self.raw.decode(), str(tuple(row)))
        restored = Store(self.store.path)
        view = batches.read(restored, self.admin, bid)
        self.assertEqual(view['versions'][0]['payload'], payload)
        metadata, raw = batches.original(restored, self.accountant, bid, fid)
        self.assertEqual(raw, self.raw)
        self.assertEqual(metadata['name'], 'secret-customer.xlsx')
        events = batches.read(restored, self.admin, bid)['events']
        self.assertEqual([e['action'] for e in events], ['upload', 'edit', 'view', 'download'])

    def test_scope_role_and_live_revocation(self):
        item = self.create()
        for actor in (self.other, self.foreign, self.teacher, self.platform):
            with self.subTest(actor=actor['username']), self.assertRaises(AccessDenied):
                batches.read(self.store, actor, item['id'])
        for column, value in [('active', 0), ('org_id', 'changed'), ('role', 'student')]:
            with self.store.connect() as db:
                db.execute('UPDATE users SET ' + column + '=? WHERE id=?', (value, self.accountant['id']))
            with self.subTest(column=column), self.assertRaises(AccessDenied):
                batches.original(self.store, self.accountant, item['id'], item['files'][0]['id'])
            with self.store.connect() as db:
                db.execute("UPDATE users SET active=1,org_id='batch-org',role='accountant' WHERE id=?", (self.accountant['id'],))

    def test_missing_invalid_and_wrong_key_fail_closed(self):
        for key in ('', 'not-base64', base64.b64encode(b'x' * 16).decode()):
            before = self.counts()
            with patch.dict(os.environ, {'TAXPEARLS_MATERIAL_KEY': key}), self.assertRaises(batches.MaterialStorageError):
                self.create()
            self.assertEqual(self.counts(), before)
        item = self.create()
        with patch.dict(os.environ, {'TAXPEARLS_MATERIAL_KEY': base64.b64encode(b'y' * 32).decode()}):
            with self.assertRaises(batches.MaterialStorageError):
                batches.original(self.store, self.admin, item['id'], item['files'][0]['id'])

    def test_object_substitution_and_tampering_rejected(self):
        one, two = self.create(), self.create()
        with self.store.connect() as db:
            cipher = db.execute('SELECT bytes_cipher FROM material_originals WHERE id=?', (one['files'][0]['id'],)).fetchone()[0]
            db.execute('UPDATE material_originals SET bytes_cipher=? WHERE id=?', (cipher, two['files'][0]['id']))
        with self.assertRaises(batches.MaterialStorageError):
            batches.original(self.store, self.admin, two['id'], two['files'][0]['id'])
        batches.append_revision(self.store, self.admin, one['id'], 0, 'analysis', {'test': 'value'})
        with self.store.connect() as db:
            db.execute("UPDATE material_revisions SET payload_cipher=x'010203' WHERE batch_id=?", (one['id'],))
        with self.assertRaises(batches.MaterialStorageError):
            batches.read(self.store, self.admin, one['id'])

    def test_revision_compare_and_swap_across_store_instances(self):
        item = self.create()
        batches.append_revision(self.store, self.admin, item['id'], 0, 'analysis', {'inputs': {}})
        second = Store(self.store.path)
        with self.assertRaises(AccessDenied) as raised:
            batches.append_revision(second, self.admin, item['id'], 0, 'edit', {'inputs': {}})
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(batches.read(self.store, self.admin, item['id'])['revision'], 1)
        for bad in (float('nan'), float('inf')):
            with self.assertRaises(batches.MaterialStorageError):
                batches.append_revision(second, self.admin, item['id'], 1, 'edit', {'value': bad})

    def test_revision_kind_and_event_action_are_authenticated(self):
        item = self.create()
        batches.append_revision(self.store, self.admin, item['id'], 0, 'edit', {'value': 10})
        with self.store.connect() as db:
            db.execute("UPDATE material_revisions SET kind='confirmation' WHERE batch_id=?", (item['id'],))
        with self.assertRaises(batches.MaterialStorageError):
            batches.read(self.store, self.admin, item['id'])
        with self.store.connect() as db:
            db.execute("UPDATE material_revisions SET kind='edit' WHERE batch_id=?", (item['id'],))
            db.execute("UPDATE material_events SET action='confirmation' WHERE batch_id=? AND action='edit'", (item['id'],))
        with self.assertRaises(batches.MaterialStorageError):
            batches.read(self.store, self.admin, item['id'])

    def test_delete_only_admin_invalidates_confirmation_retains_evidence(self):
        item = self.create()
        bid, fid = item['id'], item['files'][0]['id']
        batches.append_revision(self.store, self.admin, bid, 0, 'confirmation', {'company': 'confirmed'})
        impact = batches.deletion_impact(self.store, self.admin, bid, fid)
        self.assertEqual(impact['versions'], 1)
        for operation in (batches.deletion_impact, batches.delete_original):
            args = (self.store, self.accountant, bid, fid) + ((1,) if operation == batches.delete_original else ())
            with self.assertRaises(AccessDenied):
                operation(*args)
        with self.assertRaises(AccessDenied):
            batches.delete_original(self.store, self.admin, bid, fid, 0)
        self.assertTrue(batches.delete_original(self.store, self.admin, bid, fid, 1))
        self.assertFalse(batches.delete_original(self.store, self.admin, bid, fid, 2))
        view = batches.read(self.store, self.admin, bid)
        self.assertEqual(view['revision'], 2)
        self.assertEqual(view['versions'][0]['payload']['company'], 'confirmed')
        self.assertEqual(view['versions'][-1]['kind'], 'material_change')
        self.assertTrue(view['files'][0]['deleted_at'])
        with self.assertRaises(AccessDenied) as raised:
            batches.original(self.store, self.admin, bid, fid)
        self.assertEqual(raised.exception.status, 410)

    def test_transactional_logs_rollback_upload_revision_read_and_delete(self):
        item = self.create()
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER reject_material_log BEFORE INSERT ON audit_log WHEN NEW.action LIKE 'material_%' BEGIN SELECT RAISE(ABORT,'forced'); END")
        for call in (
            lambda: self.create(),
            lambda: batches.append_revision(self.store, self.admin, item['id'], 0, 'analysis', {}),
            lambda: batches.read(self.store, self.admin, item['id']),
            lambda: batches.original(self.store, self.admin, item['id'], item['files'][0]['id']),
            lambda: batches.delete_original(self.store, self.admin, item['id'], item['files'][0]['id'], 0),
        ):
            before = self.counts()
            with self.assertRaises(sqlite3.IntegrityError):
                call()
            self.assertEqual(self.counts(), before)
        with self.store.connect() as db:
            row = db.execute('SELECT bytes_cipher,deleted_at FROM material_originals WHERE id=?', (item['files'][0]['id'],)).fetchone()
            self.assertIsNotNone(row['bytes_cipher'])
            self.assertIsNone(row['deleted_at'])

    def test_input_limits_and_no_cross_batch_file_access(self):
        for uploads in ([], [('a.xlsx', b'')], [('a.xlsx', b'a' * (batches.MAX_FILE + 1))], [('a.xlsx', b'a')] * 21):
            with self.assertRaises(batches.MaterialStorageError):
                batches.create(self.store, self.admin, uploads)
        one, two = self.create(), self.create()
        with self.assertRaises(AccessDenied):
            batches.original(self.store, self.admin, one['id'], two['files'][0]['id'])

    def test_assigned_client_and_reassignment_immediately_changes_access(self):
        client = self.store.upsert_client(self.admin, '原件客户', 'BATCH-CLIENT', self.accountant['id'])
        item = batches.create(self.store, self.accountant, [('customer.xlsx', self.raw)], client['id'])
        with self.assertRaises(AccessDenied):
            batches.create(self.store, self.other, [('customer.xlsx', self.raw)], client['id'])
        self.store.upsert_client(self.admin, '原件客户', 'BATCH-CLIENT', self.other['id'])
        with self.assertRaises(AccessDenied):
            batches.read(self.store, self.accountant, item['id'])
        self.assertEqual(batches.original(self.store, self.other, item['id'], item['files'][0]['id'])[1], self.raw)

    def test_concurrent_revision_has_exactly_one_winner(self):
        item = self.create()
        second = Store(self.store.path)

        def save(store):
            try:
                return batches.append_revision(store, self.admin, item['id'], 0, 'edit', {'value': 'edited'})
            except AccessDenied as exc:
                return exc.status

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save, [self.store, second]))
        self.assertEqual(sorted(results), [1, 409])
        self.assertEqual(len(batches.read(self.store, self.admin, item['id'])['versions']), 1)

    def test_encrypted_backup_restore_preserves_original_and_version_without_storing_key(self):
        item = self.create()
        batches.append_revision(self.store, self.admin, item['id'], 0, 'analysis', {'source': 'sheet!A3'})
        root = Path(self.tmp.name)
        backup, destination = root / 'materials.tpbackup', root / 'restored.db'
        with patch.dict(os.environ, {'TAXPEARLS_BACKUP_KEY': base64.b64encode(b'b' * 32).decode()}):
            create_encrypted_backup(self.store.path, backup, retention_days=30)
            restore_encrypted_backup(backup, destination, safety_retention_days=30)
        restored = Store(destination)
        self.assertEqual(batches.original(restored, self.admin, item['id'], item['files'][0]['id'])[1], self.raw)
        self.assertEqual(batches.read(restored, self.admin, item['id'])['versions'][0]['payload'], {'source': 'sheet!A3'})
        with patch.dict(os.environ, {'TAXPEARLS_MATERIAL_KEY': ''}), self.assertRaises(batches.MaterialStorageError):
            batches.original(restored, self.admin, item['id'], item['files'][0]['id'])


if __name__ == '__main__':
    unittest.main()
