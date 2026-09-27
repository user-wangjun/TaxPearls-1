"""Browser-bound email proofs shared by registration, login and reset.

Only digests enter SQLite. Six-digit codes are HMACed with the browser secret,
so possession of a DB copy is insufficient for a one-million-value code scan.
Long magic links are consumed by an explicit POST, never by visiting a page.
"""
import hashlib
import hmac
import re
import secrets
from datetime import UTC, datetime, timedelta

from webapp import members

MINUTES = 10
MAX_ATTEMPTS = 5
PURPOSES = {'register', 'login', 'reset', 'oauth_bind', 'oauth_unbind'}
INVALID = '邮箱凭证无效或已过期，请在发起请求的浏览器中重新申请。'


def migrate(db):
    columns = {row['name'] for row in db.execute('PRAGMA table_info(email_tokens)')}
    for name in ('magic_used_at', 'proof_hash', 'invite_hash'):
        if name not in columns:
            db.execute(f'ALTER TABLE email_tokens ADD COLUMN {name} TEXT')
    db.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_email_proof ON email_tokens(proof_hash) WHERE proof_hash IS NOT NULL')


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def valid_browser(value):
    return isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_-]{43}',value) is not None


def browser_secret(existing=None):
    return existing if valid_browser(existing) else secrets.token_urlsafe(32)


def code_digest(browser, email, purpose, code):
    return hmac.new(browser.encode('ascii'),
                    f'taxpearls-email-v1\0{purpose}\0{email}\0{code}'.encode('utf-8'),
                    hashlib.sha256).hexdigest()


def issue(store, email, purpose, browser, *, invite_code=''):
    """Return delivery-only secrets; caller must never return them as API JSON.

    None for unknown/inactive login or reset identities and existing registration
    identities. Callers select an appropriate generic public response.
    """
    from webapp.storage import _validate_email, _normalize_invite_code
    if purpose not in PURPOSES or not valid_browser(browser):
        raise ValueError(INVALID)
    address = _validate_email(email)
    if not address:
        raise ValueError('邮箱格式不正确。')
    token, code = secrets.token_urlsafe(32), f'{secrets.randbelow(1000000):06d}'
    stamp = members.now()
    expires = (datetime.now(UTC)+timedelta(minutes=MINUTES)).isoformat(timespec='seconds')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        person = db.execute('SELECT active FROM users WHERE email=?',(address,)).fetchone()
        if (purpose=='register' and person) or (purpose!='register' and (not person or not person['active'])):
            return None
        db.execute('DELETE FROM email_tokens WHERE email=? AND purpose=? AND used_at IS NULL',(address,purpose))
        db.execute('''INSERT INTO email_tokens
            (token_hash,email,purpose,attempts,session_key,code_hash,expires_at,created_at,invite_hash)
            VALUES (?,?,?,0,?,?,?,?,?)''',
            (digest(token),address,purpose,digest(browser),code_digest(browser,address,purpose,code),expires,stamp,
             digest(_normalize_invite_code(invite_code)) if purpose=='register' and invite_code else None))
    return {'email':address,'code':code,'token':token,'expires_at':expires,'purpose':purpose}


def revoke_delivery(store, token):
    """Failed delivery invalidates only its own request, not a later resend."""
    with store.connect() as db:
        db.execute('UPDATE email_tokens SET used_at=? WHERE token_hash=? AND used_at IS NULL',
                   (members.now(),digest(token)))


def bound(row, browser, purpose=None):
    return bool(row and valid_browser(browser) and row['session_key']
                and hmac.compare_digest(row['session_key'],digest(browser))
                and not row['used_at'] and row['expires_at']>members.now()
                and row['attempts']<MAX_ATTEMPTS and (purpose is None or row['purpose']==purpose))


def verify_code(db, email, purpose, code, browser):
    """Return (row, error). Caller MUST commit wrong-code attempt increments.

    A different browser cannot burn someone else's attempts. This function
    neither consumes a correct proof nor opens a transaction; the caller keeps
    proof validation and its business mutation within one BEGIN IMMEDIATE.
    """
    row = db.execute('SELECT * FROM email_tokens WHERE email=? AND purpose=? AND used_at IS NULL',
                     (email,purpose)).fetchone()
    if not bound(row,browser,purpose):
        return None, INVALID
    if not row['code_hash'] or not hmac.compare_digest(row['code_hash'],code_digest(browser,email,purpose,(code or '').strip())):
        attempts = row['attempts']+1
        db.execute('UPDATE email_tokens SET attempts=?,used_at=? WHERE token_hash=?',
                   (attempts,members.now() if attempts>=MAX_ATTEMPTS else None,row['token_hash']))
        return None, '验证码错误次数过多，请重新获取。' if attempts>=MAX_ATTEMPTS else f'验证码不正确（还可尝试 {MAX_ATTEMPTS-attempts} 次）。'
    return row,None


