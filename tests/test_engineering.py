"""Regression tests for the repository engineering review."""
from io import BytesIO
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile, ZIP_DEFLATED
from contextlib import contextmanager
from copy import deepcopy

from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

from src import loader, materials, render
from src.models import Company, Dataset
from webapp import app as module
from webapp.storage import Store, serialize_dataset, deserialize_dataset

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / 'samples/样例企业-审计材料.xlsx'


class EngineeringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'test.db')
        replacement = patch.object(module, 'store', self.store)
        replacement.start()
        self.addCleanup(replacement.stop)
        self.owner = self.store.create_user('owner', 'Review-Probe-2026!', '机构', 'org_admin', 'org')
        self.admin = self.store.create_user('root', 'Review-Probe-2026!', '平台', 'platform_admin', 'platform')
        self.client = TestClient(module.app, raise_server_exceptions=False)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def login(self, name='owner'):
        _, token = self.store.authenticate(name, 'Review-Probe-2026!')
        self.client.cookies.set(module.COOKIE_NAME, token)

    def test_import_does_not_create_database(self):
        target = Path(self.tmp.name) / 'must-not-exist.db'
        result = subprocess.run([sys.executable, '-B', '-c',
            'from webapp import app; assert app.store is None'], cwd=ROOT,
            env={**os.environ, 'TAXPEARLS_DB':str(target)}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(target.exists())

    def test_unauthenticated_uploads_are_rejected_before_parsing(self):
        with patch.object(UploadFile, 'write', side_effect=AssertionError('body parsed')) as write:
            for path in ('/api/audit', '/api/materials/preview', '/api/org/logo'):
                response = self.client.post(path, files={'file':('sample.xlsx', b'0' * (2*1024*1024))})
                self.assertEqual(response.status_code, 401)
        write.assert_not_called()

    def test_both_upload_paths_reject_expansion_and_large_bodies_without_spooling(self):
        self.login()
        buffer = BytesIO(SAMPLE.read_bytes())
        with ZipFile(buffer, 'a', ZIP_DEFLATED) as archive:
            with archive.open('unused.txt', 'w') as stream:
                for _ in range(51):
                    stream.write(b'0' * (1024*1024))
        data = buffer.getvalue()
        response = self.client.post('/api/audit', files={'file':('padded.xlsx', data)})
        self.assertEqual(response.status_code, 422)
        preview = self.client.post('/api/materials/preview', files={'files':('padded.xlsx', data)})
        self.assertIn('解压', preview.json()['documents'][0]['error'])
        original = UploadFile.write
        rolled = []
        async def observe(file, content):
            await original(file, content)
            rolled.append(file.file._rolled)
        with patch.object(UploadFile, 'write', observe):
            for path, field in (('/api/audit','file'),('/api/materials/preview','files')):
                response = self.client.post(path, files={field:('huge.xlsx',b'0' * (11*1024*1024))})
                self.assertEqual(response.status_code, 422)
        self.assertFalse(any(rolled))

    def test_audit_and_auto_client_roll_back_when_log_fails(self):
        self.login()
        with patch.object(self.store, '_log', side_effect=RuntimeError('log unavailable')):
            response = self.client.post('/api/audit', files={'file':('sample.xlsx', SAMPLE.read_bytes())})
        self.assertEqual(response.status_code, 500)
        with self.store.connect() as db:
            for table in ('audits', 'clients', 'audit_report_versions', 'notifications'):
                self.assertEqual(db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0], 0)
        response = self.client.post('/api/audit', files={'file':('sample.xlsx', SAMPLE.read_bytes())})
        self.assertEqual(response.status_code, 200)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_log WHERE action='create_audit'").fetchone()[0], 1)

    def test_rule_initialization_rolls_back_and_other_defaults_survive(self):
        self.login('root')
        with patch.object(self.store, '_log', side_effect=RuntimeError('log unavailable')):
            self.assertEqual(self.client.put('/api/rules/R-001/state', json={'enabled':False}).status_code, 500)
        self.assertIsNone(self.store.enabled_rule_ids())
        self.assertEqual(self.client.put('/api/rules/R-001/state', json={'enabled':False}).status_code, 200)
        enabled = self.store.enabled_rule_ids()
        self.assertEqual(len(enabled), 23)
        self.assertNotIn('R-001', enabled)
        self.store.change_rule_state('R-025', True, self.admin, {'R-025', *enabled, 'R-001'})
        self.assertNotIn('R-001', self.store.enabled_rule_ids())

    def test_material_sources_round_trip_and_legacy_do_not_invent_tables(self):
        data = Dataset(Company('来源测试', 'TEST', '', '2026'), [], {}, {}, sources=['关联资料.xlsx'])
        restored = deserialize_dataset(serialize_dataset(data))
        html, _ = render.render_html(restored, [], write=False)
        self.assertIn('关联资料.xlsx', html)
        self.assertNotIn('<td>科目余额表、增值税纳税申报表</td>', html)
        legacy = serialize_dataset(data)
        legacy.pop('sources')
        restored = deserialize_dataset(legacy)
        self.assertNotIn('sources', serialize_dataset(restored))
        self.assertIn('历史记录未保存', render.material_sources(restored)[0])
        loaded = loader.load(SAMPLE)
        self.assertIn('企业信息', loaded.sources)
        documents = materials.preview([('实际文件.xlsx', SAMPLE.read_bytes())], set(loaded.metrics))
        selected = {d['id']:{'id':d['id']} for d in documents}
        combined = materials.build_dataset(documents, selected, {}, set(loaded.metrics))
        self.assertEqual(combined.sources, ['实际文件.xlsx'])

    def test_dashboard_pages_one_company_with_constant_query_count(self):
        from webapp.dashboard import collect
        dataset = loader.load(SAMPLE)
        for index in range(35):
            data = deepcopy(dataset)
            data.company.period = f'{2020+index//12}-{index%12+1:02}'
            self.store.save_audit(f'audit-{index:02}', self.owner, None, data, [], {}, f'2026-09-27T00:00:{index:02}', create_client=True)
        foreign = self.store.create_user('foreign','Review-Probe-2026!','外部','org_admin','elsewhere')
        statements = []
        connect = self.store.connect
        @contextmanager
        def traced():
            with connect() as db:
                db.set_trace_callback(statements.append)
                yield db
        with patch.object(self.store, 'connect', traced):
            first = collect(self.store, self.owner, page_size=20)
        selects = [query for query in statements if query.lstrip().upper().startswith('SELECT')]
        self.assertLessEqual(len(selects), 6)
        self.assertEqual((len(first['records']), first['total_periods'], first['has_more']), (20,35,True))
        second = collect(self.store, self.owner, first['company'], page=2, page_size=20)
        self.assertEqual((len(second['records']),second['has_more']), (15,False))
        self.assertFalse({r['id'] for r in first['records']} & {r['id'] for r in second['records']})
        self.assertEqual(collect(self.store, foreign, first['company'])['records'], [])

    def test_operator_export_reads_exact_frozen_bytes_without_regeneration(self):
        from scripts import export_audit_pdf
        result = module._save_audit(loader.load(SAMPLE), self.owner)
        aid = result['audit_id']
        archived = self.store.get_report_version(aid, 1)
        out = Path(self.tmp.name) / 'frozen.html'
        args = [aid, '--database', str(self.store.path), '--version','1','--output',str(out)]
        with patch.object(render, 'render_html', side_effect=AssertionError('must not regenerate')):
            self.assertEqual(export_audit_pdf.main(args), 1)  # PDF not archived yet.
            self.assertFalse(out.exists())
            self.assertEqual(export_audit_pdf.main([*args, '--format','html']), 0)
            self.assertEqual(out.read_bytes(), archived['html'].encode())
            self.assertEqual(export_audit_pdf.main([*args, '--format','html']), 1)  # No overwrite.
            self.store.attach_report_pdf(aid, 1, b'%PDF-synthetic-frozen-bytes')
            pdf = Path(self.tmp.name) / 'frozen.pdf'
            with patch.dict(os.environ, {'TAXPEARLS_DB':str(self.store.path)}):
                self.assertEqual(export_audit_pdf.main([aid,'--output',str(pdf)]), 0)
            self.assertEqual(pdf.read_bytes(), b'%PDF-synthetic-frozen-bytes')
        self.assertEqual(self.store.get_report_version(aid,1)['html_sha256'], archived['html_sha256'])
