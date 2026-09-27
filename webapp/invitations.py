"""Organization-owned, multi-use invitation credentials; no plaintext storage.

All mutations and redemptions serialize with member/quota changes. An 8-character
random organization alias is public routing-free metadata; the 24-character
secret provides 120 bits. Authorization always looks up the complete hash.
"""
import json
import re
import secrets
from typing import Literal

from fastapi import Cookie
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from webapp import members
from webapp.access import AccessDenied

MEMBER_ROLES = {'accountant', 'teacher', 'student'}


def migrate(db):
    db.execute('''CREATE TABLE IF NOT EXISTS org_invitation_policy (
        org_id TEXT PRIMARY KEY, alias TEXT NOT NULL UNIQUE,
        domain_enabled INTEGER NOT NULL DEFAULT 0,
        domains TEXT NOT NULL DEFAULT '[]', revision INTEGER NOT NULL DEFAULT 1
    )''')
    db.execute('''CREATE TABLE IF NOT EXISTS member_invitations (
        id TEXT PRIMARY KEY, org_id TEXT NOT NULL REFERENCES org_invitation_policy(org_id),
        role TEXT NOT NULL CHECK(role IN ('accountant','teacher','student')),
        token_hash TEXT NOT NULL UNIQUE, active INTEGER NOT NULL DEFAULT 1,
        revision INTEGER NOT NULL DEFAULT 1, used_count INTEGER NOT NULL DEFAULT 0,
        last_used_at TEXT, created_by TEXT NOT NULL REFERENCES users(id),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(org_id,role)
    )''')


def domain(value):
    if not isinstance(value, str):
        raise AccessDenied('邮箱域名格式无效。', 422)
    try:
        normalized = value.strip().lower().encode('idna').decode('ascii')
    except UnicodeError:
        raise AccessDenied('邮箱域名格式无效。', 422) from None
    labels = normalized.split('.')
    if len(normalized) > 253 or len(labels) < 2 or any(
        not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part) for part in labels
    ):
        raise AccessDenied('请填写完整邮箱域名，不含 @、通配符、路径或端口。', 422)
    return normalized


def policy(db, org_id):
    row = db.execute('SELECT * FROM org_invitation_policy WHERE org_id=?', (org_id,)).fetchone()
    return {'enabled': bool(row['domain_enabled']) if row else False,
            'domains': json.loads(row['domains']) if row else [],
            'revision': row['revision'] if row else 0}


def ensure_alias(db, org_id):
    from webapp.storage import _generate_invite_code
    row = db.execute('SELECT alias FROM org_invitation_policy WHERE org_id=?', (org_id,)).fetchone()
    if row:
        return row['alias']
    for _ in range(10):
        alias = _generate_invite_code(8)
        if not db.execute('SELECT 1 FROM org_invitation_policy WHERE alias=?', (alias,)).fetchone():
            db.execute('INSERT INTO org_invitation_policy(org_id,alias) VALUES (?,?)', (org_id,alias))
            return alias
    raise AccessDenied('凭证生成失败，请重试。', 503)


def credential(db, org_id):
    from webapp.storage import _generate_invite_code, _hash_token
    alias = ensure_alias(db, org_id)
    for _ in range(10):
        code = alias + _generate_invite_code(24)
        digest = _hash_token(code)
        if not db.execute('SELECT 1 FROM member_invitations WHERE token_hash=? UNION ALL '
                          'SELECT 1 FROM invite_codes WHERE token_hash=?', (digest,digest)).fetchone():
            return code, digest
    raise AccessDenied('凭证生成失败，请重试。', 503)


def ledger(db, org_id):
    rows = db.execute('''SELECT id,role,active,revision,used_count,last_used_at,created_at,updated_at
        FROM member_invitations WHERE org_id=? ORDER BY role''', (org_id,)).fetchall()
    return {'links': [dict(row) for row in rows], 'policy': policy(db,org_id),
            'quota': members.quota(db,org_id)}


def overview(store, actor, org_id):
    with store.connect() as db:
        db.execute('BEGIN')
        members.authorize(db,actor,org_id)
        return ledger(db,org_id)


def issue(store, actor, org_id, role):
    if role not in MEMBER_ROLES:
        raise AccessDenied('邀请角色只能是会计、教师或学生。',422)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        members.authorize(db,actor,org_id)
        if db.execute('SELECT 1 FROM member_invitations WHERE org_id=? AND role=?', (org_id,role)).fetchone():
            raise AccessDenied('该角色已有邀请链接，请刷新或调整状态。',409)
        code, digest = credential(db,org_id)
        link_id, stamp = secrets.token_hex(12), members.now()
        db.execute('''INSERT INTO member_invitations
            (id,org_id,role,token_hash,created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?)''',
                   (link_id,org_id,role,digest,actor['id'],stamp,stamp))
        members.log(db,{**actor,'org_id':org_id},'issue_member_invitation','member_invitation',link_id,f'role={role};revision=1')
        return {'id':link_id,'code':code,'path':'/p/'+code,'revision':1,'active':True}


