"""Consistent SQLite backup, verification, restore, and rollback helpers.

The commands are safe to run against a live database for *backup*: SQLite's
online backup API captures a consistent snapshot, including committed WAL
changes.  Restore must be performed while the application is stopped.  Every
restore creates a verified safety backup of the current database first, so the
same command can roll back the restore if needed.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import getpass
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import closing, contextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "instance" / "taxpearls.db"
MANIFEST_VERSION = 1
ENCRYPTED_MAGIC = b"TPBACKUP1\n"
MAX_ENCRYPTED_BYTES = 512 * 1024 * 1024


class BackupError(RuntimeError):
    """Raised when a backup cannot be trusted or safely restored."""


def _utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def _database_path(value: str | Path | None) -> Path:
    configured = os.environ.get("TAXPEARLS_DB")
    path = Path(value or configured or DEFAULT_DB)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_path(backup: Path) -> Path:
    return backup.with_name(f"{backup.name}.manifest.json")


def _check_database(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise BackupError(f"数据库文件不存在：{path}")
    # Only called for complete offline backup/restore artifacts, never a live
    # WAL database: immutable reads cannot create journal sidecars themselves.
    uri = path.as_uri() + "?mode=ro&immutable=1"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=10)) as db:
            check = db.execute("PRAGMA quick_check").fetchone()
            if not check or check[0] != "ok":
                raise BackupError(f"SQLite quick_check 未通过：{check[0] if check else '无结果'}")
            schema_version = int(db.execute("PRAGMA schema_version").fetchone()[0])
            tables = [
                row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            counts = {name: int(db.execute('SELECT COUNT(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0])
                      for name in tables}
    except sqlite3.Error as exc:
        raise BackupError(f"无法读取 SQLite 数据库：{exc}") from exc
    return {"quick_check": "ok", "schema_version": schema_version, "table_counts": counts}


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _backup_key() -> bytes:
    """Only read a dedicated secret from the process; never accept it in argv/logs."""
    try:
        key = base64.b64decode(os.environ.get("TAXPEARLS_BACKUP_KEY", ""), validate=True)
    except (ValueError, binascii.Error):
        raise BackupError("备份密钥格式无效，须为 32 字节随机密钥的 Base64。") from None
    if len(key) != 32:
        raise BackupError("加密备份须配置独立 TAXPEARLS_BACKUP_KEY（32 字节随机密钥的 Base64）。")
    return key


def _memory_snapshot(source: Path, *, offline=False) -> bytes:
    """Online SQLite backup into RAM: no intermediate plaintext backup file."""
    if not source.is_file():
        raise BackupError("源数据库不存在。")
    if source.stat().st_size > MAX_ENCRYPTED_BYTES - 4096:
        raise BackupError("数据库超过当前内存加密备份大小限制。")
    try:
        immutable = "&immutable=1" if offline else ""
        with closing(sqlite3.connect(source.as_uri() + f"?mode=ro{immutable}", uri=True, timeout=30)) as original:
            with closing(sqlite3.connect(":memory:")) as snapshot:
                snapshot.execute('PRAGMA temp_store=MEMORY')
                page_size = original.execute('PRAGMA page_size').fetchone()[0]
                def bounded_copy(_status, _remaining, total):
                    if total * page_size > MAX_ENCRYPTED_BYTES - 4096:
                        raise BackupError("数据库超过当前内存加密备份大小限制。")
                original.backup(snapshot, pages=256, progress=bounded_copy)
                blob = snapshot.serialize()
        # SQLite documents this normalization for deserializing WAL snapshots:
        # https://www.sqlite.org/c3ref/deserialize.html (bytes 18/19).
        blob = blob[:18] + b"\x01\x01" + blob[20:]
        _check_memory_snapshot(blob)
        return blob
    except sqlite3.Error:
        raise BackupError("无法生成一致性内存备份。") from None


def _check_memory_snapshot(blob: bytes) -> dict[str, Any]:
    if not blob.startswith(b"SQLite format 3\x00"):
        raise BackupError("解密内容不是 SQLite 数据库。")
    try:
        with closing(sqlite3.connect(":memory:")) as db:
            db.execute('PRAGMA temp_store=MEMORY')
            db.deserialize(blob)
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise BackupError("解密数据库完整性校验失败。")
            names = [row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            counts = {name: db.execute('SELECT COUNT(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
                      for name in names}
            return {"quick_check": "ok", "schema_version": db.execute("PRAGMA schema_version").fetchone()[0],
                    "table_counts": counts}
    except sqlite3.Error:
        raise BackupError("解密数据库不可读或已损坏。") from None


def create_encrypted_backup(database=None, destination=None, *, retention_days: int) -> dict[str, Any]:
    """Authenticated encrypted snapshot; retention is explicitly chosen by operator."""
    if type(retention_days) is not int or not 1 <= retention_days <= 3650:
        raise BackupError("保留期须为 1–3650 天；这不是法定保留期的认定。")
    key = _backup_key()
    source = _database_path(database)
    target = Path(destination).resolve() if destination else ROOT / "backups" / f"taxpearls-{_utc_stamp()}.tpbackup"
    if target.is_dir():
        target = target / f"taxpearls-{_utc_stamp()}.tpbackup"
    if target == source or target.exists():
        raise BackupError("加密备份目标已存在或与源数据库相同；不覆盖已有文件。")
    return _seal_snapshot(_memory_snapshot(source), target, retention_days, key)


def _seal_snapshot(blob, target, retention_days, key):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if type(retention_days) is not int or not 1 <= retention_days <= 3650:
        raise BackupError("保留期须为 1–3650 天；这不是法定保留期的认定。")
    if target.exists():
        raise BackupError("加密备份目标已存在或与源数据库相同；不覆盖已有文件。")
    if len(blob) > MAX_ENCRYPTED_BYTES - 4096:
        raise BackupError("数据库超过当前内存加密备份大小限制。")
    created = datetime.now(UTC)
    header = json.dumps({"version": 1, "cipher": "AES-256-GCM",
                         "created_at": created.isoformat(timespec="seconds"),
                         "expires_at": (created + timedelta(days=retention_days)).isoformat(timespec="seconds"),
                         "retention_days": retention_days, "key_id": hashlib.sha256(key).hexdigest()[:16]},
                        sort_keys=True, separators=(",", ":")).encode("ascii")
    nonce = os.urandom(12)
    prefix = ENCRYPTED_MAGIC + len(header).to_bytes(4, "big") + header + nonce
    sealed = prefix + AESGCM(key).encrypt(nonce, blob, prefix)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Reserve the exact final name without overwrite; publish only after fsync.
    fd, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(sealed); stream.flush(); os.fsync(stream.fileno())
        try:
            os.link(temporary, target)  # Exclusive atomic publication, including concurrent writers.
        except FileExistsError:
            raise BackupError("加密备份目标已存在；不覆盖已有文件。") from None
        return verify_encrypted_backup(target)
    finally:
        temporary.unlink(missing_ok=True)


def _open_encrypted_backup(backup) -> tuple[Path, bytes, dict[str, Any]]:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    path = Path(backup).resolve()
    try:
        if path.stat().st_size > MAX_ENCRYPTED_BYTES:
            raise BackupError("加密备份超过当前大小限制。")
        with path.open('rb') as stream:
            sealed = stream.read(MAX_ENCRYPTED_BYTES + 1)
        if len(sealed) > MAX_ENCRYPTED_BYTES:
            raise BackupError("加密备份超过当前大小限制。")
    except OSError:
        raise BackupError("加密备份文件不可读。") from None
    offset = len(ENCRYPTED_MAGIC)
    if not sealed.startswith(ENCRYPTED_MAGIC) or len(sealed) < offset + 4:
        raise BackupError("不支持的加密备份格式。")
    length = int.from_bytes(sealed[offset:offset+4], "big")
    end = offset + 4 + length
    if not 1 <= length <= 4096 or len(sealed) < end + 12 + 16:
        raise BackupError("加密备份头或密文不完整。")
    try:
        blob = AESGCM(_backup_key()).decrypt(sealed[end:end+12], sealed[end+12:], sealed[:end+12])
    except InvalidTag:
        raise BackupError("备份认证失败：密钥错误或备份被改动；禁止恢复。") from None
    try:
        header = json.loads(sealed[offset+4:end])
        if header["version"] != 1 or header["cipher"] != "AES-256-GCM":
            raise ValueError()
        created, expires = (datetime.fromisoformat(header[field]) for field in ("created_at", "expires_at"))
        days = header["retention_days"]
        if (created.tzinfo is None or expires.tzinfo is None or type(days) is not int or not 1 <= days <= 3650
                or expires - created != timedelta(days=days)):
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise BackupError("加密备份元数据无效。") from None
    verified = {"backup": str(path), "encrypted": True, "bytes": len(sealed),
                "sha256": hashlib.sha256(sealed).hexdigest(), **header, **_check_memory_snapshot(blob)}
    return path, blob, verified


def verify_encrypted_backup(backup) -> dict[str, Any]:
    return _open_encrypted_backup(backup)[2]


def _closed_database(target):
    for suffix in ("-wal", "-shm", "-journal"):
        if Path(f"{target}{suffix}").exists():
            raise BackupError("检测到 SQLite 日志文件；请先停止服务并确认连接已关闭。")


@contextmanager
def _operation_lock(directory, name):
    """Cooperating CLI operations only; not a substitute for stopping the app."""
    path = directory / name
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise BackupError("已有运维操作锁；确认原操作已退出后人工核查，不能自动抢占。") from None
    try:
        os.close(fd)
        yield
    finally:
        path.unlink(missing_ok=True)


def restore_encrypted_backup(backup, database=None, *, safety_retention_days: int) -> dict[str, Any]:
    source, blob, verified = _open_encrypted_backup(backup)
    if type(safety_retention_days) is not int or not 1 <= safety_retention_days <= 3650:
        raise BackupError("恢复前安全备份保留期须为 1–3650 天。")
    target = _database_path(database)
    if source == target:
        raise BackupError("恢复源不能与目标数据库相同。")
    target.parent.mkdir(parents=True, exist_ok=True)
    with _operation_lock(target.parent, f".{target.name}.restore-lock"):
        _closed_database(target)
        before = _sha256(target) if target.exists() else None
        safety = None
        if before is not None:
            safety_path = target.with_name(f"{target.name}.pre-restore-{_utc_stamp()}.tpbackup")
            # No WAL/journal exists and maintenance was explicitly confirmed.
            # immutable=1 prevents a read-only safety snapshot creating sidecars.
            safety = _seal_snapshot(_memory_snapshot(target, offline=True), safety_path,
                                    safety_retention_days, _backup_key())
        fd, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".restore", dir=target.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(blob); stream.flush(); os.fsync(stream.fileno())
            _check_database(temporary)
            _closed_database(target)
            actual = _sha256(target) if target.exists() else None
            if actual != before:
                raise BackupError("恢复期间目标数据库变化，已取消覆盖；请确认服务停止。")
            os.replace(temporary, target)
            return {"database": str(target), "restored_from": str(source),
                    "restored_sha256": hashlib.sha256(blob).hexdigest(),
                    "source_ciphertext_sha256": verified["sha256"],
                    "safety_backup": safety["backup"] if safety else None,
                    "safety_encrypted": bool(safety), **_check_database(target)}
        finally:
            temporary.unlink(missing_ok=True)


def _backup_directory(directory):
    path = Path(directory).resolve()
    if not path.is_dir() or path in (ROOT, Path.home(), Path(path.anchor)):
        raise BackupError("请明确指定专用备份目录，不使用仓库、用户目录或磁盘根目录。")
    return path


def _managed_file(backup, directory):
    path = Path(backup).absolute()
    if path.is_symlink() or path.resolve().parent != directory or path.parent.resolve() != directory:
        raise BackupError("销毁目标必须是指定目录直接包含的普通备份文件，不能是符号链接。")
    if path.suffix != '.tpbackup' or not path.is_file() or path.stat().st_nlink != 1:
        raise BackupError("只能销毁单一链接的 .tpbackup 普通文件，不处理硬链接或其他用户文件。")
    return path.resolve()


def _retention_item(backup, directory, now):
    path = _managed_file(backup, directory)
    verified = verify_encrypted_backup(path)
    marker = path.with_name(path.name + '.hold')
    held = marker.exists() or marker.is_symlink()
    return {key: verified[key] for key in ('backup', 'sha256', 'bytes', 'created_at', 'expires_at', 'key_id')} | {
        'legal_hold': held, 'eligible': not held and datetime.fromisoformat(verified['expires_at']) <= now}


def retention_plan(directory, *, now=None) -> dict[str, Any]:
    """Read-only, direct children only; file names/mtime do not establish expiry."""
    root = _backup_directory(directory)
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise BackupError("保留期检查时间必须包含时区。")
    entries, rejected = [], []
    for path in sorted(root.glob('*.tpbackup')):
        try:
            entries.append(_retention_item(path, root, current))
        except BackupError as exc:
            rejected.append({'backup': str(path), 'error': str(exc)})
    return {'directory': str(root), 'checked_at': current.isoformat(), 'dry_run': True,
            'entries': entries, 'rejected': rejected}


def _signed_receipt(record, key):
    payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    signing_key = hmac.digest(key, b'TaxPearls/backup/destruction-receipt/v1', 'sha256')
    return {**record, 'hmac_sha256': hmac.digest(signing_key, payload, 'sha256').hex()}


def verify_destruction_receipt(receipt):
    try:
        with Path(receipt).open('rb') as stream:
            encoded = stream.read(65537)
        if len(encoded) > 65536:
            raise BackupError("销毁回执超过大小限制。")
        record = json.loads(encoded)
        signature = record.pop('hmac_sha256')
        expected = _signed_receipt(record, _backup_key())['hmac_sha256']
        if not isinstance(signature, str) or not hmac.compare_digest(expected, signature):
            raise BackupError("销毁回执认证失败。")
        return record
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
        raise BackupError("销毁回执无效或不可读。") from None


def destroy_encrypted_backup(backup, directory, *, expected_sha256, reason, receipt=None,
                             confirm=False, now=None):
    """Destroy one expired reviewed file, never a directory or non-expired backup."""
    root = _backup_directory(directory)
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise BackupError("销毁检查时间必须包含时区。")
    with (_operation_lock(root, '.backup-destruction-lock') if confirm else nullcontext()):
        item = _retention_item(backup, root, current)
        if not isinstance(expected_sha256, str) or not hmac.compare_digest(item['sha256'], expected_sha256):
            raise BackupError("文件与审阅指纹不同，请重新预览，不执行销毁。")
        if not confirm:
            return {**item, 'dry_run': True, 'destroyed': False}
        if not item['eligible']:
            raise BackupError("备份未到期或存在 .hold 保留标记，禁止销毁。")
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 200:
            raise BackupError("请提供 1–200 字符的销毁审批/工单说明，不填写密钥或财务内容。")
        record_path = Path(receipt).absolute() if receipt else None
        if (not record_path or record_path.is_symlink() or record_path.resolve().parent != root
                or record_path.suffix != '.json' or record_path.exists()):
            raise BackupError("须指定备份目录内新的 .json 回执文件，不覆盖已有文件。")
        path = Path(item['backup'])
        key = _backup_key()
        record = {'schema': 1, 'state': 'prepared', 'prepared_at': current.isoformat(),
                  **item, 'operator': getpass.getuser(), 'reason': reason.strip(),
                  'notice': '仅逻辑删除此副本，不证明 SSD/云端/异地副本物理擦除。'}
        # An empty private claim directory makes the move target unambiguous.
        claim_directory = Path(tempfile.mkdtemp(prefix='.destroy-', dir=root))
        claimed = claim_directory / path.name
        record['claimed_path'] = str(claimed)
        try:
            # Reserve receipt name exclusively before any destructive step.
            fd = os.open(record_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(_signed_receipt(record, key), stream, ensure_ascii=False)
                stream.flush(); os.fsync(stream.fileno())
            os.replace(path, claimed)
            try:
                if claimed.is_symlink() or not claimed.is_file():
                    raise BackupError("销毁前目标不再是普通文件，已取消删除。")
                verified = verify_encrypted_backup(claimed)
                marker = path.with_name(path.name + '.hold')
                if (verified['sha256'] != item['sha256'] or claimed.stat().st_nlink != 1
                        or marker.exists() or marker.is_symlink()):
                    raise BackupError("目标在销毁前发生变化，已取消删除。")
            except Exception:
                # Restore without overwriting anything concurrently created at the old name.
                try:
                    os.link(claimed, path); claimed.unlink()
                    record['state'] = 'cancelled'
                except OSError:
                    record['state'] = 'needs_recovery'
                _write_json_atomic(record_path, _signed_receipt(record, key))
                raise
            claimed.unlink()
            record.update(state='completed', completed_at=datetime.now(UTC).isoformat())
            _write_json_atomic(record_path, _signed_receipt(record, key))
            return {**item, 'dry_run': False, 'destroyed': True, 'receipt': str(record_path),
                    'notice': record['notice']}
        finally:
            if not claimed.exists() and not claimed.is_symlink():
                claim_directory.rmdir()


def create_backup(database: str | Path | None = None, destination: str | Path | None = None,
                  *, _offline=False) -> dict[str, Any]:
    source = _database_path(database)
    if not source.is_file():
        raise BackupError(f"源数据库不存在：{source}")
    if destination is None:
        target = ROOT / "backups" / f"taxpearls-{_utc_stamp()}.sqlite3"
    else:
        target = Path(destination).resolve()
        if target.exists() and target.is_dir():
            target = target / f"taxpearls-{_utc_stamp()}.sqlite3"
    if source == target:
        raise BackupError("备份目标不能与源数据库相同")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    temporary.unlink(missing_ok=True)
    try:
        if _offline:
            _closed_database(source)
        source_uri = source.as_uri() + "?mode=ro" + ("&immutable=1" if _offline else "")
        with closing(sqlite3.connect(source_uri, uri=True, timeout=30)) as source_db:
            with closing(sqlite3.connect(temporary, timeout=30)) as target_db:
                source_db.backup(target_db)
        verification = _check_database(temporary)
        os.replace(temporary, target)
        manifest = {
            "manifest_version": MANIFEST_VERSION,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "backup_file": target.name,
            "bytes": target.stat().st_size,
            "sha256": _sha256(target),
            **verification,
        }
        manifest_path = _manifest_path(target)
        _write_json_atomic(manifest_path, manifest)
        return {"backup": str(target), "manifest": str(manifest_path), **manifest}
    except sqlite3.Error as exc:
        raise BackupError(f"SQLite 在线备份失败：{exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def verify_backup(backup: str | Path, manifest: str | Path | None = None) -> dict[str, Any]:
    path = Path(backup).resolve()
    manifest_path = Path(manifest).resolve() if manifest else _manifest_path(path)
    if not manifest_path.is_file():
        raise BackupError(f"缺少备份清单：{manifest_path}")
    try:
        expected = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError(f"备份清单不可读：{exc}") from exc
    if expected.get("manifest_version") != MANIFEST_VERSION:
        raise BackupError("不支持的备份清单版本")
    if expected.get("backup_file") != path.name:
        raise BackupError("备份文件名与清单不一致")
    actual_hash = _sha256(path)
    if actual_hash != expected.get("sha256"):
        raise BackupError("备份 SHA-256 校验失败")
    if path.stat().st_size != expected.get("bytes"):
        raise BackupError("备份文件大小与清单不一致")
    verification = _check_database(path)
    if verification["schema_version"] != expected.get("schema_version"):
        raise BackupError("备份 schema_version 与清单不一致")
    if verification["table_counts"] != expected.get("table_counts"):
        raise BackupError("备份表记录数与清单不一致")
    return {"backup": str(path), "manifest": str(manifest_path), "sha256": actual_hash, **verification}


def restore_backup(
    backup: str | Path,
    database: str | Path | None = None,
    manifest: str | Path | None = None,
) -> dict[str, Any]:
    # Share the lock with encrypted restores. A stale lock requires human review.
    target = _database_path(database)
    verify_backup(backup, manifest)
    target.parent.mkdir(parents=True, exist_ok=True)
    with _operation_lock(target.parent, f".{target.name}.restore-lock"):
        return _restore_plain_backup(backup, database, manifest)


def _restore_plain_backup(backup, database=None, manifest=None):
    source = Path(backup).resolve()
    verified = verify_backup(source, manifest)
    target = _database_path(database)
    if source == target:
        raise BackupError("恢复源不能与目标数据库相同")
    _closed_database(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    safety: dict[str, Any] | None = None
    before = _sha256(target) if target.exists() else None
    if target.exists():
        safety_target = target.with_name(f"{target.name}.pre-restore-{_utc_stamp()}.sqlite3")
        safety = create_backup(target, safety_target, _offline=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".restore", dir=target.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with source.open("rb") as input_stream, temporary.open("wb") as output_stream:
            for chunk in iter(lambda: input_stream.read(1024 * 1024), b""):
                output_stream.write(chunk)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        _check_database(temporary)
        if _sha256(temporary) != verified['sha256']:
            raise BackupError("恢复源在校验后发生变化，已取消覆盖。")
        _closed_database(target)
        if (_sha256(target) if target.exists() else None) != before:
            raise BackupError("恢复期间目标数据库变化，已取消覆盖；请确认服务停止。")
        os.replace(temporary, target)
        restored = _check_database(target)
        return {
            "database": str(target),
            "restored_from": str(source),
            "restored_sha256": verified["sha256"],
            "safety_backup": safety["backup"] if safety else None,
            "safety_manifest": safety["manifest"] if safety else None,
            **restored,
        }
    finally:
        temporary.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TaxPearls SQLite 备份、校验与恢复")
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup", help="旧明文备份兼容入口；必须显式允许明文，不会自动降级")
    backup.add_argument("--database", help="数据库路径；默认读取 TAXPEARLS_DB")
    backup.add_argument("--output", help="备份文件或目录；默认写入 backups/")
    backup.add_argument("--allow-plaintext", action="store_true", help="明确允许旧明文格式，仅用于迁移/测试")
    encrypted = commands.add_parser("backup-encrypted", help="创建认证加密备份；密钥只读取 TAXPEARLS_BACKUP_KEY")
    encrypted.add_argument("--database")
    encrypted.add_argument("--output")
    encrypted.add_argument("--retention-days", type=int, required=True, help="操作者明确选择保留天数，不认定法定期限")
    encrypted_verify = commands.add_parser("verify-encrypted", help="解密认证并在内存核对数据库，不输出明文备份")
    encrypted_verify.add_argument("backup")
    encrypted_restore = commands.add_parser("restore-encrypted", help="停止服务后恢复；恢复前安全备份保持加密")
    encrypted_restore.add_argument("backup")
    encrypted_restore.add_argument("--database")
    encrypted_restore.add_argument("--safety-retention-days", type=int, required=True)
    encrypted_restore.add_argument("--yes", action="store_true")
    retention = commands.add_parser("retention-plan", help="只读预览指定目录的认证到期备份；不递归或删除")
    retention.add_argument("directory")
    destroy = commands.add_parser("destroy-encrypted", help="仅销毁已审阅且到期的单个备份；默认只预览")
    destroy.add_argument("backup")
    destroy.add_argument("--directory", required=True)
    destroy.add_argument("--sha256", required=True)
    destroy.add_argument("--reason", required=True)
    destroy.add_argument("--receipt", help="本目录内新的 JSON 回执路径；实际销毁时必填")
    destroy.add_argument("--yes", action="store_true")
    receipt = commands.add_parser("verify-receipt", help="认证销毁回执，不把 prepared/needs_recovery 当作完成")
    receipt.add_argument("receipt")
    verify = commands.add_parser("verify", help="校验 SHA-256、SQLite 完整性与表记录数")
    verify.add_argument("backup")
    verify.add_argument("--manifest")
    restore = commands.add_parser("restore", help="停止服务后恢复；自动保留恢复前安全备份")
    restore.add_argument("backup")
    restore.add_argument("--database", help="目标数据库；默认读取 TAXPEARLS_DB")
    restore.add_argument("--manifest")
    restore.add_argument("--yes", action="store_true", help="确认执行覆盖恢复")
    restore.add_argument("--allow-plaintext", action="store_true", help="明确允许旧明文恢复及明文安全备份")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            if not args.allow_plaintext:
                parser.error("旧 backup 会生成明文；请用 backup-encrypted，兼容迁移须显式 --allow-plaintext")
            result = create_backup(args.database, args.output)
        elif args.command == "backup-encrypted":
            result = create_encrypted_backup(args.database, args.output, retention_days=args.retention_days)
        elif args.command == "verify-encrypted":
            result = verify_encrypted_backup(args.backup)
        elif args.command == "restore-encrypted":
            if not args.yes:
                parser.error("restore-encrypted 会替换目标数据库，必须显式传入 --yes 并先停止服务")
            result = restore_encrypted_backup(args.backup, args.database, safety_retention_days=args.safety_retention_days)
        elif args.command == "retention-plan":
            result = retention_plan(args.directory)
        elif args.command == "destroy-encrypted":
            result = destroy_encrypted_backup(args.backup, args.directory, expected_sha256=args.sha256,
                      reason=args.reason, receipt=args.receipt, confirm=args.yes)
        elif args.command == "verify-receipt":
            result = verify_destruction_receipt(args.receipt)
        elif args.command == "verify":
            result = verify_backup(args.backup, args.manifest)
        else:
            if not args.yes or not args.allow_plaintext:
                parser.error("旧 restore 须显式 --yes --allow-plaintext；加密文件请用 restore-encrypted")
            result = restore_backup(args.backup, args.database, args.manifest)
    except (BackupError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
