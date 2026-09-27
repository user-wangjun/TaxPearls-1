"""Allowlisted extraction program fingerprints, never environment or secrets."""
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def program(kind):
    # Hash actual deployed sources, not a branch name or an uncommitted HEAD.
    # Missing source/package information remains unknown, never inferred.
    files = ('ai_extraction.py', 'ai_transport.py', 'settings.py') if kind == 'ai' else (
        'materials.py', 'loader.py', 'material_review.py', 'workbooks.py',
        'config.py', 'periods.py', 'models.py', 'related_graph.py')
    packages = ('pydantic', 'pypdfium2', 'Pillow') if kind == 'ai' else (
        'openpyxl', 'pdfplumber', 'pdfminer.six')
    sources, dependencies = {}, {}
    for name in (*files, 'material_provenance.py'):
        try:
            sources[name] = sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
        except OSError:
            sources[name] = None
    for name in packages:
        try:
            dependencies[name] = version(name)
        except PackageNotFoundError:
            dependencies[name] = None
    return {'contract': 'extraction-provenance-v1', 'sources': sources, 'dependencies': dependencies}
