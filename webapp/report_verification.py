"""Authenticated original-byte checks, not a public report lookup or PDF signature."""
from __future__ import annotations

import hmac
from typing import Literal

from fastapi import Cookie, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from src.report_protection import IDENTIFIER


class CheckBody(BaseModel):
    format: Literal["html","pdf"]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def register(app, store_provider, user_for_session, allow, audit_for_user, cookie_name):
    def original(identifier, session):
        user=user_for_session(session)
        allow(user,"org_admin","accountant","teacher")
        if not IDENTIFIER.fullmatch(identifier):
            raise HTTPException(404,"标识不存在或无权核验。")
        store=store_provider();target=store.report_protection_target(identifier,user)
        if not target:
            raise HTTPException(404,"标识不存在或无权核验。")
        try:
            if target["kind"]=="audit":
                audit_for_user(target["audit_id"],user)  # Current client assignment, not archived permissions.
                record=store.get_report_version(target["audit_id"],target["version"])
                manifest=record["manifest"] if record else {}
                binding={"audit_id":target["audit_id"],"version":target["version"]}
            else:
                if user["role"] not in {"org_admin"}:
                    raise HTTPException(404,"标识不存在或无权核验。")
                record=store.get_org_report(target["org_report_id"],user)
                manifest=record["snapshot"] if record else {}
                binding={"org_report_id":target["org_report_id"]}
            protection=manifest.get("protection",{})
            if not record or protection.get("id")!=identifier or manifest.get("org_id")!=user["org_id"]:
                raise ValueError("标识与归档不一致，请核查备份。")
        except ValueError as exc:
            raise HTTPException(409,str(exc)) from None
        result={"id":identifier,"kind":target["kind"],**binding,"report_no":protection["report_no"],
                "customer_name":protection["customer_name"],"report_date":protection["report_date"],
                "archived_at":record["created_at"],"html_sha256":record["html_sha256"],"pdf_sha256":record["pdf_sha256"],
                "notice":"仅核对服务器原件字节；标识/水印可被复制，核验不是第三方签名、税务鉴证或防转发保证。"}
        return user,result

    @app.get("/api/report-verification/{identifier}")
    def metadata(identifier: str, session: str | None = Cookie(default=None,alias=cookie_name)):
        user,result=original(identifier,session)
        store_provider().log(user,"view_report_verification","report_protection",identifier)
        return JSONResponse(result,headers={"Cache-Control":"private, no-store"})

    @app.post("/api/report-verification/{identifier}")
    def check(identifier: str, body: CheckBody, session: str | None = Cookie(default=None,alias=cookie_name)):
        user,result=original(identifier,session)
        expected=result[body.format+"_sha256"]
        if expected is None:
            raise HTTPException(409,"此版本 PDF 尚未导出存档，不能核验；不会为核验重建文件。")
        matches=hmac.compare_digest(body.sha256,expected)
        store_provider().log(user,"check_report_original","report_protection",identifier,f"format={body.format};match={matches}")
        return JSONResponse({**result,"format":body.format,"sha256_matches":matches,
            "message":"文件指纹与归档原件一致。" if matches else "文件与归档原件不一致：可能已修改、重保存或选错版本，不判定具体原因。"},
            headers={"Cache-Control":"private, no-store"})
