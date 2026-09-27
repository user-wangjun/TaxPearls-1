"use strict";
const fs = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");
for (const name of fs.readdirSync("webapp/static").filter(name => name.endsWith(".js"))) {
  new vm.Script(fs.readFileSync("webapp/static/" + name, "utf8"), {filename:name});
}
for (const match of fs.readFileSync("webapp/static/index.html", "utf8").matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)) {
  if (match[1].trim()) new vm.Script(match[1]);
}
// Run the actual role-selection function with a small API/DOM boundary.
const graph = fs.readFileSync("webapp/static/graph.js", "utf8");
const selection = graph.slice(graph.indexOf("async function loadKnowledge()"), graph.indexOf("async function loadGraph()"));
(async () => {
  for (const role of ["platform_admin", "student", "org_admin", "accountant", "teacher"]) {
    const calls = [], errors = [], choices = [];
    const context = {currentUser:{id:'user',role}, graphRequest:0, graphCurrent:()=>true, clearGraph(){}, $:()=>({value:"", replaceChildren(){}}),
      option:(_select, value)=>choices.push(value), showError:error=>errors.push(error),
      api:async url=>{calls.push(url); return [{id:"audit-1"}];}, loadGraph:async()=>{}};
    vm.createContext(context);
    vm.runInContext(selection, context);
    await context.loadKnowledge();
    assert.deepEqual(calls, ["org_admin", "accountant", "teacher"].includes(role) ? ["/api/audits"] : []);
    assert.equal(errors.length, 0);
    assert.equal(choices[0], "");
  }
  // Exercise the actual notification functions, including out-of-order reads
  // and navigation while a write is in flight. No browser globals are replaced
  // in application code; this tiny boundary provides only the needed DOM/API.
  const source = fs.readFileSync("webapp/static/console.js", "utf8");
  assert.ok(source.includes('AI 候选原值（待核对）'));
  assert.ok(!source.includes('`原件：${row.ai_raw_value'));
  const notificationCode = source.slice(source.indexOf("function noticeCurrent("), source.indexOf("let currentRiskChanges"));
  const nodes = new Map();
  const node = () => ({children:[], textContent:"", disabled:false, checked:false,
    classList:{contains:()=>true}, replaceChildren(){this.children=[];},
    append(...items){this.children.push(...items);}, get childElementCount(){return this.children.length;}});
  const graphNodes=new Map(),graphCalls=[],graphDraws=[];
  const graphCtx={currentUser:{id:'owner',role:'org_admin'},graphCanvas:{...node(),style:{},setAttribute(){}},
    $:id=>{if(!graphNodes.has(id))graphNodes.set(id,{...node(),value:'',style:{}});return graphNodes.get(id);},
    option:(select,value)=>select.append(value),updateAIContext(){},
    api:url=>new Promise((resolve,reject)=>graphCalls.push({url,resolve,reject})),
    filterGraph:()=>graphDraws.push(vm.runInContext('graphData.audit_id',graphCtx)),focusGraph(){}};
  vm.createContext(graphCtx);
  vm.runInContext('let graphRequest=0,graphData={nodes:[],edges:[]},graphSelected=null,graphVisible=new Set(),graphPositions=new Map(),graphFocused=false,graphTransform={},graphDrag=null,graphBusy=false;'+
    graph.slice(graph.indexOf('function graphCurrent('),graph.indexOf('$("graphAudit").addEventListener')),graphCtx);
  const listOld=graphCtx.loadKnowledge(),oldList=graphCalls.shift();
  const listNew=graphCtx.loadKnowledge();graphCalls.shift().resolve([{id:'new'}]);
  await new Promise(resolve=>setImmediate(resolve));
  graphCalls.shift().resolve({nodes:[],edges:[],audit_id:null,ai:{configured:false}});await listNew;
  oldList.resolve([{id:'old'}]);await listOld;
  assert.deepEqual(graphCtx.$('graphAudit').children,['','new']);assert.equal(graphCalls.length,0);
  const graphOld=graphCtx.loadGraph(),oldGraph=graphCalls.shift();
  const graphNew=graphCtx.loadGraph();graphCalls.shift().resolve({nodes:[],edges:[],audit_id:'new',ai:{configured:false}});await graphNew;
  oldGraph.resolve({nodes:[],edges:[],audit_id:'old',ai:{configured:true}});await graphOld;
  assert.equal(vm.runInContext('graphData.audit_id',graphCtx),'new');
  const leaving=graphCtx.loadGraph(),leaveCall=graphCalls.shift();graphCtx.invalidateGraph();
  leaveCall.resolve({nodes:[{id:'secret'}],edges:[],audit_id:'old',ai:{configured:true}});await leaving;
  assert.equal(vm.runInContext('graphData.nodes.length',graphCtx),0);assert.equal(graphCtx.$('graphSend').disabled,true);
  const failure=graphCtx.loadGraph();graphCalls.shift().reject(new Error('graph failure'));await failure;
  assert.equal(graphCtx.$('graphCount').textContent,'graph failure');assert.equal(graphCtx.$('graphSummary').children.length,0);
  const changedOwner=graphCtx.loadKnowledge(),ownerCall=graphCalls.shift();graphCtx.currentUser={id:'other',role:'org_admin'};
  ownerCall.resolve([{id:'private'}]);await changedOwner;assert.deepEqual(graphCtx.$('graphAudit').children,['']);
  vm.runInContext(graph.slice(graph.indexOf('function neighbors('),graph.indexOf('function layoutGraph(')),graphCtx);
  graphCtx.layoutGraph=()=>{};graphCtx.drawGraph=()=>{};graphCtx.renderNode=()=>{};
  vm.runInContext('graphData={nodes:[{id:"trade",category:"关联方图",status:"skipped",kind:"trade"}],edges:[]};',graphCtx);
  graphCtx.$('knowledgeCategory').value='关联方图';graphCtx.$('graphStatus').value='skipped';
  graphCtx.filterGraph();assert.equal(vm.runInContext('graphVisible.has("trade")',graphCtx),true);
  vm.runInContext(graph.slice(graph.indexOf('function layoutGraph('),graph.indexOf('function drawGraph(')),graphCtx);
  vm.runInContext('graphData={nodes:[],edges:[]};graphFocused=true;graphVisible=new Set();',graphCtx);
  graphCtx.innerWidth=390;graphCtx.layoutGraph(); // empty focused mobile view must not dereference undefined
  const pending = [];
  const ctx = {currentUser:{id:"user-1",role:"accountant"},
    $:id=>{if(!nodes.has(id)) nodes.set(id,node());return nodes.get(id);}, el:node,
    document:{querySelectorAll:()=>[]},
    api:url=>new Promise((resolve,reject)=>pending.push({url,resolve,reject})),
    renderResult(){}, switchPanel(){}};
  vm.createContext(ctx);
  vm.runInContext('let noticeRequest=0,noticeBusy=false,noticeReady=false,noticeHasEmail=false;'+notificationCode,ctx);
  const preferences = {has_email:true,audit_completed:true,high_risk:false,email_enabled:false,delivery_enabled:false};
  const resolveLoad = items => {items[0].resolve(preferences);items[1].resolve([]);};
  const old = ctx.loadNotifications(), oldCalls = pending.splice(0);
  const newer = ctx.loadNotifications(); resolveLoad(pending.splice(0)); await newer;
  oldCalls[0].reject(new Error("stale failure")); oldCalls[1].resolve([]); await old;
  assert.ok(!ctx.$("noticeStatus").textContent.includes("stale failure"));
  const older = ctx.loadNotifications(), olderCalls = pending.splice(0);
  const latest = ctx.loadNotifications(), latestCalls = pending.splice(0);
  latestCalls[0].reject(new Error("current failure"));latestCalls[1].resolve([]);await latest;
  resolveLoad(olderCalls);await older;
  assert.ok(ctx.$("noticeStatus").textContent.includes("current failure"));
  assert.equal(ctx.$("noticeSave").disabled,true);
  const recover = ctx.loadNotifications();resolveLoad(pending.splice(0));await recover;
  let finish, writes=0;
  const write = ctx.noticeAction(()=>{writes++;return new Promise(resolve=>{finish=resolve;});});
  await ctx.noticeAction(()=>{writes++;});
  assert.equal(writes,1);
  vm.runInContext('noticeRequest++;',ctx); // leave and return before save responds
  finish();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(pending.length,2); // return to the panel refreshes persisted state
  resolveLoad(pending.splice(0));await write;
  assert.equal(ctx.$("noticeSave").disabled,false);
  const failedWrite = ctx.noticeAction(()=>Promise.reject(new Error("write failed")));
  await failedWrite;
  assert.equal(ctx.$("noticeSave").disabled,true);
  assert.ok(ctx.$("noticeStatus").textContent.includes("write failed"));
  const ruleNodes = new Map(), ruleCalls = [], shown = [], ruleErrors = [];
  const ruleCtx = {currentUser:{id:"manager",role:"platform_admin"},editingRule:{id:"R-001"},
    $:id=>{if(!ruleNodes.has(id))ruleNodes.set(id,{...node(),value:"audit-1",querySelectorAll:()=>[]});return ruleNodes.get(id);},
    el:node,api:url=>new Promise((resolve,reject)=>ruleCalls.push({url,resolve,reject})),
    ruleDraftBody:()=>({new_version:"2.1"}),showRuleTrial:r=>shown.push(r),showError:e=>ruleErrors.push(e),
    openRuleEditor:r=>shown.push(r),loadAdmin:async()=>{}};
  vm.createContext(ruleCtx);
  vm.runInContext('let ruleEditorRequest=0,ruleHistoryRequest=0,ruleBusy=false;'+
    source.slice(source.indexOf("async function loadRuleVersionHistory("),source.indexOf("function ruleDraftBody(")),ruleCtx);
  const history1=ruleCtx.loadRuleVersionHistory("R-001"),firstHistory=ruleCalls.shift();
  const history2=ruleCtx.loadRuleVersionHistory("R-001");ruleCalls.shift().resolve([]);await history2;
  const latestChildren=ruleCtx.$("ruleVersionHistory").children.length;
  firstHistory.resolve([{version:"stale"}]);await history1;
  assert.equal(ruleCtx.$("ruleVersionHistory").children.length,latestChildren);
  const trial=ruleCtx.submitRuleDraft(false);
  await ruleCtx.submitRuleDraft(false);assert.equal(ruleCalls.length,1);
  vm.runInContext('ruleEditorRequest++;editingRule={id:"R-002"};',ruleCtx);
  ruleCalls.shift().resolve({version:"2.1"});await trial;assert.equal(shown.length,0);
  const staleSave=ruleCtx.submitRuleDraft(true);
  vm.runInContext('ruleEditorRequest++;',ruleCtx);
  ruleCalls.shift().reject(new Error("stale publish error"));await staleSave;
  assert.equal(ruleErrors.length,0);
  const save=ruleCtx.submitRuleDraft(true);ruleCalls.shift().resolve({id:"R-002",version:"2.1"});await save;
  assert.equal(shown.length,1);assert.ok(ruleCtx.$("ruleEditorStatus").textContent.includes("已保存"));
  const uploadNodes=new Map(),uploadCalls=[],reviews=[],uploadErrors=[];
  const uploadCtx={currentUser:{id:'u1'},dz:{style:{}},
    $:id=>{if(!uploadNodes.has(id))uploadNodes.set(id,{...node(),style:{},value:'local'});return uploadNodes.get(id);},
    FormData:class{append(){}},hideError(){},showError:e=>uploadErrors.push(e),
    api:()=>new Promise((resolve,reject)=>uploadCalls.push({resolve,reject})),
    renderMaterialReview:()=>reviews.push(vm.runInContext('materialDraft.token',uploadCtx)),
    clearMaterials:()=>vm.runInContext('materialRequest++;materialDraft=null;',uploadCtx)};
  vm.createContext(uploadCtx);
  vm.runInContext('let materialRequest=0,materialDraft=null;'+
    source.slice(source.indexOf('function materialCurrent('),source.indexOf('async function refreshAIStatus('))+
    source.slice(source.indexOf('async function upload(files)'),source.indexOf('function companyInputs(')),uploadCtx);
  const upload1=uploadCtx.upload([{name:'old.xlsx',size:10}]),oldUpload=uploadCalls.shift();
  const upload2=uploadCtx.upload([{name:'new.xlsx',size:10}]);uploadCalls.shift().resolve({token:'new'});await upload2;
  oldUpload.resolve({token:'old'});await upload1;assert.deepEqual(reviews,['new']);
  const upload3=uploadCtx.upload([{name:'failed.xlsx',size:10}]),oldFailure=uploadCalls.shift();
  const upload4=uploadCtx.upload([{name:'last.xlsx',size:10}]);uploadCalls.shift().resolve({token:'last'});await upload4;
  oldFailure.reject(new Error('old upload error'));await upload3;
  assert.deepEqual(reviews,['new','last']);assert.equal(uploadErrors.length,0);
  const evidenceBox=node();
  const evidenceCtx={$:()=>evidenceBox,el:(tag,cls,text)=>({...node(),tag,textContent:text||""})};
  vm.createContext(evidenceCtx);
  vm.runInContext(source.slice(source.indexOf('function renderMaterialEvidence('),source.indexOf('function renderAuditNarrative(')),evidenceCtx);
  evidenceCtx.renderMaterialEvidence([{name:'合同.四流完整合同数量',value:'0.00',source:'原始来源',detail:'待完善 1 份'},
    {name:'人力.社保参保人数',value:'1.00',source:'人力来源',detail:'核对月 2026-05'},
    {name:'其他.指标',value:'99'}]);
  assert.equal(evidenceBox.hidden,false);assert.equal(evidenceBox.children.length,4);
  assert.ok(evidenceBox.children[1].textContent.includes('不等于税务合规'));
  assert.equal(evidenceBox.children[2].children[1].textContent,'待完善 1 份');
  evidenceCtx.renderMaterialEvidence([]);
  assert.equal(evidenceBox.hidden,true);assert.equal(evidenceBox.children.length,0);
  console.log("Frontend syntax, role, material evidence and notification/rule/material race contracts passed.");
})().catch(error => {console.error(error); process.exitCode = 1;});
