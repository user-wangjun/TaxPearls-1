"""E10 cross-assignment profiles without invented attempt history or ability scores."""
from collections import Counter
from datetime import UTC, datetime
from fractions import Fraction
import hashlib
import json

from webapp.training_stats import COUNTS, finish_counts, frozen_rules, mean, percent, selected_answers


def counts():
    return dict.fromkeys(COUNTS, 0)


def add_counts(target, source):
    for key in COUNTS:
        target[key] += source[key]


def timestamp(value):
    value = datetime.fromisoformat(value)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('unknown timezone')
    return value.astimezone(UTC)


def rate_change(current, previous, numerator, denominator):
    if not current[denominator] or not previous[denominator]:
        return None
    difference = Fraction(current[numerator], current[denominator]) - Fraction(previous[numerator], previous[denominator])
    return percent(difference.numerator, difference.denominator)


def collect(db, student, classroom=None, include_withdrawn=False):
    """Caller has checked the active student and, if supplied, class ownership.

    All permission filters and snapshots are read in the caller's transaction.
    The student view never reveals withdrawn or revoked materials through totals.
    """
    where = """a.org_id=? AND (a.target_student_id IS NULL OR a.target_student_id=?)"""
    args = [student['org_id'], student['id']]
    if classroom is None:
        where += """ AND a.published=1 AND (s.class_id IS NULL OR EXISTS (
            SELECT 1 FROM training_class_members m WHERE m.class_id=s.class_id AND m.student_id=?))"""
        args.append(student['id'])
    else:
        where += " AND s.class_id=? AND a.created_by=? AND (a.published=1 OR (? AND sub.id IS NOT NULL))"
        args.extend([classroom['id'], classroom['owner_id'], int(include_withdrawn)])
    rows = db.execute(
        f"""SELECT a.id,a.title,a.audit_id,a.published,s.paper_id,
                   f.org_id AS audit_org,f.findings_json,
                   sub.id AS submission_id,sub.answers_json,sub.score,sub.adjusted_score,sub.submitted_at
            FROM assignments a LEFT JOIN training_assignment_settings s ON s.assignment_id=a.id
            LEFT JOIN audits f ON f.id=a.audit_id
            LEFT JOIN submissions sub ON sub.assignment_id=a.id AND sub.student_id=?
            WHERE {where} ORDER BY a.id""", [student['id'], *args]).fetchall()
    totals = {'assignments': len(rows), 'submitted': 0, 'valid': 0, 'invalid': 0,
              'unsubmitted': 0, 'adjusted': 0, 'undated': 0}
    records, phases, rules, categories = [], {}, {}, {}
    audits, scores, automatic_scores = set(), [], []
    for row in rows:
        if row['submission_id'] is None:
            totals['unsubmitted'] += 1
            continue  # No analysis of unanswered cases; no answer-key side channel.
        totals['submitted'] += 1
        record = {k: row[k] for k in ('id', 'title', 'paper_id')}
        record.update(published=bool(row['published']), submitted_at=None, valid=False, warning=None,
                      score=None, automatic_score=None, adjusted=False)
        phase = None
        try:
            date = timestamp(row['submitted_at'])
            record['submitted_at'] = date.isoformat()
            phase = phases.setdefault(date, {'submitted_at': date.isoformat(), 'records': [],
                                            'invalid': 0, 'categories': {}, '_basis': {}})
            if row['audit_org'] != student['org_id']:
                raise ValueError('inaccessible snapshot')
            frozen = frozen_rules(row['findings_json'])
            chosen = selected_answers(row, frozen)
        except (ValueError, TypeError, KeyError):
            totals['invalid'] += 1
            if phase is None:
                totals['undated'] += 1
            else:
                phase['invalid'] += 1
                phase['records'].append(record['id'])
            record['warning'] = '提交、保存时间或冻结案例不可用；不计入画像，不当作答错或未提交。'
            records.append(record)
            continue
        record.update(valid=True, score=row['adjusted_score'] if row['adjusted_score'] is not None else row['score'],
                      automatic_score=row['score'], adjusted=row['adjusted_score'] is not None)
        totals['valid'] += 1
        totals['adjusted'] += record['adjusted']
        audits.add(row['audit_id'])
        scores.append(record['score'])
        automatic_scores.append(record['automatic_score'])
        per_category = {}
        for rid, rule in frozen.items():
            counter = counts()
            if rule['status'] == 'hit':
                counter['detected' if rid in chosen else 'missed'] = 1
            elif rule['status'] == 'pass':
                counter['false_positive' if rid in chosen else 'cleared'] = 1
            else:
                counter['skipped'] = 1
                counter['unsupported'] = int(rid in chosen)
            bucket = rules.setdefault(rule['definition_id'], {
                **{k: v for k, v in rule.items() if k != 'status'}, **counts()})
            add_counts(bucket, counter)
            category = per_category.setdefault(rule['category'], counts())
            add_counts(category, counter)
            phase['_basis'].setdefault(rule['category'], Counter())[(rule['definition_id'], rule['status'])] += 1
        all_counts = counts()
        for category, counter in per_category.items():
            aggregate = categories.setdefault(category, {
                'category': category, 'case_count': 0, 'missed_cases': 0, 'false_positive_cases': 0,
                'unsupported_cases': 0, **counts()})
            aggregate['case_count'] += 1
            for error in ('missed', 'false_positive', 'unsupported'):
                aggregate[error + '_cases'] += counter[error] > 0
            add_counts(aggregate, counter)
            point = phase['categories'].setdefault(category, {'category': category, 'case_count': 0, **counts()})
            point['case_count'] += 1
            add_counts(point, counter)
            add_counts(all_counts, counter)
        record.update(finish_counts(all_counts))
        phase['records'].append(record['id'])
        records.append(record)
    timeline, previous, seen_categories = [], {}, set()
    for date, phase in sorted(phases.items()):
        current = {}
        for category, point in sorted(phase['categories'].items()):
            finish_counts(point)
            basis = json.dumps(sorted((key[0], key[1], count) for key, count in phase['_basis'][category].items()))
            point['basis_id'] = hashlib.sha256(basis.encode()).hexdigest()
            prior = previous.get(category)
            point.update(comparison='first', miss_rate_change=None, false_positive_rate_change=None)
            if totals['undated'] or phase['invalid'] or (timeline and timeline[-1]['invalid']):
                point['comparison'] = 'incomplete'
            elif prior:
                point['comparison'] = 'same_basis' if point['basis_id'] == prior['basis_id'] else 'changed_basis'
                if point['comparison'] == 'same_basis':
                    for rate, numerator, denominator in (
                        ('miss_rate', 'missed', 'hit_opportunities'),
                        ('false_positive_rate', 'false_positive', 'pass_opportunities'),
                    ):
                        point[rate + '_change'] = rate_change(point, prior, numerator, denominator)
            elif category in seen_categories:
                point['comparison'] = 'gap'
            current[category] = point
            seen_categories.add(category)
        phase['categories'] = list(current.values())
        phase.pop('_basis')
        phase['records'].sort()
        timeline.append(phase)
        previous = current
    for value in [*categories.values(), *rules.values()]:
        finish_counts(value)
    totals.update(unique_cases=len(audits), repeated_cases=totals['valid']-len(audits),
                  mean_score=mean(scores), automatic_mean_score=mean(automatic_scores))
    return {
        'student': {k: student[k] for k in ('id', 'username', 'display_name')},
        'class': {k: classroom[k] for k in ('id', 'name', 'revision')} if classroom else None,
        'include_withdrawn': bool(classroom and include_withdrawn), 'totals': totals,
        'records': sorted(records, key=lambda r: (r['submitted_at'] is None, r['submitted_at'] or '', r['id'])),
        'timeline': timeline,
        'categories': sorted(categories.values(), key=lambda c: (-c['errors'], c['category'])),
        'rules': sorted(rules.values(), key=lambda r: (-r['errors'], r['rule_id'], r['definition_id'])),
        'scope': ('仅此班当前名册学生、本教师明确分配到此班且适用于该学生的作业。' if classroom else
                  '仅本人当前有权访问的已发布作业（含已截止），含全机构公开及当前班级作业；撤回、移出班级后不再显示。')
                 + '每份作业仅采用最新保存提交，重交会替换该作业的时间点，不是历次尝试记录。',
        'method': '按最新提交的 UTC 时刻排序，同一时刻合并，不推测先后；未交不算答错。'
                  '漏检率以命中机会为分母，误报率以已执行通过机会为分母；未执行另列。'
                  '只有相邻时点的同类别规则定义、版本、冻结阈值、命中/通过/未执行组成及机会数都相同时才连线并计算百分点变化。'
                  '口径变化、空缺或损坏则断开；没有足够时点不判断趋势。'
                  '题目难度未标定、重复案例可能受记忆影响，连线仅描述作答表现，不证明能力提高；人工调分不改变漏检/误报。',
    }