def change(store, actor, org_id, link_id, revision, *, active=None):
    """active=None rotates only the secret; a disabled link stays disabled."""
    if type(revision) is not int or revision < 1 or (active is not None and type(active) is not bool):
        raise AccessDenied('链接版本或状态无效。',422)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        members.authorize(db,actor,org_id)
        row = db.execute('SELECT * FROM member_invitations WHERE id=? AND org_id=?', (link_id,org_id)).fetchone()
        if not row:
            raise AccessDenied('邀请链接不存在或无权管理。')
        if row['revision'] != revision:
            raise AccessDenied('链接已变化，请刷新列表后重试。',409)
        result = {'id':link_id,'revision':revision+1,'active':bool(row['active']) if active is None else active}
        digest = row['token_hash']
        if active is None:
            code,digest = credential(db,org_id)
            result.update(code=code,path='/p/'+code)
        db.execute('UPDATE member_invitations SET token_hash=?,active=?,revision=revision+1,updated_at=? WHERE id=?',
                   (digest,int(result['active']),members.now(),link_id))
        members.log(db,{**actor,'org_id':org_id},'refresh_member_invitation' if active is None else 'set_member_invitation_state',
                    'member_invitation',link_id,f'revision={revision+1};active={int(result["active"])}')
        return result


def set_policy(store, actor, org_id, enabled, domains, revision):
    if type(enabled) is not bool or type(revision) is not int or revision < 0 or not isinstance(domains,list) or len(domains)>20:
        raise AccessDenied('白名单配置无效（最多 20 个域名）。',422)
    normalized = sorted({domain(value) for value in domains})
    if enabled and not normalized:
        raise AccessDenied('开启白名单时至少填写一个完整域名。',422)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        members.authorize(db,actor,org_id)
        if policy(db,org_id)['revision'] != revision:
            raise AccessDenied('白名单配置已变化，请刷新后重试。',409)
        ensure_alias(db,org_id)
        db.execute('UPDATE org_invitation_policy SET domain_enabled=?,domains=?,revision=? WHERE org_id=?',
                   (int(enabled),json.dumps(normalized),revision+1,org_id))
        members.log(db,{**actor,'org_id':org_id},'set_invitation_policy','org',org_id,f'enabled={int(enabled)};revision={revision+1}')
        return policy(db,org_id)


def resolve(db, digest, address):
    """Called only inside the registration write transaction, after email proof."""
    row = db.execute('SELECT * FROM member_invitations WHERE token_hash=?', (digest,)).fetchone()
    if not row:
        return None
    if not row['active'] or row['role'] not in MEMBER_ROLES:
        raise ValueError('邀请码无效或已停用。')
    state = policy(db,row['org_id'])
    if state['enabled'] and domain(address.rpartition('@')[2]) not in state['domains']:
        raise ValueError('该邮箱域名不在机构允许的注册范围内。')
    return row


class IssueBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    role: Literal['accountant','teacher','student']


class RevisionBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=1,strict=True)


class StateBody(RevisionBody):
    active: StrictBool


class PolicyBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: StrictBool
    domains: list[str] = Field(max_length=20)
    revision: int = Field(ge=0,strict=True)


def register(app, store_provider, user_for_session, cookie_name):
    @app.get('/api/members/{org_id}/invitations')
    def list_links(org_id: str, session: str | None=Cookie(default=None,alias=cookie_name)):
        return overview(store_provider(),user_for_session(session),org_id)

    @app.post('/api/members/{org_id}/invitations')
    def create_link(org_id: str, body: IssueBody, session: str | None=Cookie(default=None,alias=cookie_name)):
        return issue(store_provider(),user_for_session(session),org_id,body.role)

    @app.post('/api/members/{org_id}/invitations/{link_id}/refresh')
    def refresh_link(org_id: str, link_id: str, body: RevisionBody, session: str | None=Cookie(default=None,alias=cookie_name)):
        return change(store_provider(),user_for_session(session),org_id,link_id,body.revision)

    @app.put('/api/members/{org_id}/invitations/{link_id}/state')
    def set_link_state(org_id: str, link_id: str, body: StateBody, session: str | None=Cookie(default=None,alias=cookie_name)):
        return change(store_provider(),user_for_session(session),org_id,link_id,body.revision,active=body.active)

    @app.put('/api/members/{org_id}/invitation-policy')
    def change_policy(org_id: str, body: PolicyBody, session: str | None=Cookie(default=None,alias=cookie_name)):
        return set_policy(store_provider(),user_for_session(session),org_id,body.enabled,body.domains,body.revision)
