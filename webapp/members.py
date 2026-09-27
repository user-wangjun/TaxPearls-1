"""Institution membership and transactional active-seat accounting."""
from datetime import UTC, datetime

from fastapi import Cookie
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from webapp.access import AccessDenied, current_actor


def now():
    return datetime.now(UTC).isoformat(timespec='seconds')


def migrate(db):
    from webapp import invitations
    if 'revision' not in {r['name'] for r in db.execute('PRAGMA table_info(org_quota)')}:
        db.execute('ALTER TABLE org_quota ADD COLUMN revision INTEGER NOT NULL DEFAULT 1')
    db.execute('''CREATE TABLE IF NOT EXISTS member_origins (
        user_id TEXT PRIMARY KEY REFERENCES users(id), source_kind TEXT NOT NULL,
        credential_id TEXT, actor_id TEXT REFERENCES users(id), created_at TEXT NOT NULL
    )''')
    invitations.migrate(db)


def authorize(db, actor, org_id, platform_only=False):
    current_actor(db, actor, {'platform_admin'} if platform_only else {'org_admin', 'platform_admin'})
    if actor['role'] != 'platform_admin' and actor['org_id'] != org_id:
        raise AccessDenied('机构不存在或无权管理。')
    if not db.execute("SELECT 1 FROM users WHERE org_id=? AND role!='platform_admin'", (org_id,)).fetchone():
        raise AccessDenied('机构不存在或无权管理。')


def quota(db, org_id):
    row = db.execute('SELECT seats,revision FROM org_quota WHERE org_id=?', (org_id,)).fetchone()
    used = db.execute("SELECT COUNT(*) FROM users WHERE org_id=? AND active=1 AND role!='platform_admin'", (org_id,)).fetchone()[0]
    return {'seats':row['seats'] if row else None, 'revision':row['revision'] if row else 0,
            'used':used, 'remaining':max(0,row['seats']-used) if row else None,
            'over_quota':bool(row and used>row['seats'])}


def require_slot(db, org_id):
    """Call inside BEGIN IMMEDIATE, alongside the actual activation/insertion."""
    state = quota(db, org_id)
    if state['seats'] is None:
        raise AccessDenied('机构席位尚未配置，请联系平台管理员配置后重试。',409)
    if state['remaining'] < 1:
        raise AccessDenied('机构席位已满，请先停用离岗成员或联系平台调整配额。',409)


def log(db, actor, action, kind, target, detail):
    db.execute('INSERT INTO audit_log(user_id,org_id,action,target_type,target_id,detail,created_at) VALUES (?,?,?,?,?,?,?)',
               (actor['id'],actor['org_id'],action,kind,target,detail,now()))


def organizations(store, actor):
    with store.connect() as db:
        db.execute('BEGIN')
        current_actor(db,actor,{'platform_admin','org_admin'})
        restriction, args = ('', []) if actor['role']=='platform_admin' else (' AND u.org_id=?',[actor['org_id']])
        rows=db.execute("""SELECT DISTINCT u.org_id,
            COALESCE(o.display_name,(SELECT owner.display_name FROM users owner
                WHERE owner.org_id=u.org_id AND owner.role='org_admin' ORDER BY owner.created_at,owner.id LIMIT 1),u.org_id) AS name
            FROM users u LEFT JOIN org_settings o ON o.org_id=u.org_id
            WHERE u.role!='platform_admin'"""+restriction+' ORDER BY name,u.org_id', args).fetchall()
        return [dict(row) for row in rows]


def overview(store, actor, org_id):
    with store.connect() as db:
        db.execute('BEGIN')
        authorize(db,actor,org_id)
        rows=db.execute('''SELECT u.id,u.username,u.display_name,u.role,u.email,u.active,u.created_at,
            COALESCE(m.source_kind,CASE WHEN i.redeemed_by IS NOT NULL THEN 'founder' ELSE 'legacy' END) AS source_kind,
            m.actor_id,m.credential_id,mi.role AS invitation_role,mi.active AS invitation_active,
            mi.created_at AS invitation_created_at
            FROM users u LEFT JOIN member_origins m ON m.user_id=u.id
            LEFT JOIN invite_codes i ON i.redeemed_by=u.id
            LEFT JOIN member_invitations mi ON mi.id=m.credential_id AND mi.org_id=u.org_id
            WHERE u.org_id=? AND u.role!='platform_admin' ORDER BY u.created_at,u.id''',(org_id,)).fetchall()
        return {'org_id':org_id,'quota':quota(db,org_id),'members':[dict(row) for row in rows]}


