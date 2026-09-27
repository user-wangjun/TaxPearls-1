"""Single-enterprise input analysis. Never executes a risk rule or graph rule.

Documents passed here must be server-side parser output, not browser JSON.
The returned input snapshot will be persisted by the enterprise service before
confirmation; this module alone does not authorize or execute an audit.
"""
from copy import deepcopy
import re

from src import config, engine, materials, periods, related_graph, material_review
from src.input_errors import InputError
from src.snapshots import serialize_dataset


def _text(value):
    return str(value).strip() if value is not None else ''


def _identity(value):
    return re.sub(r'\s+', '', _text(value)).upper()


def prefill(documents):
    """Only unambiguous material facts are prefilled, never filenames/dates."""
    result = {}
    for key in ('name', 'taxpayer_id', 'industry'):
        values = {_text(d.get('company', {}).get(key)) for d in documents if not d.get('error')}
        values.discard('')
        result[key] = next(iter(values)) if len(values) == 1 else ''
    intervals = set()
    for doc in documents:
        value = doc.get('company', {}).get('period')
        if doc.get('error') or not value:
            continue
        try:
            period = periods.parse_period(value, '材料所属期')
            intervals.add((period.start.isoformat(), period.end.isoformat()))
        except InputError:
            continue
    result['period_start'], result['period_end'] = next(iter(intervals)) if len(intervals) == 1 else ('', '')
    return result


