"""HTTP test helpers for the public upload -> explicit confirmation workflow.

No automatic correction or fake response: a failed upload/confirmation is
returned unchanged. The key is synthetic, scoped to each test, never a .env key.
"""
import base64
import os
from unittest.mock import patch


def material_key(target):
    return patch.dict(os.environ, {'TAXPEARLS_MATERIAL_KEY': base64.b64encode(b't' * 32).decode(),
                                  'TAXPEARLS_AI_ENABLED': '0'})(target)


def audit(client, *, files, data=None):
    items = files.items() if isinstance(files, dict) else files
    uploaded = client.post('/api/enterprise/materials', data=data or {},
                           files=[('files', value) for _, value in items])
    if uploaded.status_code != 200:
        return uploaded
    batch = uploaded.json()
    return confirm(client, batch)


def confirm(client, batch):
    return client.post('/api/enterprise/materials/' + batch['id'] + '/confirm',
                       json={'expected_revision': batch['revision']})
