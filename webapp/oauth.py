"""G12 persistence boundary for OAuth authorization-code flows.

Provider transport must validate the code server-side before calling complete().
No provider email, nickname, role or institution can select a local account.
Local email ownership plus a live local session is required to create a binding.
Only hashes of state, browser secrets and provider subjects are persisted; PKCE
verifiers are derived from the browser secret and never stored in SQLite.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from datetime import UTC, datetime, timedelta

from webapp import email_auth, members

PROVIDERS = frozenset({'google', 'microsoft', 'github'})
FLOW_SECONDS = 600
MAX_FLOWS = 4096
INVALID = '第三方登录请求无效或已过期，请在发起请求的浏览器重新操作。'
UNBOUND = '该第三方身份尚未绑定。请先使用邮箱登录或邀请码注册，再在账号安全中绑定。'
USER_COLUMNS = 'u.id,u.username,u.display_name,u.role,u.org_id,u.active,u.email,u.created_at'


def migrate(db):
    db.execute('''CREATE TABLE IF NOT EXISTS oauth_identities (
        provider TEXT NOT NULL, client_key TEXT NOT NULL, subject_hash TEXT NOT NULL,
        user_id TEXT NOT NULL REFERENCES users(id), bound_at REAL NOT NULL,
        valid_after REAL NOT NULL,
        PRIMARY KEY(provider,client_key,subject_hash), UNIQUE(user_id,provider)
    )''')
    db.execute('''CREATE TABLE IF NOT EXISTS oauth_flows (
        state_hash TEXT PRIMARY KEY, browser_hash TEXT NOT NULL,
        provider TEXT NOT NULL, client_key TEXT NOT NULL,
        purpose TEXT NOT NULL CHECK(purpose IN ('login','bind')),
        session_hash TEXT, user_id TEXT REFERENCES users(id), email TEXT,
        redirect_uri TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('pending','exchanging','completed','cancelled'))
    )''')
    db.execute('CREATE INDEX IF NOT EXISTS idx_oauth_flow_expiry ON oauth_flows(expires_at)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_oauth_flow_user ON oauth_flows(user_id)')


def _provider(provider, client_id):
    if provider not in PROVIDERS or not isinstance(client_id,str) or not 1<=len(client_id)<=512:
        raise ValueError(INVALID)
    return email_auth.digest(client_id)


def _session_user(db, session):
    if not isinstance(session,str) or not session or len(session)>512:
        raise ValueError('请重新登录后操作。')
    row=db.execute(f'''SELECT {USER_COLUMNS} FROM sessions s JOIN users u ON u.id=s.user_id
        WHERE s.token_hash=? AND s.expires_at>? AND u.active=1''',
        (email_auth.digest(session),members.now())).fetchone()
    if not row:
        raise ValueError('请重新登录后操作。')
    return dict(row)


def _email_user(db, session):
    user=_session_user(db,session)
    if not user['email']:
        raise ValueError('账号未绑定邮箱，不能配置第三方登录。')
    return user


def issue_management_code(store, session, action, browser):
    """Delivery-only result. HTTP layer must rate-limit and send ONLY the code."""
    if action not in {'bind','unbind'}:
        raise ValueError(INVALID)
    with store.connect() as db:
        user=_email_user(db,session)
    delivery=email_auth.issue(store,user['email'],'oauth_'+action,browser)
    if not delivery:
        raise ValueError('请重新登录后操作。')
    return delivery


def _pkce(browser, state, provider, client_key):
    raw=hmac.new(browser.encode('ascii'),
        f'taxpearls-oauth-pkce-v1\0{provider}\0{client_key}\0{state}'.encode('ascii'),hashlib.sha256).digest()
    verifier=base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')
    challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest()).decode('ascii').rstrip('=')
    return verifier,challenge


def begin(store, provider, client_id, redirect_uri, browser, *, purpose='login',
          session='', email_code='', email_browser=''):
    """Return authorization-request state/challenge; never a local login session.

    redirect_uri must come from server configuration, not user input. Binding
    consumes a purpose-specific fresh email proof, but never creates an account.
    """
    client_key=_provider(provider,client_id)
    if purpose not in {'login','bind'} or not email_auth.valid_browser(browser):
        raise ValueError(INVALID)
    state=secrets.token_urlsafe(32)
    stamp=time.time()
    user=None
    error=None
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('DELETE FROM oauth_flows WHERE expires_at<=?',(stamp,))
        if db.execute('SELECT COUNT(*) FROM oauth_flows').fetchone()[0]>=MAX_FLOWS:
            raise ValueError('第三方登录请求容量暂满，请稍后重试。')
        if purpose=='bind':
            user=_email_user(db,session)
            if db.execute('SELECT 1 FROM oauth_identities WHERE user_id=? AND provider=?',(user['id'],provider)).fetchone():
                raise ValueError('该平台已绑定，请先解绑后再更换身份。')
            proof,error=email_auth.verify_code(db,user['email'],'oauth_bind',email_code,email_browser)
            if not error:
                email_auth.consume(db,proof)
        if not error:
            db.execute('''DELETE FROM oauth_flows WHERE browser_hash=? AND provider=? AND purpose=?''',
                       (email_auth.digest(browser),provider,purpose))
            db.execute('''INSERT INTO oauth_flows VALUES (?,?,?,?,?,?,?,?,?,?,?, 'pending')''',
                (email_auth.digest(state),email_auth.digest(browser),provider,client_key,purpose,
                 email_auth.digest(session) if user else None,user['id'] if user else None,
                 user['email'] if user else None,redirect_uri,stamp,stamp+FLOW_SECONDS))
    if error:
        raise ValueError(error)
    _,challenge=_pkce(browser,state,provider,client_key)
    return {'state':state,'code_challenge':challenge,'code_challenge_method':'S256',
            'redirect_uri':redirect_uri,'expires_in':FLOW_SECONDS}


def _flow(db, state, browser, provider, client_id, status):
    key=_provider(provider,client_id)
    if not email_auth.valid_browser(state) or not email_auth.valid_browser(browser):
        raise ValueError(INVALID)
    row=db.execute('SELECT * FROM oauth_flows WHERE state_hash=?',(email_auth.digest(state),)).fetchone()
    if (not row or row['status']!=status or row['expires_at']<=time.time()
            or row['provider']!=provider or row['client_key']!=key
            or not hmac.compare_digest(row['browser_hash'],email_auth.digest(browser))):
        raise ValueError(INVALID)
    return dict(row)


def _binding_actor(db, flow, session):
    user=_email_user(db,session)
    if (flow['user_id']!=user['id'] or flow['session_hash']!=email_auth.digest(session)
            or flow['email']!=user['email']):
        raise ValueError(INVALID)
    return user


def claim(store, state, browser, provider, client_id, *, session=''):
    """Consume state BEFORE any provider request; exactly one exchange can start.

    A failed/timeout exchange cannot be retried using this state. Network IO must
    run outside the DB lock. complete() rechecks the session/member afterwards.
    """
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        flow=_flow(db,state,browser,provider,client_id,'pending')
        if flow['purpose']=='bind':
            _binding_actor(db,flow,session)
        db.execute("UPDATE oauth_flows SET status='exchanging' WHERE state_hash=?",(flow['state_hash'],))
    verifier,_=_pkce(browser,state,provider,flow['client_key'])
    return {'purpose':flow['purpose'],'redirect_uri':flow['redirect_uri'],'code_verifier':verifier}


def cancel(store, state, browser, provider, client_id):
    """Provider denial/errors burn a correctly bound state, never another browser's."""
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            flow=_flow(db,state,browser,provider,client_id,'pending')
        except ValueError:
            flow=_flow(db,state,browser,provider,client_id,'exchanging')
        db.execute("UPDATE oauth_flows SET status='cancelled' WHERE state_hash=?",(flow['state_hash'],))