def analyze(documents, company, selections, rules):
    """Build candidate input plus three feedback classes; no hit/pass status.

    Selection purpose is current/history/excluded. Historical document metrics
    enter supported period-series only, never current accounts/declarations.
    Explicit facts conflicting with user-entered scope are not erased by edits.
    """
    if not isinstance(company, dict) or not isinstance(selections, dict):
        raise InputError('企业信息或材料选择格式无效。')
    if not isinstance(documents, list) or not 1 <= len(documents) <= 20:
        raise InputError('请选择 1–20 份材料。')
    ids = [d.get('id') for d in documents]
    if any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids) or set(selections) - set(ids):
        raise InputError('材料编号重复或无效。')
    result = {'company': {}, 'files': [], 'edits': [], 'feedback': {'blocking': [], 'limited': [], 'suggested': []},
              'checks': [], 'dataset': None, 'can_confirm': False}

    def note(level, code, message, file_id=None, impact='', required=''):
        result['feedback'][level].append({'code': code, 'message': message, 'file_id': file_id,
                                          'impact': impact, 'required': required})

    target = {key: _text(company.get(key)) for key in ('name', 'taxpayer_id', 'industry', 'period_start', 'period_end')}
    if any(len(value) > 200 for value in target.values()):
        raise InputError('企业或期间信息过长。')
    target['taxpayer_id'] = _identity(target['taxpayer_id'])
    for key, title in [('name', '企业名称'), ('taxpayer_id', '税号'), ('period_start', '主期间起始日'), ('period_end', '主期间终止日')]:
        if not target[key]:
            note('blocking', 'required_scope', f'请填写{title}。', required=title)
    main = None
    if target['period_start'] and target['period_end']:
        try:
            main = periods.parse_period(target['period_start'] + '至' + target['period_end'], '主检测期间')
        except InputError as exc:
            note('blocking', 'invalid_period', str(exc), required='明确当前支持的完整月、季度、半年或年度。')
    result['company'] = target
    if result['feedback']['blocking']:
        return result
    company_values = {'name': target['name'], 'taxpayer_id': target['taxpayer_id'],
                      'industry': target['industry'] or '未提供', 'period': main.label}
    prepared, prepared_selections = [], {}
    selected_count = 0
    keys = {key for rule in rules for key in rule.inputs}
    # Parsing may contain other supported metrics needed by period derivation.
    keys |= set(config.PERIOD_SERIES)
    for original in documents:
        file_id = original['id']
        selection = selections.get(file_id, {})
        if not isinstance(selection, dict):
            raise InputError('材料选择格式无效。')
        if set(selection) - {'purpose', 'rows', 'company', 'standard_edits'}:
            raise InputError('材料选择含未知字段。')
        purpose = selection.get('purpose', 'current')
        if purpose not in {'current', 'history', 'excluded'}:
            raise InputError('材料用途必须为本期、历史参考或排除。')
        result['files'].append({'id': file_id, 'name': original['name'], 'fingerprint': original['fingerprint'],
                                'sha256': original.get('sha256'),
                                'purpose': purpose, 'material_company': deepcopy(original['company']),
                                'extraction': deepcopy(original.get('extraction', {}))})
        if purpose == 'excluded':
            continue
        selected_count += 1
        if original.get('error'):
            note('blocking', 'read_failed', original['error'], file_id, '本文件未读取，不能当作没有字段。', '替换或明确移除此文件。')
            continue
        original_scope = original['company']
        if original_scope.get('taxpayer_id') and _identity(original_scope['taxpayer_id']) != target['taxpayer_id']:
            note('blocking', 'identity_conflict', '材料税号与被检测企业不同。', file_id,
                 '不能通过修改字段消除原材料冲突。', '核对材料归属，移除或另开检测。')
            continue
        if original_scope.get('name') and original_scope['name'] != target['name']:
            if not _identity(original_scope.get('taxpayer_id')):
                note('blocking', 'identity_unresolved', '材料企业名称不同，且原文无税号可核对归属。', file_id,
                     '用户补填税号不能证明该材料属于被检测企业。',
                     '核对企业名称；如材料确属其他企业，排除或另开检测；如属简称或更名，补充明确归属的材料。')
                continue
            note('suggested', 'name_difference', '材料企业名称与用户指定名称不同。', file_id,
                 '用户指定名称不等于材料已经证实名称。', '核对更名、简称或材料归属。')
        doc_period = None
        if original_scope.get('period'):
            try:
                doc_period = periods.parse_period(original_scope['period'], '材料所属期')
            except InputError as exc:
                note('blocking', 'material_period_unknown', str(exc), file_id, required='核对材料实际业务期间或移除文件。')
                continue
        if purpose == 'history' and (not doc_period or doc_period.end >= main.start):
            note('blocking', 'history_period', '历史参考须明确实际期间，且早于主期间。', file_id,
                 '不能把本期或未来材料作为历史基期。', '核对用途和实际期间。')
            continue
        if purpose == 'current' and doc_period and (doc_period.start, doc_period.end) != (main.start, main.end):
            note('blocking', 'period_conflict', '材料实际期间与主期间不同。', file_id,
                 '不能混入本期金额。', '历史材料明确选择历史用途，其他期间另开检测。')
            continue
        if not original_scope.get('taxpayer_id') or not doc_period:
            note('suggested', 'scope_user_supplied', '部分归属或业务期间由用户补填。', file_id,
                 '不是材料原文已证明的归属。', '核对原件中的企业与实际业务期间。')
        # Validate the public edit envelope separately from parser feedback;
        # bad cell IDs/types are rejected, meaningful but invalid changes are
        # retained as blocking candidates so users can correct them in place.
        corrections = material_review.normalize(original, selection.get('standard_edits', {}))
        try:
            doc, cell_changes = material_review.apply(original, corrections)
        except InputError as exc:
            note('blocking', 'standard_edit_invalid', str(exc), file_id,
                 '修正值尚不能参与检测，原件未修改。', '按原文核对修正值和来源口径，保存后重新分析。')
            continue
        result['edits'].extend(cell_changes)
        normalized = {**company_values, 'period': doc_period.label if purpose == 'history' else main.label}
        for key in materials.COMPANY_KEYS:
            if _text(original_scope.get(key)) != normalized[key]:
                result['edits'].append({'file_id': file_id, 'field': 'company.' + key,
                                        'old': original_scope.get(key, ''), 'new': normalized[key],
                                        'origin': 'user' if key != 'period' or not doc_period else 'normalized',
                                        'source': original['name'] + ' / 企业信息'})
        # Preserve original facts separately; normalization here is only for
        # the legacy parser's same-scope assembly contract.
        doc['company'] = normalized
        edited = {'company': normalized, 'reviewed': True}
        if 'rows' in selection:
            if not isinstance(selection['rows'], list):
                raise InputError('指标编辑须为列表。')
            if not (doc['kind'] == 'pdf' or doc.get('review_required')):
                raise InputError('标准账表请使用对应单元格的数值修正入口，不能伪装成原文指标。')
            edited['rows'] = deepcopy(selection['rows'])
            if selection['rows'] != original['rows']:
                result['edits'].append({'file_id': file_id, 'field': 'rows', 'old': deepcopy(original['rows']),
                                        'new': deepcopy(selection['rows']), 'origin': 'user', 'source': original['name']})
        if selection.get('company'):
            # Scope comes from the single batch form; per-file overrides are
            # not a second route for hiding explicit source contradictions.
            raise InputError('请在单组企业信息中修改，不接受逐文件改写归属。')
        if purpose == 'history':
            try:
                historical = materials.build_dataset([doc], {file_id: edited}, normalized, keys)
                series = []
                for name, metric in historical.metrics.items():
                    if name in config.PERIOD_SERIES:
                        series.append({'name': name, 'value': str(metric.value), 'period': doc_period.label,
                                       'source': metric.source, 'detail': metric.detail})
                # Also preserve older explicit period-series records, with
                # their actual periods; reject current/future contamination.
                for row in doc.get('period_series', []):
                    interval = periods.parse_period(row['period'], '历史参考序列')
                    if interval.end >= main.start:
                        raise InputError('历史参考序列包含本期或未来值，请核对后移除。')
                    series.append(deepcopy(row))
                if not series:
                    note('suggested', 'history_not_mapped', '历史原件保留，但没有当前支持的期间序列指标。', file_id,
                         '不混入本期数据，不宣称参与趋势计算。', '核对材料中的可用历史指标。')
                    continue
                doc.update(company=company_values, accounts=[], declarations={}, rows=[], invoices=[],
                           bank_transactions=[], bank_adjustments=[], human_records=[], contracts=[],
                           fulfillments=[], contract_links=[], related_graph=None, period_series=series,
                           review_required=False, kind='history')
                edited = {'company': company_values}
            except InputError as exc:
                note('blocking', 'history_invalid', str(exc), file_id, required='修正历史参考或移除文件。')
                continue
        for warning in original.get('warnings', []):
            note('suggested', 'parser_warning', str(warning), file_id, required='按原文核对提取口径。')
        prepared.append(doc)
        prepared_selections[file_id] = edited
    if not selected_count or not any(f['purpose'] == 'current' for f in result['files']):
        note('blocking', 'no_current_material', '请提供至少一份本期材料。', required='上传或选择本期材料。')
    if result['feedback']['blocking']:
        return result
    try:
        dataset = materials.build_dataset(prepared, prepared_selections, company_values, keys)
    except InputError as exc:
        note('blocking', 'input_conflict', str(exc), required='核对冲突、单位及原始材料，不自动覆盖或相加。')
        return result
    for rule in rules:
        problems = engine.readiness(rule, dataset)
        result['checks'].append({'rule_id': rule.id, 'name': rule.name, 'version': rule.version,
                                 'ready': not problems, 'reasons': problems})
        if problems:
            note('limited', 'check_unavailable', '；'.join(problems), impact=rule.id + ' ' + rule.name,
                 required='；'.join(rule.inputs.values()))
    graph_rule, graph_problems = related_graph.definition(), related_graph.readiness(dataset)
    result['checks'].append({'rule_id': graph_rule.id, 'name': graph_rule.name, 'version': graph_rule.version,
                             'engine': 'graph', 'ready': not graph_problems, 'reasons': graph_problems})
    if graph_problems:
        note('limited', 'graph_unavailable', '；'.join(graph_problems), impact=graph_rule.id + ' ' + graph_rule.name,
             required='如需此检查，补充有来源的主体、关系和交易材料。')
    result['dataset'] = serialize_dataset(dataset)
    result['can_confirm'] = True
    return result