def set_quota(store, actor, org_id, seats, revision):
    if type(seats) is not int or not 1<=seats<=200 or type(revision) is not int or revision<0:
        raise AccessDenied('配额须为 1–200 的整数，版本必须有效。',422)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        authorize(db,actor,org_id,platform_only=True)
        state=quota(db,org_id)
        if state['revision'] != revision:
            raise AccessDenied('席位配置已变化，请刷新后重试。',409)
        if seats < state['used']:
            raise AccessDenied('配额不能低于当前在岗成员数，请先处理离岗成员。',409)
        db.execute('''INSERT INTO org_quota(org_id,seats,updated_by,updated_at,revision) VALUES (?,?,?,?,1)
            ON CONFLICT(org_id) DO UPDATE SET seats=excluded.seats,updated_by=excluded.updated_by,
                updated_at=excluded.updated_at,revision=org_quota.revision+1''',(org_id,seats,actor['id'],now()))
        log(db,{**actor,'org_id':org_id},'set_member_quota','org',org_id,f'seats={seats};revision={revision+1}')
        return quota(db,org_id)


def set_active(store, actor, org_id, user_id, active, expected_active):
    if type(active) is not bool or type(expected_active) is not bool:
        raise AccessDenied('成员状态必须为布尔值。',422)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        authorize(db,actor,org_id)
        target=db.execute("SELECT * FROM users WHERE id=? AND org_id=? AND role!='platform_admin'",(user_id,org_id)).fetchone()
        if not target:
            raise AccessDenied('成员不存在或无权管理。')
        if actor['id']==user_id or (actor['role']=='org_admin' and target['role']=='org_admin'):
            raise AccessDenied('不能在此修改本人或其他机构管理员的状态。',403)
        if bool(target['active']) != expected_active:
            raise AccessDenied('成员状态已变化，请刷新后重试。',409)
        if bool(target['active'])==active:
            return {'id':user_id,'active':active,'quota':quota(db,org_id)}
        if active:
            require_slot(db,org_id)
        elif target['role']=='org_admin' and db.execute("SELECT COUNT(*) FROM users WHERE org_id=? AND role='org_admin' AND active=1",(org_id,)).fetchone()[0]<=1:
            raise AccessDenied('不能停用机构最后一位有效管理员。',409)
        db.execute('UPDATE users SET active=? WHERE id=?',(int(active),user_id))
        # Re-enable must never revive any pre-disable cookie, including tokens
        # left by an older deployment. Login serializes with this transaction.
        db.execute('DELETE FROM sessions WHERE user_id=?',(user_id,))
        from webapp import oauth
        # Also invalidate requests started while disabled; restoring the member
        # must require a new provider authorization rather than revive those.
        oauth.revoke_pending(db,user_id)
        from webapp import channel_bindings
        channel_bindings.revoke_pending(db,user_id)
        if not active:
            db.execute('''UPDATE email_tokens SET used_at=?,proof_hash=NULL
                WHERE email=? AND used_at IS NULL''',(now(),target['email']))
            db.execute("""UPDATE notification_deliveries SET status='suppressed',error_code='recipient_unavailable',updated_at=?
                WHERE status IN ('pending','failed') AND notification_id IN (SELECT id FROM notifications WHERE user_id=?)""",(now(),user_id))
        log(db,{**actor,'org_id':org_id},'activate_member' if active else 'deactivate_member','user',user_id,f'active={int(active)}')
        return {'id':user_id,'active':active,'quota':quota(db,org_id)}


class QuotaBody(BaseModel):
    model_config=ConfigDict(extra='forbid')
    seats: int=Field(ge=1,le=200,strict=True)
    revision: int=Field(ge=0,strict=True)


class ActiveBody(BaseModel):
    model_config=ConfigDict(extra='forbid')
    active: StrictBool
    expected_active: StrictBool


def register(app, store_provider, user_for_session, cookie_name):
    from webapp import invitations
    invitations.register(app,store_provider,user_for_session,cookie_name)
    @app.get('/api/members/organizations')
    def list_orgs(session: str | None=Cookie(default=None,alias=cookie_name)):
        return organizations(store_provider(),user_for_session(session))

    @app.get('/api/members/{org_id}')
    def list_members(org_id: str,session: str | None=Cookie(default=None,alias=cookie_name)):
        return overview(store_provider(),user_for_session(session),org_id)

    @app.put('/api/members/{org_id}/quota')
    def change_quota(org_id: str,body: QuotaBody,session: str | None=Cookie(default=None,alias=cookie_name)):
        return set_quota(store_provider(),user_for_session(session),org_id,body.seats,body.revision)

    @app.put('/api/members/{org_id}/{user_id}/active')
    def change_active(org_id: str,user_id: str,body: ActiveBody,session: str | None=Cookie(default=None,alias=cookie_name)):
        return set_active(store_provider(),user_for_session(session),org_id,user_id,body.active,body.expected_active)
