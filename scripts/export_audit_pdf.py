"""Export frozen report bytes from a read-only database, never regenerate them.

    python scripts/export_audit_pdf.py --list
    python scripts/export_audit_pdf.py AUDIT_ID --version 1 --output saved.pdf

PDF must already have been exported/archived in the workbench. An absent PDF
is an error; --format html exports the frozen HTML instead. Existing files
are never overwritten. The operator must be authorized to read the database.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import os
from pathlib import Path
import re
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src import settings  # noqa: E402,F401 - load environment, no database initialization
from webapp.storage import Store  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audit_id", nargs="?")
    parser.add_argument("--database", type=Path)
    parser.add_argument("--version", type=int)
    parser.add_argument("--format", choices=("pdf", "html"), default="pdf")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)
    path = args.database or Path(os.getenv("TAXPEARLS_DB") or ROOT / "instance/taxpearls.db")
    if not path.is_absolute():
        path = ROOT / path
    try:
        if not path.is_file():
            raise ValueError("数据库不存在。")
        if args.version is not None and (args.version < 1 or not args.audit_id):
            raise ValueError("版本须为正整数，并同时指定审计编号。")
        with closing(sqlite3.connect(path.resolve().as_uri()+"?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            if args.list:
                for row in db.execute("SELECT audit_id,version,created_at,pdf_sha256 FROM audit_report_versions ORDER BY created_at DESC,version DESC"):
                    print(row['audit_id'], 'v'+str(row['version']), row['created_at'], 'PDF' if row['pdf_sha256'] else 'HTML only')
                return 0
            clauses, values = [], []
            if args.audit_id:
                clauses.append('audit_id=?'); values.append(args.audit_id)
            if args.version is not None:
                clauses.append('version=?'); values.append(args.version)
            where = ' WHERE '+' AND '.join(clauses) if clauses else ''
            row = db.execute('SELECT * FROM audit_report_versions'+where+' ORDER BY created_at DESC,version DESC LIMIT 1',values).fetchone()
            if row is None:
                raise ValueError("没有匹配的冻结报告版本。")
            Store._verify_report_version(row, full=True)
            content = row['pdf_bytes'] if args.format=='pdf' else row['html'].encode('utf-8')
            if content is None:
                raise ValueError("该版本 PDF 尚未归档；请先在工作台导出，或使用 --format html。")
            safe_id = re.sub(r'[^A-Za-z0-9_-]', '_', row['audit_id'])[:64]
            destination = args.output or ROOT / 'output' / f"archive-{safe_id}-v{row['version']}.{args.format}"
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open('xb') as stream:
                stream.write(content)
            print(f"已导出冻结原件：{destination.resolve()}")
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(f"导出失败：{exc}", file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
