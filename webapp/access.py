"""Shared application-enforced tenant scope (SQLite has no native row policies)."""


class AccessDenied(PermissionError):
    def __init__(self, message='对象不存在或无权访问。', status=404):
        super().__init__(message)
        self.status = status


def is_teaching_dataset(dataset):
    """Explicit synthetic label, not a guarantee of the material's provenance."""
    return ("仿真" in dataset.company.name or "纯合成测试" in dataset.company.name
            or "TEST" in dataset.company.taxpayer_id.upper())


def current_actor(db, actor, roles=None):
    """Use inside the transaction that reads/writes the protected business rows."""
    row = db.execute('SELECT id,org_id,role,active FROM users WHERE id=?', (actor['id'],)).fetchone()
    if (not row or not row['active'] or row['org_id'] != actor['org_id'] or row['role'] != actor['role']
            or roles is not None and row['role'] not in roles):
        raise AccessDenied('账号权限已变化，请刷新后重试。', 403)
    return row


def audit_scope(actor):
    """SQL aliases a=audits, c=clients. Platform management is not a bypass."""
    if actor['role'] not in {'org_admin', 'accountant', 'teacher'}:
        return '0', []
    where, args = 'a.org_id=? AND (a.client_id IS NULL OR c.org_id=a.org_id)', [actor['org_id']]
    if actor['role'] == 'accountant':
        where += ' AND c.accountant_id=?'
        args.append(actor['id'])
    elif actor['role'] == 'teacher':
        # Preparation cases and cases already attached to this teacher's own
        # assignments. Legacy assignments are not an institution-wide grant.
        where += """ AND a.client_id IS NULL
            AND (instr(a.company_name,'仿真')>0 OR instr(a.company_name,'纯合成测试')>0
                 OR instr(upper(a.taxpayer_id),'TEST')>0)
            AND (a.created_by=? OR EXISTS (SELECT 1 FROM assignments teaching
                 WHERE teaching.audit_id=a.id AND teaching.org_id=a.org_id AND teaching.created_by=?))"""
        args.extend([actor['id'], actor['id']])
    return where, args


def audit_row(db, audit_id, actor):
    current_actor(db, actor)
    where, args = audit_scope(actor)
    return db.execute('SELECT a.* FROM audits a LEFT JOIN clients c ON c.id=a.client_id WHERE a.id=? AND ' + where,
                      [audit_id, *args]).fetchone()
