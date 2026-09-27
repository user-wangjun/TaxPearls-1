"""Persistent, student-owned mistakes and practice isolated from formal grades.

All reads use a single authorization snapshot; writes recheck authorization in
BEGIN IMMEDIATE. Old submissions are imported explicitly, never invented.
"""
from datetime import UTC, datetime
import hashlib
import json
import math
import secrets
from typing import Annotated

from fastapi import Cookie, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from src.training import score_submission
from webapp import classroom
from webapp.training_profiles import timestamp
from webapp.training_stats import frozen_rules, selected_answers


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def migrate(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS training_mistake_cases (
            id TEXT PRIMARY KEY,student_id TEXT NOT NULL REFERENCES users(id),
            assignment_id TEXT NOT NULL REFERENCES assignments(id),source_hash TEXT NOT NULL,
            case_hash TEXT NOT NULL,errors_json TEXT NOT NULL,first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,
            latest_attempt_id TEXT,UNIQUE(student_id,assignment_id)
        );
        CREATE TABLE IF NOT EXISTS training_practice_attempts (
            id TEXT PRIMARY KEY,case_id TEXT NOT NULL REFERENCES training_mistake_cases(id),
            request_id TEXT NOT NULL,request_hash TEXT NOT NULL,answers_json TEXT NOT NULL,
            result_json TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(case_id,request_id)
        );
        CREATE INDEX IF NOT EXISTS idx_training_practice_case ON training_practice_attempts(case_id,created_at);
    """)


def active_student(db, who):
    row = db.execute("SELECT * FROM users WHERE id=? AND org_id=? AND role='student' AND active=1",
                     (who['id'], who['org_id'])).fetchone()
    if not row:
        raise HTTPException(404, '学生账号不可用。')


def assignment(db, aid, who):
    row = db.execute('SELECT * FROM assignments WHERE id=?', (aid,)).fetchone()
    item = classroom.decorate(db, dict(row)) if row else None
    if not classroom.can_access(db, item, who):
        raise HTTPException(404, '错题不存在或已不可访问。')
    return item


def snapshot(db, item):
    # Local import avoids a storage/schema initialization cycle.
    from webapp.storage import deserialize_dataset, deserialize_findings
    row = db.execute('SELECT * FROM audits WHERE id=? AND org_id=?', (item['audit_id'], item['org_id'])).fetchone()
    if not row:
        raise ValueError('missing snapshot')
    rules = frozen_rules(row['findings_json'])
    raw_dataset, raw_findings = json.loads(row['dataset_json']), json.loads(row['findings_json'])
    dataset = deserialize_dataset(raw_dataset)
    name, taxpayer = dataset.company.name, dataset.company.taxpayer_id
    if not ('仿真' in name or '纯合成测试' in name or 'TEST' in taxpayer.upper()):
        raise ValueError('non-synthetic material')
    findings = deserialize_findings(raw_findings)
    weights = json.loads(item['weights_json'])
    # Existing scorer validates positivity; reject non-finite configuration too.
    fingerprint = digest([item['audit_id'], raw_dataset, raw_findings, weights, item['false_positive_penalty']])
    if not isinstance(weights, dict) or set(weights) - {r for r in rules if rules[r]['status'] == 'hit'}:
        raise ValueError('invalid weights')
    if (any(not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 for v in weights.values())
            or not isinstance(item['false_positive_penalty'], (int, float))
            or not math.isfinite(item['false_positive_penalty']) or not 0 <= item['false_positive_penalty'] <= 100):
        raise ValueError('invalid grading configuration')
    return rules, findings, dataset, weights, fingerprint


def accumulate(errors, rules, chosen, stamp, origin):
    """Skipped selections are explicitly unsupported, not ordinary false alarms."""
    wrong = []
    for rid, rule in rules.items():
        kind = ('missed' if rid not in chosen else None) if rule['status'] == 'hit' else (
            ('unsupported' if rule['status'] == 'skipped' else 'false_positive') if rid in chosen else None)
        if kind is None:
            continue
        key = rid + ':' + kind
        entry = errors.setdefault(key, {**rule, 'kind': kind, 'first_seen': stamp, 'last_seen': stamp,
                                       'formal_count': 0, 'practice_count': 0})
        entry[origin + '_count'] += 1
        entry['last_seen'] = stamp
        wrong.append(key)
    return wrong


def capture(db, aid, sid, force=False):
    """Called inside the formal submission transaction, or an explicit import.

    Invalid legacy records remain untouched and are counted as unavailable.
    Storage failures are not swallowed: the formal submission must roll back.
    """
    item = db.execute('SELECT * FROM assignments WHERE id=?', (aid,)).fetchone()
    sub = db.execute('SELECT * FROM submissions WHERE assignment_id=? AND student_id=?', (aid, sid)).fetchone()
    if not item or not sub:
        return 'invalid'
    case = db.execute('SELECT * FROM training_mistake_cases WHERE assignment_id=? AND student_id=?', (aid, sid)).fetchone()
    try:
        rules, _, _, _, fingerprint = snapshot(db, item)
        chosen = selected_answers(sub, rules)
        timestamp(sub['submitted_at'])
        source = digest([sub['id'], sub['submitted_at'], sorted(chosen)])
        if case and case['case_hash'] != fingerprint:
            return 'invalid'
        errors = checked_errors(case, rules) if case else {}
        if case and case['source_hash'] == source and not force:
            return 'unchanged'
        wrong = accumulate(errors, rules, chosen, sub['submitted_at'], 'formal')
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return 'invalid'
    if not wrong:
        # A correct formal resubmission doesn't erase earlier mistakes or pretend
        # that a separate practice attempt occurred.
        if case:
            db.execute('UPDATE training_mistake_cases SET source_hash=? WHERE id=?', (source, case['id']))
        return 'unchanged'
    if case:
        db.execute('''UPDATE training_mistake_cases SET source_hash=?,errors_json=?,last_seen=?,
                      revision=revision+1,latest_attempt_id=NULL WHERE id=?''',
                   (source, encode(errors), sub['submitted_at'], case['id']))
        return 'updated'
    db.execute('''INSERT INTO training_mistake_cases
                  (id,student_id,assignment_id,source_hash,case_hash,errors_json,first_seen,last_seen)
                  VALUES (?,?,?,?,?,?,?,?)''',
               (secrets.token_hex(12), sid, aid, source, fingerprint, encode(errors), sub['submitted_at'], sub['submitted_at']))
    return 'added'


def checked_errors(case, rules):
    errors = json.loads(case['errors_json'])
    if not isinstance(errors, dict) or not errors:
        raise ValueError('missing mistake records')
    for key, error in errors.items():
        rule = rules[error['rule_id']]
        expected_kind = {'hit': 'missed', 'pass': 'false_positive', 'skipped': 'unsupported'}[rule['status']]
        if (key != rule['rule_id'] + ':' + expected_kind or error['kind'] != expected_kind
                or any(error[k] != v for k, v in rule.items())
                or any(type(error[k]) is not int or error[k] < 0 for k in ('formal_count', 'practice_count'))
                or error['formal_count'] + error['practice_count'] < 1):
            raise ValueError('invalid mistake records')
        timestamp(error['first_seen'])
        timestamp(error['last_seen'])
    return errors


def checked_snapshot(db, item, case):
    try:
        result = snapshot(db, item)
        if result[-1] != case['case_hash']:
            raise ValueError('changed snapshot')
        checked_errors(case, result[0])
        return result
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise HTTPException(409, '原案例材料或评分口径缺失／变化，暂不可重练，请核查备份。') from None


def case_for_user(db, cid, who):
    active_student(db, who)
    case = db.execute('SELECT * FROM training_mistake_cases WHERE id=? AND student_id=?', (cid, who['id'])).fetchone()
    if not case:
        raise HTTPException(404, '错题不存在或已不可访问。')
    return case, assignment(db, case['assignment_id'], who)


def summary(db, case, item):
    attempt = db.execute('SELECT * FROM training_practice_attempts WHERE id=? AND case_id=?',
                         (case['latest_attempt_id'], case['id'])).fetchone()
    result = json.loads(attempt['result_json']) if attempt else None
    chosen = set(json.loads(attempt['answers_json'])) if attempt else None
    errors = list(json.loads(case['errors_json']).values())
    for error in errors:
        error['correct_in_latest_practice'] = (None if chosen is None else
            ((error['rule_id'] in chosen) == (error['status'] == 'hit')))
    return {'id': case['id'], 'assignment_id': item['id'], 'title': item['title'],
            'revision': case['revision'], 'first_seen': case['first_seen'], 'last_seen': case['last_seen'],
            'errors': sorted(errors, key=lambda e: (e['rule_id'], e['kind'])),
            'status': 'corrected' if result and not result['missed'] and not result['false_positives'] else 'pending',
            'practice_count': db.execute('SELECT COUNT(*) FROM training_practice_attempts WHERE case_id=?', (case['id'],)).fetchone()[0],
            'latest_practice': None if not attempt else {'id': attempt['id'], 'created_at': attempt['created_at'], 'result': result}}


class PracticeBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=1, strict=True)
    request_id: str = Field(pattern=r'^[a-zA-Z0-9-]{16,64}$', strict=True)
    selected_rule_ids: list[Annotated[str, Field(min_length=1, max_length=100, strict=True)]] = Field(max_length=500)


def register(app, store_provider, get_user, allow, cookie_name):
    def student(session):
        who = get_user(session)
        allow(who, 'student')
        return who

    def response(body):
        return JSONResponse(body, headers={'Cache-Control': 'private, no-store'})

    @app.post('/api/training/mistakes/sync')
    def sync(session: str | None = Cookie(default=None, alias=cookie_name)):
        who = student(session)
        with store_provider().connect() as db:
            db.execute('BEGIN IMMEDIATE')
            active_student(db, who)
            totals = dict.fromkeys(('added', 'updated', 'unchanged', 'invalid'), 0)
            rows = db.execute('''SELECT sub.assignment_id FROM submissions sub JOIN assignments a ON a.id=sub.assignment_id
                                 WHERE sub.student_id=? AND a.org_id=?''', (who['id'], who['org_id'])).fetchall()
            for row in rows:
                try:
                    assignment(db, row['assignment_id'], who)
                except HTTPException as exc:
                    if exc.status_code == 404:
                        continue
                    raise
                totals[capture(db, row['assignment_id'], who['id'])] += 1
        return response(totals)

    @app.get('/api/training/mistakes')
    def listing(session: str | None = Cookie(default=None, alias=cookie_name)):
        who = student(session)
        with store_provider().connect() as db:
            db.execute('BEGIN')
            active_student(db, who)
            cases = []
            for case in db.execute('SELECT * FROM training_mistake_cases WHERE student_id=? ORDER BY last_seen DESC,id', (who['id'],)).fetchall():
                try:
                    item = assignment(db, case['assignment_id'], who)
                except HTTPException as exc:
                    if exc.status_code == 404:
                        continue
                    raise
                try:
                    checked_snapshot(db, item, case)
                    entry = summary(db, case, item)
                    entry['available'] = True
                except HTTPException:
                    # Do not expose obsolete answers or fabricate a clean slate.
                    entry = {'id': case['id'], 'assignment_id': item['id'], 'title': item['title'],
                             'available': False, 'status': 'unavailable', 'errors': []}
                cases.append(entry)
        return response({'cases': cases, 'method': '新正式提交自动留存错项；旧记录须主动同步，仅导入当前保留的最新提交，不补造过往重答。'
                         '错项按原案例规则口径留存，材料不足仍标风险单列。重练为整案例独立练习，不覆盖正式成绩、教师批改或学情画像；'
                         '已截止但仍获授权的案例可课后重练，撤回或撤权后不可访问。一次订正不代表已掌握。'})

    @app.get('/api/training/mistakes/{case_id}')
    def detail(case_id: str, session: str | None = Cookie(default=None, alias=cookie_name)):
        who = student(session)
        with store_provider().connect() as db:
            db.execute('BEGIN')
            case, item = case_for_user(db, case_id, who)
            rules, findings, data, _, _ = checked_snapshot(db, item, case)
            entry = summary(db, case, item)
            entry['review'] = [{'rule_id': f.rule.id, 'calculation': f.calculation, 'threshold': f.threshold_desc,
                                'conclusion': f.conclusion, 'skip_reason': f.skip_reason, 'legal_basis': f.rule.legal_basis}
                               for f in findings if any(e['rule_id'] == f.rule.id for e in entry['errors'])]
            entry['material'] = {'company': {'name': data.company.name, 'period': data.company.period},
                'accounts': [{k: str(getattr(a, k)) for k in ('code', 'name', 'opening', 'debit', 'credit', 'closing')} for a in data.accounts],
                'declarations': {k: str(v) for k, v in data.declarations.items()},
                'metrics': [{'name': m.name, 'value': str(m.value), 'source': m.source, 'detail': m.detail} for m in data.metrics.values()]}
            entry['rules'] = [{k: r[k] for k in ('rule_id', 'name', 'category')} for r in sorted(rules.values(), key=lambda r: r['rule_id'])]
        return response(entry)

    @app.post('/api/training/mistakes/{case_id}/practice')
    def practice(case_id: str, body: PracticeBody, session: str | None = Cookie(default=None, alias=cookie_name)):
        who = student(session)
        with store_provider().connect() as db:
            db.execute('BEGIN IMMEDIATE')
            case, item = case_for_user(db, case_id, who)
            rules, findings, _, weights, _ = checked_snapshot(db, item, case)
            if len(body.selected_rule_ids) != len(set(body.selected_rule_ids)) or set(body.selected_rule_ids) - rules.keys():
                raise HTTPException(422, '答案含重复或未知规则。')
            request_hash = digest([body.revision, sorted(body.selected_rule_ids)])
            prior = db.execute('SELECT * FROM training_practice_attempts WHERE case_id=? AND request_id=?', (case_id, body.request_id)).fetchone()
            if prior:
                if prior['request_hash'] != request_hash:
                    raise HTTPException(409, '重试标识已用于不同答案，请刷新后重练。')
                return response(json.loads(prior['result_json']))
            if body.revision != case['revision']:
                raise HTTPException(409, '错题记录已更新，请重新打开后重练。')
            result = score_submission(findings, body.selected_rule_ids, weights, item['false_positive_penalty'])
            stamp, attempt_id = datetime.now(UTC).isoformat(), secrets.token_hex(12)
            errors = json.loads(case['errors_json'])
            accumulate(errors, rules, set(body.selected_rule_ids), stamp, 'practice')
            db.execute('INSERT INTO training_practice_attempts VALUES (?,?,?,?,?,?,?)',
                       (attempt_id, case_id, body.request_id, request_hash, encode(sorted(body.selected_rule_ids)), encode(result), stamp))
            db.execute('UPDATE training_mistake_cases SET errors_json=?,revision=revision+1,latest_attempt_id=? WHERE id=?',
                       (encode(errors), attempt_id, case_id))
        return response(result)