def registration_proof(db, email, proof, browser):
    row = db.execute("SELECT * FROM email_tokens WHERE proof_hash=? AND email=? AND purpose='register'",(digest(proof),email)).fetchone()
    if not bound(row,browser,'register'):
        raise ValueError(INVALID)
    return row


def consume(db, row):
    db.execute('UPDATE email_tokens SET used_at=?,proof_hash=NULL WHERE token_hash=?',
               (members.now(),row['token_hash']))


def login_session(db, email):
    """Call under the same write lock as proof redemption and member disable."""
    person = db.execute('''SELECT id,username,display_name,role,org_id,active,email,created_at
        FROM users WHERE email=? AND active=1''',(email,)).fetchone()
    if not person:
        raise ValueError(INVALID)
    token = secrets.token_urlsafe(32)
    from webapp.storage import SESSION_HOURS
    expires = (datetime.now(UTC)+timedelta(hours=SESSION_HOURS)).isoformat(timespec='seconds')
    db.execute('INSERT INTO sessions VALUES (?,?,?,?)',(digest(token),person['id'],expires,members.now()))
    members.log(db,dict(person),'email_login','user',person['id'],'browser_bound=1')
    return dict(person),token


def login_with_code(store, email, code, browser):
    from webapp.storage import _validate_email
    address = _validate_email(email)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row,error = verify_code(db,address,'login',code,browser)
        if not error:
            user,session = login_session(db,address)
            consume(db,row)
    if error:
        raise ValueError(error)
    return user,session


def redeem_magic(store, token, browser):
    """Explicitly redeem once. Registration/reset yield a short-lived proof;
    login creates the real session. No GET endpoint should call this method.
    """
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM email_tokens WHERE token_hash=?',(digest(token),)).fetchone()
        if not bound(row,browser) or row['magic_used_at'] or row['purpose'] not in {'register','login','reset'}:
            raise ValueError(INVALID)
        if row['purpose']=='login':
            user,session = login_session(db,row['email'])
            consume(db,row)
            return {'purpose':'login','user':user,'session':session}
        # A reset proof cannot revive an inactive or removed identity.
        if row['purpose']=='reset' and not db.execute('SELECT 1 FROM users WHERE email=? AND active=1',(row['email'],)).fetchone():
            raise ValueError(INVALID)
        proof = secrets.token_urlsafe(32)
        db.execute('UPDATE email_tokens SET magic_used_at=?,proof_hash=? WHERE token_hash=?',
                   (members.now(),digest(proof),row['token_hash']))
        return {'purpose':row['purpose'],'email':row['email'],'proof':proof,
                'has_invite':bool(row['invite_hash']),'expires_at':row['expires_at']}


def reset_password(store, proof, password, browser):
    from webapp.storage import _validate_password, _passwords
    _validate_password(password)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT * FROM email_tokens WHERE proof_hash=? AND purpose='reset'",(digest(proof),)).fetchone()
        if not bound(row,browser,'reset'):
            raise ValueError(INVALID)
        user = db.execute('''SELECT id,username,display_name,role,org_id,active,email,created_at
            FROM users WHERE email=? AND active=1''',(row['email'],)).fetchone()
        if not user:
            raise ValueError(INVALID)
        _validate_password(password,user['email'])
        db.execute('UPDATE users SET password_hash=? WHERE id=?',(_passwords.hash(password),user['id']))
        db.execute('DELETE FROM sessions WHERE user_id=?',(user['id'],))
        from webapp import oauth
        oauth.revoke_pending(db,user['id'])
        from webapp import channel_bindings
        channel_bindings.revoke_pending(db,user['id'])
        # Includes other purposes: a pre-reset emailed login proof must not
        # immediately bypass the new password or re-establish an old session.
        db.execute('UPDATE email_tokens SET used_at=?,proof_hash=NULL WHERE email=? AND used_at IS NULL',
                   (members.now(),user['email']))
        members.log(db,dict(user),'password_reset','user',user['id'],'browser_bound=1')
        return dict(user)