def _session(db, user, action, provider):
    from webapp.storage import SESSION_HOURS
    token=secrets.token_urlsafe(32)
    expiry=(datetime.now(UTC)+timedelta(hours=SESSION_HOURS)).isoformat(timespec='seconds')
    db.execute('INSERT INTO sessions(token_hash,user_id,expires_at,created_at) VALUES (?,?,?,?)',
               (email_auth.digest(token),user['id'],expiry,members.now()))
    members.log(db,user,action,'user',user['id'],f'provider={provider}')
    return {'user':user,'session':token}


def complete(store, state, browser, provider, client_id, subject, *, session=''):
    """Internal only: subject MUST come from the provider's authenticated API.

    Never expose a route accepting subject/identity JSON from the browser. The
    provider transport must pass the subject, not an email or unvalidated JWT.
    """
    if not isinstance(subject,str) or not 1<=len(subject)<=512 or any(ord(c)<32 for c in subject):
        raise ValueError(INVALID)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        flow=_flow(db,state,browser,provider,client_id,'exchanging')
        identity=db.execute('''SELECT * FROM oauth_identities
            WHERE provider=? AND client_key=? AND subject_hash=?''',
            (provider,flow['client_key'],email_auth.digest(subject))).fetchone()
        if flow['purpose']=='bind':
            user=_binding_actor(db,flow,session)
            if identity or db.execute('SELECT 1 FROM oauth_identities WHERE user_id=? AND provider=?',(user['id'],provider)).fetchone():
                raise ValueError('该平台身份已绑定，请勿重复绑定或跨账号绑定。')
            stamp=time.time()
            db.execute('INSERT INTO oauth_identities VALUES (?,?,?,?,?,?)',
                       (provider,flow['client_key'],email_auth.digest(subject),user['id'],stamp,stamp))
            members.log(db,user,'oauth_bind','user',user['id'],f'provider={provider}')
            result={'purpose':'bind','provider':provider}
        else:
            if not identity:
                raise ValueError(UNBOUND)
            # Unlink/rebind, password reset or disable/restore must not revive
            # a provider request that was already in flight beforehand.
            if flow['created_at']<=identity['valid_after']:
                raise ValueError(INVALID)
            row=db.execute(f'SELECT {USER_COLUMNS} FROM users u WHERE id=? AND active=1',
                           (identity['user_id'],)).fetchone()
            if not row or not row['email']:
                raise ValueError(INVALID)
            result={'purpose':'login',**_session(db,dict(row),'oauth_login',provider)}
        db.execute("UPDATE oauth_flows SET status='completed' WHERE state_hash=?",(flow['state_hash'],))
        return result


