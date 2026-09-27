"""Persistent enterprise materials: analyze first, explicitly confirm/exe once.

Teaching keeps its independent legacy path. These endpoints never accept a
client-provided Dataset, rules, parser output or confirmation snapshot.
"""
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import secrets
from urllib.parse import quote
from threading import BoundedSemaphore

from fastapi import Cookie, HTTPException, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException

from src import engine, materials, related_graph, material_review
from src.ai_extraction import AIExtractor
from src.input_errors import InputError
from src.models import Rule
from src.settings import AISettings
from src.snapshots import deserialize_dataset
from webapp import enterprise_analysis, material_batches as batches, material_operations
from webapp.access import AccessDenied
from webapp.uploads import bounded_stream, multipart


def _latest(view):
    records = [v for v in view['versions'] if v['kind'] in {'analysis', 'edit'}]
    if not records:
        raise AccessDenied('材料尚未完成分析。', 409)
    return records[-1]


def _analysis_payload(documents, company, selections, base_rules, rules_for_dataset):
    selections = deepcopy(selections)
    for document in documents:
        selection = selections.get(document['id'], {})
        if not isinstance(selection, dict):
            raise InputError('材料选择格式无效。')
        if 'standard_edits' in selection:
            selection['standard_edits'] = material_review.normalize(document, selection['standard_edits'])
    analysis = enterprise_analysis.analyze(documents, company, selections, base_rules)
    selected = base_rules
    if analysis['dataset'] is not None:
        try:
            selected = rules_for_dataset(deserialize_dataset(analysis['dataset']))
        except HTTPException as exc:
            if exc.status_code != 422:
                raise
            analysis['feedback']['blocking'].append({'code': 'rule_period', 'message': str(exc.detail),
                'file_id': None, 'impact': '当前规则版本不能覆盖整个主期间。', 'required': '核对或拆分检测期间。'})
            analysis['can_confirm'] = False
        else:
            # Use all supported metrics while limiting readiness to selected
            # effective rule versions; disabled rules are not "passed".
            analysis['checks'] = [check for check in analysis['checks'] if check.get('engine') == 'graph']
            analysis['feedback']['limited'] = [n for n in analysis['feedback']['limited'] if n['code'] != 'check_unavailable']
            data = deserialize_dataset(analysis['dataset'])
            for rule in selected:
                problems = engine.readiness(rule, data)
                analysis['checks'].append({'rule_id': rule.id, 'name': rule.name, 'version': rule.version,
                                          'ready': not problems, 'reasons': problems})
                if problems:
                    analysis['feedback']['limited'].append({'code': 'check_unavailable', 'message': '；'.join(problems),
                        'file_id': None, 'impact': rule.id + ' ' + rule.name, 'required': '；'.join(rule.inputs.values())})
    return {'documents': documents, 'company': company, 'selections': selections, 'analysis': analysis,
            'rules': [asdict(r) for r in selected], 'graph_rule': asdict(related_graph.definition()),
            'parser_version': 'enterprise-materials-v2',
            'fields': {key: detail for rule in base_rules for key, detail in rule.inputs.items()}}


def _field_changes(previous, current):
    """Per-operation deltas, not just cumulative differences from the original.

    Actor and timestamp are supplied by the immutable revision transaction.
    """
    output = []
    for key, value in current['company'].items():
        if value != previous['company'].get(key):
            output.append({'field': 'company.' + key, 'old': previous['company'].get(key),
                           'new': value, 'source': '本组企业与期间核对', 'origin': 'user'})
    for document in current['documents']:
        file_id = document['id']
        before = previous['selections'].get(file_id, {})
        after = current['selections'].get(file_id, {})
        for field, default in [('purpose', 'current'), ('rows', document['rows'])]:
            old, new = before.get(field, default), after.get(field, default)
            if old != new:
                output.append({'file_id': file_id, 'field': field, 'old': old, 'new': new,
                               'source': document['name'], 'origin': 'user'})
        for field in material_review.fields(document):
            address = field['id']
            old = before.get('standard_edits', {}).get(address, field['value'])
            new = after.get('standard_edits', {}).get(address, field['value'])
            if old != new:
                output.append({'file_id': file_id, 'field': address, 'old': old, 'new': new,
                               'original': field['value'], 'source': document['name'] + ' / ' + address,
                               'origin': 'user'})
    return output


