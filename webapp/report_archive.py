"""Build immutable HTML/manifest snapshots; PDF is stored on first export."""
from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json

from src import render
from src.report_protection import protect_html
from src.snapshots import serialize_dataset, serialize_findings


def digest(data: bytes) -> str:
    return sha256(data).hexdigest()


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def build_snapshot(entry, branding, narrative=None, origin="generated") -> dict:
    template = (render.TEMPLATE_DIR / "report.html").read_text(encoding="utf-8")
    when = datetime.fromisoformat(entry["audited_at"])
    html, _ = render.render_html(
        entry["dataset"], entry["findings"], when=when, write=False,
        org_name=branding["display_name"], report_title=branding["report_title"],
        footer_text=branding["footer_text"], logo_data_uri=branding["logo_data_uri"],
        ai_narrative=narrative, template_source=template, protect=False,
        material_reference=entry.get('material_reference'),
    )
    manifest = {
        "schema": 1, "origin": origin, "audit_id": entry["id"], "org_id": entry["org_id"],
        "audited_at": entry["audited_at"], "report_no": render.make_report_no(entry["dataset"].company.name, when),
        "report_title": branding["report_title"], "org_name": branding["display_name"],
        "footer_text": branding["footer_text"], "logo_sha256": digest(branding["logo_data_uri"].encode()),
        "template_sha256": digest(template.encode()),
        "audit_sha256": digest(canonical({"dataset": serialize_dataset(entry["dataset"]),
                                          "findings": serialize_findings(entry["findings"])}).encode()),
        "rules": [{key: getattr(item.rule, key) for key in ("id", "version", "effective_from", "effective_to")}
                  for item in sorted(entry["findings"], key=lambda item: item.rule.id)],
        "narrative_sha256": digest(canonical(narrative).encode()) if narrative else None,
        "narrative_model": narrative.get("model") if narrative else None,
    }
    if entry.get('material_reference'):
        manifest['material_reference'] = entry['material_reference']
    html, protection = protect_html(html,entry["dataset"].company.name,when.date().isoformat(),
                                    manifest["report_no"],context=canonical(manifest),registered=True)
    manifest["protection"] = protection
    return {"html": html, "manifest": manifest}