def identities(store, session):
    with store.connect() as db:
        db.execute('BEGIN')
        user=_session_user(db,session)
        return [dict(row) for row in db.execute('''SELECT provider,bound_at FROM oauth_identities
            WHERE user_id=? ORDER BY provider''',(user['id'],))]


def revoke_pending(db, user_id):
    """Call inside password-reset/member-change/unlink's existing transaction."""
    db.execute('UPDATE oauth_identities SET valid_after=? WHERE user_id=?',(time.time(),user_id))
    db.execute("UPDATE oauth_flows SET status='cancelled' WHERE user_id=? AND status IN ('pending','exchanging')",(user_id,))


def unlink(store, session, provider, email_code, email_browser):
    if provider not in PROVIDERS:
        raise ValueError(INVALID)
    error=None
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        user=_email_user(db,session)
        if not db.execute('SELECT 1 FROM oauth_identities WHERE user_id=? AND provider=?',(user['id'],provider)).fetchone():
            raise ValueError('该平台尚未绑定。')
        proof,error=email_auth.verify_code(db,user['email'],'oauth_unbind',email_code,email_browser)
        if not error:
            email_auth.consume(db,proof)
            db.execute('DELETE FROM oauth_identities WHERE user_id=? AND provider=?',(user['id'],provider))
            revoke_pending(db,user['id'])
            # Verified email remains a login/recovery channel even after removing
            # the last OAuth identity. Rotate the current session, revoke all old.
            db.execute('DELETE FROM sessions WHERE user_id=?',(user['id'],))
            db.execute('UPDATE email_tokens SET used_at=?,proof_hash=NULL WHERE email=? AND used_at IS NULL',
                       (members.now(),user['email']))
            result={'purpose':'unlink','provider':provider,**_session(db,user,'oauth_unbind',provider)}
    if error:
        raise ValueError(error)
    return result
