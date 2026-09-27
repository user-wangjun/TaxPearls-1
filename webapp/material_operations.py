"""Durable request journal; business commit markers share the business transaction.

Only opaque identifiers, timestamps and server-owned enums are stored here.
No request bodies, file names, exception messages or credentials are accepted.
An unfinished request is unknown, not automatically failed after a restart.
"""
from contextvars import ContextVar
import logging
import re
import secrets

from fastapi import Cookie, HTTPException
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from webapp import members
from webapp.access import AccessDenied, current_actor, audit_row

ACTIVE = ContextVar('material_operation', default=None)
ROLES = {'org_admin', 'accountant'}
EVENTS = {'upload': 'upload', 'supplement': 'supplement', 'analyze': 'edit',
          'confirm': 'execution', 'delete': 'delete', 'download': 'download',
          'view': 'view', 'confirmation_view': 'view_confirmation'}


def migrate(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS material_operations (
            id TEXT PRIMARY KEY, org_id TEXT NOT NULL,
            actor_id TEXT NOT NULL REFERENCES users(id), action TEXT NOT NULL,
            batch_id TEXT REFERENCES material_batches(id), client_id TEXT REFERENCES clients(id),
            state TEXT NOT NULL CHECK(state IN ('started','committed','failed')),
            started_at TEXT NOT NULL, committed_at TEXT, observed_at TEXT,
            http_status INTEGER, failure_code TEXT
        );
        CREATE INDEX IF NOT EXISTS material_operations_scope ON material_operations(org_id,batch_id);
    ''')


def begin(store, actor, action, batch_id=None, audit_id=None):
    from webapp import material_batches as batches
    if action not in EVENTS:
        raise ValueError('Unknown material action')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current_actor(db, actor, ROLES)
        if audit_id:
            if not audit_row(db, audit_id, actor):
                raise AccessDenied()
            link = batches.reference(db, audit_id)
            if not link:
                return None  # Legacy result has no material operation to track.
            batch_id = link['batch_id']
        batch = batches._authorize(db, actor, batch_id) if batch_id else None
        entry = {'id': secrets.token_hex(16), 'org_id': actor['org_id'], 'actor_id': actor['id'],
                 'action': action, 'batch_id': batch_id, 'client_id': batch['client_id'] if batch else None}
        db.execute('INSERT INTO material_operations(id,org_id,actor_id,action,batch_id,client_id,state,started_at) '
                   "VALUES (?,?,?,?,?,?,'started',?)",
                   (*[entry[k] for k in ('id', 'org_id', 'actor_id', 'action', 'batch_id', 'client_id')], members.now()))
        members.log(db, actor, 'material_request_started', 'material_operation', entry['id'], action)
    return entry


def bind_client(db, actor, client_id):
    """Called only after the selected client has been authorized in this transaction."""
    entry = ACTIVE.get()
    if entry and entry['actor_id'] == actor['id'] and entry['org_id'] == actor['org_id']:
        db.execute("UPDATE material_operations SET client_id=? WHERE id=? AND state='started'",
                   (client_id, entry['id']))


def committed(db, actor, batch, event):
    entry = ACTIVE.get()
    if not entry or EVENTS[entry['action']] != event:
        return None
    if entry['actor_id'] != actor['id'] or entry['org_id'] != actor['org_id'] or batch['org_id'] != actor['org_id']:
        raise AccessDenied()
    if entry['batch_id'] and entry['batch_id'] != batch['id']:
        raise AccessDenied()
    row = db.execute('SELECT state,batch_id FROM material_operations WHERE id=? AND org_id=? AND actor_id=? AND action=?',
                     (entry['id'], actor['org_id'], actor['id'], entry['action'])).fetchone()
    if not row or row['state'] not in {'started', 'committed'} or row['batch_id'] not in {None, batch['id']}:
        raise RuntimeError('Operation journal is inconsistent')
    db.execute("UPDATE material_operations SET state='committed',batch_id=?,client_id=?,committed_at=? "
               "WHERE id=? AND state='started'", (batch['id'], batch['client_id'], members.now(), entry['id']))
    return entry['id']


def observe(store, actor, entry, status):
    """Called after response/rollback. Never downgrades a committed business action."""
    failure = {401: 'authentication', 403: 'permission', 404: 'unavailable', 409: 'conflict',
               410: 'deleted', 413: 'too_large', 422: 'invalid_input', 429: 'busy'}.get(status, 'internal')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM material_operations WHERE id=? AND actor_id=? AND org_id=?',
                         (entry['id'], actor['id'], entry['org_id'])).fetchone()
        if not row:
            raise RuntimeError('Missing operation journal')
        # The initiating actor can be revoked mid-flight. This internal finalizer
        # records that fact without granting any new read or business permission.
        state = 'failed' if status >= 400 and row['state'] == 'started' else row['state']
        db.execute('UPDATE material_operations SET state=?,observed_at=?,http_status=?,failure_code=? WHERE id=?',
                   (state, members.now(), status, failure if status >= 400 else None, entry['id']))
        members.log(db, actor, 'material_request_observed', 'material_operation', entry['id'],
                    f'{state}:{status}')
        return state


def listing(store, actor):
    with store.connect() as db:
        db.execute('BEGIN')
        current_actor(db, actor, ROLES)
        # Once linked to a batch, its CURRENT client is authoritative after
        # auto-client creation or reassignment, not the request's old client.
        where = ('o.org_id=? AND (o.batch_id IS NULL OR b.org_id=o.org_id) '
                 'AND (COALESCE(b.client_id,o.client_id) IS NULL OR c.org_id=o.org_id)')
        args = [actor['org_id']]
        if actor['role'] == 'accountant':
            where += (' AND (c.accountant_id=? OR (COALESCE(b.client_id,o.client_id) IS NULL '
                      'AND COALESCE(b.owner_id,o.actor_id)=?))')
            args.extend([actor['id'], actor['id']])
        return [dict(row) for row in db.execute('SELECT o.id,o.action,o.batch_id,o.state,o.started_at,'
            'o.committed_at,o.observed_at,o.http_status,o.failure_code,o.actor_id FROM material_operations o '
            'LEFT JOIN material_batches b ON b.id=o.batch_id '
            'LEFT JOIN clients c ON c.id=COALESCE(b.client_id,o.client_id) WHERE ' + where +
            ' ORDER BY o.rowid DESC LIMIT 100', args)]


def route(method, path):
    base = '/api/enterprise/materials'
    if path.endswith('/'):
        return None  # Canonical redirect is not a material operation.
    if method == 'POST' and path.rstrip('/') == base:
        return ('upload', None, None)
    match = re.fullmatch(base + r'/([^/]+)(?:/(analyze|supplement|confirm|trace)|/originals/([^/]+))?/?', path)
    if match:
        batch, action, original = match.groups()
        if method == 'POST' and action in {'analyze', 'supplement', 'confirm'}:
            return (action, batch, None)
        if original and method in {'GET', 'DELETE'}:
            return ('download' if method == 'GET' else 'delete', batch, None)
        if method == 'GET' and not original and action in {None, 'trace'} and batch != 'config':
            return ('view', batch, None)
    match = re.fullmatch(r'/api/audits/([^/]+)/materials/?', path)
    if method == 'GET' and match:
        return ('confirmation_view', None, match[1])
    return None


def register(app, get_store, get_user, allow):
    @app.get('/api/enterprise/material-operations')
    def operations(session: str | None = Cookie(default=None, alias='taxpearls_session')):
        actor = get_user(session)
        allow(actor, *ROLES)
        return listing(get_store(), actor)

    @app.middleware('http')
    async def journal(request, call_next):
        target = route(request.method, request.url.path)
        if not target:
            return await call_next(request)
        try:
            actor = await run_in_threadpool(get_user, request.cookies.get('taxpearls_session'))
            allow(actor, *ROLES)
            entry = await run_in_threadpool(begin, get_store(), actor, *target)
        except (AccessDenied, HTTPException) as exc:
            return JSONResponse({'detail': str(exc) if isinstance(exc, AccessDenied) else exc.detail},
                                status_code=exc.status if isinstance(exc, AccessDenied) else exc.status_code,
                                headers={'Cache-Control': 'private, no-store'})
        except Exception:
            return JSONResponse({'detail': '操作记录暂不可写，本次未开始处理，请稍后重试。'}, status_code=503,
                                headers={'Cache-Control': 'private, no-store'})
        if entry is None:
            return await call_next(request)
        token = ACTIVE.set(entry)
        try:
            try:
                response = await call_next(request)
            except Exception:
                response = JSONResponse({'detail': '材料处理响应失败，请先查看操作记录及已有结果，再决定是否重试。'}, status_code=500)
            try:
                state = await run_in_threadpool(observe, get_store(), actor, entry, response.status_code)
            except Exception:
                state = 'unknown'
                logging.getLogger(__name__).warning('material_operation_observation_unavailable id=%s', entry['id'])
            response.headers['X-Material-Operation'] = entry['id']
            response.headers['X-Material-Operation-State'] = state
            response.headers['Cache-Control'] = 'private, no-store'
            return response
        finally:
            ACTIVE.reset(token)