def register(app, get_store, get_user, allow, rules_dir, rules_for_dataset, save_audit, audit_result):
    material_operations.register(app, get_store, get_user, allow)
    slots = BoundedSemaphore(2)
    def actor(session):
        user = get_user(session)
        allow(user, 'org_admin', 'accountant')
        return user

    def project(view):
        latest = _latest(view)
        # Keep the raw grids server-side; only authorized, addressable numeric
        # cells needed for review are projected. Originals remain immutable.
        documents = [{**{k: v for k, v in doc.items() if k not in {
                         'accounts', 'declarations', 'standard_tables', 'account_cell_sources', 'declaration_cell_sources'}},
                      'standard_fields': material_review.fields(doc) if 'standard_tables' in doc else None}
                     for doc in latest['payload']['documents']]
        dataset = latest['payload']['analysis'].get('dataset') or {}
        return {'id': view['id'], 'revision': view['revision'], 'analysis_revision': latest['revision'],
                'files': view['files'], 'documents': documents,
                'company': latest['payload']['company'], 'selections': latest['payload']['selections'],
                'analysis': {k: v for k, v in latest['payload']['analysis'].items() if k != 'dataset'},
                'metrics': list(dataset.get('metrics', {}).values()),
                'fields': latest['payload'].get('fields', {}), 'executions': view['executions']}

    def parse_uploads(uploads, mode):
        # Fail before parsing or model calls when protected retention is not
        # configured. No real key is generated or disclosed by the service.
        batches._key()
        batches.validate_uploads(uploads)
        names = [name for name, _ in uploads]
        if len(set(names)) != len(names):
            raise InputError('同批文件名重复，请区分文件名后重新上传，避免来源混淆。')
        if any('/' in name or '\\' in name for name in names):
            raise InputError('上传文件名不能包含目录路径。')
        base_rules = engine.load_rules(rules_dir)
        keys = {key for rule in base_rules for key in rule.inputs}
        settings = AISettings.from_env()
        if mode == 'ai' and settings.problem():
            raise InputError(settings.problem())
        extractor = AIExtractor(settings, {key: detail for r in base_rules for key, detail in r.inputs.items()}) \
            if mode != 'local' and not settings.problem() else None
        documents = materials.preview(uploads, keys, extractor, allow_incomplete_company=True, capture_standard=True)
        # Server-generated stable IDs survive reanalysis/restart, not indices
        # whose meaning could change after supplementing another upload.
        for doc in documents:
            doc['id'] = secrets.token_hex(12)
            source = next((name for name in names if doc['name'] == name), None)
            if source is None:
                source = next((name for name in names if name.lower().endswith('.zip') and doc['name'].startswith(name + '/')), None)
            if source is None:
                raise InputError('无法关联材料与上传原件。')
            doc['original_upload_name'] = source
        return documents, base_rules

    def upload_work(user, uploads, mode, client_id):
        batches.authorize_client(get_store(), user, client_id)
        documents, base_rules = parse_uploads(uploads, mode)
        company = enterprise_analysis.prefill(documents)
        payload = _analysis_payload(documents, company, {}, base_rules, rules_for_dataset)
        item = batches.create(get_store(), user, uploads, client_id, initial_payload=payload)
        return project(batches.read(get_store(), user, item['id']))

    async def body(request, allowed):
        try:
            raw = b''.join([part async for part in bounded_stream(request, 2 * 1024 * 1024)])
            def invalid_constant(_value):
                raise ValueError()
            value = json.loads(raw, parse_constant=invalid_constant)
            if not isinstance(value, dict) or set(value) - allowed:
                raise ValueError()
            if type(value.get('expected_revision')) is not int or value['expected_revision'] < 0:
                raise ValueError()
            return value
        except (ValueError, MultiPartException):
            raise HTTPException(422, '材料请求格式无效、版本号无效或超过 2MB。') from None

    @app.get('/api/enterprise/materials/config')
    def configuration(session: str | None = Cookie(default=None, alias='taxpearls_session')):
        actor(session)
        try:
            batches._key()
            return {'retention_ready': True, 'message': '原件受控留存已配置；一次检测一家企业、一个主期间。'}
        except batches.MaterialStorageError:
            return {'retention_ready': False, 'message': '请由部署人员配置独立材料加密密钥，再使用企业上传。不会降级明文留存。'}

    @app.post('/api/enterprise/materials')
    async def upload(request: Request, session: str | None = Cookie(default=None, alias='taxpearls_session')):
        user = actor(session)
        try:
            async with multipart(request, total_limit=materials.MAX_TOTAL + 1024 * 1024, max_files=20, max_fields=2) as form:
                uploads = [(v.filename or '未命名', await v.read()) for _, v in form.multi_items() if isinstance(v, UploadFile)]
                mode, client_id = form.get('extraction', 'local'), form.get('client_id') or None
            if mode not in {'local', 'auto', 'ai'} or client_id is not None and not isinstance(client_id, str):
                raise InputError('提取方式或客户档案格式无效。')
            if not slots.acquire(blocking=False):
                raise HTTPException(429, '材料分析任务较多，请稍后重试。')
            try:
                return await run_in_threadpool(upload_work, user, uploads, mode, client_id)
            finally:
                slots.release()
        except (InputError, batches.MaterialStorageError, MultiPartException) as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get('/api/enterprise/materials')
    def list_batches(session: str | None = Cookie(default=None, alias='taxpearls_session')):
        return batches.listing(get_store(), actor(session))

    @app.get('/api/enterprise/materials/{batch_id}')
    def details(batch_id: str, session: str | None = Cookie(default=None, alias='taxpearls_session')):
        user = actor(session)
        try:
            return project(batches.read(get_store(), user, batch_id))
        except batches.MaterialStorageError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get('/api/enterprise/materials/{batch_id}/trace')
    def trace(batch_id: str, session: str | None = Cookie(default=None, alias='taxpearls_session')):
        user = actor(session)
        try:
            view = batches.read(get_store(), user, batch_id)
        except batches.MaterialStorageError as exc:
            raise HTTPException(409, str(exc)) from None
        # Scope and review details are visible only after current batch/client
        # authorization. Do not expose raw accounting/personnel datasets here.
        versions = []
        for record in view['versions']:
            payload = record['payload']
            detail = {k: payload[k] for k in ('company', 'selections', 'parser_version',
                      'supplement_differences', 'deleted_file_id', 'requires_confirmation',
                      'analysis_revision', 'audit_id', 'analysis_sha256', 'scope',
                      'files', 'edits', 'checks', 'feedback') if k in payload}
            if 'changes' in payload:
                detail['changes'] = payload['changes']
            if 'analysis' in payload:
                detail['analysis'] = {k: v for k, v in payload['analysis'].items() if k != 'dataset'}
            if 'documents' in payload:
                # Keep failed/incomplete-scope extraction visible as well: those
                # documents need not have reached analysis.files yet.
                detail['extractions'] = [{k: doc.get(k) for k in
                    ('id', 'name', 'sha256', 'original_id', 'extraction', 'error')}
                    for doc in payload['documents']]
            versions.append({**{k: v for k, v in record.items() if k != 'payload'}, 'detail': detail})
        return {'id': batch_id, 'revision': view['revision'], 'versions': versions, 'events': view['events']}

    @app.get('/api/audits/{audit_id}/materials')
    def confirmed_materials(audit_id: str, session: str | None = Cookie(default=None, alias='taxpearls_session')):
        try:
            return {'confirmation': batches.confirmed(get_store(), actor(session), audit_id)}
        except batches.MaterialStorageError as exc:
            raise HTTPException(409, str(exc)) from None

    def upgrade_standard_sources(user, view, previous):
        """Older retained batches gain editable source coordinates on reanalysis.

        Read exact retained bytes with a dedicated event; never redownload from
        outside, invoke AI, replace originals or rewrite historical revisions.
        """
        from src.workbooks import open_workbook
        previous = deepcopy(previous)
        expanded = {}
        for doc in previous['documents']:
            if (doc['kind'] != 'xlsx' or doc.get('error') or doc.get('review_required')
                    or 'standard_tables' in doc):
                continue
            fid = doc.get('original_id')
            original_file = next((f for f in view['files'] if f['id'] == fid), None)
            if not original_file or original_file['deleted_at']:
                continue
            if fid not in expanded:
                meta, raw = batches.original(get_store(), user, view['id'], fid, purpose='read_for_reanalysis')
                expanded[fid] = dict(materials.expand_uploads([(meta['name'], raw)]))
            raw = expanded[fid].get(doc['name'])
            if raw is None or hashlib.sha256(raw).hexdigest()[:16] != doc['fingerprint']:
                raise InputError('原件与历史解析文件指纹不匹配，请核查材料。')
            book = open_workbook(raw)
            try:
                doc['standard_tables'] = material_review.capture(book)
            finally:
                book.close()
            material_review.annotate(doc, doc['standard_tables'], {})
        return previous

    def analyze_work(user, batch_id, value):
        view = batches.read(get_store(), user, batch_id)
        if view['revision'] != value['expected_revision']:
            raise AccessDenied('材料已变化，请重新读取。', 409)
        previous = upgrade_standard_sources(user, view, _latest(view)['payload'])
        company = {**previous['company'], **value.get('company', {})}
        if set(company) - {'name', 'taxpayer_id', 'industry', 'period_start', 'period_end'}:
            raise InputError('企业信息含未知字段。')
        if any(not isinstance(v, str) for v in company.values()):
            raise InputError('企业信息须为文本。')
        selections = deepcopy(previous['selections'])
        for file_id, change in value.get('selections', {}).items():
            if not isinstance(change, dict):
                raise InputError('材料选择格式无效。')
            selections[file_id] = {**selections.get(file_id, {}), **change}
        for doc in previous['documents']:
            original_file = next((f for f in view['files'] if f['id'] == doc.get('original_id')), None)
            if original_file is None or original_file['deleted_at']:
                # Removal must be explicit; never silently execute a stale
                # parsed candidate whose original has since been deleted.
                if selections.get(doc['id'], {}).get('purpose') != 'excluded':
                    raise AccessDenied('原件已删除，请明确排除关联材料后重新分析。', 409)
        payload = _analysis_payload(previous['documents'], company, selections,
                                    engine.load_rules(rules_dir), rules_for_dataset)
        payload['changes'] = _field_changes(previous, payload)
        revision = batches.append_revision(get_store(), user, batch_id, view['revision'], 'edit', payload)
        result = project(batches.read(get_store(), user, batch_id))
        if result['revision'] != revision:
            raise AccessDenied('材料随后发生变化，请重新读取。', 409)
        return result

    def supplement_work(user, batch_id, revision, uploads, mode):
        view = batches.read(get_store(), user, batch_id)
        if view['revision'] != revision:
            raise AccessDenied('材料已变化，请重新读取后补传。', 409)
        previous = upgrade_standard_sources(user, view, _latest(view)['payload'])
        documents, base_rules = parse_uploads(uploads, mode)
        combined = deepcopy(previous['documents']) + documents
        if len(combined) > 20:
            raise InputError('单批次累计最多 20 份解析材料；请另开检测，不覆盖旧材料。')
        selections = deepcopy(previous['selections'])
        for doc in combined:
            deleted = next((f for f in view['files'] if f['id'] == doc.get('original_id') and f['deleted_at']), None)
            if deleted and selections.get(doc['id'], {}).get('purpose') != 'excluded':
                raise AccessDenied('请先明确排除已删除原件关联的材料。', 409)
        candidate = enterprise_analysis.prefill(documents)
        company = deepcopy(previous['company'])
        differences = []
        # Previously entered values always survive. New candidate differences
        # are shown, not used to silently overwrite the last analysis.
        for field, value in candidate.items():
            if value and company.get(field) and value != company[field]:
                differences.append({'field': field, 'retained': company[field], 'new_candidate': value})
            elif value and not company.get(field):
                company[field] = value
        payload = _analysis_payload(combined, company, selections, base_rules, rules_for_dataset)
        payload['supplement_differences'] = differences
        payload['analysis']['supplement_differences'] = differences
        batches.supplement(get_store(), user, batch_id, revision, uploads, payload)
        return project(batches.read(get_store(), user, batch_id))

    @app.post('/api/enterprise/materials/{batch_id}/supplement')
    async def supplement_materials(batch_id: str, request: Request,
                                   session: str | None = Cookie(default=None, alias='taxpearls_session')):
        user = actor(session)
        # Deny inaccessible batches before reading uploaded bytes.
        await run_in_threadpool(batches.read, get_store(), user, batch_id)
        try:
            async with multipart(request, total_limit=materials.MAX_TOTAL + 1024 * 1024, max_files=20, max_fields=2) as form:
                uploads = [(v.filename or '未命名', await v.read()) for _, v in form.multi_items() if isinstance(v, UploadFile)]
                mode = form.get('extraction', 'local')
                raw_revision = form.get('expected_revision', '')
            if not isinstance(raw_revision, str) or not raw_revision.isdecimal() or mode not in {'local', 'auto', 'ai'}:
                raise InputError('补传版本号或提取方式无效。')
            if not slots.acquire(blocking=False):
                raise HTTPException(429, '材料分析任务较多，请稍后重试。')
            try:
                return await run_in_threadpool(supplement_work, user, batch_id, int(raw_revision), uploads, mode)
            finally:
                slots.release()
        except (InputError, batches.MaterialStorageError, MultiPartException) as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post('/api/enterprise/materials/{batch_id}/analyze')
    async def analyze_materials(batch_id: str, request: Request,
                                session: str | None = Cookie(default=None, alias='taxpearls_session')):
        user = actor(session)
        value = await body(request, {'expected_revision', 'company', 'selections'})
        if not isinstance(value.get('company', {}), dict) or not isinstance(value.get('selections', {}), dict):
            raise HTTPException(422, '企业信息和材料选择须为对象。')
        if not slots.acquire(blocking=False):
            raise HTTPException(429, '材料分析正在处理其他请求，请稍后重试。')
        try:
            return await run_in_threadpool(analyze_work, user, batch_id, value)
        except (InputError, batches.MaterialStorageError) as exc:
            raise HTTPException(422, str(exc)) from None
        finally:
            slots.release()

    def execute_work(user, batch_id, revision):
        store = get_store()
        existing = batches.execution(store, user, batch_id, revision)
        if existing:
            return audit_result(existing, user)
        view = batches.read(store, user, batch_id)
        latest = _latest(view)
        if view['revision'] != revision or latest['revision'] != revision:
            raise AccessDenied('材料已变化，请重新核对并确认当前分析。', 409)
        payload = latest['payload']
        if payload['analysis']['can_confirm'] is not True:
            raise AccessDenied('请先处理阻止检测的材料问题。', 409)
        if payload.get('graph_rule') != asdict(related_graph.definition()):
            raise AccessDenied('关联方检查范围或版本已变化，请重新分析并确认材料。', 409)
        context = {'batch_id': batch_id, 'revision': revision, 'sha256': hashlib.sha256(batches._json(payload)).hexdigest()}
        try:
            return save_audit(deserialize_dataset(payload['analysis']['dataset']), user, view['client_id'],
                              frozen_rules=[Rule(**r) for r in payload['rules']],
                              frozen_graph_rule=Rule(**payload['graph_rule']), material_context=context)
        except AccessDenied as exc:
            if exc.status != 409:
                raise
            # Another confirmed request may have won the write transaction.
            existing = batches.execution(store, user, batch_id, revision)
            if existing:
                return audit_result(existing, user)
            raise

    @app.post('/api/enterprise/materials/{batch_id}/confirm')
    async def confirm(batch_id: str, request: Request, session: str | None = Cookie(default=None, alias='taxpearls_session')):
        user = actor(session)
        value = await body(request, {'expected_revision'})
        try:
            return await run_in_threadpool(execute_work, user, batch_id, value['expected_revision'])
        except batches.MaterialStorageError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get('/api/enterprise/materials/{batch_id}/originals/{file_id}')
    def download(batch_id: str, file_id: str, session: str | None = Cookie(default=None, alias='taxpearls_session')):
        user = actor(session)
        try:
            meta, raw = batches.original(get_store(), user, batch_id, file_id)
        except batches.MaterialStorageError as exc:
            raise HTTPException(409, str(exc)) from None
        return Response(raw, media_type='application/octet-stream', headers={
            'Content-Disposition': "attachment; filename*=UTF-8''" + quote(meta['name'], safe=''),
            'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff'})

    @app.get('/api/enterprise/materials/{batch_id}/originals/{file_id}/deletion-impact')
    def impact(batch_id: str, file_id: str, session: str | None = Cookie(default=None, alias='taxpearls_session')):
        user = actor(session)
        return batches.deletion_impact(get_store(), user, batch_id, file_id)

    @app.delete('/api/enterprise/materials/{batch_id}/originals/{file_id}')
    async def remove(batch_id: str, file_id: str, request: Request,
                     session: str | None = Cookie(default=None, alias='taxpearls_session')):
        user = actor(session)
        value = await body(request, {'expected_revision'})
        try:
            deleted = await run_in_threadpool(batches.delete_original, get_store(), user, batch_id, file_id,
                                              value['expected_revision'])
        except batches.MaterialStorageError as exc:
            raise HTTPException(409, str(exc)) from None
        return {'deleted': deleted, 'backup_copies_may_remain': True}
