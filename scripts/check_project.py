"""Run repository checks with isolated databases and no external credentials."""
from pathlib import Path
import ast
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def main():
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    def fingerprint():
        paths = [ROOT / "main.py", *[p for d in ("src", "webapp", "scripts", "tests", "rules", "templates") for p in (ROOT / d).rglob("*") if p.suffix in {".py", ".js", ".html", ".css", ".yaml"}]]
        return {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    before = fingerprint()
    for path in [ROOT / 'main.py', *[p for d in ('src', 'webapp', 'scripts', 'tests')
                                    for p in (ROOT / d).rglob('*.py')]]:
        ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))
    subprocess.run([sys.executable, '-m', 'pip', 'check'], check=True)
    subprocess.run([sys.executable, '-m', 'ruff', 'check', 'main.py', 'src', 'webapp', 'scripts', 'tests'], check=True)
    subprocess.run(['node', 'scripts/check_frontend.cjs'], check=True)
    subprocess.run(['node', 'scripts/check_enterprise_frontend.cjs'], check=True)
    with tempfile.TemporaryDirectory(prefix='taxpearls-check-') as directory:
        os.environ.update(TAXPEARLS_DB=str(Path(directory) / 'bootstrap.db'),
                          TAXPEARLS_AI_ENABLED='0', TAXPEARLS_NOTIFICATION_EMAIL_ENABLED='0',
                          TAXPEARLS_AI_API_KEY='', TAXPEARLS_RESEND_API_KEY='',
                          TAXPEARLS_BACKUP_KEY='', TAXPEARLS_MATERIAL_KEY='')
        # Multiple explicit patterns let related migrations share one isolated
        # process and the same source-stability/contract checks.
        suite = unittest.TestSuite(unittest.defaultTestLoader.discover('tests', pattern=pattern)
                                   for pattern in dict.fromkeys(sys.argv[1:] or ['test_*.py']))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    after = fingerprint()
    changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
    if changed:
        print("检查期间源码发生变化，请复验：" + ", ".join(changed))
    return 0 if result.wasSuccessful() and not changed else 1


if __name__ == '__main__':
    raise SystemExit(main())
