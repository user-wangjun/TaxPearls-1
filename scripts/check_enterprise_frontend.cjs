"use strict";
// Execute the real enterprise controller with a minimal DOM/network boundary.
const fs = require("node:fs"), vm = require("node:vm"), assert = require("node:assert/strict");
function node(tag="div", className="", text="") {
  return {tag, className, textContent:text, children:[], style:{}, value:"", hidden:false, disabled:false, listeners:{},
    classList:{add(){}}, scrollIntoView(){}, setAttribute(key,value){this[key]=value;},
    append(...items){this.children.push(...items);}, replaceChildren(...items){this.children=items;},
    addEventListener(name,fn){this.listeners[name]=fn;}, remove(){}, click(){return this.onclick?.();}};
}
async function main() {
  const nodes=new Map(), calls=[], errors=[], results=[];
  let actor={id:"owner",role:"org_admin"}, controller;
  const context={URL:{revokeObjectURL(){}},setTimeout(){},window:{confirm:()=>true},
    FormData:class{append(){}},fetch:()=>{throw new Error("unexpected download");}};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync("webapp/static/enterprise.js","utf8"),context);
  const $=id=>{if(!nodes.has(id))nodes.set(id,node());return nodes.get(id);};
  controller=context.createEnterpriseMaterials({api:(url,options)=>new Promise((resolve,reject)=>calls.push({url,options,resolve,reject})),
    el:node,$,user:()=>actor,clear:()=>controller.reset(),showError:error=>errors.push(error),showResult:r=>results.push(r)});
  const sample=()=>({id:"batch",revision:1,analysis_revision:1,company:{name:"原文企业",taxpayer_id:"ID",period_start:"2026-01-01",period_end:"2026-01-31"},
    selections:{},documents:[],files:[],fields:{},metrics:[],executions:[],analysis:{can_confirm:true,checks:[],edits:[],feedback:{blocking:[],limited:[],suggested:[]}}});
  const all=(root=$("materialReview"))=>[root,...root.children.flatMap(child=>all(child))];
  const button=text=>{const found=all().find(n=>n.tag==="button"&&n.textContent===text);assert.ok(found,text);return found;};
  const field=title=>all().find(n=>n["aria-label"]===title);
  const settleLists=()=>{for(const call of calls.splice(0)){assert.ok(["/api/enterprise/materials","/api/enterprise/materials/config"].includes(call.url),call.url);call.resolve(call.url.endsWith("/config")?{retention_ready:true,message:"configured"}:[]);}};
  // Late responses from a previous account/reset must not paint private data.
  const stale=controller.open("old"), pending=calls.shift();controller.reset();
  pending.resolve(sample());await stale;assert.equal($("materialReview").children.length,0);
  const opening=controller.open("batch");calls.shift().resolve(sample());await opening;
  assert.equal(button("确认材料并开始检测").disabled,false);
  field("企业名称（必填）").value="手工企业";field("企业名称（必填）").listeners.input();
  assert.equal(button("确认材料并开始检测").disabled,true);
  assert.equal(field("补传材料").disabled,true);
  // Failed edits preserve the typed value. A retry sends only editable data.
  const failed=button("保存修改并重新分析").click();calls.shift().reject(new Error("save failed"));await failed;
  assert.equal(field("企业名称（必填）").value,"手工企业");assert.equal(errors.at(-1),"save failed");
  const saving=button("保存修改并重新分析").click(),write=calls.shift();
  const body=JSON.parse(write.options.body);assert.equal(body.company.name,"手工企业");
  assert.deepEqual(Object.keys(body).sort(),["company","expected_revision","selections"]);
  const modified={...sample(),revision:2,analysis_revision:2,company:{...sample().company,name:"手工企业"}};
  write.resolve(modified);await saving;settleLists();await Promise.resolve();
  assert.equal(button("确认材料并开始检测").disabled,false);
  // Concurrent clicks cannot dispatch two confirmations; refresh after success.
  const confirming=button("确认材料并开始检测").click(),confirmation=calls.shift();
  await button("确认材料并开始检测").click();assert.equal(calls.length,0);
  confirmation.resolve({audit_id:"audit1"});await new Promise(r=>setImmediate(r));
  calls.shift().resolve({...modified,revision:3,executions:[{analysis_revision:2,audit_id:"audit1"}]});
  await confirming;settleLists();await Promise.resolve();
  assert.equal(results.length,1);assert.equal(button("确认材料并开始检测").disabled,true);
  // A 409 protects against stale confirmation; local fields remain for review.
  const reload=controller.open("batch");calls.shift().resolve(modified);await reload;
  const conflict=button("确认材料并开始检测").click();calls.shift().reject(Object.assign(new Error("stale"),{status:409}));await conflict;
  assert.equal(button("确认材料并开始检测").disabled,true);assert.equal(button("保存修改并重新分析").disabled,true);
  const recover=controller.open("batch");calls.shift().resolve(modified);await recover;
  assert.equal(button("确认材料并开始检测").disabled,false);
  // Standard-cell edits are document-scoped, survive paging/filtering and
  // failures, and never send source grids or client-derived metrics.
  const standard=sample();
  const standardFields=Array.from({length:42},(_,i)=>({id:`利润表!B${i+2}`,table:"利润表",label:`项目${i}`,value:i===0?null:String(i)}));
  standard.documents=[{id:"d1",name:"本期.xlsx",kind:"xlsx",company:{},standard_fields:standardFields},
    {id:"d2",name:"另一表.xlsx",kind:"xlsx",company:{},standard_fields:[standardFields[0]]},
    {id:"legacy",name:"旧版.xlsx",kind:"xlsx",company:{},standard_fields:null}];
  const standardOpen=controller.open("batch");calls.shift().resolve(standard);await standardOpen;
  assert.ok(all().some(n=>n.textContent.includes("这是旧版解析材料")));
  const amount=title=>field(title+" 用于检测的值");
  amount("本期.xlsx 利润表!B2").value="0";amount("本期.xlsx 利润表!B2").listeners.input();
  assert.ok(all().some(n=>n.textContent.includes("匹配 42 个字段 · 本文件修正 1 项")));
  assert.equal(button("确认材料并开始检测").disabled,true);
  await button("下一页字段").click();
  amount("本期.xlsx 利润表!B43").value="900";amount("本期.xlsx 利润表!B43").listeners.input();
  const search=field("本期.xlsx 筛选账表字段");search.value="项目0";search.listeners.input();
  assert.equal(amount("本期.xlsx 利润表!B2").value,"0");
  const stdFailed=button("保存修改并重新分析").click(),stdWrite=calls.shift();
  const stdBody=JSON.parse(stdWrite.options.body);
  assert.deepEqual(stdBody.selections.d1.standard_edits,{"利润表!B2":"0","利润表!B43":"900"});
  assert.deepEqual(stdBody.selections.d2.standard_edits,{});
  assert.deepEqual(Object.keys(stdBody.selections.d1).sort(),["purpose","standard_edits"]);
  stdWrite.reject(new Error("standard save failed"));await stdFailed;
  assert.equal(amount("本期.xlsx 利润表!B2").value,"0");
  await button("恢复原文 · 利润表!B2").click();
  assert.equal(amount("本期.xlsx 利润表!B2").value,"");
  assert.ok(all().some(n=>n.textContent.includes("匹配 1 个字段 · 本文件修正 1 项")));
  const stdRetry=button("保存修改并重新分析").click(),stdRetryWrite=calls.shift();
  assert.deepEqual(JSON.parse(stdRetryWrite.options.body).selections.d1.standard_edits,{"利润表!B43":"900"});
  standard.selections.d1={standard_edits:{"利润表!B43":"900"}};
  standard.revision=standard.analysis_revision=2;stdRetryWrite.resolve(standard);await stdRetry;settleLists();await Promise.resolve();
  await button("下一页字段").click();assert.equal(amount("本期.xlsx 利润表!B43").value,"900");
  assert.equal(button("确认材料并开始检测").disabled,false);
  // Revoked material access clears displayed sensitive fields.
  const reference={batch_id:"batch",analysis_revision:1,confirmation_revision:2};
  const resultA={audit_id:"old-audit",material_reference:reference};
  const confirmed={...reference,audit_id:"old-audit",confirmed_by:"owner",confirmed_at:"then",current_revision:8,
    analysis_sha256:"digest",parser_version:"v2",company:{name:"old-company"},metrics:[{value:"100000"}],edits:[],checks:[],feedback:{},
    documents:[{name:"original.xlsx",fingerprint:"hash",purpose:"current",original_id:"file",material_company:{name:"old-company"}}],
    files:[{id:"file",name:"original.xlsx",deleted_at:"deleted-later"}]};
  const referenceButton=title=>all($("auditMaterials")).find(n=>n.tag==="button"&&n.textContent===title);
  controller.renderReference(resultA);
  const oldRead=referenceButton("查看当时确认的材料").click(),oldReadCall=calls.shift();
  controller.renderReference({...resultA,audit_id:"new-audit"});
  oldReadCall.resolve({confirmation:confirmed});await oldRead;
  assert.ok(!all($("auditMaterials")).some(n=>n.textContent.includes("old-company")));
  controller.renderReference(resultA);
  const readSnapshot=referenceButton("查看当时确认的材料").click();calls.shift().resolve({confirmation:confirmed});await readSnapshot;
  const historicalNodes=all($("auditMaterials"));
  assert.ok(historicalNodes.some(n=>n.textContent.includes("当前批次已到版本 8")));
  assert.ok(historicalNodes.some(n=>n.textContent.includes("原件当前已删除")));
  assert.ok(!historicalNodes.some(n=>n.tag==="input"||n.tag==="select"));
  assert.ok(!historicalNodes.some(n=>n.tag==="button"&&n.textContent.startsWith("下载确认原件")));
  const refused=referenceButton("查看当时确认的材料").click();calls.shift().reject(Object.assign(new Error("permission revoked"),{status:403}));await refused;
  assert.ok(!all($("auditMaterials")).some(n=>n.textContent.includes("old-company")));
  assert.ok(referenceButton("重试读取确认材料"));
  controller.renderReference({audit_id:"legacy"});
  assert.ok(all($("auditMaterials")).some(n=>n.textContent.includes("未保存材料确认版本关联")));
  assert.equal(calls.length,0);
  controller.renderReference(resultA);
  const resetRead=referenceButton("查看当时确认的材料").click(),resetCall=calls.shift();controller.reset();
  resetCall.resolve({confirmation:confirmed});await resetRead;
  assert.equal($("auditMaterials").children.length,0);
  const resume=controller.open("batch");calls.shift().resolve(modified);await resume;
  const revoked=button("保存修改并重新分析").click();calls.shift().reject(Object.assign(new Error("denied"),{status:403}));await revoked;
  assert.equal(field("企业名称（必填）"),undefined);
  assert.equal($("materialReview").hidden,false);assert.equal($("busy").style.display,"none");
  // Operation journal distinguishes rollback, committed-response failure and
  // interruption, never paints a late prior-session response, and can retry.
  const historyButton=text=>all($("enterpriseHistory")).find(n=>n.tag==="button"&&n.textContent===text);
  const journalList=controller.refresh();settleLists();await journalList;
  const journal=historyButton("查看材料操作与失败记录").click();
  const journalCall=calls.shift();assert.equal(journalCall.url,"/api/enterprise/material-operations");
  journalCall.resolve([{id:"failed",action:"upload",state:"failed",failure_code:"invalid_input",started_at:"now",actor_id:"owner"},
    {id:"committed",action:"upload",state:"committed",http_status:500,observed_at:"then",batch_id:"batch",started_at:"now",actor_id:"owner"},
    {id:"interrupted",action:"confirm",state:"started",started_at:"now",actor_id:"owner"}]);await journal;
  const journalNodes=all($("enterpriseHistory"));
  for(const text of ["本次操作未提交","业务已提交","未确认结果（处理中或中断）","不要据此重复上传"])
    assert.ok(journalNodes.some(n=>n.textContent.includes(text)),text);
  const journalFail=historyButton("查看材料操作与失败记录").click();calls.shift().reject(Object.assign(new Error("access denied"),{status:403}));await journalFail;
  assert.ok(!all($("enterpriseHistory")).some(n=>n.textContent.includes("请求 committed")));
  const retryJournal=historyButton("重试读取操作记录").click(),lateJournal=calls.shift();controller.reset();
  lateJournal.resolve([{id:"private-operation",action:"upload",state:"committed",started_at:"now"}]);await retryJournal;
  assert.ok(!all($("enterpriseHistory")).some(n=>n.textContent.includes("private-operation")));
  // A prior tenant's material-list response cannot leak after a role switch.
  const listing=controller.refresh();actor={id:"other",role:"teacher"};
  calls.shift().resolve([{id:"private",revision:1,created_at:"today"}]);calls.shift().resolve({retention_ready:true,message:"ready"});await listing;
  await controller.refresh();assert.equal($("enterpriseHistory").hidden,true);assert.equal($("enterpriseHistory").children.length,0);
  assert.equal(controller.enabled(),false);
  console.log("Enterprise frontend: routing, standard-cell review/paging/restore, dirty guard, failed save, retry, idempotent dispatch, stale response, version conflict and revoked access passed.");
}
main().catch(error=>{console.error(error);process.exitCode=1;});
