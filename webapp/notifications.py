"""FR-C05 summary boundary: never expose financial values or source evidence."""
from __future__ import annotations

from html import escape
from dataclasses import dataclass
import json
import logging
import os
import re
from threading import Event, Thread
from types import MappingProxyType
from typing import Callable

from src.models import Finding

EVENT_LABELS = {"audit_completed": "审计完成", "high_risk": "高风险条目提醒"}
RECIPIENT_ROLES = {"org_admin", "accountant", "teacher"}


@dataclass(frozen=True)
class Recipient:
    key: str
    # External adapters must resolve a CURRENT verified binding and consent in
    # SQLite. A binding generation prevents unsubscribe/unlink/relink revival.
    revision: str = ''


@dataclass(frozen=True)
class ChannelAdapter:
    name: str
    recipient: Callable  # (db, user) -> Recipient | None; DB-only, no network
    render: Callable     # (whitelisted_summary) -> JSON payload; no network
    send: Callable       # (recipient, frozen_payload, idempotency_key, timeout) -> provider ID
    enabled: Callable    # deployment switch, NOT recipient consent


class DeliveryError(Exception):
    def __init__(self, *, rejected=False):
        super().__init__('notification_delivery_failed')
        self.rejected = rejected


class InvalidRecipient(ValueError):
    """Invalid resolver output, distinct from a resolver/database failure."""


def _email_recipient(db, user):
    row=db.execute('''SELECT u.email,p.email_enabled FROM users u
        JOIN notification_preferences p ON p.user_id=u.id WHERE u.id=?''',(user['id'],)).fetchone()
    return Recipient(row['email']) if row and row['email_enabled'] and row['email'] else None


def _email_render(summary):
    from src.mailer import _from_header
    subject,html,text=email_content(summary)
    return {'subject':subject,'html':html,'text':text,'from_header':_from_header()}


def _email_send(recipient, payload, idempotency_key, timeout, sender=None):
    from src.mailer import MailError, send_email
    try:
        return (sender or send_email)(to=recipient,**payload,idempotency_key=idempotency_key,timeout=timeout)
    except MailError as exc:
        raise DeliveryError(rejected=exc.status is not None and 400<=exc.status<500) from None


def channel_registry(extra=None):
    """Trusted application adapters only; never register destinations from JSON.

    H02/H03/H04 will supply real adapters/verified identity resolvers. Only email
    is installed here; test adapters do not imply a live IM integration.
    """
    registry={'email':ChannelAdapter('email',_email_recipient,_email_render,_email_send,email_delivery_enabled)}
    for name,adapter in (extra or {}).items():
        if (not isinstance(name,str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,31}',name)
                or name=='email' or not isinstance(adapter,ChannelAdapter) or adapter.name!=name
                or not all(callable(getattr(adapter,key)) for key in ('recipient','render','send','enabled'))):
            raise ValueError('通知渠道适配器无效。')
        registry[name]=adapter
    if len(registry)>8:
        raise ValueError('通知渠道数量超出上限。')
    return MappingProxyType(registry)


def recipient_for(adapter, db, user):
    result=adapter.recipient(db,user)
    if result is None:
        return None
    if (not isinstance(result,Recipient) or not isinstance(result.key,str) or not 1<=len(result.key)<=512
            or any(ord(c)<32 for c in result.key) or not isinstance(result.revision,str)
            or len(result.revision)>128 or adapter.name!='email' and not result.revision):
        raise InvalidRecipient('通知接收身份无效。')
    return result


def whitelisted_summary(summary):
    """No provider renderer receives raw dataset/evidence or extra JSON fields."""
    if not isinstance(summary,dict) or not isinstance(summary.get('event'),str) or summary['event'] not in EVENT_LABELS:
        raise ValueError('通知类型无效。')
    result={key:summary[key] for key in ('audit_id','event','audited_at','total_rules','hit_count','high_count','risk_count')}
    if (any(not isinstance(result[key],str) for key in ('audit_id','audited_at'))
            or any(type(result[key]) is not int or result[key]<0 for key in ('total_rules','hit_count','high_count','risk_count'))
            or not isinstance(summary.get('risks'),list)):
        raise ValueError('通知摘要无效。')
    result['risks']=[]
    for item in summary['risks'][:20]:
        if not isinstance(item,dict) or any(not isinstance(item.get(key),str) for key in ('id','name','severity')):
            raise ValueError('通知风险摘要无效。')
        result['risks'].append({key:item[key] for key in ('id','name','severity')})
    return result


def frozen_payload(adapter, summary):
    payload=adapter.render(whitelisted_summary(summary))
    return validated_payload(payload)


def validated_payload(payload):
    """Apply the same JSON/type/size contract on enqueue and restored queues."""
    if not isinstance(payload,dict):
        raise ValueError('通知载荷无效。')
    encoded=json.dumps(payload,ensure_ascii=False,allow_nan=False)
    if len(encoded.encode('utf-8'))>128*1024:
        raise ValueError('通知载荷过大。')
    return payload


