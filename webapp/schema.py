"""Additive SQLite schema installation; invoked only by explicit Store creation."""


def initialize(store) -> None:
    from webapp import classroom, mistake_book, members, email_auth, oauth
    with store.connect() as db:
        # WAL 一次设置、持久化于库文件
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL, display_name TEXT NOT NULL,
                role TEXT NOT NULL, org_id TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                expires_at TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS clients (
                id TEXT PRIMARY KEY, org_id TEXT NOT NULL, name TEXT NOT NULL,
                taxpayer_id TEXT NOT NULL, accountant_id TEXT REFERENCES users(id),
                created_at TEXT NOT NULL, UNIQUE(org_id, taxpayer_id)
            );
            CREATE TABLE IF NOT EXISTS audits (
                id TEXT PRIMARY KEY, org_id TEXT NOT NULL,
                client_id TEXT REFERENCES clients(id), created_by TEXT NOT NULL REFERENCES users(id),
                company_name TEXT NOT NULL, taxpayer_id TEXT NOT NULL,
                industry TEXT NOT NULL, period TEXT NOT NULL,
                dataset_json TEXT NOT NULL, findings_json TEXT NOT NULL,
                summary_json TEXT NOT NULL, audited_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_audits_org ON audits(org_id, audited_at DESC);
            CREATE TABLE IF NOT EXISTS audit_report_versions (
                audit_id TEXT NOT NULL REFERENCES audits(id), version INTEGER NOT NULL,
                html TEXT NOT NULL, html_sha256 TEXT NOT NULL,
                manifest_json TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
                content_sha256 TEXT NOT NULL, created_by TEXT NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL, pdf_bytes BLOB, pdf_sha256 TEXT, pdf_created_at TEXT,
                PRIMARY KEY(audit_id,version), UNIQUE(audit_id,content_sha256)
            );
            CREATE TABLE IF NOT EXISTS org_reports (
                id TEXT PRIMARY KEY, org_id TEXT NOT NULL,
                created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
                snapshot_json TEXT NOT NULL, snapshot_sha256 TEXT NOT NULL,
                html TEXT NOT NULL, html_sha256 TEXT NOT NULL,
                pdf_bytes BLOB, pdf_sha256 TEXT, pdf_created_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_org_reports_org ON org_reports(org_id,created_at DESC);
            CREATE TABLE IF NOT EXISTS report_protections (
                id TEXT PRIMARY KEY,org_id TEXT NOT NULL,kind TEXT NOT NULL CHECK(kind IN ('audit','org')),
                audit_id TEXT,version INTEGER,org_report_id TEXT REFERENCES org_reports(id),
                FOREIGN KEY(audit_id,version) REFERENCES audit_report_versions(audit_id,version),
                CHECK((kind='audit' AND audit_id IS NOT NULL AND version IS NOT NULL AND org_report_id IS NULL)
                   OR (kind='org' AND org_report_id IS NOT NULL AND audit_id IS NULL AND version IS NULL))
            );
            CREATE TABLE IF NOT EXISTS assignments (
                id TEXT PRIMARY KEY, org_id TEXT NOT NULL, title TEXT NOT NULL,
                audit_id TEXT NOT NULL REFERENCES audits(id), created_by TEXT NOT NULL REFERENCES users(id),
                target_student_id TEXT REFERENCES users(id), weights_json TEXT NOT NULL,
                false_positive_penalty REAL NOT NULL, published INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS generated_exercises (
                audit_id TEXT PRIMARY KEY REFERENCES audits(id),org_id TEXT NOT NULL,
                metadata_json TEXT NOT NULL,metadata_sha256 TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_assignments_teacher_audit
                ON assignments(org_id,created_by,audit_id);
            CREATE TABLE IF NOT EXISTS submissions (
                id TEXT PRIMARY KEY, assignment_id TEXT NOT NULL REFERENCES assignments(id),
                student_id TEXT NOT NULL REFERENCES users(id), answers_json TEXT NOT NULL,
                score REAL NOT NULL, details_json TEXT NOT NULL, submitted_at TEXT NOT NULL,
                adjusted_score REAL, feedback TEXT, reviewed_by TEXT REFERENCES users(id),
                UNIQUE(assignment_id, student_id)
            );
            CREATE TABLE IF NOT EXISTS rule_state (
                rule_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1,
                updated_by TEXT REFERENCES users(id), updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rule_overrides (
                rule_id TEXT PRIMARY KEY, version TEXT NOT NULL,
                logic_json TEXT NOT NULL, threshold_basis TEXT NOT NULL,
                updated_by TEXT REFERENCES users(id), updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rule_version_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_id TEXT NOT NULL, version TEXT NOT NULL,
                effective_from TEXT, effective_to TEXT,
                logic_json TEXT NOT NULL, threshold_basis TEXT NOT NULL,
                rule_json TEXT,
                updated_by TEXT REFERENCES users(id), updated_at TEXT NOT NULL,
                UNIQUE(rule_id, version),
                CHECK(effective_from IS NOT NULL OR effective_to IS NULL)
            );
            CREATE INDEX IF NOT EXISTS idx_rule_version_period
                ON rule_version_history(rule_id, effective_from, effective_to);
            CREATE TABLE IF NOT EXISTS org_settings (
                org_id TEXT PRIMARY KEY, display_name TEXT NOT NULL DEFAULT '税海拾珠',
                report_title TEXT NOT NULL DEFAULT '税务风险审计报告',
                footer_text TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
                logo_mime TEXT, logo_bytes BLOB, logo_updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT,
                org_id TEXT NOT NULL, action TEXT NOT NULL,
                target_type TEXT NOT NULL, target_id TEXT NOT NULL,
                detail TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notification_preferences (
                user_id TEXT PRIMARY KEY REFERENCES users(id),
                audit_completed INTEGER NOT NULL DEFAULT 0,
                high_risk INTEGER NOT NULL DEFAULT 0,
                email_enabled INTEGER NOT NULL DEFAULT 0,
                updated_by TEXT REFERENCES users(id), updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notifications (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                org_id TEXT NOT NULL, audit_id TEXT NOT NULL REFERENCES audits(id),
                event TEXT NOT NULL, summary_json TEXT NOT NULL,
                created_at TEXT NOT NULL, read_at TEXT,
                UNIQUE(user_id,audit_id,event)
            );
            CREATE INDEX IF NOT EXISTS idx_notifications_user
                ON notifications(user_id,org_id,created_at);
            CREATE TABLE IF NOT EXISTS notification_deliveries (
                notification_id TEXT PRIMARY KEY REFERENCES notifications(id),
                recipient_email TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, claim_token TEXT,
                provider_id TEXT, error_code TEXT, claimed_at TEXT, payload_json TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_notification_deliveries_status
                ON notification_deliveries(status,created_at,notification_id);
            CREATE TABLE IF NOT EXISTS finding_interpretations (
                audit_id TEXT NOT NULL REFERENCES audits(id),
                rule_id TEXT NOT NULL, evidence_hash TEXT NOT NULL,
                model TEXT NOT NULL, result_json TEXT NOT NULL,
                created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
                PRIMARY KEY(audit_id, rule_id, evidence_hash)
            );
            CREATE TABLE IF NOT EXISTS audit_narratives (
                audit_id TEXT NOT NULL REFERENCES audits(id),
                evidence_hash TEXT NOT NULL, model TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
                PRIMARY KEY(audit_id, evidence_hash)
            );
        """)
        version_columns = {row["name"] for row in db.execute("PRAGMA table_info(rule_version_history)")}
        if "rule_json" not in version_columns:
            db.execute("ALTER TABLE rule_version_history ADD COLUMN rule_json TEXT")
        delivery_columns = {row["name"] for row in db.execute("PRAGMA table_info(notification_deliveries)")}
        if "payload_json" not in delivery_columns:
            db.execute("ALTER TABLE notification_deliveries ADD COLUMN payload_json TEXT")
        from webapp.notifications import migrate_deliveries
        migrate_deliveries(db)
        # Preserve pre-B12 overrides as undated legacy versions. Existing
        # audits already contain immutable Finding snapshots.
        db.execute("""INSERT OR IGNORE INTO rule_version_history
                   (rule_id,version,effective_from,effective_to,logic_json,
                    threshold_basis,updated_by,updated_at)
                   SELECT rule_id,version,NULL,NULL,logic_json,
                          threshold_basis,updated_by,updated_at FROM rule_overrides""")
        # Existing P1 databases predate configurable organization logos.
        columns = {row["name"] for row in db.execute("PRAGMA table_info(org_settings)")}
        for name, sql_type in (
            ("logo_mime", "TEXT"), ("logo_bytes", "BLOB"), ("logo_updated_at", "TEXT")
        ):
            if name not in columns:
                db.execute(f"ALTER TABLE org_settings ADD COLUMN {name} {sql_type}")
        # 注册与开户（FR-G10/G11）：用户绑定邮箱。email 可空但唯一（部分唯一索引）。
        # 因「用户自设密码」，password_hash 保持 NOT NULL——仅需加列，无需重建表。
        user_columns = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
        if "email" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN email TEXT")
        db.execute("""CREATE TABLE IF NOT EXISTS email_tokens (
                token_hash TEXT PRIMARY KEY,
                email TEXT NOT NULL,
                purpose TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                session_key TEXT,
                code_hash TEXT,
                expires_at TEXT NOT NULL,
                used_at TEXT,
                created_at TEXT NOT NULL
            )""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_email_tokens_email ON email_tokens(email, purpose)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_audits_org_period ON audits(org_id,taxpayer_id,period,audited_at DESC)")
        email_auth.migrate(db)
        oauth.migrate(db)
        from webapp import channel_bindings
        channel_bindings.migrate(db)
        db.execute("""CREATE TABLE IF NOT EXISTS invite_codes (
                token_hash TEXT PRIMARY KEY,
                org_name TEXT NOT NULL,
                seats INTEGER NOT NULL,
                bound_email TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                redeemed_by TEXT REFERENCES users(id),
                redeemed_at TEXT,
                revoked INTEGER NOT NULL DEFAULT 0,
                created_by TEXT NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL,
                CHECK (redeemed_by IS NULL OR redeemed_at IS NOT NULL)
            )""")
        db.execute("""CREATE TABLE IF NOT EXISTS org_quota (
                org_id TEXT PRIMARY KEY,
                seats INTEGER NOT NULL,
                updated_by TEXT REFERENCES users(id),
                updated_at TEXT NOT NULL
            )""")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email "
                   "ON users(email) WHERE email IS NOT NULL AND email <> ''")
        count = db.execute("SELECT COUNT(*) FROM users WHERE role='platform_admin'").fetchone()[0]
        if count > 1:
            raise RuntimeError("现有数据库含多个平台管理员，需人工确认归并后才能安装唯一约束；未自动删除账号。")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_single_platform_admin "
                   "ON users(role) WHERE role='platform_admin'")
        classroom.migrate(db)
        mistake_book.migrate(db)
        members.migrate(db)
        from webapp import material_batches
        material_batches.migrate(db)
