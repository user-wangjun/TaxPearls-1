"""H04 shared binding protocol. No public endpoint accepts an external identity.

accept_code() is an internal entry point for authenticated private-message
adapters (H02/H03), never for browser JSON. It only records a candidate: the
original local session must explicitly confirm before any binding is usable.
This module grants neither a login session nor permission to business objects.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import secrets
import time

from webapp import members
from webapp.access import AccessDenied, current_actor
from webapp.notifications import Recipient, RECIPIENT_ROLES

CHANNELS = frozenset({'feishu', 'dingtalk'})
LIFETIME = 600
CAPACITY = 4096
ALPHABET = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'
INVALID = '绑定请求不存在、已失效或不属于当前会话，请重新发起。'


@dataclass(frozen=True)
class Scope:
    channel: str
    app_id: str
    tenant_id: str

    @property
    def key(self):
        if self.channel not in CHANNELS:
            raise ValueError('通知渠道不支持。')
        for value in (self.app_id, self.tenant_id):
            _identifier(value)
        # IDs are scoped to BOTH application and tenant. Neither nickname nor
        # provider email is a usable cross-application/local-account identity.
        return _digest(json.dumps([self.channel,self.app_id,self.tenant_id],separators=(',',':')))


def _digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _identifier(value):
    if (not isinstance(value,str) or not 1<=len(value)<=256
            or value.strip()!=value or any(ord(c)<33 for c in value)):
        raise ValueError('外部身份标识无效。')


def migrate(db):
    db.execute('''CREATE TABLE IF NOT EXISTS channel_bindings (
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
        org_id TEXT NOT NULL, role TEXT NOT NULL, channel TEXT NOT NULL,
        scope_key TEXT NOT NULL, subject_key TEXT NOT NULL, recipient_key TEXT NOT NULL,
        revision TEXT NOT NULL, notifications_enabled INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(channel,scope_key,subject_key), UNIQUE(user_id,channel)
    )''')
    db.execute('''CREATE TABLE IF NOT EXISTS channel_binding_flows (
        id TEXT PRIMARY KEY, code_hash TEXT NOT NULL UNIQUE,
        user_id TEXT NOT NULL REFERENCES users(id), org_id TEXT NOT NULL, role TEXT NOT NULL,
        session_hash TEXT NOT NULL, channel TEXT NOT NULL, scope_key TEXT NOT NULL,
        created_at REAL NOT NULL, expires_at REAL NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('pending','candidate','completed','cancelled')),
        subject_key TEXT, recipient_key TEXT, event_hash TEXT
    )''')
    db.execute('CREATE INDEX IF NOT EXISTS idx_channel_binding_flows_user ON channel_binding_flows(user_id,channel)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_channel_binding_flows_expiry ON channel_binding_flows(expires_at)')
    db.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_channel_binding_event ON channel_binding_flows(channel,scope_key,event_hash) WHERE event_hash IS NOT NULL')


def _session(db, token):
    if not isinstance(token,str) or not 1<=len(token)<=512:
        raise AccessDenied('请重新登录后操作。',401)
    user = db.execute('''SELECT u.id,u.org_id,u.role FROM sessions s JOIN users u ON u.id=s.user_id
        WHERE s.token_hash=? AND s.expires_at>? AND u.active=1''',(_digest(token),members.now())).fetchone()
    if not user:
        raise AccessDenied('请重新登录后操作。',401)
    return dict(user)


def _flow_actor(db, row, token):
    actor = _session(db,token)
    if (not row or row['user_id']!=actor['id'] or row['org_id']!=actor['org_id']
            or row['role']!=actor['role'] or row['session_hash']!=_digest(token)
            or row['expires_at']<=time.time() or row['status'] not in {'pending','candidate'}):
        raise AccessDenied(INVALID,409)
    return actor


def begin(store, session, scope):
    """scope is server configuration, never an app/tenant supplied by a browser."""
    scope_key = scope.key
    stamp = time.time()
    code = ''.join(secrets.choice(ALPHABET) for _ in range(20))
    flow_id = secrets.token_hex(16)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        actor = _session(db,session)
        db.execute('DELETE FROM channel_binding_flows WHERE expires_at<=?',(stamp,))
        if db.execute('SELECT 1 FROM channel_bindings WHERE user_id=? AND channel=?',(actor['id'],scope.channel)).fetchone():
            raise AccessDenied('该渠道已绑定，请先解绑后再更换。',409)
        latest = db.execute('SELECT MAX(created_at) FROM channel_binding_flows WHERE user_id=? AND channel=?',
                            (actor['id'],scope.channel)).fetchone()[0]
        if latest is not None and stamp-latest<30:
            raise AccessDenied('请等待 30 秒后再生成绑定码。',429)
        if db.execute('SELECT COUNT(*) FROM channel_binding_flows').fetchone()[0]>=CAPACITY:
            raise AccessDenied('绑定请求容量暂满，请稍后重试。',503)
        db.execute("UPDATE channel_binding_flows SET status='cancelled' WHERE user_id=? AND channel=? AND status IN ('pending','candidate')",
                   (actor['id'],scope.channel))
        db.execute('''INSERT INTO channel_binding_flows
            (id,code_hash,user_id,org_id,role,session_hash,channel,scope_key,created_at,expires_at,status)
            VALUES (?,?,?,?,?,?,?,?,?,?,'pending')''',
            (flow_id,_digest(code),actor['id'],actor['org_id'],actor['role'],_digest(session),scope.channel,scope_key,stamp,stamp+LIFETIME))
        members.log(db,actor,'channel_binding_begin','channel_binding',flow_id,'channel='+scope.channel)
    return {'id':flow_id,'channel':scope.channel,'code':'-'.join(code[i:i+5] for i in range(0,20,5)),
            'expires_in':LIFETIME,'status':'pending'}


def accept_code(store, scope, subject, code, event_id, *, private_message):
    """Only call AFTER provider authentication, app/tenant and sender checks.

    No reply needs to expose a local account or flow ID. Duplicate delivery of
    the SAME event is acknowledged, but never creates a second binding/flow.
    Even a stolen code cannot complete binding without local confirmation.
    """
    scope_key = scope.key
    _identifier(subject)
    _identifier(event_id)
    if private_message is not True or not isinstance(code,str) or len(code)>30:
        return False
    normalized = code.replace('-','').strip().upper()
    if not re.fullmatch('['+ALPHABET+']{20}',normalized):
        return False
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM channel_binding_flows WHERE code_hash=?',(_digest(normalized),)).fetchone()
        if (not row or row['channel']!=scope.channel or row['scope_key']!=scope_key
                or row['expires_at']<=time.time() or row['status'] not in {'pending','candidate'}):
            return False
        actor = {'id':row['user_id'],'org_id':row['org_id'],'role':row['role']}
        try:
            current_actor(db,actor)
        except AccessDenied:
            return False
        if not db.execute('SELECT 1 FROM sessions WHERE token_hash=? AND user_id=? AND expires_at>?',
                          (row['session_hash'],row['user_id'],members.now())).fetchone():
            return False
        if row['status']=='candidate':
            return row['subject_key']==_digest(subject) and row['event_hash']==_digest(event_id)
        if db.execute('SELECT 1 FROM channel_binding_flows WHERE channel=? AND scope_key=? AND event_hash=?',
                      (scope.channel,scope_key,_digest(event_id))).fetchone():
            return False
        if db.execute('SELECT 1 FROM channel_bindings WHERE channel=? AND scope_key=? AND subject_key=?',
                      (scope.channel,scope_key,_digest(subject))).fetchone():
            return False
        db.execute("UPDATE channel_binding_flows SET status='candidate',subject_key=?,recipient_key=?,event_hash=? WHERE id=?",
                   (_digest(subject),subject,_digest(event_id),row['id']))
        members.log(db,actor,'channel_binding_candidate','channel_binding',row['id'],'channel='+scope.channel)
        return True


def status(store, session, flow_id):
    with store.connect() as db:
        db.execute('BEGIN')
        row = db.execute('SELECT * FROM channel_binding_flows WHERE id=?',(flow_id,)).fetchone()
        _flow_actor(db,row,session)
        # Fingerprint is for comparing the locally confirmed candidate, not an
        # authentication factor or a human-readable provider identity claim.
        return {'id':row['id'],'channel':row['channel'],'status':row['status'],
                'candidate_fingerprint':row['subject_key'][:12] if row['subject_key'] else None,
                'expires_in':max(0,int(row['expires_at']-time.time()))}


def confirm(store, session, flow_id, scope, *, notifications=False):
    if type(notifications) is not bool:
        raise ValueError('订阅状态必须是布尔值。')
    scope_key = scope.key
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM channel_binding_flows WHERE id=?',(flow_id,)).fetchone()
        actor = _flow_actor(db,row,session)
        if row['status']!='candidate' or row['scope_key']!=scope_key or row['channel']!=scope.channel:
            raise AccessDenied(INVALID,409)
        if notifications and actor['role'] not in RECIPIENT_ROLES:
            raise AccessDenied('当前角色不支持审计风险通知。',403)
        if db.execute('''SELECT 1 FROM channel_bindings WHERE (user_id=? AND channel=?)
            OR (channel=? AND scope_key=? AND subject_key=?)''',
            (actor['id'],scope.channel,scope.channel,scope_key,row['subject_key'])).fetchone():
            raise AccessDenied('该渠道或外部身份已绑定，请取消后重新操作。',409)
        binding_id = secrets.token_hex(16)
        stamp = members.now()
        db.execute('INSERT INTO channel_bindings VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (binding_id,actor['id'],actor['org_id'],actor['role'],scope.channel,scope_key,row['subject_key'],
             row['recipient_key'],secrets.token_hex(16),int(notifications),stamp,stamp))
        db.execute("UPDATE channel_binding_flows SET status='completed',recipient_key=NULL WHERE id=?",(flow_id,))
        members.log(db,actor,'channel_binding_confirm','channel_binding',binding_id,'channel='+scope.channel)
        return {'id':binding_id,'channel':scope.channel,'notifications_enabled':notifications,'created_at':stamp}


def cancel(store, session, flow_id):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM channel_binding_flows WHERE id=?',(flow_id,)).fetchone()
        actor = _flow_actor(db,row,session)
        db.execute("UPDATE channel_binding_flows SET status='cancelled',recipient_key=NULL WHERE id=?",(flow_id,))
        members.log(db,actor,'channel_binding_cancel','channel_binding',flow_id,'channel='+row['channel'])


def list_bindings(store, session):
    with store.connect() as db:
        db.execute('BEGIN')
        actor = _session(db,session)
        return [dict(row) for row in db.execute('''SELECT id,channel,notifications_enabled,created_at,updated_at
            FROM channel_bindings WHERE user_id=? ORDER BY channel''',(actor['id'],))]


def _suppress(db, user_id, channel):
    db.execute("""UPDATE notification_deliveries SET status='suppressed',error_code='unsubscribed',updated_at=?
        WHERE channel=? AND status IN ('pending','failed','uncertain')
        AND notification_id IN (SELECT id FROM notifications WHERE user_id=?)""",(members.now(),channel,user_id))


def set_notifications(store, session, binding_id, enabled):
    if type(enabled) is not bool:
        raise ValueError('订阅状态必须是布尔值。')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        actor = _session(db,session)
        row = db.execute('SELECT * FROM channel_bindings WHERE id=? AND user_id=?',(binding_id,actor['id'])).fetchone()
        if not row:
            raise AccessDenied()
        if row['org_id']!=actor['org_id'] or row['role']!=actor['role']:
            raise AccessDenied('账号归属或角色已变化，请解绑后重新绑定。',409)
        if enabled and actor['role'] not in RECIPIENT_ROLES:
            raise AccessDenied('当前角色不支持审计风险通知。',403)
        if bool(row['notifications_enabled'])!=enabled:
            db.execute('UPDATE channel_bindings SET notifications_enabled=?,revision=?,updated_at=? WHERE id=?',
                (int(enabled),secrets.token_hex(16),members.now(),binding_id))
            _suppress(db,actor['id'],row['channel'])
        members.log(db,actor,'channel_subscription','channel_binding',binding_id,f'channel={row["channel"]};enabled={int(enabled)}')


def unlink(store, session, binding_id):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        actor = _session(db,session)
        row = db.execute('SELECT * FROM channel_bindings WHERE id=? AND user_id=?',(binding_id,actor['id'])).fetchone()
        if not row:
            raise AccessDenied()
        db.execute('DELETE FROM channel_bindings WHERE id=?',(binding_id,))
        db.execute("UPDATE channel_binding_flows SET status='cancelled',recipient_key=NULL WHERE user_id=? AND channel=?",
                   (actor['id'],row['channel']))
        _suppress(db,actor['id'],row['channel'])
        members.log(db,actor,'channel_binding_unlink','channel_binding',binding_id,'channel='+row['channel'])


def recipient(db, actor, scope):
    """H01 DB-only resolver; never performs provider calls or grants permission."""
    current_actor(db,actor)
    if actor['role'] not in RECIPIENT_ROLES:
        return None
    row = db.execute('''SELECT recipient_key,revision FROM channel_bindings
        WHERE user_id=? AND org_id=? AND role=? AND channel=? AND scope_key=? AND notifications_enabled=1''',
        (actor['id'],actor['org_id'],actor['role'],scope.channel,scope.key)).fetchone()
    return Recipient(row['recipient_key'],row['revision']) if row else None


def actor_for_subject(store, scope, subject):
    """Resolve only. Every later business read/write MUST recheck its own ACL."""
    _identifier(subject)
    with store.connect() as db:
        row = db.execute('''SELECT u.id,u.org_id,u.role FROM channel_bindings b JOIN users u ON u.id=b.user_id
            WHERE b.channel=? AND b.scope_key=? AND b.subject_key=? AND u.active=1
            AND b.org_id=u.org_id AND b.role=u.role''',(scope.channel,scope.key,_digest(subject))).fetchone()
        return dict(row) if row else None


def revoke_pending(db, user_id):
    """Use in member status/reset transactions; old generations never revive."""
    db.execute("UPDATE channel_binding_flows SET status='cancelled',recipient_key=NULL WHERE user_id=? AND status IN ('pending','candidate')",(user_id,))
    db.execute('UPDATE channel_bindings SET notifications_enabled=0,revision=?,updated_at=? WHERE user_id=?',
               (secrets.token_hex(16),members.now(),user_id))
    for channel in CHANNELS:
        _suppress(db,user_id,channel)