def migrate_deliveries(db):
    """Preserve all legacy email receipts/payloads/claims while adding fan-out.

    No other table references this queue. The savepoint makes copy/swap atomic,
    including for legacy databases where schema creation committed earlier.
    """
    db.execute('SAVEPOINT notification_channels')
    try:
        # Acquire a write reservation BEFORE inspecting the schema. A second
        # startup must not rebuild an already-migrated multi-channel queue as
        # legacy email after waiting for the first startup's schema swap.
        db.execute('UPDATE notification_deliveries SET notification_id=notification_id WHERE 0')
        columns={r['name'] for r in db.execute('PRAGMA table_info(notification_deliveries)')}
        if 'channel' in columns:
            db.execute('RELEASE notification_channels')
            return
        db.execute('ALTER TABLE notification_deliveries RENAME TO notification_deliveries_h01_old')
        db.execute('''CREATE TABLE notification_deliveries (
            notification_id TEXT NOT NULL REFERENCES notifications(id),
            channel TEXT NOT NULL DEFAULT 'email', recipient_key TEXT,
            recipient_revision TEXT NOT NULL DEFAULT '', recipient_email TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            claim_token TEXT, provider_id TEXT, error_code TEXT, claimed_at TEXT, payload_json TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY(notification_id,channel))''')
        db.execute('''INSERT INTO notification_deliveries
            (notification_id,recipient_key,recipient_email,status,attempts,claim_token,
             provider_id,error_code,claimed_at,payload_json,created_at,updated_at)
            SELECT notification_id,recipient_email,recipient_email,status,attempts,claim_token,
                   provider_id,error_code,claimed_at,payload_json,created_at,updated_at
            FROM notification_deliveries_h01_old''')
        db.execute('DROP TABLE notification_deliveries_h01_old')
        db.execute('''CREATE INDEX idx_notification_deliveries_status
            ON notification_deliveries(status,created_at,notification_id,channel)''')
        db.execute('RELEASE notification_channels')
    except Exception:
        db.execute('ROLLBACK TO notification_channels')
        db.execute('RELEASE notification_channels')
        raise


def email_delivery_enabled() -> bool:
    return (os.getenv("TAXPEARLS_NOTIFICATION_EMAIL_ENABLED", "") == "1"
            and bool(os.getenv("TAXPEARLS_RESEND_API_KEY", "").strip()))


def deliver_pending(store, limit: int = 20, sender=None) -> int:
    """Send claimed frozen payloads; unknown outcomes never auto-retry."""
    registry=store.notification_adapters
    # Backward-compatible explicit mail transport injection, never used for IM.
    channels=['email'] if sender else [key for key,adapter in registry.items() if adapter.enabled()]
    count = 0
    for _ in range(limit):
        claim = store.claim_notification_delivery(channels=channels)
        if claim is None:
            break
        channel=claim['channel']
        try:
            # Keep legacy email keys exactly unchanged (including manual retry).
            key='audit-notification/'+('' if channel=='email' else channel+'/')+claim['notification_id']
            if sender:
                provider_id=_email_send(claim['recipient_key'],claim['payload'],key,10,sender)
            else:
                provider_id=registry[channel].send(claim['recipient_key'],claim['payload'],key,10)
            store.finish_notification_delivery(claim["notification_id"], claim["claim_token"], "accepted", provider_id,channel=channel)
        except DeliveryError as exc:
            store.finish_notification_delivery(claim["notification_id"], claim["claim_token"],
                                               "failed" if exc.rejected else "uncertain", error_code="provider_rejected" if exc.rejected else "send_unknown",channel=channel)
        except Exception:
            # Neither exception text nor provider response is safe to log.
            store.finish_notification_delivery(claim["notification_id"], claim["claim_token"], "uncertain", error_code="send_unknown",channel=channel)
        count += 1
    return count


class NotificationWorker:
    def __init__(self, store_getter, interval: float = 2):
        self.store_getter = store_getter
        self.interval = interval
        self.stop_event = Event()
        self.thread = Thread(target=self.run, name="notification-delivery", daemon=True)

    def start(self):
        if any(adapter.enabled() for adapter in self.store_getter().notification_adapters.values()):
            self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=12)

    def run(self):
        while not self.stop_event.is_set():
            try:
                target = self.store_getter()
                if any(adapter.enabled() for adapter in target.notification_adapters.values()):
                    target.recover_notification_claims()
                    # One send per iteration bounds shutdown to the request timeout.
                    deliver_pending(target, limit=1)
            except Exception:
                logging.getLogger(__name__).warning("Notification worker iteration failed; details withheld")
            self.stop_event.wait(self.interval)


def safe_summary(audit_id: str, findings: list[Finding], event: str, when: str) -> dict:
    if event not in EVENT_LABELS:
        raise ValueError("通知类型无效")
    hits = [item for item in findings if item.status == "hit"]
    high = [item for item in hits if item.rule.severity == "high"]
    shown = high if event == "high_risk" else hits
    return {
        "audit_id": audit_id, "event": event, "audited_at": when,
        "total_rules": len(findings), "hit_count": len(hits), "high_count": len(high),
        "risk_count": len(shown),
        "risks": [{"id": item.rule.id, "name": item.rule.name,
                   "severity": item.rule.severity} for item in shown[:20]],
    }


def email_content(summary: dict) -> tuple[str, str, str]:
    """Format only the explicitly whitelisted summary, not arbitrary JSON fields."""
    label = EVENT_LABELS[summary["event"]]
    lines = [label, f"审计编号：{summary['audit_id']}",
             f"风险条目 {int(summary['hit_count'])} 项，其中高风险 {int(summary['high_count'])} 项。"]
    lines.extend(f"{item['id']}：{item['name']}" for item in summary["risks"][:20])
    if int(summary["risk_count"]) > len(summary["risks"]):
        lines.append("更多条目请登录工作台查看。")
    lines.extend(["本邮件不附带原始财务数据；风险摘要不等于违法认定。",
                  "如需退订，请登录工作台关闭邮件通知。"])
    text = "\n".join(lines)
    html = "<div>" + "".join(f"<p>{escape(line)}</p>" for line in lines) + "</div>"
    return f"【税海拾珠】{label}", html, text
