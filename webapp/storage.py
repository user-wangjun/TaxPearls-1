"""P1 persistence, authentication and audit trail.

Only normalized audit results are persisted.  Uploaded workbooks are parsed in
memory and are never written to disk.  Passwords use Argon2id and session
tokens are stored as SHA-256 digests so a database copy cannot be used as a
logged-in browser session.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import unicodedata
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import Any
from contextlib import contextmanager
from webapp import classroom, mistake_book, members, invitations, email_auth
from webapp.access import AccessDenied, audit_row, audit_scope, current_actor, is_teaching_dataset

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from src.models import Dataset, Finding

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "instance" / "taxpearls.db"
SESSION_HOURS = 12
REMEMBER_DAYS = 30
ROLES = {"teacher", "student", "org_admin", "accountant", "platform_admin"}
COMMON_PASSWORDS = {
    "password123!", "password1234!", "password12345!", "password2026!",
    "admin123456!", "administrator1!", "1234567890a!", "qwerty12345!",
    "qwerty123456!", "welcome123!", "welcome1234!", "changeme123!",
    "letmein123!", "iloveyou123!", "abc123456!", "test123456!",
}


class SetupAlreadyInitialized(Exception):
    pass

_passwords = PasswordHasher()


def session_max_age(remember: bool = False) -> int:
    return REMEMBER_DAYS * 24 * 3600 if remember else SESSION_HOURS * 3600


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _validate_password(password: str, email: str = "") -> None:
    """One setting policy for every role; normalization is for checks, not hashes."""
    if not isinstance(password, str) or not 10 <= len(password) <= 128:
        raise ValueError("密码长度须为 10–128 位")
    canonical = unicodedata.normalize("NFKC", password)
    classes = sum((any(ch.islower() for ch in canonical),
                   any(ch.isupper() for ch in canonical),
                   any(ch.isdecimal() for ch in canonical),
                   any(not ch.isalnum() and not ch.isspace() for ch in canonical)))
    if classes < 3 or canonical.strip().casefold() in COMMON_PASSWORDS:
        raise ValueError("密码须包含至少三类字符，且不能使用常见弱口令")
    local = unicodedata.normalize("NFKC", _normalize_email(email).partition("@")[0]).casefold()
    if local and local in canonical.casefold():
        raise ValueError("密码不能包含邮箱的 @ 前部分（不区分大小写）")


def _normalize_email(email: str) -> str:
    """邮箱归一化：去空格 + 转小写。同一邮箱只允许一个账号。"""
    return (email or "").strip().lower()


def _validate_email(email: str) -> str:
    """校验并归一化邮箱；允许为空（邮箱是可选绑定项），非法格式直接拒绝。"""
    address = _normalize_email(email)
    if not address:
        return ""
    if " " in address or address.count("@") != 1 or len(address) > 254:
        raise ValueError("邮箱格式不正确")
    local, _, domain = address.partition("@")
    if not local or not domain or "." not in domain or domain.startswith(".") or domain.endswith("."):
        raise ValueError("邮箱格式不正确")
    return address


INVITE_CODE_DAYS = 7
CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _generate_invite_code(length: int = 12) -> str:
    """生成邀请码：Crockford Base32（排除 I/L/O/U，5.7.2）。"""
    return "".join(secrets.choice(CROCKFORD_ALPHABET) for _ in range(length))


def _normalize_invite_code(code: str) -> str:
    """邀请码规范化：去分隔符与空白 → 大写 → 形近归一。"""
    cleaned = "".join(ch for ch in (code or "").upper() if ch.isalnum())
    return cleaned.translate(str.maketrans({"I": "1", "L": "1", "O": "0"}))


def _username_from_email(email: str, db) -> str:
    """由邮箱本地部分生成用户名，撞名自动追加随机后缀。"""
    local = _normalize_email(email).partition("@")[0]
    base = "".join(ch for ch in local if ch.isalnum())[:16] or "user"
    if not db.execute("SELECT 1 FROM users WHERE username=?", (base,)).fetchone():
        return base
    for _ in range(5):
        candidate = f"{base}{secrets.token_hex(2)}"
        if not db.execute("SELECT 1 FROM users WHERE username=?", (candidate,)).fetchone():
            return candidate
    return f"{base}{secrets.token_hex(4)}"


from src.snapshots import serialize_dataset, deserialize_dataset, serialize_findings, deserialize_findings


class Store:
    """Small SQLite repository with explicit organization and ownership checks."""

    def __init__(self, path: str | Path | None = None, *, notification_adapters=None) -> None:
        from webapp.notifications import channel_registry
        self.notification_adapters = channel_registry(notification_adapters)
        configured = os.environ.get("TAXPEARLS_DB")
        p = Path(path or configured or DEFAULT_DB)
        # 相对路径一律锚定项目根，与启动时的工作目录无关（防止在子目录启动时误建空库）。
        self.path = p if p.is_absolute() else (ROOT / p).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._init_schema()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            # journal_mode=WAL 在 _init_schema 时设置一次并持久化于库文件；
            # 不在每次连接时执行——并发连接同时切 WAL 在 Windows 上会以
            # SQLITE_READONLY 的面目报错（tests/test_auth_hardening 的偶发抖动根因）。
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _init_schema(self) -> None:
        from webapp.schema import initialize
        initialize(self)

    def has_users(self) -> bool:
        with self.connect() as db:
            return bool(db.execute("SELECT 1 FROM users LIMIT 1").fetchone())

    ORG_DEFAULTS = {
        "org_id": "", "display_name": "税海拾珠", "report_title": "税务风险审计报告",
        "footer_text": "", "logo_mime": None, "logo_updated_at": None, "has_logo": False,
    }

    @staticmethod
    def _org_access(db, org_id, actor, *, write=False):
        # actor=None is reserved for trusted internal rendering/provisioning.
        # Every public branding handler supplies its authenticated actor.
        if actor is not None:
            current_actor(db, actor, {'org_admin'} if write else {'org_admin', 'accountant', 'teacher', 'student'})
            if org_id != actor['org_id']:
                raise AccessDenied('无权访问该机构设置。', 403)

    def _org_settings(self, db, org_id):
        row = db.execute(
            """SELECT org_id,display_name,report_title,footer_text,logo_mime,logo_updated_at
               FROM org_settings WHERE org_id=?""", (org_id,),
        ).fetchone()
        if row:
            result = dict(row)
            result["has_logo"] = bool(result["logo_mime"])
            return result
        return {**self.ORG_DEFAULTS, "org_id": org_id}

    def get_org_settings(self, org_id: str, *, actor=None) -> dict[str, Any]:
        with self.connect() as db:
            db.execute('BEGIN')
            self._org_access(db, org_id, actor)
            return self._org_settings(db, org_id)

    def update_org_settings(
        self, org_id: str, display_name: str, report_title: str, footer_text: str, *, actor=None
    ) -> dict[str, Any]:
        display_name = display_name.strip()
        report_title = report_title.strip()
        footer_text = footer_text.strip()
        if not display_name or not report_title:
            raise ValueError("机构名称与报告标题不能为空")
        if any(ord(ch) < 32 for ch in display_name + report_title):
            raise ValueError("机构名称与报告标题不能包含控制字符")
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._org_access(db, org_id, actor, write=True)
            db.execute(
                "INSERT INTO org_settings (org_id, display_name, report_title, footer_text, updated_at)"
                " VALUES (?,?,?,?,?)"
                " ON CONFLICT(org_id) DO UPDATE SET display_name=excluded.display_name,"
                " report_title=excluded.report_title, footer_text=excluded.footer_text,"
                " updated_at=excluded.updated_at",
                (org_id, display_name, report_title, footer_text, _now()),
            )
            if actor is not None:
                self._log(db, actor, 'update_org_settings', 'org', org_id, f'title={report_title}')
            return self._org_settings(db, org_id)

    def get_org_logo(self, org_id: str, *, actor=None) -> tuple[str, bytes] | None:
        with self.connect() as db:
            db.execute('BEGIN')
            self._org_access(db, org_id, actor)
            row = db.execute(
                "SELECT logo_mime,logo_bytes FROM org_settings WHERE org_id=?", (org_id,)
            ).fetchone()
        if not row or not row["logo_mime"] or not row["logo_bytes"]:
            return None
        return str(row["logo_mime"]), bytes(row["logo_bytes"])

    def update_org_logo(self, org_id: str, mime: str, content: bytes, *, actor=None) -> dict[str, Any]:
        now = _now()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._org_access(db, org_id, actor, write=True)
            db.execute(
                """INSERT INTO org_settings
                       (org_id,display_name,report_title,footer_text,updated_at,logo_mime,logo_bytes,logo_updated_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(org_id) DO UPDATE SET logo_mime=excluded.logo_mime,
                       logo_bytes=excluded.logo_bytes,logo_updated_at=excluded.logo_updated_at,
                       updated_at=excluded.updated_at""",
                (org_id, self.ORG_DEFAULTS["display_name"], self.ORG_DEFAULTS["report_title"], "",
                 now, mime, content, now),
            )
            if actor is not None:
                self._log(db, actor, 'update_org_logo', 'org', org_id, f'mime={mime}; bytes={len(content)}')
            return self._org_settings(db, org_id)

    def clear_org_logo(self, org_id: str, *, actor=None) -> dict[str, Any]:
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._org_access(db, org_id, actor, write=True)
            db.execute(
                "UPDATE org_settings SET logo_mime=NULL,logo_bytes=NULL,logo_updated_at=NULL,updated_at=? WHERE org_id=?",
                (_now(), org_id),
            )
            if actor is not None:
                self._log(db, actor, 'delete_org_logo', 'org', org_id)
            return self._org_settings(db, org_id)

    def create_user(self, username: str, password: str, display_name: str, role: str,
                    org_id: str, email: str = "", *, actor=None) -> dict[str, Any]:
        """Internal provisioning, or authenticated org-admin accountant creation.

        Public handlers must supply actor; platform self-service is invite-only.
        """
        username = username.strip().lower()
        if role not in ROLES:
            raise ValueError("无效角色")
        if len(username) < 3 or not display_name.strip() or not org_id.strip():
            raise ValueError("用户名至少3位，姓名和机构不能为空")
        address = _validate_email(email)  # 可选；非法格式拒绝，空则不绑定
        _validate_password(password, address)
        user_id = secrets.token_hex(12)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if actor is not None:
                current_actor(db, actor, {'org_admin'})
                if role != 'accountant' or org_id.strip() != actor['org_id']:
                    raise AccessDenied('机构管理员只能创建本机构会计账号。', 403)
                members.require_slot(db, actor['org_id'])
            db.execute(
                """INSERT INTO users (id,username,password_hash,display_name,role,org_id,active,email,created_at)
                   VALUES (?,?,?,?,?,?,1,?,?)""",
                (user_id, username, _passwords.hash(password), display_name.strip(), role,
                 org_id.strip(), address or None, _now()),
            )
            if actor is not None:
                db.execute('INSERT INTO member_origins VALUES (?,?,?,?,?)',
                           (user_id,'admin_created',None,actor['id'],_now()))
                self._log(db, actor, 'create_user', 'user', user_id, role)
        return self.get_user(user_id)

    def create_initial_admin(self, username: str, password: str, display_name: str,
                             org_id: str, email: str = "") -> dict[str, Any]:
        username = username.strip().lower()
        if len(username) < 3 or not display_name.strip() or not org_id.strip():
            raise ValueError("用户名至少3位，姓名和机构不能为空")
        address = _validate_email(email)
        _validate_password(password, address)
        user_id = secrets.token_hex(12)
        with self._lock:
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                    raise SetupAlreadyInitialized()
                # The public setup endpoint remains reachable after bootstrap;
                # reject before expensive Argon2 work, under the same write lock.
                password_hash = _passwords.hash(password)
                db.execute(
                    """INSERT INTO users (id,username,password_hash,display_name,role,org_id,active,email,created_at)
                       VALUES (?,?,?,?,?,?,1,?,?)""",
                    (user_id, username, password_hash, display_name.strip(), "platform_admin",
                     org_id.strip(), address or None, _now()),
                )
                self._log(db, {'id': user_id, 'org_id': org_id.strip()}, 'setup', 'system', 'initial')
        user = self.get_user(user_id)
        assert user is not None
        return user

    @staticmethod
    def _with_email(row: dict[str, Any]) -> dict[str, Any]:
        # 对外统一为空字符串，避免前端把未绑定邮箱渲染成 null。
        row["email"] = row.get("email") or ""
        return row

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT id,username,display_name,role,org_id,active,created_at,email FROM users WHERE id=?", (user_id,)
            ).fetchone()
        return self._with_email(dict(row)) if row else None

    def list_users(self, org_id: str | None = None, *, actor=None) -> list[dict[str, Any]]:
        sql = "SELECT id,username,display_name,role,org_id,active,created_at,email FROM users"
        args: tuple[Any, ...] = ()
        if org_id:
            sql += " WHERE org_id=?"
            args = (org_id,)
        with self.connect() as db:
            db.execute('BEGIN')
            if actor is not None:
                current_actor(db, actor, {'platform_admin', 'org_admin', 'teacher'})
                if actor['role'] != 'platform_admin' and (not org_id or org_id != actor['org_id']):
                    raise AccessDenied('无权读取该机构成员。', 403)
                if actor['role'] == 'teacher':
                    # Only the active student picker, not management/email data.
                    return [dict(row) for row in db.execute(
                        "SELECT id,username,display_name,role,active FROM users WHERE org_id=? AND role='student' AND active=1 ORDER BY created_at",
                        (org_id,))]
            return [self._with_email(dict(row)) for row in db.execute(sql + " ORDER BY created_at", args)]

    def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        address = _normalize_email(email)
        if not address:
            return None
        with self.connect() as db:
            row = db.execute(
                """SELECT id,username,display_name,role,org_id,active,created_at,email
                   FROM users WHERE email=? AND active=1""",
                (address,),
            ).fetchone()
        return self._with_email(dict(row)) if row else None

    def create_invite_code(self, creator: dict[str, Any], org_name: str, seats: int,
                           bound_email: str = "", expires_days: int = INVITE_CODE_DAYS) -> dict[str, Any]:
        """平台管理员签发一次性创始码。明文只在此刻返回一次。

        创始码只与机构名称捆绑（不绑邮箱）：一码一位、私聊交付；注册时
        仍须通过邮箱验证码核验，构成「码 + 邮箱验证」双因子。bound_email
        留空即不绑定（保留参数以兼容旧码）。
        """
        if creator.get("role") != "platform_admin":
            raise ValueError("只有平台管理员可以签发创始码")
        org_name = (org_name or "").strip()
        if not org_name:
            raise ValueError("机构名称不能为空")
        if not isinstance(seats, int) or not 1 <= seats <= 200:
            raise ValueError("席位须为 1–200 的整数")
        address = _validate_email(bound_email) if (bound_email or "").strip() else ""
        code = _generate_invite_code(12)
        expires = (datetime.now(UTC) + timedelta(days=expires_days)).isoformat(timespec="seconds")
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current_actor(db, creator, {'platform_admin'})
            db.execute(
                """INSERT INTO invite_codes
                       (token_hash,org_name,seats,bound_email,expires_at,revoked,created_by,created_at)
                   VALUES (?,?,?,?,?,0,?,?)""",
                (_hash_token(_normalize_invite_code(code)), org_name, seats, address, expires,
                 creator["id"], _now()),
            )
            self._log(db, creator, 'invite_created', 'invite', _hash_token(_normalize_invite_code(code)),
                      f'org={org_name};seats={seats}')
        return {"code": code, "org_name": org_name, "seats": seats,
                "bound_email": address, "expires_at": expires}

    def list_invite_codes(self, *, actor=None) -> list[dict[str, Any]]:
        """平台管理员查看创始码（不含明文）。"""
        with self.connect() as db:
            db.execute('BEGIN')
            if actor is not None:
                current_actor(db, actor, {'platform_admin'})
            rows = db.execute(
                """SELECT i.token_hash,i.org_name,i.seats,i.bound_email,i.expires_at,i.redeemed_by,
                          i.redeemed_at,i.revoked,i.created_at,u.email AS redeemed_email
                   FROM invite_codes i LEFT JOIN users u ON u.id=i.redeemed_by
                   ORDER BY i.created_at DESC"""
            ).fetchall()
        return [dict(row) for row in rows]

    def register_with_code(self, email: str, code: str, invite_code: str,
                           password: str, *, browser_session: str | None = None,
                           email_proof: str = '') -> tuple[dict[str, Any], str]:
        """邮箱验证后按完整凭证哈希查找创始码或成员邀请。

        凭证决定机构和角色；席位、核销、来源、审计及会话同事务。
        仅错误验证码的尝试计数独立提交，其余失败不消耗有效凭证。
        """
        address = _validate_email(email)
        if not address:
            raise ValueError("邮箱格式不正确")
        _validate_password(password, address)
        now = _now()
        invite_hash = _hash_token(_normalize_invite_code(invite_code))
        # ⚠️ 验证码输错时的尝试计数必须**提交**而非回滚——在 with 块内 raise 会触发
        # 整体回滚（含 attempts 自增），计数永远停在 0。因此错误以标志位记录、
        # 事务提交后再抛出。
        error: str | None = None
        user_id = org_id = session_token = None
        with self._lock:
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                if not email_auth.valid_browser(browser_session):
                    raise ValueError(email_auth.INVALID)
                if email_proof:
                    token_row = email_auth.registration_proof(db,address,email_proof,browser_session)
                else:
                    token_row,error = email_auth.verify_code(db,address,'register',code,browser_session)
                if token_row and token_row['invite_hash']:
                    if invite_code and invite_hash != token_row['invite_hash']:
                        error = '邀请凭证与发起验证时不一致，请重新获取邮箱验证。'
                    invite_hash = token_row['invite_hash']

                if error is None:
                    # Both tables are searched by the FULL hash. Never dispatch
                    # by length/prefix, even for legacy or deliberately crafted rows.
                    invite = db.execute(
                        """SELECT org_name,seats,bound_email,expires_at,revoked,redeemed_by
                           FROM invite_codes WHERE token_hash=?""",
                        (invite_hash,),
                    ).fetchone()
                    member_invite = db.execute('SELECT 1 FROM member_invitations WHERE token_hash=?', (invite_hash,)).fetchone()
                    if invite and member_invite:
                        error = "邀请码无效。"
                    elif member_invite:
                        member_invite = invitations.resolve(db,invite_hash,address)
                    elif not invite or invite["revoked"]:
                        error = "邀请码无效。"
                    elif invite["redeemed_by"]:
                        error = "邀请码已被使用。"
                    elif invite["expires_at"] < now:
                        error = "邀请码已过期，请联系平台管理员重新签发。"
                    elif invite["bound_email"] and _normalize_email(invite["bound_email"]) != address:
                        error = "该创始码绑定的是其他邮箱，请使用绑定的邮箱注册。"
                    if error is None and db.execute("SELECT 1 FROM users WHERE email=?", (address,)).fetchone():
                        error = "该邮箱已注册，请直接登录。"  # 一个邮箱 = 一个账号 = 一个机构（5.8.3）
                if error is None:
                    org_id = member_invite['org_id'] if member_invite else "org-" + secrets.token_hex(6)
                    if member_invite:
                        members.require_slot(db,org_id)
                    role = member_invite['role'] if member_invite else 'org_admin'
                    user_id = secrets.token_hex(12)
                    username = _username_from_email(address, db)
                    db.execute(
                        """INSERT INTO users (id,username,password_hash,display_name,role,org_id,active,email,created_at)
                           VALUES (?,?,?,?,?,?,1,?,?)""",
                        (user_id, username, _passwords.hash(password), username if member_invite else invite["org_name"], role,
                         org_id, address, now),
                    )
                    if member_invite:
                        db.execute('UPDATE member_invitations SET used_count=used_count+1,last_used_at=? WHERE id=?',
                                   (now,member_invite['id']))
                        db.execute('INSERT INTO member_origins VALUES (?,?,?,?,?)',
                                   (user_id,'member_invitation',member_invite['id'],member_invite['created_by'],now))
                    else:
                        db.execute(
                            "INSERT INTO org_quota (org_id,seats,updated_at) VALUES (?,?,?)",
                            (org_id, invite["seats"], now),
                        )
                        db.execute("UPDATE invite_codes SET redeemed_by=?,redeemed_at=? WHERE token_hash=?",
                                   (user_id, now, invite_hash))
                    db.execute("UPDATE email_tokens SET used_at=? WHERE token_hash=?",
                               (now, token_row["token_hash"]))
                    session_token = secrets.token_urlsafe(32)
                    expires = (datetime.now(UTC) + timedelta(hours=SESSION_HOURS)).isoformat(timespec="seconds")
                    db.execute("INSERT INTO sessions VALUES (?,?,?,?)",
                               (_hash_token(session_token), user_id, expires, now))
                    members.log(db,{'id':user_id,'org_id':org_id},'register','user',user_id,
                                f'credential={member_invite["id"] if member_invite else "founder"};role={role}')
        if error:
            raise ValueError(error)
        user = self.get_user(user_id)
        assert user is not None
        return user, session_token

    def authenticate(self, username: str, password: str, *, remember: bool = False) -> tuple[dict[str, Any], str] | None:
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            identity = username.strip().lower()
            # A bound email takes precedence over a legacy username with the
            # same spelling, including when that email's account is disabled.
            row = db.execute("SELECT * FROM users WHERE email=?", (identity,)).fetchone() if identity else None
            if row is None:
                row = db.execute("SELECT * FROM users WHERE username=?", (identity,)).fetchone()
            if not row or not row["active"]:
                return None
            try:
                _passwords.verify(row["password_hash"], password)
            except (VerifyMismatchError, InvalidHashError):
                return None
            token = secrets.token_urlsafe(32)
            expires = (datetime.now(UTC) + timedelta(seconds=session_max_age(remember))).isoformat(timespec="seconds")
            db.execute("DELETE FROM sessions WHERE expires_at < ?", (_now(),))
            db.execute("INSERT INTO sessions VALUES (?,?,?,?)", (_hash_token(token), row["id"], expires, _now()))
        user = self.get_user(row["id"])
        assert user is not None
        return user, token

    def user_for_token(self, token: str | None) -> dict[str, Any] | None:
        if not token:
            return None
        with self.connect() as db:
            row = db.execute(
                """SELECT u.id,u.username,u.display_name,u.role,u.org_id,u.active,u.created_at
                   FROM sessions s JOIN users u ON u.id=s.user_id
                   WHERE s.token_hash=? AND s.expires_at>=? AND u.active=1""",
                (_hash_token(token), _now()),
            ).fetchone()
        return dict(row) if row else None

    def logout(self, token: str | None) -> None:
        if token:
            with self.connect() as db:
                db.execute("DELETE FROM sessions WHERE token_hash=?", (_hash_token(token),))

    def log(self, user: dict[str, Any] | None, action: str, target_type: str, target_id: str, detail: str = "") -> None:
        with self.connect() as db:
            self._log(db, user, action, target_type, target_id, detail)

    @staticmethod
    def _log(db, user, action, target_type, target_id, detail=""):
        db.execute(
            "INSERT INTO audit_log(user_id,org_id,action,target_type,target_id,detail,created_at) VALUES (?,?,?,?,?,?,?)",
            (user["id"] if user else None, user["org_id"] if user else "system", action, target_type, target_id, detail, _now()),
        )

    def list_logs(self, user: dict[str, Any]) -> list[dict[str, Any]]:
        with self.connect() as db:
            db.execute('BEGIN')
            current_actor(db, user, {'platform_admin', 'org_admin', 'teacher'})
            if user["role"] == "platform_admin":
                rows = db.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 300")
            elif user['role'] == 'teacher':
                scope, args = audit_scope(user)
                # Filter by the logged object, not all activity of a student
                # who may also attend another teacher's class.
                rows = db.execute(f"""WITH teaching_audits AS (
                    SELECT a.id FROM audits a LEFT JOIN clients c ON c.id=a.client_id WHERE {scope}
                ), teaching_assignments AS (
                    SELECT id FROM assignments WHERE org_id=? AND created_by=?
                    AND audit_id IN (SELECT id FROM teaching_audits)
                ) SELECT l.* FROM audit_log l WHERE l.org_id=? AND (
                    (l.target_type='audit' AND l.target_id IN (SELECT id FROM teaching_audits)) OR
                    (l.target_type='assignment' AND l.target_id IN (SELECT id FROM teaching_assignments)) OR
                    (l.target_type='submission' AND l.target_id IN (SELECT id FROM submissions
                        WHERE assignment_id IN (SELECT id FROM teaching_assignments))) OR
                    (l.target_type='class' AND l.target_id IN (SELECT id FROM training_classes WHERE org_id=? AND owner_id=?)) OR
                    (l.target_type='paper' AND l.target_id IN (SELECT id FROM training_papers WHERE org_id=? AND owner_id=?)) OR
                    (l.target_type IN ('user','session') AND l.target_id=?) OR
                    (l.target_type='notification' AND l.target_id IN (SELECT id FROM notifications
                        WHERE org_id=? AND user_id=? AND audit_id IN (SELECT id FROM teaching_audits)))
                ) ORDER BY l.id DESC LIMIT 300""", [*args, user['org_id'], user['id'], user['org_id'],
                    user['org_id'], user['id'], user['org_id'], user['id'], user['id'], user['org_id'], user['id']])
            else:
                rows = db.execute("SELECT * FROM audit_log WHERE org_id=? ORDER BY id DESC LIMIT 300", (user["org_id"],))
            return [dict(row) for row in rows]

    def upsert_client(self, user: dict[str, Any], name: str, taxpayer_id: str, accountant_id: str | None = None) -> dict[str, Any]:
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current_actor(db, user, {'org_admin', 'accountant'})
            result = self._upsert_client(db, user, name, taxpayer_id, accountant_id)
            self._log(db, user, "upsert_client", "client", result["id"])
            return result

    def _upsert_client(self, db, user, name, taxpayer_id, accountant_id=None):
        if accountant_id:
            assignee = db.execute("SELECT 1 FROM users WHERE id=? AND org_id=? AND role='accountant' AND active=1",
                                  (accountant_id, user['org_id'])).fetchone()
            if not assignee:
                raise AccessDenied('负责人必须是本机构有效会计。', 422)
        if user['role'] == 'accountant' and accountant_id not in (None, user['id']):
            raise AccessDenied('会计不能指派客户负责人。', 403)
        row = db.execute("SELECT * FROM clients WHERE org_id=? AND taxpayer_id=?", (user["org_id"], taxpayer_id)).fetchone()
        if row:
            if user['role'] == 'accountant':
                if row['accountant_id'] != user['id']:
                    raise AccessDenied('会计只能审计自己负责的客户；请由机构管理员指派。', 403)
            else:
                db.execute("UPDATE clients SET name=?, accountant_id=COALESCE(?,accountant_id) WHERE id=?", (name, accountant_id, row["id"]))
            client_id = row["id"]
        else:
            client_id = secrets.token_hex(12)
            assigned = user['id'] if user['role'] == 'accountant' else accountant_id
            db.execute("INSERT INTO clients VALUES (?,?,?,?,?,?)", (client_id, user["org_id"], name, taxpayer_id, assigned, _now()))
        result = dict(db.execute('SELECT * FROM clients WHERE id=? AND org_id=?', (client_id, user['org_id'])).fetchone())
        return result

    def get_client(self, client_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
        return dict(row) if row else None

    def list_clients(self, user: dict[str, Any]) -> list[dict[str, Any]]:
        with self.connect() as db:
            db.execute("BEGIN")
            current_actor(db, user, {'org_admin', 'accountant'})
            if user["role"] == "accountant":
                rows = db.execute(
                    """SELECT c.*,u.display_name AS accountant_name,u.username AS accountant_username
                       FROM clients c LEFT JOIN users u ON u.id=c.accountant_id AND u.org_id=c.org_id
                       WHERE c.org_id=? AND c.accountant_id=? ORDER BY c.name""",
                    (user["org_id"], user["id"]),
                )
            else:
                rows = db.execute(
                    """SELECT c.*,u.display_name AS accountant_name,u.username AS accountant_username
                       FROM clients c LEFT JOIN users u ON u.id=c.accountant_id AND u.org_id=c.org_id
                       WHERE c.org_id=? ORDER BY c.name""",
                    (user["org_id"],),
                )
            return [dict(row) for row in rows]

    def notification_preferences(self, user_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM notification_preferences WHERE user_id=?", (user_id,)).fetchone()
        return {"user_id": user_id, "audit_completed": bool(row["audit_completed"]) if row else False,
                "high_risk": bool(row["high_risk"]) if row else False,
                "email_enabled": bool(row["email_enabled"]) if row else False}

    def list_notification_recipients(self, actor: dict[str, Any]) -> list[dict[str, Any]]:
        with self.connect() as db:
            db.execute('BEGIN')
            current_actor(db, actor, {'platform_admin', 'org_admin'})
            where, args = ('', []) if actor['role']=='platform_admin' else (' AND u.org_id=?', [actor['org_id']])
            rows = db.execute('''SELECT u.id,u.display_name,u.org_id,u.role,u.email,
                COALESCE(p.audit_completed,0) AS audit_completed,COALESCE(p.high_risk,0) AS high_risk,
                COALESCE(p.email_enabled,0) AS email_enabled
                FROM users u LEFT JOIN notification_preferences p ON p.user_id=u.id
                WHERE u.active=1 AND u.role IN ('org_admin','accountant','teacher')'''+where+' ORDER BY u.display_name,u.id',args).fetchall()
            return [{'id':r['id'],'user_id':r['id'],'display_name':r['display_name'],'org_id':r['org_id'],
                     'role':r['role'],'has_email':bool(r['email']),
                     **{key:bool(r[key]) for key in ('audit_completed','high_risk','email_enabled')}} for r in rows]

    def set_notification_preferences(self, actor: dict[str, Any], user_id: str,
                                     audit_completed: bool, high_risk: bool, email_enabled: bool) -> dict[str, Any]:
        from webapp.notifications import RECIPIENT_ROLES

        with self._lock:
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                current_actor(db, actor, {'platform_admin', 'org_admin', 'accountant', 'teacher'})
                target = db.execute("SELECT * FROM users WHERE id=? AND active=1", (user_id,)).fetchone()
                if not target or target["role"] not in RECIPIENT_ROLES:
                    raise ValueError("接收人不存在、已停用或角色不支持审计通知。")
                if actor["id"] != user_id and not (actor["role"] == "platform_admin" or
                        actor["role"] == "org_admin" and actor["org_id"] == target["org_id"]):
                    raise PermissionError("无权配置该接收人的通知。")
                current = db.execute("SELECT email_enabled FROM notification_preferences WHERE user_id=?", (user_id,)).fetchone()
                if actor["id"] != user_id and email_enabled != bool(current["email_enabled"] if current else False):
                    raise PermissionError("邮件通知必须由接收人本人开启或关闭。")
                if email_enabled and not target["email"]:
                    raise ValueError("请先绑定本人邮箱，再开启邮件通知。")
                db.execute("""INSERT INTO notification_preferences VALUES (?,?,?,?,?,?)
                           ON CONFLICT(user_id) DO UPDATE SET audit_completed=excluded.audit_completed,
                               high_risk=excluded.high_risk,email_enabled=excluded.email_enabled,
                               updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
                           (user_id, int(audit_completed), int(high_risk), int(email_enabled), actor["id"], _now()))
                # A queued message is cancelled immediately on unsubscribe.
                # Already claimed/accepted messages cannot be retracted.
                db.execute("""UPDATE notification_deliveries SET status='suppressed',error_code='unsubscribed',updated_at=?
                           WHERE status IN ('pending','failed') AND notification_id IN
                             (SELECT id FROM notifications WHERE user_id=? AND
                              (notification_deliveries.channel='email' AND ?=0
                               OR event='audit_completed' AND ?=0 OR event='high_risk' AND ?=0))""",
                           (_now(), user_id, int(email_enabled), int(audit_completed), int(high_risk)))
                members.log(db,actor,'notification_preferences','user',user_id,
                            f'audit_completed={audit_completed};high_risk={high_risk};email={email_enabled}')
        return self.notification_preferences(user_id)

    def list_notifications(self, user: dict[str, Any], limit: int = 100) -> list[dict[str, Any]]:
        from webapp.notifications import RECIPIENT_ROLES

        if user["role"] not in RECIPIENT_ROLES:
            return []
        scope, scope_args = audit_scope(user)
        where = "n.user_id=? AND n.org_id=? AND " + scope
        args: list[Any] = [user["id"], user["org_id"], *scope_args]
        args.append(max(1, min(limit, 100)))
        with self.connect() as db:
            db.execute('BEGIN')
            current_actor(db, user, RECIPIENT_ROLES)
            rows = db.execute(f"""SELECT n.*,d.status AS email_status FROM notifications n
                              JOIN audits a ON a.id=n.audit_id LEFT JOIN clients c ON c.id=a.client_id
                              LEFT JOIN notification_deliveries d ON d.notification_id=n.id AND d.channel='email'
                              WHERE {where} ORDER BY n.created_at DESC,n.id LIMIT ?""", args).fetchall()
            deliveries = {}
            if rows:
                placeholders=','.join('?' for _ in rows)
                for delivery in db.execute(f'''SELECT notification_id,channel,status FROM notification_deliveries
                    WHERE notification_id IN ({placeholders}) ORDER BY channel''',[row['id'] for row in rows]):
                    deliveries.setdefault(delivery['notification_id'],[]).append({'channel':delivery['channel'],'status':delivery['status']})
        return [{**{key: row[key] for key in ("id", "audit_id", "event", "created_at", "read_at", "email_status")},
                 "summary": json.loads(row["summary_json"]),"deliveries":deliveries.get(row['id'],[])} for row in rows]

    def mark_notification_read(self, user: dict[str, Any], notification_id: str) -> bool:
        from webapp.notifications import RECIPIENT_ROLES

        if user["role"] not in RECIPIENT_ROLES:
            return False
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current_actor(db, user, RECIPIENT_ROLES)
            row = db.execute("""SELECT n.id,n.audit_id FROM notifications n
                               JOIN audits a ON a.id=n.audit_id LEFT JOIN clients c ON c.id=a.client_id
                               WHERE n.id=? AND n.user_id=? AND n.org_id=? AND a.org_id=?""",
                             (notification_id, user["id"], user["org_id"], user["org_id"])).fetchone()
            if not row or not audit_row(db, row['audit_id'], user):
                return False
            result = db.execute("""UPDATE notifications SET read_at=COALESCE(read_at,?)
                                  WHERE id=? AND user_id=? AND org_id=?""",
                                (_now(), notification_id, user["id"], user["org_id"]))
            return result.rowcount == 1

    def claim_notification_delivery(self, *, channels=None) -> dict[str, Any] | None:
        """Same queue/state machine for all channels; recheck ownership and consent."""
        from webapp.notifications import RECIPIENT_ROLES, EVENT_LABELS, InvalidRecipient, recipient_for, frozen_payload, validated_payload, whitelisted_summary

        channels=list(self.notification_adapters) if channels is None else list(channels)
        if not channels:
            return None
        if any(channel not in self.notification_adapters for channel in channels):
            raise ValueError('通知渠道未配置。')
        placeholders=','.join('?' for _ in channels)

        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(f"""SELECT d.*,n.user_id,n.org_id,n.audit_id,n.event,n.summary_json,
                          u.active,u.role,u.org_id AS user_org,u.email,
                          p.audit_completed,p.high_risk,p.email_enabled,a.org_id AS audit_org,
                          c.org_id AS client_org,c.accountant_id
                          FROM notification_deliveries d JOIN notifications n ON n.id=d.notification_id
                          JOIN users u ON u.id=n.user_id JOIN audits a ON a.id=n.audit_id
                          LEFT JOIN notification_preferences p ON p.user_id=u.id
                          LEFT JOIN clients c ON c.id=a.client_id
                          WHERE d.status='pending' AND d.channel IN ({placeholders})
                          ORDER BY d.created_at,d.notification_id,d.channel LIMIT 100""",channels).fetchall()
            for row in rows:
                authorized = (row["active"] and row["role"] in RECIPIENT_ROLES and
                              row["user_org"] == row["org_id"] == row["audit_org"])
                actor = {'id': row['user_id'], 'org_id': row['user_org'], 'role': row['role']}
                authorized = authorized and bool(audit_row(db, row['audit_id'], actor))
                adapter=self.notification_adapters[row['channel']]
                try:
                    recipient=recipient_for(adapter,db,actor) if authorized else None
                except InvalidRecipient:
                    # A damaged binding must not starve unrelated channels.
                    # Resolver/database exceptions still abort this transaction.
                    db.execute("""UPDATE notification_deliveries SET status='failed',error_code='invalid_recipient',updated_at=?
                        WHERE notification_id=? AND channel=? AND status='pending'""",(_now(),row['notification_id'],row['channel']))
                    continue
                old_key=row['recipient_key'] or (row['recipient_email'] if row['channel']=='email' else '')
                consent = (row['event'] in EVENT_LABELS and row[row['event']] and recipient is not None
                           and recipient.key==old_key and recipient.revision==row['recipient_revision'])
                if not authorized or not consent:
                    db.execute("""UPDATE notification_deliveries SET status='suppressed',
                                  error_code='recipient_unavailable',updated_at=? WHERE notification_id=? AND channel=? AND status='pending'""",
                               (_now(), row["notification_id"],row['channel']))
                    continue
                token = secrets.token_hex(16)
                try:
                    summary = whitelisted_summary(json.loads(row['summary_json']))
                    payload = validated_payload(json.loads(row["payload_json"])) if row["payload_json"] else frozen_payload(adapter,summary)
                except (ValueError,KeyError,TypeError):
                    db.execute("""UPDATE notification_deliveries SET status='failed',error_code='invalid_payload',updated_at=?
                        WHERE notification_id=? AND channel=? AND status='pending'""",(_now(),row['notification_id'],row['channel']))
                    continue
                db.execute("""UPDATE notification_deliveries SET status='claimed',claim_token=?,
                              attempts=attempts+1,claimed_at=?,updated_at=?,payload_json=?
                              WHERE notification_id=? AND channel=? AND status='pending'""",
                           (token, _now(), _now(), _json(payload), row["notification_id"],row['channel']))
                return {"notification_id": row["notification_id"], "claim_token": token,
                        "channel":row['channel'],"recipient_key":old_key,"recipient_email":row["recipient_email"],
                        "summary": summary, "payload": payload}
        return None

    def finish_notification_delivery(self, notification_id: str, claim_token: str, status: str,
                                     provider_id: str = "", error_code: str = "", *, channel='email') -> bool:
        if (status not in {"accepted", "failed", "uncertain"} or status == "accepted" and not provider_id
                or len(provider_id) > 128 or not re.fullmatch(r"[a-z0-9_]{0,40}", error_code)):
            raise ValueError("通知发送回执无效。")
        with self.connect() as db:
            result = db.execute("""UPDATE notification_deliveries SET status=?,provider_id=?,error_code=?,
                                  updated_at=? WHERE notification_id=? AND channel=? AND status='claimed' AND claim_token=?""",
                                (status, provider_id or None, error_code or None, _now(), notification_id, channel,claim_token))
            return result.rowcount == 1

    def recover_notification_claims(self) -> int:
        """A crashed send may have reached the provider: never automatically resend it."""
        threshold = (datetime.now(UTC) - timedelta(minutes=5)).isoformat(timespec="seconds")
        with self.connect() as db:
            result = db.execute("""UPDATE notification_deliveries SET status='uncertain',error_code='interrupted',updated_at=?
                                  WHERE status='claimed' AND claimed_at<?""", (_now(), threshold))
            return result.rowcount

    def retry_notification_delivery(self, user: dict[str, Any], notification_id: str, *, channel='email') -> bool:
        from webapp.notifications import RECIPIENT_ROLES

        if user["role"] not in RECIPIENT_ROLES:
            return False
        if channel not in self.notification_adapters:
            raise ValueError('通知渠道未配置。')
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current_actor(db, user, RECIPIENT_ROLES)
            row = db.execute("""SELECT d.*,n.audit_id
                               FROM notification_deliveries d JOIN notifications n ON n.id=d.notification_id
                               JOIN audits a ON a.id=n.audit_id LEFT JOIN clients c ON c.id=a.client_id
                               WHERE n.id=? AND d.channel=? AND n.user_id=? AND n.org_id=? AND a.org_id=?""",
                             (notification_id, channel,user["id"], user["org_id"], user["org_id"])).fetchone()
            if not row or not audit_row(db, row['audit_id'], user):
                return False
            if row["status"] not in {"failed", "uncertain"}:
                raise ValueError("只有失败或结果未知的通知可申请重试。")
            cutoff = (datetime.now(UTC) - timedelta(hours=23)).isoformat(timespec="seconds")
            if row["attempts"] >= 3 or row["created_at"] < cutoff:
                raise ValueError("已超出安全重试次数或 23 小时窗口；请人工核查，不再重发。")
            db.execute("""UPDATE notification_deliveries SET status='pending',claim_token=NULL,
                          provider_id=NULL,error_code=NULL,updated_at=? WHERE notification_id=? AND channel=?""", (_now(), notification_id,channel))
            members.log(db,user,'notification_retry','notification',notification_id,'manual retry;channel='+channel)
            return True

    def _enqueue_audit_notifications(self, db, org_id: str, client_id: str | None,
                                     audit_id: str, findings: list[Finding], when: str) -> None:
        from webapp.notifications import RECIPIENT_ROLES, safe_summary, recipient_for, frozen_payload

        members = db.execute("""SELECT u.id,u.role,u.email,p.audit_completed,p.high_risk,p.email_enabled
                              FROM notification_preferences p JOIN users u ON u.id=p.user_id
                              WHERE u.active=1 AND u.org_id=?""", (org_id,)).fetchall()
        high = any(item.status == "hit" and item.rule.severity == "high" for item in findings)
        for member in members:
            if member["role"] not in RECIPIENT_ROLES:
                continue
            actor = {'id': member['id'], 'org_id': org_id, 'role': member['role']}
            if not audit_row(db, audit_id, actor):
                continue
            for event in ("audit_completed", "high_risk"):
                if not member[event] or event == "high_risk" and not high:
                    continue
                key = hashlib.sha256(f"{member['id']}:{audit_id}:{event}".encode()).hexdigest()
                summary = safe_summary(audit_id, findings, event, when)
                created = _now()
                db.execute("""INSERT OR IGNORE INTO notifications VALUES (?,?,?,?,?,?,?,NULL)""",
                           (key, member["id"], org_id, audit_id, event, _json(summary), created))
                for channel,adapter in self.notification_adapters.items():
                    recipient=recipient_for(adapter,db,actor)
                    if recipient is None:
                        continue
                    payload=frozen_payload(adapter,summary)
                    db.execute("""INSERT OR IGNORE INTO notification_deliveries
                               (notification_id,channel,recipient_key,recipient_revision,recipient_email,created_at,updated_at,payload_json)
                               VALUES (?,?,?,?,?,?,?,?)""",
                               (key,channel,recipient.key,recipient.revision,recipient.key if channel=='email' else '',created,created,_json(payload)))

    def save_audit(self, audit_id: str, user: dict[str, Any], client_id: str | None,
                   dataset: Dataset, findings: list[Finding], summary: dict[str, Any], audited_at: str,
                   report_snapshot: dict | None = None, exercise_metadata: dict | None = None,
                   *, create_client: bool = False, material_context: dict | None = None) -> None:
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current_actor(db, user, {'org_admin', 'accountant', 'teacher'})
            if user['role'] == 'teacher' and (client_id is not None or not is_teaching_dataset(dataset)):
                raise AccessDenied('教师备课只能使用未关联客户档案的仿真案例。', 403)
            if client_id:
                client = db.execute('SELECT * FROM clients WHERE id=? AND org_id=?', (client_id, user['org_id'])).fetchone()
                if not client or user['role'] == 'accountant' and client['accountant_id'] != user['id']:
                    raise AccessDenied('客户不存在或权限已变化。')
            elif create_client and user['role'] in {'org_admin', 'accountant'}:
                client_id = self._upsert_client(db, user, dataset.company.name, dataset.company.taxpayer_id,
                                               user['id'] if user['role'] == 'accountant' else None)['id']
            elif user['role'] == 'accountant':
                raise AccessDenied('会计审计必须关联负责的客户。')
            db.execute(
                "INSERT INTO audits VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (audit_id, user["org_id"], client_id, user["id"], dataset.company.name,
                 dataset.company.taxpayer_id, dataset.company.industry, dataset.company.period,
                 _json(serialize_dataset(dataset)), _json(serialize_findings(findings)),
                 _json(summary), audited_at),
            )
            self._enqueue_audit_notifications(db, user["org_id"], client_id, audit_id, findings, audited_at)
            if material_context is not None:
                from webapp.material_batches import record_execution
                record_execution(db, user, audit_id, client_id, dataset, findings, material_context)
            if report_snapshot is not None:
                self._insert_report_version(db, audit_id, user, report_snapshot)
            if exercise_metadata is not None:
                from webapp.report_archive import canonical, digest
                if (user["role"] != "teacher" or exercise_metadata.get("standard_answer") != sorted(f.rule.id for f in findings if f.hit)
                        or exercise_metadata.get("requested_rule_id") not in exercise_metadata["standard_answer"]):
                    raise ValueError("出题记录与冻结审计答案不一致。")
                encoded = canonical(exercise_metadata)
                db.execute("INSERT INTO generated_exercises VALUES (?,?,?,?)",
                           (audit_id,user["org_id"],encoded,digest(encoded.encode())))
            self._log(db, user, "create_audit", "audit", audit_id,
                      f"{dataset.company.name}; rules={len(findings)}")

    def get_generated_exercise(self, audit_id, user):
        if user["role"] != "teacher":
            raise PermissionError("仅教师可读出题参数与标准答案。")
        with self.connect() as db:
            db.execute('BEGIN')
            if not audit_row(db, audit_id, user):
                return None
            return self._generated_exercise(db, audit_id, user['org_id'])

    def has_generated_exercise(self, audit_id, org_id):
        with self.connect() as db:
            return db.execute("SELECT 1 FROM generated_exercises WHERE audit_id=? AND org_id=?",
                              (audit_id, org_id)).fetchone() is not None

    def _generated_exercise(self, db, audit_id, org_id):
        from webapp.report_archive import digest
        row=db.execute("SELECT * FROM generated_exercises WHERE audit_id=? AND org_id=?",
                       (audit_id,org_id)).fetchone()
        if not row:
            return None
        if digest(row["metadata_json"].encode()) != row["metadata_sha256"]:
            raise ValueError("出题记录完整性校验失败，请核查备份。")
        return json.loads(row["metadata_json"])

    def get_generated_material(self, audit_id, user, assignment_id=None):
        """Authorize first; return only frozen inputs, never teacher metadata."""
        if user["role"] not in {"teacher", "student"}:
            raise PermissionError("仅教师或获发布作业的学生可下载仿真材料。")
        with self.connect() as db:
            db.execute('BEGIN')
            current_actor(db, user, {'teacher', 'student'})
            if user['role'] == 'student':
                row = db.execute('SELECT * FROM assignments WHERE id=?', (assignment_id,)).fetchone()
                assignment = classroom.decorate(db, dict(row)) if row else None
                if (not assignment or assignment['audit_id'] != audit_id
                        or not classroom.can_access(db, assignment, user)):
                    return None
                row = db.execute('SELECT * FROM audits WHERE id=? AND org_id=?', (audit_id, user['org_id'])).fetchone()
            else:
                row = audit_row(db, audit_id, user)
            if not row:
                return None
            entry = self._audit_dict(row)
            metadata = self._generated_exercise(db, audit_id, user['org_id'])
        if not metadata:
            return None
        from src.exercise_generator import case_digest
        rules = [f.rule for f in entry["findings"] if f.rule.id in metadata["rule_versions"]]
        if (metadata["standard_answer"] != sorted(f.rule.id for f in entry["findings"] if f.hit)
                or case_digest(entry["dataset"], rules) != metadata["case_sha256"]):
            raise ValueError("出题记录与冻结材料不一致，请核查备份。")
        return entry["dataset"]

    def _insert_report_version(self, db, audit_id, user, snapshot):
        from webapp.report_archive import canonical, digest

        html, manifest = snapshot["html"], snapshot["manifest"]
        audit = db.execute("SELECT org_id FROM audits WHERE id=?", (audit_id,)).fetchone()
        if not audit or manifest["audit_id"] != audit_id or manifest["org_id"] != audit["org_id"]:
            raise ValueError("归档主体不一致。")
        from webapp.material_batches import reference
        if manifest.get('material_reference') != reference(db, audit_id):
            raise ValueError('报告与材料确认版本关联不一致。')
        manifest_json = canonical(manifest)
        html_hash = digest(html.encode())
        manifest_hash = digest(manifest_json.encode())
        content_hash = digest((html_hash + manifest_hash).encode())
        existing = db.execute("SELECT version FROM audit_report_versions WHERE audit_id=? AND content_sha256=?",
                              (audit_id, content_hash)).fetchone()
        if existing:
            return existing["version"], False
        version = db.execute("SELECT COALESCE(MAX(version),0)+1 FROM audit_report_versions WHERE audit_id=?",
                             (audit_id,)).fetchone()[0]
        db.execute("""INSERT INTO audit_report_versions
                   (audit_id,version,html,html_sha256,manifest_json,manifest_sha256,content_sha256,created_by,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                   (audit_id, version, html, html_hash, manifest_json, manifest_hash, content_hash, user["id"], _now()))
        self._register_report_protection(db,manifest.get("protection"),audit["org_id"],"audit",audit_id,version)
        return version, True

    @staticmethod
    def _register_report_protection(db, protection, org_id, kind, audit_id=None, version=None, org_report_id=None):
        from src.report_protection import IDENTIFIER
        if protection is None:
            return  # Legacy archives remain unmarked; no fabricated old registrations.
        if (not isinstance(protection,dict) or protection.get("method") != "archive-original"
                or not isinstance(protection.get("id"),str) or not IDENTIFIER.fullmatch(protection["id"])):
            raise ValueError("报告追溯标识无效。")
        db.execute("INSERT INTO report_protections VALUES (?,?,?,?,?,?)",
                   (protection["id"],org_id,kind,audit_id,version,org_report_id))

    def report_protection_target(self, identifier, user):
        if user["role"] not in {"org_admin","accountant","teacher"}:
            raise PermissionError("无权核验报告。")
        with self.connect() as db:
            row=db.execute("SELECT * FROM report_protections WHERE id=? AND org_id=?",
                           (identifier,user["org_id"])).fetchone()
        return dict(row) if row else None

    def archive_report(self, audit_id, user, snapshot):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not audit_row(db, audit_id, user):
                raise AccessDenied()
            version, created = self._insert_report_version(db, audit_id, user, snapshot)
            if created:
                self._log(db, user, "archive_report", "audit", audit_id, f"version={version}")
            return version, created

    @staticmethod
    def _verify_report_version(row, full=False):
        from webapp.report_archive import digest

        if (digest(row["manifest_json"].encode()) != row["manifest_sha256"]
                or digest((row["html_sha256"] + row["manifest_sha256"]).encode()) != row["content_sha256"]
                or full and digest(row["html"].encode()) != row["html_sha256"]
                or full and row["pdf_bytes"] is not None and digest(row["pdf_bytes"]) != row["pdf_sha256"]):
            raise ValueError("归档完整性校验失败；请核查备份，不重新覆盖该版本。")

    def report_versions(self, audit_id):
        with self.connect() as db:
            rows = db.execute("""SELECT audit_id,version,html_sha256,manifest_json,manifest_sha256,content_sha256,
                                 created_by,created_at,pdf_sha256,pdf_created_at FROM audit_report_versions
                                 WHERE audit_id=? ORDER BY version DESC""", (audit_id,)).fetchall()
        result = []
        for row in rows:
            self._verify_report_version(row)
            item = dict(row)
            item["manifest"] = json.loads(item.pop("manifest_json"))
            result.append(item)
        return result

    def get_report_version(self, audit_id, version):
        with self.connect() as db:
            row = db.execute("SELECT * FROM audit_report_versions WHERE audit_id=? AND version=?",
                             (audit_id, version)).fetchone()
        if not row:
            return None
        self._verify_report_version(row, full=True)
        item = dict(row)
        item["manifest"] = json.loads(item.pop("manifest_json"))
        return item

    def attach_report_pdf(self, audit_id, version, pdf, *, actor=None):
        """First completed exporter wins; no overwrite, even across processes."""
        from webapp.report_archive import digest

        if not pdf.startswith(b"%PDF"):
            raise ValueError("PDF 归档内容无效。")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if actor is not None and not audit_row(db, audit_id, actor):
                raise AccessDenied()
            db.execute("""UPDATE audit_report_versions SET pdf_bytes=?,pdf_sha256=?,pdf_created_at=?
                          WHERE audit_id=? AND version=? AND pdf_bytes IS NULL""",
                       (pdf, digest(pdf), _now(), audit_id, version))
        return self.get_report_version(audit_id, version)

    def search_audits(self, user, query="", period="", date_from=None, date_to=None,
                      risk="all", page=1, page_size=20):
        def literal(value):
            return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"

        where, args = audit_scope(user)
        if query:
            where += " AND (a.company_name LIKE ? ESCAPE '\\' OR a.taxpayer_id LIKE ? ESCAPE '\\' OR a.id LIKE ? ESCAPE '\\')"
            args.extend([literal(query)] * 3)
        if period:
            where += " AND a.period LIKE ? ESCAPE '\\'"
            args.append(literal(period))
        for field, operator in ((date_from, ">="), (date_to, "<=")):
            if field:
                where += f" AND substr(a.audited_at,1,10){operator}?"
                args.append(field)
        if risk in {"hit", "high"}:
            key = "hit" if risk == "hit" else "high"
            where += f" AND json_extract(a.summary_json,'$.{key}')>0"
        join = "FROM audits a LEFT JOIN clients c ON c.id=a.client_id WHERE " + where
        with self.connect() as db:
            # Count and page must describe the same read snapshot during uploads.
            db.execute("BEGIN")
            current_actor(db, user)
            total = db.execute("SELECT COUNT(*) " + join, args).fetchone()[0]
            rows = db.execute("""SELECT a.id,a.company_name,a.taxpayer_id,a.period,a.audited_at,a.summary_json,
                              (SELECT COUNT(*) FROM audit_report_versions v WHERE v.audit_id=a.id) AS report_versions """ + join
                              + " ORDER BY a.audited_at DESC,a.rowid DESC LIMIT ? OFFSET ?",
                              [*args, page_size, (page-1)*page_size]).fetchall()
        items = []
        for row in rows:
            item = dict(row); item["summary"] = json.loads(item.pop("summary_json")); items.append(item)
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    def org_report_sources(self, user):
        """Capture current clients, audit revisions and branding in one read snapshot."""
        import base64
        if user["role"] not in {"org_admin"}:
            raise PermissionError("只有机构管理员可生成本机构总览。")
        with self.connect() as db:
            db.execute("BEGIN")
            current_actor(db, user, {'org_admin'})
            clients = [dict(row) for row in db.execute(
                """SELECT c.*,u.display_name AS accountant_name FROM clients c
                   LEFT JOIN users u ON u.id=c.accountant_id AND u.org_id=c.org_id
                   WHERE c.org_id=? ORDER BY c.name,c.id""", (user["org_id"],))]
            audits = [dict(row) for row in db.execute(
                "SELECT * FROM audits WHERE org_id=? ORDER BY audited_at DESC,rowid DESC", (user["org_id"],))]
            row = db.execute("SELECT * FROM org_settings WHERE org_id=?", (user["org_id"],)).fetchone()
            branding = dict(row) if row else dict(self.ORG_DEFAULTS)
        logo = branding.pop("logo_bytes", None)
        branding["logo_data_uri"] = (f"data:{branding.get('logo_mime')};base64,{base64.b64encode(logo).decode()}" if logo else "")
        return {"clients": clients, "audits": audits, "branding": {
            key: branding[key] for key in ("display_name", "report_title", "footer_text", "logo_data_uri")}}

    def save_org_report(self, user, snapshot, html):
        from webapp.report_archive import canonical, digest
        if user["role"] not in {"org_admin"} or snapshot["org_id"] != user["org_id"]:
            raise PermissionError("无权归档该机构报告。")
        encoded = canonical(snapshot)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current_actor(db, user, {'org_admin'})
            db.execute("""INSERT INTO org_reports
                       (id,org_id,created_by,created_at,snapshot_json,snapshot_sha256,html,html_sha256)
                       VALUES (?,?,?,?,?,?,?,?)""",
                       (snapshot["id"],user["org_id"],user["id"],snapshot["created_at"],encoded,digest(encoded.encode()),html,digest(html.encode())))
            self._register_report_protection(db,snapshot.get("protection"),user["org_id"],"org",org_report_id=snapshot["id"])
            self._log(db, user, "create_org_report", "org_report", snapshot["id"],
                      f"clients={len(snapshot['rows'])};period={snapshot['period']}")
        return self.get_org_report(snapshot["id"], user)

    def get_org_report(self, report_id, user):
        from webapp.report_archive import digest
        if user["role"] not in {"org_admin"}:
            raise PermissionError("无权访问机构总览。")
        with self.connect() as db:
            db.execute("BEGIN")
            current_actor(db, user, {'org_admin'})
            row = db.execute("SELECT * FROM org_reports WHERE id=? AND org_id=?", (report_id,user["org_id"])).fetchone()
        if row is None:
            return None
        if (digest(row["snapshot_json"].encode()) != row["snapshot_sha256"]
                or digest(row["html"].encode()) != row["html_sha256"]
                or row["pdf_bytes"] is not None and digest(row["pdf_bytes"]) != row["pdf_sha256"]):
            raise ValueError("机构报告完整性校验失败，请核查备份，不重新覆盖归档。")
        result = dict(row); result["snapshot"] = json.loads(result.pop("snapshot_json"))
        return result

    def list_org_reports(self, user):
        if user["role"] not in {"org_admin"}:
            raise PermissionError("无权访问机构总览。")
        with self.connect() as db:
            db.execute("BEGIN")
            current_actor(db, user, {'org_admin'})
            return [dict(row) for row in db.execute(
                """SELECT id,created_at,created_by,html_sha256,snapshot_sha256,pdf_sha256
                   FROM org_reports WHERE org_id=? ORDER BY created_at DESC,rowid DESC LIMIT 100""", (user["org_id"],))]

    def attach_org_report_pdf(self, report_id, user, pdf):
        from webapp.report_archive import digest
        if not pdf.startswith(b"%PDF"):
            raise ValueError("PDF 内容无效。")
        if not self.get_org_report(report_id, user):
            return None
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current_actor(db, user, {'org_admin'})
            db.execute("""UPDATE org_reports SET pdf_bytes=?,pdf_sha256=?,pdf_created_at=?
                       WHERE id=? AND org_id=? AND pdf_bytes IS NULL""", (pdf,digest(pdf),_now(),report_id,user["org_id"]))
        return self.get_org_report(report_id, user)

    def get_audit(self, audit_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            db.execute('BEGIN')
            row = db.execute("SELECT * FROM audits WHERE id=?", (audit_id,)).fetchone()
            if not row:
                return None
            from webapp.material_batches import reference
            return {**self._audit_dict(row), 'material_reference': reference(db, audit_id)}

    @staticmethod
    def _audit_dict(row):
        item = dict(row)
        item["dataset"] = deserialize_dataset(json.loads(item.pop("dataset_json")))
        item["findings"] = deserialize_findings(json.loads(item.pop("findings_json")))
        item["summary"] = json.loads(item.pop("summary_json"))
        return item

    def get_audit_for_user(self, audit_id, user):
        with self.connect() as db:
            db.execute('BEGIN')
            row = audit_row(db, audit_id, user)
            if not row:
                return None
            from webapp.material_batches import reference
            return {**self._audit_dict(row), 'material_reference': reference(db, audit_id)}

    def list_audits(self, user: dict[str, Any]) -> list[dict[str, Any]]:
        where, args = audit_scope(user)
        with self.connect() as db:
            db.execute('BEGIN')
            current_actor(db, user)
            rows = db.execute(
                f"""SELECT a.id,a.company_name,a.taxpayer_id,a.industry,a.period,a.audited_at,
                           a.summary_json,a.client_id
                    FROM audits a LEFT JOIN clients c ON c.id=a.client_id
                    WHERE {where} ORDER BY a.audited_at DESC, a.rowid DESC""", args,
            )
            result = []
            for row in rows:
                item = dict(row)
                item["summary"] = json.loads(item.pop("summary_json"))
                result.append(item)
            return result

    def audit_history_for_comparison(self, entry: dict[str, Any], user) -> list[dict[str, Any]]:
        """Exact identity and current access; candidates themselves must not leak."""
        with self.connect() as db:
            db.execute('BEGIN')
            if not audit_row(db, entry['id'], user):
                raise AccessDenied()
            scope, args = audit_scope(user)
            return [dict(row) for row in db.execute(
                f"""SELECT a.id,a.org_id,a.client_id,a.taxpayer_id,a.period,a.audited_at FROM audits a
                   LEFT JOIN clients c ON c.id=a.client_id
                   WHERE a.org_id=? AND a.client_id IS ? AND a.taxpayer_id=? AND {scope}
                   ORDER BY a.audited_at DESC,a.rowid DESC""",
                [entry["org_id"], entry["client_id"], entry["taxpayer_id"], *args])]

    def get_finding_interpretation(self, audit_id: str, rule_id: str,
                                   evidence_hash: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                """SELECT model,result_json,created_at FROM finding_interpretations
                   WHERE audit_id=? AND rule_id=? AND evidence_hash=?""",
                (audit_id, rule_id, evidence_hash),
            ).fetchone()
        if not row:
            return None
        result = json.loads(row["result_json"])
        return {**result, "model": row["model"], "created_at": row["created_at"], "cached": True}

    def save_finding_interpretation(self, audit_id: str, rule_id: str,
                                    evidence_hash: str, result: dict[str, Any],
                                    user: dict[str, Any]) -> dict[str, Any]:
        stored = {key: value for key, value in result.items() if key not in {"model", "cached", "created_at"}}
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not audit_row(db, audit_id, user):
                raise AccessDenied()
            db.execute(
                """INSERT OR IGNORE INTO finding_interpretations
                   (audit_id,rule_id,evidence_hash,model,result_json,created_by,created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (audit_id, rule_id, evidence_hash, result["model"], _json(stored), user["id"], _now()),
            )
        return self.get_finding_interpretation(audit_id, rule_id, evidence_hash)

    def get_audit_narrative(self, audit_id: str, evidence_hash: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                """SELECT model,result_json,created_at FROM audit_narratives
                   WHERE audit_id=? AND evidence_hash=?""",
                (audit_id, evidence_hash),
            ).fetchone()
        if not row:
            return None
        result = json.loads(row["result_json"])
        return {**result, "model": row["model"], "created_at": row["created_at"], "cached": True}

    def save_audit_narrative(self, audit_id: str, evidence_hash: str,
                             result: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        stored = {key: value for key, value in result.items() if key not in {"model", "cached", "created_at"}}
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not audit_row(db, audit_id, user):
                raise AccessDenied()
            db.execute(
                """INSERT OR IGNORE INTO audit_narratives
                   (audit_id,evidence_hash,model,result_json,created_by,created_at)
                   VALUES (?,?,?,?,?,?)""",
                (audit_id, evidence_hash, result["model"], _json(stored), user["id"], _now()),
            )
        return self.get_audit_narrative(audit_id, evidence_hash)

    def create_assignment(self, user: dict[str, Any], title: str, audit_id: str,
                          target_student_id: str | None, weights: dict[str, float],
                          false_positive_penalty: float, published: bool,
                          class_id: str | None = None, deadline_at=None) -> str:
        assignment_id = secrets.token_hex(12)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current_actor(db, user, {'teacher'})
            if not audit_row(db, audit_id, user):
                raise AccessDenied('案例不存在或无权访问。')
            if target_student_id and not db.execute("SELECT 1 FROM users WHERE id=? AND org_id=? AND role='student' AND active=1",
                                                    (target_student_id, user['org_id'])).fetchone():
                raise AccessDenied('指定学生不存在或无权访问。')
            due = classroom.deadline(deadline_at)
            if published and due and datetime.fromisoformat(due) <= classroom.now():
                raise classroom.ClassroomError("发布时截止时间必须在未来。")
            db.execute(
                "INSERT INTO assignments VALUES (?,?,?,?,?,?,?,?,?,?)",
                (assignment_id, user["org_id"], classroom.clean_title(title), audit_id, user["id"], target_student_id,
                 _json(weights), false_positive_penalty, int(published), _now()),
            )
            if class_id or due:
                classroom.set_assignment_settings(db,assignment_id,user,class_id,due)
                if class_id and target_student_id and not db.execute(
                    "SELECT 1 FROM training_class_members WHERE class_id=? AND student_id=?",
                    (class_id,target_student_id),
                ).fetchone():
                    raise classroom.ClassroomError("指定学生不在所选班级名册中。")
            self._log(db, user, "create_assignment", "assignment", assignment_id, audit_id)
        return assignment_id

    def list_assignments(self, user: dict[str, Any]) -> list[dict[str, Any]]:
        with self.connect() as db:
            db.execute("BEGIN")
            current_actor(db, user, {'teacher', 'student'})
            if user["role"] == "student":
                rows = db.execute(
                    """SELECT * FROM assignments WHERE org_id=? AND published=1
                       AND (target_student_id IS NULL OR target_student_id=?) ORDER BY created_at DESC""",
                    (user["org_id"], user["id"]),
                )
            else:
                rows = db.execute("SELECT * FROM assignments WHERE org_id=? ORDER BY created_at DESC", (user["org_id"],))
            result=[]
            for row in rows.fetchall():
                item=classroom.decorate(db,self._assignment_dict(dict(row)))
                if classroom.can_access(db,item,user):
                    if user["role"] == "student":
                        item.pop("weights",None)  # Configured hit weights can reveal answers.
                    result.append(item)
            return result

    def get_assignment(self, assignment_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
            return classroom.decorate(db,self._assignment_dict(dict(row))) if row else None

    def get_assignment_for_user(self, assignment_id, user):
        with self.connect() as db:
            db.execute("BEGIN")
            current_actor(db, user, {'teacher', 'student'})
            row=db.execute("SELECT * FROM assignments WHERE id=?",(assignment_id,)).fetchone()
            item=classroom.decorate(db,self._assignment_dict(dict(row))) if row else None
            return item if classroom.can_access(db,item,user) else None

    @staticmethod
    def _assignment_dict(item: dict[str, Any]) -> dict[str, Any]:
        item["weights"] = json.loads(item.pop("weights_json"))
        item["published"] = bool(item["published"])
        return item

    def save_submission(self, assignment_id: str, student_id: str, answers: list[str],
                        score: float, details: dict[str, Any], user=None) -> str:
        submission_id = secrets.token_hex(12)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            account=db.execute("SELECT id,role,org_id FROM users WHERE id=?",(student_id,)).fetchone()
            if not account or (user and user["id"] != student_id):
                raise classroom.ClassroomError("作业不存在。",404)
            classroom.check_submission(db,assignment_id,user or dict(account))
            db.execute(
                """INSERT INTO submissions(id,assignment_id,student_id,answers_json,score,details_json,submitted_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(assignment_id,student_id) DO UPDATE SET
                     answers_json=excluded.answers_json, score=excluded.score,
                     details_json=excluded.details_json, submitted_at=excluded.submitted_at,
                     adjusted_score=NULL, feedback=NULL, reviewed_by=NULL""",
                (submission_id, assignment_id, student_id, _json(answers), score, _json(details), _now()),
            )
            mistake_book.capture(db, assignment_id, student_id, force=True)
            self._log(db, user or dict(account), "submit_assignment", "assignment", assignment_id, f"score={score}")
        return submission_id

    def get_submission(self, assignment_id: str, student_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM submissions WHERE assignment_id=? AND student_id=?", (assignment_id, student_id)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["answers"] = json.loads(item.pop("answers_json"))
        item["details"] = json.loads(item.pop("details_json"))
        return item

    def list_submissions(self, user, assignment_id: str | None = None) -> list[dict[str, Any]]:
        scope, scope_args = audit_scope(user)
        where, args = "assignment.org_id=? AND assignment.created_by=? AND " + scope, [user['org_id'], user['id'], *scope_args]
        if assignment_id:
            where += " AND s.assignment_id=?"
            args.append(assignment_id)
        with self.connect() as db:
            db.execute('BEGIN')
            current_actor(db, user, {'teacher'})
            rows = db.execute(
                f"""SELECT s.*,u.display_name,assignment.title FROM submissions s
                    JOIN assignments assignment ON assignment.id=s.assignment_id
                    JOIN audits a ON a.id=assignment.audit_id LEFT JOIN clients c ON c.id=a.client_id
                    JOIN users u ON u.id=s.student_id AND u.org_id=assignment.org_id
                    WHERE {where} ORDER BY s.submitted_at DESC""", args,
            )
            result = []
            for row in rows:
                item = dict(row)
                item["answers"] = json.loads(item.pop("answers_json"))
                item["details"] = json.loads(item.pop("details_json"))
                result.append(item)
            return result

    def submission_org(self, submission_id: str, user=None) -> str | None:
        with self.connect() as db:
            db.execute('BEGIN')
            if user:
                current_actor(db, user, {'teacher'})
            row = db.execute(
                """SELECT a.* FROM submissions s JOIN assignments a ON a.id=s.assignment_id
                   WHERE s.id=?""", (submission_id,),
            ).fetchone()
            if row and user and not classroom.can_access(db,classroom.decorate(db,dict(row)),user):
                return None
            return row["org_id"] if row else None

    def review_submission(self, submission_id: str, teacher_id: str, adjusted_score: float, feedback: str) -> None:
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            actor = db.execute("SELECT * FROM users WHERE id=? AND role='teacher' AND active=1", (teacher_id,)).fetchone()
            row = db.execute('SELECT a.* FROM submissions s JOIN assignments a ON a.id=s.assignment_id WHERE s.id=?', (submission_id,)).fetchone()
            if not actor or not row or not classroom.can_access(db, classroom.decorate(db, dict(row)), dict(actor)):
                raise AccessDenied('提交记录不存在或无权复核。')
            db.execute("UPDATE submissions SET adjusted_score=?,feedback=?,reviewed_by=? WHERE id=?",
                       (adjusted_score, feedback, teacher_id, submission_id))
            self._log(db, dict(actor), "review_submission", "submission", submission_id, f"score={adjusted_score}")

    def enabled_rule_ids(self) -> set[str] | None:
        with self.connect() as db:
            rows = list(db.execute("SELECT rule_id,enabled FROM rule_state"))
        if not rows:
            return None
        return {row["rule_id"] for row in rows if row["enabled"]}

    def set_rule_enabled(self, rule_id: str, enabled: bool, user_id: str) -> None:
        with self.connect() as db:
            db.execute(
                """INSERT INTO rule_state VALUES (?,?,?,?)
                   ON CONFLICT(rule_id) DO UPDATE SET enabled=excluded.enabled,
                     updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
                (rule_id, int(enabled), user_id, _now()),
            )

    def change_rule_state(self, rule_id, enabled, actor, valid_ids):
        """Initialize defaults, edit a rule and log the change in one transaction."""
        if rule_id not in valid_ids:
            raise AccessDenied("规则不存在。")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current_actor(db, actor, {"platform_admin"})
            stamp = _now()
            db.executemany("INSERT OR IGNORE INTO rule_state VALUES (?,?,?,?)",
                           [(key, 1, actor["id"], stamp) for key in sorted(valid_ids)])
            db.execute("UPDATE rule_state SET enabled=?,updated_by=?,updated_at=? WHERE rule_id=?",
                       (int(enabled), actor["id"], stamp, rule_id))
            self._log(db, actor, "set_rule_state", "rule", rule_id, f"enabled={enabled}")

    def rule_overrides(self) -> dict[str, dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT o.rule_id,o.version,o.logic_json,o.threshold_basis,o.updated_by,o.updated_at,
                          h.effective_from,h.effective_to
                   FROM rule_overrides o LEFT JOIN rule_version_history h
                     ON h.rule_id=o.rule_id AND h.version=o.version"""
            )
            return {
                row["rule_id"]: {
                    "version": row["version"],
                    "logic": json.loads(row["logic_json"]),
                    "threshold_basis": row["threshold_basis"],
                    "updated_by": row["updated_by"],
                    "updated_at": row["updated_at"],
                    "effective_from": row["effective_from"],
                    "effective_to": row["effective_to"],
                }
                for row in rows
            }

    def rule_versions(self, rule_id: str | None = None) -> dict[str, list[dict[str, Any]]]:
        with self.connect() as db:
            if rule_id is None:
                rows = db.execute("""SELECT * FROM rule_version_history
                                     ORDER BY rule_id,id""").fetchall()
            else:
                rows = db.execute("""SELECT * FROM rule_version_history
                                     WHERE rule_id=? ORDER BY id""", (rule_id,)).fetchall()
        versions: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            versions.setdefault(row["rule_id"], []).append({
                "version": row["version"], "effective_from": row["effective_from"],
                "effective_to": row["effective_to"], "logic": json.loads(row["logic_json"]),
                "threshold_basis": row["threshold_basis"], "updated_by": row["updated_by"],
                "updated_at": row["updated_at"],
                "rule": json.loads(row["rule_json"]) if row["rule_json"] else None,
            })
        return versions

    def set_rule_override(
        self, rule_id: str, version: str, logic: dict[str, Any],
        threshold_basis: str, actor: dict[str, Any], expected_version: str,
        effective_from: str | None = None, effective_to: str | None = None,
        rule_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if rule_snapshot is not None and (rule_snapshot.get("id") != rule_id or rule_snapshot.get("version") != version
                or rule_snapshot.get("logic") != logic or rule_snapshot.get("threshold_basis") != threshold_basis
                or rule_snapshot.get("effective_from") != effective_from or rule_snapshot.get("effective_to") != effective_to):
            raise ValueError("规则快照与发布参数不一致。")
        if effective_to and not effective_from:
            raise ValueError("规则终止日期必须同时提供起始日期。")
        for value in (effective_from, effective_to):
            if value:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    raise ValueError("规则生效日期须为 YYYY-MM-DD。")
                try:
                    date.fromisoformat(value)
                except ValueError:
                    raise ValueError("规则生效日期须为有效的 YYYY-MM-DD。") from None
        if effective_from and effective_to and effective_from > effective_to:
            raise ValueError("规则生效终止日不能早于起始日。")
        updated_at = _now()
        user_id = actor['id']
        closed_version = closed_on = None
        with self._lock:
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                current_actor(db, actor, {'platform_admin'})
                current = db.execute("SELECT version FROM rule_overrides WHERE rule_id=?", (rule_id,)).fetchone()
                if current and current["version"] != expected_version:
                    raise ValueError("规则版本已变化，请刷新后重试。")
                existing = db.execute("""SELECT version,effective_from,effective_to
                                         FROM rule_version_history WHERE rule_id=?""", (rule_id,)).fetchall()
                if any(row["version"] == version for row in existing):
                    raise ValueError("该规则版本号已使用，请填写更高版本。")
                if not effective_from and any(row["effective_from"] for row in existing):
                    raise ValueError("已有定期生效版本，后续版本必须填写生效起始日。")
                if effective_from:
                    latest_start = max((row["effective_from"] for row in existing if row["effective_from"]), default=None)
                    upper = effective_to or "9999-12-31"
                    closing = []
                    for row in existing:
                        if row["effective_from"] and effective_from <= (row["effective_to"] or "9999-12-31") and row["effective_from"] <= upper:
                            if row["effective_to"] is None and row["effective_from"] < effective_from:
                                closing.append(row["version"])
                            else:
                                raise ValueError(f"生效区间与 v{row['version']} 重叠。")
                    if latest_start and effective_from <= latest_start:
                        raise ValueError("新版本生效起始日须晚于既有定期版本。")
                    if len(closing) > 1:
                        raise ValueError("既有规则生效区间重叠，请人工修复后再保存。")
                    if closing:
                        previous_end = (date.fromisoformat(effective_from) - timedelta(days=1)).isoformat()
                        closed_version, closed_on = closing[0], previous_end
                        db.execute("""UPDATE rule_version_history SET effective_to=?
                                      WHERE rule_id=? AND version=? AND effective_to IS NULL""",
                                   (previous_end, rule_id, closing[0]))
                db.execute("""INSERT INTO rule_version_history
                           (rule_id,version,effective_from,effective_to,logic_json,
                            threshold_basis,updated_by,updated_at,rule_json)
                           VALUES (?,?,?,?,?,?,?,?,?)""",
                           (rule_id, version, effective_from, effective_to, _json(logic),
                            threshold_basis, user_id, updated_at,
                            _json(rule_snapshot) if rule_snapshot is not None else None))
                db.execute(
                    """INSERT INTO rule_overrides
                           (rule_id,version,logic_json,threshold_basis,updated_by,updated_at)
                       VALUES (?,?,?,?,?,?)
                       ON CONFLICT(rule_id) DO UPDATE SET version=excluded.version,
                           logic_json=excluded.logic_json,threshold_basis=excluded.threshold_basis,
                           updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
                    (rule_id, version, _json(logic), threshold_basis, user_id, updated_at),
                )
                members.log(db,actor,'update_rule_parameters','rule',rule_id,
                            f'expected_revision={expected_version};version={version};effective={effective_from or "legacy"}'
                            f'..{effective_to or "open"};closed={closed_version or "-"}@{closed_on or "-"}')
        return {
            "rule_id": rule_id, "version": version, "logic": logic,
            "threshold_basis": threshold_basis, "updated_by": user_id,
            "updated_at": updated_at, "effective_from": effective_from,
            "effective_to": effective_to,
            "closed_version": closed_version, "closed_on": closed_on,
        }
