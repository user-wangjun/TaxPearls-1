"""Encrypted, versioned enterprise evidence. No automatic retention deletion.

This repository is independent of the legacy in-memory upload draft. Public
routes must not accept client-supplied extraction/confirmation events verbatim.
All sensitive payloads use object-specific authenticated encryption, including
file names and edit history. Keys are not stored alongside the database.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from webapp.access import AccessDenied, audit_row, current_actor
from webapp import members, material_operations as operations

ROLES = {'org_admin', 'accountant'}
MAX_FILE = 10 * 1024 * 1024
MAX_TOTAL = 50 * 1024 * 1024
MAX_RECORD = 8 * 1024 * 1024


class MaterialStorageError(ValueError):
    """Safe diagnostics that never include payloads or key material."""


def migrate(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS material_batches (
            id TEXT PRIMARY KEY, org_id TEXT NOT NULL,
            owner_id TEXT NOT NULL REFERENCES users(id),
            client_id TEXT REFERENCES clients(id),
            revision INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_material_batches_org
            ON material_batches(org_id,owner_id);
        CREATE TABLE IF NOT EXISTS material_originals (
            id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES material_batches(id),
            uploaded_by TEXT NOT NULL REFERENCES users(id), uploaded_at TEXT NOT NULL,
            metadata_cipher BLOB NOT NULL, bytes_cipher BLOB,
            deleted_at TEXT, deleted_by TEXT REFERENCES users(id),
            CHECK ((bytes_cipher IS NULL) = (deleted_at IS NOT NULL))
        );
        CREATE INDEX IF NOT EXISTS idx_material_originals_batch ON material_originals(batch_id);
        CREATE TABLE IF NOT EXISTS material_revisions (
            batch_id TEXT NOT NULL REFERENCES material_batches(id), revision INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('analysis','edit','confirmation','material_change')),
            payload_cipher BLOB NOT NULL, created_by TEXT NOT NULL REFERENCES users(id),
            created_at TEXT NOT NULL, PRIMARY KEY(batch_id,revision)
        );
        CREATE TABLE IF NOT EXISTS material_events (
            id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES material_batches(id),
            action TEXT NOT NULL, detail_cipher BLOB NOT NULL,
            actor_id TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS material_executions (
            batch_id TEXT NOT NULL REFERENCES material_batches(id), analysis_revision INTEGER NOT NULL,
            confirmation_revision INTEGER NOT NULL, audit_id TEXT NOT NULL UNIQUE REFERENCES audits(id),
            PRIMARY KEY(batch_id,analysis_revision),
            FOREIGN KEY(batch_id,confirmation_revision) REFERENCES material_revisions(batch_id,revision)
        );
    ''')
    operations.migrate(db)


def _key():
    try:
        raw = base64.b64decode(os.getenv('TAXPEARLS_MATERIAL_KEY', ''), validate=True)
    except (ValueError, binascii.Error):
        raw = b''
    if len(raw) != 32:
        raise MaterialStorageError('原件留存须配置独立 TAXPEARLS_MATERIAL_KEY（32 字节随机密钥的 Base64）。')
    return raw


def _context(org, batch, kind, object_id):
    return json.dumps(['TaxPearls-material-v1', org, batch, kind, str(object_id)],
                      separators=(',', ':'), ensure_ascii=True).encode()


def _seal(raw, context):
    nonce = secrets.token_bytes(12)
    return nonce + AESGCM(_key()).encrypt(nonce, raw, context)


def _open(cipher, context):
    key = _key()
    try:
        return AESGCM(key).decrypt(cipher[:12], cipher[12:], context)
    except (InvalidTag, ValueError, TypeError):
        raise MaterialStorageError('材料完整性校验失败或密钥不匹配，请核查密钥与备份。') from None


def _json(value):
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), allow_nan=False).encode()
    except (TypeError, ValueError):
        raise MaterialStorageError('材料记录须为有限数值的 JSON 数据。') from None
    if len(encoded) > MAX_RECORD:
        raise MaterialStorageError('材料记录超过 8MB。')
    return encoded


def _authorize(db, actor, batch_id):
    current_actor(db, actor, ROLES)
    batch = db.execute('SELECT * FROM material_batches WHERE id=? AND org_id=?',
                       (batch_id, actor['org_id'])).fetchone()
    if not batch:
        raise AccessDenied()
    client = None
    if batch['client_id']:
        client = db.execute('SELECT accountant_id FROM clients WHERE id=? AND org_id=?',
                            (batch['client_id'], actor['org_id'])).fetchone()
        if not client:
            raise AccessDenied()
    if actor['role'] == 'accountant':
        if batch['client_id']:
            if client['accountant_id'] != actor['id']:
                raise AccessDenied()
        elif batch['owner_id'] != actor['id']:
            raise AccessDenied()
    return batch


def _event(db, batch, actor, action, detail):
    event_id, stamp = secrets.token_hex(16), members.now()
    operation_id = operations.committed(db, actor, batch, action)
    if operation_id:
        detail = {**detail, 'operation_id': operation_id}
    cipher = _seal(_json(detail), _context(batch['org_id'], batch['id'], 'event:' + action, event_id))
    db.execute('INSERT INTO material_events VALUES (?,?,?,?,?,?)',
               (event_id, batch['id'], action, cipher, actor['id'], stamp))
    # Only opaque identifiers enter the shared operations log. Sensitive
    # detail is accessible through this batch's current business permission.
    members.log(db, actor, 'material_' + action, 'material_batch', batch['id'], 'event=' + event_id)


def validate_uploads(uploads):
    if not isinstance(uploads, list) or not 1 <= len(uploads) <= 20:
        raise MaterialStorageError('请选择 1–20 份原始材料。')
    total = 0
    for entry in uploads:
        if not isinstance(entry, (tuple, list)) or len(entry) != 2:
            raise MaterialStorageError('原始材料格式无效。')
        name, raw = entry
        if not isinstance(name, str) or not name.strip() or len(name) > 255 or not isinstance(raw, bytes):
            raise MaterialStorageError('原始材料文件名或字节格式无效。')
        if not 0 < len(raw) <= MAX_FILE:
            raise MaterialStorageError('单份原始材料须非空且不超过 10MB。')
        total += len(raw)
    if total > MAX_TOTAL:
        raise MaterialStorageError('原始材料合计不超过 50MB。')


def _files(db, batch, actor, uploads, payload=None):
    files = []
    for name, raw in uploads:
        file_id = secrets.token_hex(16)
        metadata = {'name': name, 'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
        db.execute('INSERT INTO material_originals VALUES (?,?,?,?,?,?,NULL,NULL)',
                   (file_id, batch['id'], actor['id'], members.now(),
                    _seal(_json(metadata), _context(batch['org_id'], batch['id'], 'metadata', file_id)),
                    _seal(raw, _context(batch['org_id'], batch['id'], 'original', file_id))))
        files.append({'id': file_id, **metadata})
        if payload is not None:
            for doc in payload['documents']:
                if not doc.get('original_id') and doc['original_upload_name'] == name:
                    doc['original_id'] = file_id
    return files


def create(store, actor, uploads, client_id=None, *, initial_payload=None):
    """Retain original upload bytes atomically; never deduplicate across tenants."""
    validate_uploads(uploads)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current_actor(db, actor, ROLES)
        if client_id is not None:
            client = db.execute('SELECT * FROM clients WHERE id=? AND org_id=?',
                                (client_id, actor['org_id'])).fetchone()
            if not client or actor['role'] == 'accountant' and client['accountant_id'] != actor['id']:
                raise AccessDenied()
        batch_id, stamp = secrets.token_hex(16), members.now()
        db.execute('INSERT INTO material_batches VALUES (?,?,?,?,0,?)',
                   (batch_id, actor['org_id'], actor['id'], client_id, stamp))
        batch = _authorize(db, actor, batch_id)
        files = _files(db, batch, actor, uploads, initial_payload)
        _event(db, batch, actor, 'upload', {'files': files})
        revision = 0
        if initial_payload is not None:
            revision = _append(db, batch, actor, 'analysis', initial_payload)
    return {'id': batch_id, 'revision': revision, 'files': files}


def supplement(store, actor, batch_id, expected_revision, uploads, payload):
    validate_uploads(uploads)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        batch = _authorize(db, actor, batch_id)
        if type(expected_revision) is not int or batch['revision'] != expected_revision:
            raise AccessDenied('材料已变化，请重新读取后补传。', 409)
        files = _files(db, batch, actor, uploads, payload)
        _event(db, batch, actor, 'supplement', {'files': files, 'previous_revision': expected_revision})
        return _append(db, batch, actor, 'analysis', payload)


def authorize_client(store, actor, client_id):
    """Cheap authorization BEFORE parsing or paid model calls; recheck at write."""
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE' if operations.ACTIVE.get() else 'BEGIN')
        current_actor(db, actor, ROLES)
        if client_id:
            row = db.execute('SELECT accountant_id FROM clients WHERE id=? AND org_id=?',
                             (client_id, actor['org_id'])).fetchone()
            if not row or actor['role'] == 'accountant' and row['accountant_id'] != actor['id']:
                raise AccessDenied()
        operations.bind_client(db, actor, client_id)


def listing(store, actor):
    with store.connect() as db:
        db.execute('BEGIN')
        current_actor(db, actor, ROLES)
        where = 'b.org_id=? AND (b.client_id IS NULL OR c.org_id=b.org_id)'
        args = [actor['org_id']]
        if actor['role'] == 'accountant':
            where += ' AND ((b.client_id IS NULL AND b.owner_id=?) OR c.accountant_id=?)'
            args.extend([actor['id'], actor['id']])
        return [dict(r) for r in db.execute('SELECT b.id,b.revision,b.created_at FROM material_batches b '
            'LEFT JOIN clients c ON c.id=b.client_id WHERE ' + where + ' ORDER BY b.rowid DESC LIMIT 100', args)]


def _append(db, batch, actor, kind, payload):
    revision = batch['revision'] + 1
    cipher = _seal(_json(payload), _context(batch['org_id'], batch['id'], 'revision:' + kind, revision))
    db.execute('INSERT INTO material_revisions VALUES (?,?,?,?,?,?)',
               (batch['id'], revision, kind, cipher, actor['id'], members.now()))
    db.execute('UPDATE material_batches SET revision=? WHERE id=?', (revision, batch['id']))
    _event(db, batch, actor, kind, {'revision': revision})
    return revision


def append_revision(store, actor, batch_id, expected_revision, kind, payload):
    """CAS protects edits/confirmation from stale tabs. Revisions never overwrite."""
    if kind not in {'analysis', 'edit', 'confirmation'} or not isinstance(payload, dict):
        raise MaterialStorageError('材料版本类型或内容无效。')
    if type(expected_revision) is not int or expected_revision < 0:
        raise MaterialStorageError('材料版本号无效。')
    _json(payload)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        batch = _authorize(db, actor, batch_id)
        if batch['revision'] != expected_revision:
            raise AccessDenied('材料已变化，请重新读取并核对当前版本。', 409)
        revision = _append(db, batch, actor, kind, payload)
    return revision


def read(store, actor, batch_id):
    """One permission snapshot for metadata, immutable versions and event history."""
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        batch = _authorize(db, actor, batch_id)
        result = dict(batch)
        result['files'], result['versions'], result['events'] = [], [], []
        result['executions'] = [dict(row) for row in db.execute(
            'SELECT * FROM material_executions WHERE batch_id=? ORDER BY analysis_revision', (batch_id,))]
        for row in db.execute('SELECT * FROM material_originals WHERE batch_id=? ORDER BY uploaded_at,id', (batch_id,)):
            metadata = json.loads(_open(row['metadata_cipher'],
                                       _context(batch['org_id'], batch_id, 'metadata', row['id'])))
            result['files'].append({'id': row['id'], **metadata, 'uploaded_by': row['uploaded_by'],
                                    'uploaded_at': row['uploaded_at'], 'deleted_at': row['deleted_at']})
        for row in db.execute('SELECT * FROM material_revisions WHERE batch_id=? ORDER BY revision', (batch_id,)):
            payload = json.loads(_open(row['payload_cipher'],
                                      _context(batch['org_id'], batch_id, 'revision:' + row['kind'], row['revision'])))
            result['versions'].append({'revision': row['revision'], 'kind': row['kind'], 'payload': payload,
                                       'created_by': row['created_by'], 'created_at': row['created_at']})
        for row in db.execute('SELECT * FROM material_events WHERE batch_id=? ORDER BY rowid', (batch_id,)):
            detail = json.loads(_open(row['detail_cipher'],
                                     _context(batch['org_id'], batch_id, 'event:' + row['action'], row['id'])))
            result['events'].append({'action': row['action'], 'detail': detail,
                                     'actor_id': row['actor_id'], 'created_at': row['created_at']})
        _event(db, batch, actor, 'view', {'revision': batch['revision']})
        return result


def original(store, actor, batch_id, file_id, *, purpose='download'):
    if purpose not in {'download', 'read_for_reanalysis'}:
        raise ValueError('Unsupported original access purpose')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        batch = _authorize(db, actor, batch_id)
        row = db.execute('SELECT * FROM material_originals WHERE id=? AND batch_id=?', (file_id, batch_id)).fetchone()
        if not row:
            raise AccessDenied()
        if row['deleted_at'] is not None:
            raise AccessDenied('原始材料已删除；历史结果保留，不能再下载原件。', 410)
        metadata = json.loads(_open(row['metadata_cipher'], _context(batch['org_id'], batch_id, 'metadata', file_id)))
        raw = _open(row['bytes_cipher'], _context(batch['org_id'], batch_id, 'original', file_id))
        if hashlib.sha256(raw).hexdigest() != metadata['sha256'] or len(raw) != metadata['size']:
            raise MaterialStorageError('原始材料指纹或长度不匹配，请核查备份。')
        _event(db, batch, actor, purpose, {'file_id': file_id})
        return metadata, raw


def deletion_impact(store, actor, batch_id, file_id):
    """Caller must show this before an explicit deletion, not claim total erasure."""
    with store.connect() as db:
        db.execute('BEGIN')
        batch = _authorize(db, actor, batch_id)
        current_actor(db, actor, {'org_admin'})
        if not db.execute('SELECT 1 FROM material_originals WHERE id=? AND batch_id=?', (file_id, batch_id)).fetchone():
            raise AccessDenied()
        return {'file_id': file_id, 'revision': batch['revision'],
                'audits': [dict(row) for row in db.execute(
                    'SELECT e.audit_id,e.analysis_revision,e.confirmation_revision FROM material_executions e WHERE e.batch_id=?',
                    (batch_id,))],
                'versions': db.execute('SELECT COUNT(*) FROM material_revisions WHERE batch_id=?', (batch_id,)).fetchone()[0],
                'warning': '删除后不能从本库查看原件；历史记录不会被删除。备份可能仍含副本，本操作不承诺物理擦除。'}


def delete_original(store, actor, batch_id, file_id, expected_revision):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        batch = _authorize(db, actor, batch_id)
        current_actor(db, actor, {'org_admin'})
        if type(expected_revision) is not int or batch['revision'] != expected_revision:
            raise AccessDenied('材料已变化，请重新查看删除影响。', 409)
        row = db.execute('SELECT deleted_at FROM material_originals WHERE id=? AND batch_id=?', (file_id, batch_id)).fetchone()
        if not row:
            raise AccessDenied()
        if row['deleted_at'] is not None:
            _event(db, batch, actor, 'delete', {'file_id': file_id, 'already_deleted': True})
            return False
        db.execute('UPDATE material_originals SET bytes_cipher=NULL,deleted_at=?,deleted_by=? WHERE id=?',
                   (members.now(), actor['id'], file_id))
        revision = batch['revision'] + 1
        payload = _seal(_json({'deleted_file_id': file_id, 'requires_confirmation': True}),
                        _context(batch['org_id'], batch_id, 'revision:material_change', revision))
        db.execute('INSERT INTO material_revisions VALUES (?,?,?,?,?,?)',
                   (batch_id, revision, 'material_change', payload, actor['id'], members.now()))
        db.execute('UPDATE material_batches SET revision=? WHERE id=?', (revision, batch_id))
        _event(db, batch, actor, 'delete', {'file_id': file_id, 'backup_copies_may_remain': True})
        return True


def execution(store, actor, batch_id, revision):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE' if operations.ACTIVE.get() else 'BEGIN')
        batch = _authorize(db, actor, batch_id)
        row = db.execute('SELECT audit_id FROM material_executions WHERE batch_id=? AND analysis_revision=?',
                         (batch_id, revision)).fetchone()
        if row and not audit_row(db, row['audit_id'], actor):
            raise AccessDenied()
        if row and operations.ACTIVE.get():
            _event(db, batch, actor, 'execution', {'audit_id': row['audit_id'], 'reused': True})
        return row['audit_id'] if row else None


def reference(db, audit_id):
    """Opaque result association; callers must authorize the audit first."""
    row = db.execute('SELECT batch_id,analysis_revision,confirmation_revision FROM material_executions WHERE audit_id=?',
                     (audit_id,)).fetchone()
    return dict(row) if row else None


def confirmed(store, actor, audit_id):
    """Read the exact confirmed input, never the latest mutable analysis.

    Current access/deletion state and frozen facts are deliberately separate.
    Reading and its event share one transaction; no raw dataset is projected.
    """
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current_actor(db, actor, ROLES)
        audit = audit_row(db, audit_id, actor)
        if not audit:
            raise AccessDenied()
        link = reference(db, audit_id)
        if link is None:
            return None
        batch = _authorize(db, actor, link['batch_id'])
        if batch['client_id'] != audit['client_id']:
            raise MaterialStorageError('结果与材料客户关联不一致，请核查备份。')

        def revision(number, kinds):
            row = db.execute('SELECT * FROM material_revisions WHERE batch_id=? AND revision=?',
                             (batch['id'], number)).fetchone()
            if not row or row['kind'] not in kinds:
                raise MaterialStorageError('结果关联的材料版本不完整，请核查备份。')
            payload = json.loads(_open(row['payload_cipher'],
                _context(batch['org_id'], batch['id'], 'revision:' + row['kind'], number)))
            return row, payload

        confirmation, record = revision(link['confirmation_revision'], {'confirmation'})
        _, payload = revision(link['analysis_revision'], {'analysis', 'edit'})
        if (record.get('audit_id') != audit_id or record.get('analysis_revision') != link['analysis_revision']
                or record.get('analysis_sha256') != hashlib.sha256(_json(payload)).hexdigest()
                or _json(payload['analysis'].get('dataset')) != _json(json.loads(audit['dataset_json']))):
            raise MaterialStorageError('结果与确认材料完整性校验不一致，请核查备份。')
        files = []
        file_ids = {d.get('original_id') for d in payload['documents']}
        for fid in sorted(file_ids - {None}):
            row = db.execute('SELECT * FROM material_originals WHERE id=? AND batch_id=?', (fid, batch['id'])).fetchone()
            if row is None:
                raise MaterialStorageError('确认材料的原件记录不完整，请核查备份。')
            meta = json.loads(_open(row['metadata_cipher'], _context(batch['org_id'], batch['id'], 'metadata', fid)))
            files.append({'id': fid, **meta, 'uploaded_by': row['uploaded_by'], 'uploaded_at': row['uploaded_at'],
                          'deleted_at': row['deleted_at']})
        analysis = payload['analysis']
        output = {**link, 'audit_id': audit_id, 'current_revision': batch['revision'],
            'analysis_sha256': record['analysis_sha256'], 'confirmed_by': confirmation['created_by'],
            'confirmed_at': confirmation['created_at'], 'company': analysis['company'],
            'parser_version': payload.get('parser_version'), 'files': files,
            'documents': [{**doc, 'original_id': original.get('original_id')} for doc in analysis['files']
                          for original in payload['documents'] if original['id'] == doc['id']],
            'edits': analysis['edits'], 'checks': analysis['checks'], 'feedback': analysis['feedback'],
            'metrics': list(analysis['dataset'].get('metrics', {}).values())}
        _event(db, batch, actor, 'view_confirmation', {'audit_id': audit_id, **link})
        return output


def record_execution(db, actor, audit_id, client_id, dataset, findings, context):
    """Called in the SAME write transaction as audit/client/report/queue rows.

    Confirmation is not a generic browser-provided payload: compare the exact
    server-persisted analysis, dataset and frozen rules before committing it.
    """
    from dataclasses import asdict
    from src.snapshots import serialize_dataset
    batch_id, expected = context['batch_id'], context['revision']
    batch = _authorize(db, actor, batch_id)
    if batch['revision'] != expected:
        raise AccessDenied('材料已变化，请重新核对并确认。', 409)
    row = db.execute('SELECT * FROM material_revisions WHERE batch_id=? AND revision=?', (batch_id, expected)).fetchone()
    if not row or row['kind'] not in {'analysis', 'edit'}:
        raise AccessDenied('当前没有可确认的材料分析，请重新分析。', 409)
    payload = json.loads(_open(row['payload_cipher'], _context(batch['org_id'], batch_id, 'revision:' + row['kind'], expected)))
    if hashlib.sha256(_json(payload)).hexdigest() != context['sha256']:
        raise AccessDenied('材料分析版本不匹配。', 409)
    analysis = payload.get('analysis', {})
    if (analysis.get('can_confirm') is not True or analysis.get('feedback', {}).get('blocking')
            or _json(analysis.get('dataset')) != _json(serialize_dataset(dataset))):
        raise AccessDenied('输入尚未完成核对，不能开始检测。', 409)
    # Compare the ENTIRE result set, not just the intersection with YAML IDs:
    # otherwise an unconfirmed graph/extra rule could slip into the report.
    if not isinstance(payload.get('graph_rule'), dict):
        raise AccessDenied('检查范围缺少关联方规则快照，请重新分析并确认。', 409)
    definitions = [*payload['rules'], payload['graph_rule']]
    frozen = {r['id']: r for r in definitions}
    actual = {f.rule.id: asdict(f.rule) for f in findings}
    checks = {c['rule_id']: (c['name'], c['version']) for c in analysis['checks']}
    if (len(frozen) != len(definitions) or len(actual) != len(findings)
            or len(checks) != len(analysis['checks'])
            or checks != {r['id']: (r['name'], r['version']) for r in definitions}
            or _json(frozen) != _json(actual)):
        raise AccessDenied('执行规则与确认的检查范围不一致。', 409)
    if batch['client_id'] and batch['client_id'] != client_id:
        raise AccessDenied('材料客户归属不匹配。', 409)
    revision = _append(db, batch, actor, 'confirmation', {'analysis_revision': expected,
                      'analysis_sha256': context['sha256'], 'audit_id': audit_id,
                      'scope': analysis['company'], 'files': analysis['files'],
                      'edits': analysis['edits'], 'checks': analysis['checks'],
                      'feedback': analysis['feedback']})
    db.execute('UPDATE material_batches SET client_id=? WHERE id=?', (client_id, batch_id))
    db.execute('INSERT INTO material_executions VALUES (?,?,?,?)', (batch_id, expected, revision, audit_id))
    _event(db, batch, actor, 'execution', {'audit_id': audit_id, 'confirmation_revision': revision,
           'rules': [asdict(f.rule) for f in findings],
           'skipped': [{'rule_id': f.rule.id, 'reason': f.skip_reason} for f in findings if f.status == 'skipped']})
