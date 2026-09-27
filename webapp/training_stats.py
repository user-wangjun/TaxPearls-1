"""E09 read-only class statistics, based on frozen findings and latest answers.

The caller supplies one SQLite read transaction after checking class ownership.
No current rules, regrading, or synthetic zeros for absent submissions are used.
"""
from decimal import Decimal, ROUND_HALF_UP
from fractions import Fraction
import hashlib
import json
import math


COUNTS = ('detected', 'missed', 'false_positive', 'cleared', 'skipped', 'unsupported')


def percent(numerator, denominator):
    if not denominator:
        return None
    return float((Decimal(numerator) * 100 / Decimal(denominator)).quantize(
        Decimal('.01'), rounding=ROUND_HALF_UP))


def mean(values):
    if not values:
        return None
    return float((sum((Decimal(str(v)) for v in values), Decimal(0)) / len(values)).quantize(
        Decimal('.01'), rounding=ROUND_HALF_UP))


def frozen_rules(raw):
    """Reject unusable snapshots rather than making their class look successful."""
    items = json.loads(raw)
    if not isinstance(items, list) or not items:
        raise ValueError('missing findings')
    rules = {}
    for finding in items:
        rule = finding['rule']
        for key in ('id', 'name', 'category', 'version'):
            if not isinstance(rule[key], str) or not rule[key]:
                raise ValueError('invalid rule')
        if not isinstance(rule['logic'], dict) or not isinstance(finding['threshold_desc'], str):
            raise ValueError('invalid definition')
        if rule['id'] in rules or finding['status'] not in ('hit', 'pass', 'skipped'):
            raise ValueError('invalid finding')
        # Keep versions and frozen threshold descriptions separate, even when IDs
        # match. Local source paths alone do not change the teaching definition.
        definition = {k: v for k, v in rule.items() if k != 'source_file'}
        canonical = json.dumps([definition, finding['threshold_desc']], sort_keys=True,
                               ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        rules[rule['id']] = {
            'rule_id': rule['id'], 'name': rule['name'], 'category': rule['category'],
            'version': rule['version'], 'definition_id': hashlib.sha256(canonical.encode()).hexdigest(),
            'status': finding['status'],
        }
    if not any(r['status'] == 'hit' for r in rules.values()):
        raise ValueError('unscorable snapshot')
    return rules


def selected_answers(row, rules):
    answers = json.loads(row['answers_json'])
    if not isinstance(answers, list) or not all(isinstance(a, str) for a in answers):
        raise ValueError('invalid answers')
    if len(answers) != len(set(answers)) or set(answers) - rules.keys():
        raise ValueError('unknown or duplicate answers')
    for key in ('score', 'adjusted_score'):
        value = row[key]
        if key == 'adjusted_score' and value is None:
            continue
        if not isinstance(value, (float, int)) or not math.isfinite(value) or not 0 <= value <= 100:
            raise ValueError('invalid score')
    return set(answers)


def finish_counts(item):
    item['hit_opportunities'] = item['detected'] + item['missed']
    item['pass_opportunities'] = item['cleared'] + item['false_positive']
    item['executed_decisions'] = item['hit_opportunities'] + item['pass_opportunities']
    item['errors'] = item['missed'] + item['false_positive'] + item['unsupported']
    item['miss_rate'] = percent(item['missed'], item['hit_opportunities'])
    item['false_positive_rate'] = percent(item['false_positive'], item['pass_opportunities'])
    # A skipped rule is not a successful negative decision.
    item['accuracy'] = percent(item['detected'] + item['cleared'], item['executed_decisions'])
    return item


def collect(db, classroom, include_withdrawn=False):
    roster = {r['id'] for r in db.execute(
        """SELECT u.id FROM training_class_members m JOIN users u ON u.id=m.student_id
           WHERE m.class_id=? AND u.org_id=? AND u.role='student' AND u.active=1""",
        (classroom['id'], classroom['org_id']))}
    assignments = db.execute(
        """SELECT a.*,s.paper_id,s.position,s.points,s.deadline_at,
                  f.findings_json,f.org_id AS audit_org
           FROM assignments a JOIN training_assignment_settings s ON s.assignment_id=a.id
           LEFT JOIN audits f ON f.id=a.audit_id
           WHERE s.class_id=? AND a.org_id=? AND a.created_by=? ORDER BY a.created_at,a.id""",
        (classroom['id'], classroom['org_id'], classroom['owner_id'])).fetchall()
    # One batched query; the caller's BEGIN holds roster/cases/submissions in the
    # same WAL snapshot even if a teacher edits membership during calculation.
    submissions = {}
    for row in db.execute(
        """SELECT sub.* FROM submissions sub JOIN assignments a ON a.id=sub.assignment_id
           JOIN training_assignment_settings s ON s.assignment_id=a.id
           WHERE s.class_id=? AND a.org_id=? AND a.created_by=?""",
        (classroom['id'], classroom['org_id'], classroom['owner_id'])):
        submissions.setdefault(row['assignment_id'], []).append(row)
    totals = dict.fromkeys(('expected', 'submitted', 'valid', 'invalid', 'unsubmitted', 'exact_correct',
                           'excluded_submissions', 'excluded_unpublished_empty', 'excluded_withdrawn'), 0)
    questions, rules, categories = [], {}, {}
    for assignment in assignments:
        rows = submissions.get(assignment['id'], [])
        if not assignment['published']:
            if not rows:
                totals['excluded_unpublished_empty'] += 1
                continue
            if not include_withdrawn:
                totals['excluded_withdrawn'] += 1
                continue
        eligible = roster if assignment['target_student_id'] is None else roster & {assignment['target_student_id']}
        current = [s for s in rows if s['student_id'] in eligible]
        totals['excluded_submissions'] += len(rows) - len(current)
        question = {k: assignment[k] for k in ('id', 'title', 'paper_id', 'position', 'points', 'deadline_at')}
        question.update(published=bool(assignment['published']), expected=len(eligible), submitted=len(current),
                        unsubmitted=len(eligible)-len(current), valid=0, invalid=0, exact_correct=0,
                        adjusted_count=0, warnings=[])
        try:
            if assignment['audit_org'] != classroom['org_id']:
                raise ValueError('snapshot not accessible')
            frozen = frozen_rules(assignment['findings_json'])
        except (ValueError, TypeError, KeyError):
            frozen = None
            question['warnings'].append('冻结案例缺失或损坏，本题不计算正确率及平均分，请核查备份。')
        scores, automatic_scores = [], []
        for row in current:
            try:
                if frozen is None:
                    raise ValueError('missing snapshot')
                chosen = selected_answers(row, frozen)
            except (ValueError, TypeError, KeyError):
                question['invalid'] += 1
                continue
            question['valid'] += 1
            question['exact_correct'] += chosen == {rid for rid, rule in frozen.items() if rule['status'] == 'hit'}
            automatic_scores.append(row['score'])
            scores.append(row['adjusted_score'] if row['adjusted_score'] is not None else row['score'])
            question['adjusted_count'] += row['adjusted_score'] is not None
            for rid, rule in frozen.items():
                key = rule['definition_id']
                if key not in rules:
                    rules[key] = {k: v for k, v in rule.items() if k != 'status'}
                    rules[key].update(dict.fromkeys(COUNTS, 0))
                counter = rules[key]
                if rule['status'] == 'hit':
                    counter['detected' if rid in chosen else 'missed'] += 1
                elif rule['status'] == 'pass':
                    counter['false_positive' if rid in chosen else 'cleared'] += 1
                else:
                    counter['skipped'] += 1
                    counter['unsupported'] += rid in chosen
        if question['invalid']:
            question['warnings'].append(f"{question['invalid']} 份提交未纳入正确率及平均分；不可用记录不算未提交或答错。")
        question.update(accuracy=percent(question['exact_correct'], question['valid']),
                        mean_score=mean(scores), automatic_mean_score=mean(automatic_scores), rank=None)
        questions.append(question)
        for key in ('expected', 'submitted', 'valid', 'invalid', 'unsubmitted', 'exact_correct'):
            totals[key] += question[key]
    # Sort on exact ratios, not rounded percentages; equal fractions share rank.
    questions.sort(key=lambda q: (q['valid'] == 0, Fraction(q['exact_correct'], q['valid'] or 1), q['id']))
    previous, rank = None, None
    for index, question in enumerate(questions, 1):
        if not question['valid']:
            continue
        fraction = Fraction(question['exact_correct'], question['valid'])
        if fraction != previous:
            rank, previous = index, fraction
        question['rank'] = rank
    for rule in rules.values():
        category = categories.setdefault(rule['category'], {'category': rule['category'], **dict.fromkeys(COUNTS, 0)})
        for key in COUNTS:
            category[key] += rule[key]
        finish_counts(rule)
    total_errors = sum(r['errors'] for r in rules.values())
    for category in categories.values():
        finish_counts(category)
        category['error_share'] = percent(category['errors'], total_errors)
    return {
        'class': {k: classroom[k] for k in ('id', 'name', 'revision')}, 'active_students': len(roster),
        'include_withdrawn': include_withdrawn, 'totals': totals, 'questions': questions,
        'rules': sorted(rules.values(), key=lambda r: (-r['errors'], r['rule_id'], r['definition_id'])),
        'categories': sorted(categories.values(), key=lambda c: (-c['errors'], c['category'])),
        'scope': '当前有效名册与作业指定学生的交集；仅本班明确分配的作业，不含全机构公开作业。'
                 '默认统计已发布作业（含已截止）；可另含有提交的撤回作业，无提交的草稿/撤回作业不纳入。'
                 '每人每题仅取最新保存提交，未提交单列；不可用记录不进入正确率分母。',
        'method': '题目按完整案例计，全对=选中集合与冻结命中集合完全相等；按全对率从低到高排名，同率同名次。'
                  '漏检率分母为命中机会，误报率分母为已执行通过机会；未执行项单列，不算正确排除。'
                  '薄弱项按漏检、误报与材料不足仍标风险次数合计分布，按规则版本及冻结口径区分。'
                  '人工调分只影响平均分，不改变答案正确率；题目分值不加权正确率。',
    }
