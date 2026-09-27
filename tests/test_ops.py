from __future__ import annotations

import sqlite3
import base64
import os
import hashlib
import json
from datetime import UTC, datetime, timedelta
from io import StringIO
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from scripts.ops_db import (BackupError, create_backup, restore_backup, verify_backup,
                            create_encrypted_backup, verify_encrypted_backup, restore_encrypted_backup,
                            retention_plan, destroy_encrypted_backup, verify_destruction_receipt)
from scripts import ops_db
from webapp.storage import Store


class DatabaseOperationsTest(unittest.TestCase):
    def test_backup_restore_and_rollback_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "taxpearls.db"
            backup = root / "baseline.sqlite3"
            store = Store(database)
            store.create_user("admin", "correct-horse-2026", "管理员", "org_admin", "org-a")

            created = create_backup(database, backup)
            self.assertEqual(created["quick_check"], "ok")
            self.assertEqual(created["table_counts"]["users"], 1)
            self.assertTrue(Path(created["manifest"]).is_file())
            self.assertEqual(verify_backup(backup)["table_counts"]["users"], 1)

            store.create_user("teacher", "teacher-pass-2026", "教师", "teacher", "org-a")
            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM users").fetchone()[0], 2)

            restored = restore_backup(backup, database)
            safety_backup = Path(restored["safety_backup"])
            self.assertTrue(safety_backup.is_file())
            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)

            rolled_back = restore_backup(safety_backup, database)
            self.assertEqual(rolled_back["quick_check"], "ok")
            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM users").fetchone()[0], 2)

    def test_tampered_backup_is_rejected_before_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "taxpearls.db"
            backup = root / "backup.sqlite3"
            Store(database).create_user("admin", "correct-horse-2026", "管理员", "org_admin", "org-a")
            create_backup(database, backup)
            original = database.read_bytes()
            with backup.open("ab") as stream:
                stream.write(b"tampered")
            with self.assertRaisesRegex(BackupError, "SHA-256"):
                restore_backup(backup, database)
            self.assertEqual(database.read_bytes(), original)

    def test_restore_refuses_active_wal_sidecar(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "taxpearls.db"
            backup = root / "backup.sqlite3"
            Store(database).create_user("admin", "correct-horse-2026", "管理员", "org_admin", "org-a")
            create_backup(database, backup)
            Path(f"{database}-wal").write_bytes(b"active")
            with self.assertRaisesRegex(BackupError, "请先停止服务"):
                restore_backup(backup, database)


class EncryptedBackupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.database = self.root / 'tenant.db'
        self.target = self.root / 'snapshot.tpbackup'
        self.key = base64.b64encode(bytes(range(32))).decode('ascii')
        self.env = patch.dict(os.environ, {'TAXPEARLS_BACKUP_KEY': self.key})
        self.env.start()
        Store(self.database).create_user('crypto-admin', 'Encrypted-test-2026!',
                                        'sensitive tenant identity', 'org_admin', 'org-secret')

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def create(self):
        return create_encrypted_backup(self.database, self.target, retention_days=30)

    def test_paths_with_uri_metacharacters_do_not_read_another_database(self):
        folder = self.root / '备份 #100%'
        folder.mkdir()
        database = folder / 'tenant#review.db'
        Store(database).create_user('exact-user', 'Encrypted-test-2026!', '正确文件', 'org_admin', 'org-exact')
        # A URI fragment must never redirect reads to this other valid file.
        Store(folder / 'tenant').create_user('decoy-one', 'Encrypted-test-2026!', '错误文件', 'org_admin', 'decoy')
        encrypted = folder / 'saved#copy.tpbackup'
        plain = folder / 'legacy#copy.sqlite3'
        created = create_encrypted_backup(database, encrypted, retention_days=30)
        self.assertEqual(created['table_counts']['users'], 1)
        create_backup(database, plain)
        self.assertEqual(verify_backup(plain)['table_counts']['users'], 1)
        restored = folder / 'restored#copy.db'
        restore_encrypted_backup(encrypted, restored, safety_retention_days=30)
        self.assertEqual(Store(restored).list_users()[0]['username'], 'exact-user')
        restore_backup(plain, restored)
        self.assertEqual(Store(restored).list_users()[0]['username'], 'exact-user')

    def test_authenticated_backup_from_wal_has_no_plaintext_artifact(self):
        created = self.create()
        self.assertTrue(created['encrypted'])
        self.assertEqual(created['table_counts']['users'], 1)
        self.assertEqual(created, verify_encrypted_backup(self.target))
        blob = self.target.read_bytes()
        for value in (b'SQLite format 3', b'sensitive tenant identity', b'org-secret', self.key.encode()):
            self.assertNotIn(value, blob)
        # The live source may retain its own WAL/SHM; no *backup* plaintext is produced.
        self.assertEqual({p.name for p in self.root.iterdir()} - {'tenant.db-wal', 'tenant.db-shm'},
                         {'tenant.db', 'snapshot.tpbackup'})

    def test_wrong_key_cannot_authenticate_and_error_does_not_expose_key(self):
        self.create()
        wrong = base64.b64encode(b'x' * 32).decode('ascii')
        with patch.dict(os.environ, {'TAXPEARLS_BACKUP_KEY': wrong}), self.assertRaises(BackupError) as error:
            verify_encrypted_backup(self.target)
        self.assertNotIn(wrong, str(error.exception))
        self.assertNotIn(self.key, str(error.exception))

    def test_metadata_and_ciphertext_tampering_both_fail(self):
        self.create()
        original = self.target.read_bytes()
        for position in (25, len(original)-1):
            altered = bytearray(original); altered[position] ^= 1
            self.target.write_bytes(altered)
            with self.assertRaises(BackupError):
                verify_encrypted_backup(self.target)
        self.target.write_bytes(original)
        self.assertEqual(verify_encrypted_backup(self.target)['table_counts']['users'], 1)

    def test_existing_destination_never_overwritten_and_nonce_is_fresh(self):
        self.create(); original = self.target.read_bytes()
        with self.assertRaises(BackupError):
            self.create()
        self.assertEqual(self.target.read_bytes(), original)
        second = self.root / 'second.tpbackup'
        create_encrypted_backup(self.database, second, retention_days=30)
        self.assertNotEqual(second.read_bytes(), original)
        def nonce(blob):
            offset = len(ops_db.ENCRYPTED_MAGIC)
            start = offset + 4 + int.from_bytes(blob[offset:offset+4], 'big')
            return blob[start:start+12]
        self.assertNotEqual(nonce(second.read_bytes()), nonce(original))

    def test_missing_malformed_key_and_invalid_retention_fail_without_files(self):
        for key in ('', 'not-base64', base64.b64encode(b'short').decode()):
            with patch.dict(os.environ, {'TAXPEARLS_BACKUP_KEY': key}), self.assertRaises(BackupError):
                self.create()
        for days in (0, -1, True, 3651, '30'):
            with self.assertRaises(BackupError):
                create_encrypted_backup(self.database, self.target, retention_days=days)
        self.assertFalse(self.target.exists())

    def test_encrypted_restore_and_encrypted_safety_backup_can_roll_back(self):
        created = self.create()
        destination = self.root / 'restored.db'
        restored = restore_encrypted_backup(self.target, destination, safety_retention_days=45)
        self.assertIsNone(restored['safety_backup'])
        self.assertEqual(restored['source_ciphertext_sha256'], created['sha256'])
        self.assertEqual(restored['restored_sha256'], hashlib.sha256(destination.read_bytes()).hexdigest())
        Store(destination).create_user('second-user', 'Second-restore-2026!', '第二用户', 'teacher', 'org-secret')
        restored = restore_encrypted_backup(self.target, destination, safety_retention_days=45)
        self.assertTrue(restored['safety_encrypted'])
        self.assertEqual(restored['table_counts']['users'], 1)
        safety = Path(restored['safety_backup'])
        self.assertEqual(verify_encrypted_backup(safety)['table_counts']['users'], 2)
        self.assertEqual(verify_encrypted_backup(safety)['retention_days'], 45)
        self.assertNotIn(b'second-user', safety.read_bytes())
        self.assertEqual(restore_encrypted_backup(safety, destination, safety_retention_days=45)['table_counts']['users'], 2)
        self.assertFalse(list(self.root.glob('*.sqlite3')))
        self.assertFalse(list(self.root.glob('*.restore')))
        self.assertFalse(list(self.root.glob('.*restore-lock')))

    def test_encrypted_restore_preserves_sessions_and_audit_tenant_permissions(self):
        from src import loader, engine, render
        source = Store(self.database)
        admin = source.list_users('org-secret')[0]
        foreign = source.create_user('crypto-foreign', 'Foreign-restore-2026!', 'foreign', 'org_admin', 'other-org')
        data = loader.load(Path(__file__).resolve().parents[1] / 'samples' / '样例企业-审计材料.xlsx')
        customer = source.upsert_client(admin, data.company.name, data.company.taxpayer_id)
        findings = engine.run(engine.load_rules(ops_db.ROOT / 'rules'), data)
        source.save_audit('encrypted-audit', admin, customer['id'], data, findings,
                          render.build_view_model(data, findings)['summary'], '2026-09-27 12:00:00')
        token = source.authenticate('crypto-admin', 'Encrypted-test-2026!')[1]
        original = source.get_audit('encrypted-audit')
        self.create()
        destination = self.root / 'restored.db'
        restore_encrypted_backup(self.target, destination, safety_retention_days=30)
        recovered = Store(destination)
        self.assertEqual(recovered.user_for_token(token)['id'], admin['id'])
        self.assertEqual(recovered.get_audit_for_user('encrypted-audit', admin), original)
        self.assertIsNone(recovered.get_audit_for_user('encrypted-audit', foreign))

    def test_bad_encrypted_source_never_changes_target_or_creates_safety(self):
        self.create(); sealed = self.target.read_bytes()
        destination = self.root / 'untouched.db'
        Store(destination).create_user('untouched', 'Untouched-restore-2026!', 'keep', 'org_admin', 'keep-org')
        original = destination.read_bytes()
        for content in (b'bad', sealed[:-1], sealed[:-1] + bytes([sealed[-1] ^ 1])):
            self.target.write_bytes(content)
            with self.assertRaises(BackupError):
                restore_encrypted_backup(self.target, destination, safety_retention_days=30)
            self.assertEqual(destination.read_bytes(), original)
        self.target.write_bytes(sealed)
        with patch.dict(os.environ, {'TAXPEARLS_BACKUP_KEY': base64.b64encode(b'x'*32).decode()}), self.assertRaises(BackupError):
            restore_encrypted_backup(self.target, destination, safety_retention_days=30)
        self.assertEqual(destination.read_bytes(), original)
        self.assertFalse(list(self.root.glob('*.pre-restore-*')))

    def test_restore_refuses_live_journal_stale_lock_and_invalid_retention(self):
        self.create()
        destination = self.root / 'restored.db'
        for suffix in ('-wal', '-shm', '-journal'):
            marker = Path(str(destination) + suffix); marker.write_bytes(b'live')
            with self.assertRaises(BackupError):
                restore_encrypted_backup(self.target, destination, safety_retention_days=30)
            marker.unlink()
        lock = self.root / '.restored.db.restore-lock'; lock.write_bytes(b'previous operation')
        with self.assertRaises(BackupError):
            restore_encrypted_backup(self.target, destination, safety_retention_days=30)
        self.assertEqual(lock.read_bytes(), b'previous operation'); lock.unlink()
        with self.assertRaises(BackupError):
            restore_encrypted_backup(self.target, destination, safety_retention_days=0)
        self.assertFalse(destination.exists())

    def test_restore_cancels_if_target_changes_during_safety_snapshot(self):
        self.create()
        destination = self.root / 'restored.db'
        restore_encrypted_backup(self.target, destination, safety_retention_days=30)
        seal = ops_db._seal_snapshot
        def concurrent_write(*args):
            result = seal(*args)
            Store(destination).create_user('late-write', 'Late-write-2026!', 'must survive', 'teacher', 'org-secret')
            return result
        with patch.object(ops_db, '_seal_snapshot', side_effect=concurrent_write), self.assertRaises(BackupError):
            restore_encrypted_backup(self.target, destination, safety_retention_days=30)
        self.assertEqual(len(Store(destination).list_users('org-secret')), 2)
        self.assertFalse(list(self.root.glob('*.restore')))

    def test_failed_restore_publication_keeps_target_and_encrypted_safety(self):
        self.create()
        destination = self.root / 'restored.db'
        restore_encrypted_backup(self.target, destination, safety_retention_days=30)
        original = destination.read_bytes(); replace = ops_db.os.replace
        def fail_publish(source, target):
            if Path(target) == destination:
                raise OSError('simulated disk failure')
            return replace(source, target)
        with patch.object(ops_db.os, 'replace', side_effect=fail_publish), self.assertRaises(OSError):
            restore_encrypted_backup(self.target, destination, safety_retention_days=30)
        self.assertEqual(destination.read_bytes(), original)
        safeties = list(self.root.glob('*.pre-restore-*.tpbackup'))
        self.assertEqual(len(safeties), 1)
        self.assertTrue(verify_encrypted_backup(safeties[0])['encrypted'])
        self.assertFalse(list(self.root.glob('*.restore')))

    def test_retention_uses_authenticated_expiry_not_filename_or_mtime(self):
        created = self.create()
        os.utime(self.target, (0, 0))
        plan = retention_plan(self.root)
        self.assertTrue(plan['dry_run']); self.assertFalse(plan['entries'][0]['eligible'])
        after = datetime.fromisoformat(created['expires_at']) + timedelta(seconds=1)
        self.assertTrue(retention_plan(self.root, now=after)['entries'][0]['eligible'])
        nested = self.root / 'nested'; nested.mkdir()
        (nested / 'hidden.tpbackup').write_bytes(self.target.read_bytes())
        (self.root / 'invalid.tpbackup').write_bytes(b'untrusted backup')
        (self.root / 'unrelated.txt').write_text('keep')
        plan = retention_plan(self.root, now=after)
        self.assertEqual(len(plan['entries']), 1); self.assertEqual(len(plan['rejected']), 1)
        self.assertTrue((nested / 'hidden.tpbackup').exists())
        self.assertTrue((self.root / 'unrelated.txt').exists())

    def test_destroy_defaults_to_preview_and_respects_hold_and_fingerprint(self):
        created = self.create(); after = datetime.fromisoformat(created['expires_at']) + timedelta(seconds=1)
        preview = destroy_encrypted_backup(self.target, self.root, expected_sha256=created['sha256'],
                    reason='ticket-123', now=after)
        self.assertTrue(preview['dry_run']); self.assertFalse(preview['destroyed']); self.assertTrue(self.target.exists())
        receipt = self.root / 'receipt.json'
        for kwargs in ({'expected_sha256': '0'*64, 'now': after},
                       {'expected_sha256': created['sha256'], 'now': datetime.now(UTC)}):
            with self.assertRaises(BackupError):
                destroy_encrypted_backup(self.target, self.root, reason='ticket-123', receipt=receipt, confirm=True, **kwargs)
        hold = self.target.with_name(self.target.name + '.hold'); hold.write_text('preserve')
        self.assertTrue(retention_plan(self.root, now=after)['entries'][0]['legal_hold'])
        with self.assertRaises(BackupError):
            destroy_encrypted_backup(self.target, self.root, expected_sha256=created['sha256'],
                                      reason='ticket-123', receipt=receipt, confirm=True, now=after)
        self.assertTrue(self.target.exists()); self.assertFalse(receipt.exists())

    def test_destroy_only_exact_expired_file_and_authenticates_receipt(self):
        created = self.create(); after = datetime.fromisoformat(created['expires_at']) + timedelta(seconds=1)
        second = self.root / 'keep.tpbackup'
        create_encrypted_backup(self.database, second, retention_days=60)
        other = self.root / 'keep.txt'; other.write_text('user file')
        receipt = self.root / 'receipt.json'
        result = destroy_encrypted_backup(self.target, self.root, expected_sha256=created['sha256'], reason='ticket-123',
                    receipt=receipt, confirm=True, now=after)
        self.assertTrue(result['destroyed']); self.assertFalse(self.target.exists())
        self.assertTrue(second.exists()); self.assertEqual(other.read_text(), 'user file')
        verified = verify_destruction_receipt(receipt)
        self.assertEqual(verified['state'], 'completed'); self.assertEqual(verified['sha256'], created['sha256'])
        self.assertFalse(list(self.root.glob('.destroy-*')))
        edited = json.loads(receipt.read_text(encoding='utf-8')); edited['reason'] = 'forged'
        receipt.write_text(json.dumps(edited), encoding='utf-8')
        with self.assertRaises(BackupError):
            verify_destruction_receipt(receipt)

    def test_destroy_refuses_outside_directory_hardlinks_and_existing_receipt(self):
        created = self.create(); after = datetime.fromisoformat(created['expires_at']) + timedelta(seconds=1)
        receipt = self.root / 'receipt.json'; receipt.write_text('user receipt')
        with self.assertRaises(BackupError):
            destroy_encrypted_backup(self.target, self.root, expected_sha256=created['sha256'], reason='ticket-123',
                                      receipt=receipt, confirm=True, now=after)
        self.assertEqual(receipt.read_text(), 'user receipt')
        with tempfile.TemporaryDirectory() as other_directory, self.assertRaises(BackupError):
            destroy_encrypted_backup(self.target, other_directory, expected_sha256=created['sha256'], reason='ticket-123')
        linked = self.root / 'linked.tpbackup'; os.link(self.target, linked)
        with self.assertRaises(BackupError):
            destroy_encrypted_backup(self.target, self.root, expected_sha256=created['sha256'], reason='ticket-123')
        self.assertTrue(self.target.exists()); self.assertTrue(linked.exists())

    def test_destroy_rechecks_claimed_bytes_and_rolls_back_before_deletion(self):
        created = self.create(); after = datetime.fromisoformat(created['expires_at']) + timedelta(seconds=1)
        receipt = self.root / 'receipt.json'; replace = ops_db.os.replace
        altered = self.target.read_bytes()[:-1] + b'X'
        def swap_before_claim(source, target):
            if Path(source) == self.target:
                self.target.write_bytes(altered)
            return replace(source, target)
        with patch.object(ops_db.os, 'replace', side_effect=swap_before_claim), self.assertRaises(BackupError):
            destroy_encrypted_backup(self.target, self.root, expected_sha256=created['sha256'], reason='ticket-123',
                                      receipt=receipt, confirm=True, now=after)
        self.assertEqual(self.target.read_bytes(), altered)
        self.assertEqual(verify_destruction_receipt(receipt)['state'], 'cancelled')
        self.assertFalse(list(self.root.glob('.destroy-*')))

    def test_destroy_keeps_claim_if_concurrent_replacement_prevents_rollback(self):
        created = self.create(); after = datetime.fromisoformat(created['expires_at']) + timedelta(seconds=1)
        receipt = self.root / 'receipt.json'; replace = ops_db.os.replace
        replacement = b'concurrently created user file'
        def race(source, target):
            result = replace(source, target)
            if Path(source) == self.target:
                Path(target).write_bytes(b'invalid unreviewed bytes')
                self.target.write_bytes(replacement)
            return result
        with patch.object(ops_db.os, 'replace', side_effect=race), self.assertRaises(BackupError):
            destroy_encrypted_backup(self.target, self.root, expected_sha256=created['sha256'], reason='ticket-123',
                                      receipt=receipt, confirm=True, now=after)
        self.assertEqual(self.target.read_bytes(), replacement)
        record = verify_destruction_receipt(receipt)
        self.assertEqual(record['state'], 'needs_recovery')
        self.assertEqual(Path(record['claimed_path']).read_bytes(), b'invalid unreviewed bytes')

    def test_hold_created_during_claim_cancels_and_restores_backup(self):
        created = self.create(); after = datetime.fromisoformat(created['expires_at']) + timedelta(seconds=1)
        receipt = self.root / 'receipt.json'; replace = ops_db.os.replace
        original = self.target.read_bytes()
        def hold(source, target):
            result = replace(source, target)
            if Path(source) == self.target:
                self.target.with_name(self.target.name + '.hold').write_text('hold requested')
            return result
        with patch.object(ops_db.os, 'replace', side_effect=hold), self.assertRaises(BackupError):
            destroy_encrypted_backup(self.target, self.root, expected_sha256=created['sha256'], reason='ticket-123',
                                      receipt=receipt, confirm=True, now=after)
        self.assertEqual(self.target.read_bytes(), original)
        self.assertEqual(verify_destruction_receipt(receipt)['state'], 'cancelled')

    def test_cli_confirmed_destruction_and_receipt_under_simulated_expiry(self):
        created = self.create(); after = datetime.fromisoformat(created['expires_at']) + timedelta(seconds=1)
        receipt = self.root / 'receipt.json'; output = StringIO()
        with patch.object(ops_db, 'datetime', wraps=datetime) as clock, redirect_stdout(output):
            clock.now.return_value = after
            result = ops_db.main(['destroy-encrypted', str(self.target), '--directory', str(self.root),
                      '--sha256', created['sha256'], '--reason', 'test-ticket', '--receipt', str(receipt), '--yes'])
        self.assertEqual(result, 0); self.assertTrue(json.loads(output.getvalue())['destroyed'])
        self.assertFalse(self.target.exists())
        verified = verify_destruction_receipt(receipt)
        self.assertEqual(verified['state'], 'completed')
        self.assertNotIn(self.key, output.getvalue() + receipt.read_text(encoding='utf-8'))
        with patch.dict(os.environ, {'TAXPEARLS_BACKUP_KEY': base64.b64encode(b'x'*32).decode()}), self.assertRaises(BackupError):
            verify_destruction_receipt(receipt)

    def test_receipt_failure_after_unlink_preserves_prepared_record_for_reconciliation(self):
        created = self.create(); after = datetime.fromisoformat(created['expires_at']) + timedelta(seconds=1)
        receipt = self.root / 'receipt.json'
        with patch.object(ops_db, '_write_json_atomic', side_effect=OSError('disk failure')), self.assertRaises(OSError):
            destroy_encrypted_backup(self.target, self.root, expected_sha256=created['sha256'], reason='ticket-123',
                                      receipt=receipt, confirm=True, now=after)
        self.assertFalse(self.target.exists())
        self.assertEqual(verify_destruction_receipt(receipt)['state'], 'prepared')
        self.assertFalse(list(self.root.glob('.destroy-*')))

    def test_cli_legacy_plaintext_requires_explicit_opt_in(self):
        legacy = self.root / 'legacy.sqlite3'
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            ops_db.main(['backup', '--database', str(self.database), '--output', str(legacy)])
        self.assertFalse(legacy.exists())
        with redirect_stdout(StringIO()):
            self.assertEqual(ops_db.main(['backup', '--database', str(self.database), '--output', str(legacy), '--allow-plaintext']), 0)
        destination = self.root / 'legacy-restored.db'
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            ops_db.main(['restore', str(legacy), '--database', str(destination), '--yes'])
        self.assertFalse(destination.exists())
        with redirect_stdout(StringIO()):
            self.assertEqual(ops_db.main(['restore', str(legacy), '--database', str(destination), '--yes', '--allow-plaintext']), 0)

    def test_cli_backup_verify_restore_and_preview_without_secret_output(self):
        def call(*args):
            result = subprocess.run([sys.executable, '-X', 'utf8', str(ops_db.ROOT/'scripts/ops_db.py'), *args],
                         cwd=ops_db.ROOT, env=os.environ.copy(), capture_output=True, text=True, encoding='utf-8', timeout=20)
            self.assertNotIn(self.key, result.stdout + result.stderr)
            return result
        result = call('backup-encrypted', '--database', str(self.database), '--output', str(self.target), '--retention-days', '30')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['encrypted'])
        self.assertEqual(call('verify-encrypted', str(self.target)).returncode, 0)
        destination = self.root / 'cli.db'
        refused = call('restore-encrypted', str(self.target), '--database', str(destination), '--safety-retention-days', '30')
        self.assertNotEqual(refused.returncode, 0); self.assertFalse(destination.exists())
        restored = call('restore-encrypted', str(self.target), '--database', str(destination), '--safety-retention-days', '30', '--yes')
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual(json.loads(restored.stdout)['table_counts']['users'], 1)
        plan = call('retention-plan', str(self.root)); self.assertEqual(plan.returncode, 0)
        item = json.loads(plan.stdout)['entries'][0]
        preview = call('destroy-encrypted', item['backup'], '--directory', str(self.root), '--sha256', item['sha256'], '--reason', 'ticket')
        self.assertEqual(preview.returncode, 0); self.assertFalse(json.loads(preview.stdout)['destroyed'])
        self.assertTrue(self.target.exists())

if __name__ == "__main__":
    unittest.main()
