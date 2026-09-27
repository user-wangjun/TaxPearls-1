"use strict";
/* 安全约定：所有来自上传数据的文本一律通过 textContent / DOM API 写入，
   绝不 innerHTML 拼接（杜绝原型期遗留的 XSS 路径）。 */

function el(tag, className, text) {
  const n = document.createElement(tag);
  if (className) n.className = className;
  if (text !== undefined && text !== null && text !== "") n.textContent = text;
  return n;
}

const $ = (id) => document.getElementById(id);
let auditId = null;
let lastResult = null;
let snapshotReturn = null;
let currentUser = null;
let selectedClientId = "";
let openAssignmentId = null;
let exerciseOpenRequest = 0, sandboxFeedbackRequest = 0, sandboxRuleId = null;
let sandboxEvidence = new Map();
let trainingLoadRequest = 0, exerciseDeadlineTimer = null;
let latestFindings = [];
let ruleCatalog = [];
let editingRule = null;
let ruleDraftLogic = null;
let noticeRequest = 0, noticeBusy = false, noticeReady = false, noticeHasEmail = false;
let ruleEditorRequest = 0, ruleHistoryRequest = 0, ruleBusy = false, adminRequest = 0;

async function api(url, options = {}) {
  let r;
  try {
    r = await fetch(url, options);
  } catch (e) {
    throw new Error(location.protocol === "file:"
      ? "当前是直接打开的本地文件，请求不到后端接口。请通过服务地址访问，例如 http://127.0.0.1:8000 。"
      : "无法连接服务器，请确认服务已启动后重试。");
  }
  const ct = r.headers.get("content-type") || "";
  const body = ct.includes("application/json") ? await r.json().catch(() => null) : await r.text();
  if (!r.ok) {
    const detail = body?.detail;
    const message = Array.isArray(detail) ? detail.map(e=>e.msg||"字段校验失败").join("；") : detail;
    const error = new Error(message || (typeof body === "string" && body ? body : "") || `请求失败（${r.status}）`); error.status=r.status; throw error;
  }
  return body;
}

function switchPanel(id) {
  if(currentUser?.role==="platform_admin" && !["adminPanel","knowledgePanel"].includes(id)) id="adminPanel";
  if (id !== "notificationPanel") noticeRequest++;
  if (id !== "knowledgePanel" && typeof invalidateGraph === "function") invalidateGraph();
  if (id !== "adminPanel") { ruleEditorRequest++; adminRequest++; }
  if (id === "uploadPanel") { refreshAIStatus(); refreshAuditClients(); }
  document.querySelectorAll(".panel").forEach((n) => n.classList.toggle("active", n.id === id));
  document.querySelectorAll("#nav [data-panel]").forEach((n) => n.classList.toggle("active", n.dataset.panel === id));
  if (id === "historyPanel") loadHistory();
  if (id === "notificationPanel") loadNotifications();
  if (id === "trainingPanel") loadAssignments();
  if (id === "adminPanel") loadAdmin();
  if (id === "dashboardPanel") loadDashboard();
  if (id === "orgReportPanel") loadOrgOverview();
  if (id === "knowledgePanel") loadKnowledge();
}

/* ---------- 多材料上传与 PDF 核对 ---------- */
const dz = $("dropzone"), fi = $("fileInput");
let materialDraft = null, materialCards = [], originalUrls = [];
let materialRequest = 0;
function materialCurrent(request,owner){return request===materialRequest&&currentUser?.id===owner;}
async function refreshAIStatus() {
  try { const status = await api("/api/materials/config"); $("aiStatus").textContent = status.message; }
  catch(err) { $("aiStatus").textContent = "AI 配置状态不可用：" + err.message; }
}
async function refreshAuditClients() {
  const field=$("auditClientField"),select=$("auditClient");
  if(!currentUser || !["org_admin","accountant"].includes(currentUser.role)){field.style.display="none";return;}
  field.style.display="block";
  try {
    const clients=await api("/api/clients"),preferred=selectedClientId||select.value;
    select.textContent="";const blank=el("option",null,"不关联（按材料自动建档）");blank.value="";select.append(blank);
    for(const client of clients){const o=el("option",null,client.name+" · "+client.taxpayer_id);o.value=client.id;select.append(o);}
    if(clients.some(client=>client.id===preferred))select.value=preferred;
    selectedClientId=select.value;
  } catch(err){showError(err.message);}
}
$("auditClient").addEventListener("change",()=>{selectedClientId=$("auditClient").value;});
function clearMaterials() {
  materialRequest++;
  $("busy").style.display="none";$("busy").textContent="正在处理材料，请稍候……";
  originalUrls.forEach(URL.revokeObjectURL); originalUrls = [];
  materialDraft = null; materialCards = [];
  $("materialReview").replaceChildren(); $("materialReview").hidden = true;
  $("batchResults").replaceChildren(); $("batchResults").hidden = true;
  $("materialReview").after($("batchResults"));
}
const TOAST_PEEK = 16;
const TOAST_MAX = 5;
function placeToast(el, i, count, lift) {
  // 队头（最新）i=0 在最上方、完全可见；i 越大越旧、越靠下，只露出下缘
  const y = i * TOAST_PEEK + (lift || 0);
  const scale = 1 - Math.min(i, 4) * 0.04;
  el.style.transform = `translateY(${y}px) scale(${scale})`;
  el.style.zIndex = String(100 - i);
  el._y = y;
  el._scale = scale;
}
function relayoutToasts() {
  const live = Array.from(document.getElementById("toastStack").children).filter((t) => !t._closing);
  live.forEach((t, i) => placeToast(t, i, live.length));
}
function showToast(msg, type = "error", stayMs) {
  const stack = document.getElementById("toastStack");
  const el = document.createElement("div");
  el.className = "toast " + type;
  el.setAttribute("role", type === "error" ? "alert" : "status");
  const span = document.createElement("span");
  span.className = "msg";
  span.textContent = msg;
  const close = document.createElement("button");
  close.type = "button";
  close.className = "x";
  close.setAttribute("aria-label", "关闭");
  close.textContent = "×";
  el.append(span, close);
  stack.prepend(el);
  const liveCount = () => Array.from(stack.children).filter((t) => !t._closing).length;
  placeToast(el, 0, 1, -22);  // 起始：悬在栈顶上方，落入后旧卡下移让位
  el.style.opacity = "0";
  void el.offsetWidth;
  el.style.opacity = "1";
  placeToast(el, 0, 1);
  relayoutToasts();
  let timer = null;
  const dismiss = () => {
    if (el._closing) return;
    el._closing = true;
    if (timer) { clearTimeout(timer); timer = null; }
    el.classList.add("closing");
    el.style.transform = `translateY(${el._y - 40}px) scale(${el._scale})`;
    el.style.opacity = "0";
    relayoutToasts();
    setTimeout(() => el.remove(), 700);
  };
  close.addEventListener("click", dismiss);
  el._dismiss = dismiss;
  const delay = stayMs || (type === "error" ? 6000 : 3600);
  timer = setTimeout(dismiss, delay);
  const live = Array.from(stack.children).filter((t) => !t._closing);
  if (live.length > TOAST_MAX) live.slice(TOAST_MAX).forEach((t) => t._dismiss && t._dismiss());
}
function showError(msg) { showToast(msg, "error"); }
function hideError() { document.querySelectorAll("#toastStack .toast.error").forEach((t) => t._dismiss && t._dismiss()); }
dz.addEventListener("click", () => fi.click());
fi.addEventListener("click", e => e.stopPropagation());
fi.addEventListener("change", () => { if (fi.files.length) upload(Array.from(fi.files)); fi.value = ""; });
dz.addEventListener("dragover", e => { e.preventDefault(); dz.classList.add("drag"); });
dz.addEventListener("dragleave", () => dz.classList.remove("drag"));
dz.addEventListener("drop", e => { e.preventDefault(); dz.classList.remove("drag"); if (e.dataTransfer.files.length) upload(Array.from(e.dataTransfer.files)); });
async function upload(files) {
  hideError();
  if (files.length > 20) return showError("每次最多选择 20 个文件。");
  if (files.some(f => !/\.(xlsx|xml|pdf|zip)$/i.test(f.name))) return showError("支持 .xlsx、.xml、.pdf 和 .zip 文件。");
  if (files.some(f => !f.size || f.size > 10 * 1024 * 1024)) return showError("单个文件须非空且不超过 10MB。");
  if (files.reduce((n,f) => n+f.size,0) > 50 * 1024 * 1024) return showError("上传总大小不能超过 50MB。");
  clearMaterials();
  const request=materialRequest,owner=currentUser?.id;
  $("coBar").style.display = $("statRow").style.display = $("actions").style.display = $("filterBar").style.display = "none";
  $("hitSection").style.display = $("skipSection").style.display = $("passSection").style.display = "none";
  dz.style.display = "none"; $("busy").style.display = "block";
  try {
    const fd = new FormData(); files.forEach(f => fd.append("files", f));
    fd.append("extraction", $("extractionMode").value);
    let draft = await api("/api/materials/preview", {method:"POST",body:fd});
    if(!materialCurrent(request,owner))return;
    if (draft.job_id) {
      $("busy").textContent = "AI 正在提取并校验材料，请保持页面打开……";
      const jobId = draft.job_id, started = Date.now();
      for (;;) {
        await new Promise(resolve => setTimeout(resolve, 1500));
        if(!materialCurrent(request,owner))return;
        const job = await api("/api/materials/jobs/" + encodeURIComponent(jobId));
        if(!materialCurrent(request,owner))return;
        if (job.state === "failed") throw new Error(job.detail);
        if (job.state === "done") { draft = job.result; break; }
        if (Date.now() - started > 15 * 60 * 1000) throw new Error("提取任务等待超时，请稍后重新上传。");
      }
    }
    materialDraft=draft;renderMaterialReview(files);
  } catch(err) { if(materialCurrent(request,owner)){if($("uploadPanel").classList.contains("active"))showError(err.message);dz.style.display = "block";} }
  finally { if(materialCurrent(request,owner)){$("busy").style.display = "none"; $("busy").textContent = "正在处理材料，请稍候……";} }
}
function companyInputs(parent, values, locked = false) {
  const grid = el("div", "material-company"), inputs = {};
  [["name","企业名称"],["taxpayer_id","纳税人识别号"],["industry","所属行业"],["period","核对所属期（与账表保持一致）"]].forEach(([key,title]) => {
    const label = el("label", "", title), input = el("input");
    input.value = values[key] || ""; input.maxLength = 200; input.setAttribute("aria-label",title);
    input.readOnly = locked && Boolean(values[key]); inputs[key] = input; label.append(input); grid.append(label);
  });
  parent.append(grid); return () => Object.fromEntries(Object.entries(inputs).map(([k,v]) => [k,v.value.trim()]));
}
function checkLabel(parent, text) {
  const label = el("label","material-check"), input = el("input"); input.type = "checkbox";
  label.append(input, document.createTextNode(" " + text)); parent.append(label); return input;
}
function renderMaterialReview(files) {
  const request=materialRequest,owner=currentUser?.id,draft=materialDraft;
  const root = $("materialReview"); root.hidden = false;
  root.append(el("h2","","材料核对"),el("p","muted","先确认材料分组，再开始审计。核对草稿保留 10 分钟；材料不完整时，相应规则显示“未执行”。"));
  const mode = el("select"); mode.id = "materialMode"; mode.setAttribute("aria-label","审计方式");
  [["merge","同一企业、同一期间：合并为一次审计"],["separate","不同企业或不同期间：每个文件分别审计"]].forEach(([v,t]) => { const o=el("option","",t); o.value=v; mode.append(o); });
  root.append(mode);
  const scope = el("div"); const first = materialDraft.documents.find(d => !d.error);
  scope.append(el("p","muted","以下信息用于补全缺少企业信息的材料。已有信息不一致时，系统会阻止合并。"));
  const company = companyInputs(scope, first ? first.company : {});
  const sameScope = checkLabel(scope,"我已确认选中的材料属于同一企业、同一核对期间；历史指标已注明实际期间。不同企业材料请改用分别审计。");
  root.append(scope); mode.addEventListener("change",()=>scope.hidden=mode.value!=="merge");
  materialCards = [];
  materialDraft.documents.forEach(doc => {
    const card = el("div","material-card"); root.append(card);
    const selected = checkLabel(card,doc.name); selected.checked = !doc.error; selected.disabled = Boolean(doc.error);
    card.append(el("p",doc.error ? "material-note" : "muted",doc.error || doc.summary));
    if(doc.error) return;
    const editable = doc.kind === "pdf" || doc.review_required;
    const getCompany = companyInputs(card,doc.company,!editable);
    const state = {doc, selected, getCompany, rows:[]}; materialCards.push(state);
    doc.warnings.forEach(w => card.append(el("p","material-note",w)));
    if(editable) {
      const original = files.find(f=>f.name===doc.name);
      if(original && doc.kind === "pdf") { const url=URL.createObjectURL(original); originalUrls.push(url); const a=el("a","","打开原始 PDF 对照核对"); a.href=url; a.target="_blank"; a.rel="noopener"; card.append(a); }
      else card.append(el("p","muted","请打开本地原件对照核对；下方可查看提取文字及来源编号。"));
      const textDetail=el("details"), summary=el("summary","","查看提取的原文（按页）"); textDetail.append(summary);
      doc.pages.forEach(page => textDetail.append(el("h3","",page.label || "第 "+page.page+" 页"),el("pre","",page.text || "本页没有可提取文字，请查看原件。"))); card.append(textDetail);
      const wrap=el("div","material-table-wrap"), table=el("table","material-table"), head=el("thead"), tr=el("tr");
      ["标准指标 / 口径","数值（元；比率用小数）",doc.kind === "pdf" ? "页码" : "表编号","来源栏次及口径说明","操作"].forEach(t=>tr.append(el("th","",t))); head.append(tr); table.append(head);
      const tbody=el("tbody"); table.append(tbody); wrap.append(table); card.append(wrap);
      function addRow(row={}) {
        const tr=el("tr"), name=el("select"), value=el("input"), page=el("input"), detail=el("input"), remove=el("button","","移除");
        name.setAttribute("aria-label","标准指标"); value.setAttribute("aria-label","指标数值"); page.setAttribute("aria-label","来源页码"); detail.setAttribute("aria-label","口径说明");
        Object.entries(materialDraft.fields).sort(([a],[b])=>a.localeCompare(b,"zh")).forEach(([k,d])=>{const o=el("option","",k);o.value=k;o.title=d;name.append(o);});
        if(row.name) name.value=row.name;
        value.value=row.value ?? ""; value.placeholder="空白表示缺失"; value.inputMode="decimal";
        page.type="number"; page.min=1; page.max=doc.page_count; page.value=row.page || 1;
        detail.value=row.detail || ""; detail.maxLength=2000; detail.placeholder="例如：第1栏本期金额，不含税";
        name.title=materialDraft.fields[name.value]; name.addEventListener("change",()=>name.title=materialDraft.fields[name.value]);
        const entry={name,value,page,detail}; state.rows.push(entry);
        remove.addEventListener("click",()=>{tr.remove();state.rows=state.rows.filter(r=>r!==entry);});
        [name,value,page,detail,remove].forEach(n=>{const td=el("td");td.append(n);tr.append(td);});tbody.append(tr);
        if(row.ai_issues?.length) { const hint=el("p","material-note",row.ai_issues.join("；")); tr.cells[0].append(hint); }
        if(row.ai_raw_value !== undefined) tr.cells[1].append(el("p","muted",`AI 候选原值（待核对）：${row.ai_raw_value ?? "缺失"} ${row.ai_unit || ""}`));
      }
      doc.rows.forEach(addRow);
      const add=el("button","","＋ 手工添加未识别指标"); add.addEventListener("click",()=>addRow()); card.append(add);
      state.reviewed=checkLabel(card,"已确认提取结果及标注疑点：企业、期间、金额列和口径正确；金额统一为元，未确认的数值已清空或移除。");
    } else {
      const details=el("details"); details.append(el("summary","","查看已读取的报表及补充指标"));
      doc.rows.forEach(r=>details.append(el("p","muted",r.name+"："+r.value+" · "+r.source)));card.append(details);
      if(doc.invoices?.length){const inv=el("details");inv.append(el("summary","",`查看已读取的 ${doc.invoices.length} 张发票`));doc.invoices.forEach(r=>inv.append(el("p","muted",`${r.number} · ${r.issued_on} · ${r.kind||"待按企业税号判定"}${r.status} · 不含税 ${r.amount} · 税额 ${r.tax}`)));card.append(inv);}
      if(doc.bank_transactions?.length){const bank=el("details");bank.append(el("summary","",`查看已读取的 ${doc.bank_transactions.length} 笔银行流水（原始入账不直接作为收入）`));doc.bank_transactions.forEach(r=>bank.append(el("p","muted",`${r.transaction_id} · ${r.transacted_on} · ${Number(r.income)>0?"收入 "+r.income:"支出 "+r.expense} · ${r.category||"待调节底稿分类"}${r.summary?" · "+r.summary:""}`)));card.append(bank);}
      if(doc.bank_adjustments?.length){const bridge=el("details");bridge.append(el("summary","",`查看已读取的 ${doc.bank_adjustments.length} 条银行调节底稿`));doc.bank_adjustments.forEach(r=>bridge.append(el("p","muted",`${r.number} · ${r.transaction_id||"独立调节"} · ${r.category} · 权责期 ${r.period} · ${r.transaction_id?"本期不含税收入 "+(r.recognized_amount??"未填"):(r.direction||"未定方向")+" "+(r.adjustment_amount??"未填")} · 底稿 ${r.workpaper} · ${r.reviewed?"已复核":"待复核"}`)));card.append(bridge);}
      if(doc.human_records?.length){const human=el("details");human.append(el("summary","",`查看已读取的 ${doc.human_records.length} 条人力记录（不展示姓名及证件号码）`));doc.human_records.forEach(r=>human.append(el("p","muted",`${r.kind} · ${r.month} · ${r.active?"有效":"停保/作废等剔除"} · 金额 ${r.amount??"未提供"} · ${r.source}`)));card.append(human);}
      if(doc.contracts?.length){const contracts=el("details");contracts.append(el("summary","",`查看已读取的 ${doc.contracts.length} 份合同`));doc.contracts.forEach(r=>contracts.append(el("p","muted",`${r.number} · ${r.kind} · ${r.counterparty} · 含税 ${r.amount} · 履约期 ${r.performance_start} 至 ${r.performance_end} · ${r.reviewed?"已复核":"待复核"}${r.workpaper?" · 底稿 "+r.workpaper:""}`)));card.append(contracts);}
      if(doc.fulfillments?.length){const fulfillments=el("details");fulfillments.append(el("summary","",`查看已读取的 ${doc.fulfillments.length} 条履约记录`));doc.fulfillments.forEach(r=>fulfillments.append(el("p","muted",`${r.number} · 合同 ${r.contract_number} · ${r.fulfilled_on} · ${r.kind} · 含税 ${r.amount} · ${r.reviewed?"已复核":"待复核"}${r.workpaper?" · 底稿 "+r.workpaper:""}`)));card.append(fulfillments);}
      if(doc.contract_links?.length){const links=el("details");links.append(el("summary","",`查看已读取的 ${doc.contract_links.length} 条四流勾稽`));doc.contract_links.forEach(r=>links.append(el("p","muted",`${r.number} · 合同 ${r.contract_number} · 发票 ${r.invoice_number||"待补"} · 流水 ${r.transaction_id||"待补"} · 履约 ${r.fulfillment_number||"待补"} · 分摊含税 ${r.amount} · ${r.reviewed?"已复核":"待复核"}${r.workpaper?" · 底稿 "+r.workpaper:""}`)));card.append(links);}
      if(doc.related_graph){const graph=doc.related_graph,details=el("details");details.append(el("summary","",`查看关联方图：${graph.subjects.length} 个主体、${graph.relations.length} 条关系、${graph.trades.length} 笔交易`));graph.relations.forEach(r=>details.append(el("p","muted",`${r.key} · ${r.owner_key} → ${r.company_key} · ${r.kind} · ${r.start_on} 至 ${r.end_on||"持续"} · ${r.reviewed?"已复核":"待复核"} · ${r.basis||"无证据说明"} · ${r.source}`)));graph.trades.forEach(t=>details.append(el("p","muted",`${t.key} · ${t.seller_key} → ${t.buyer_key} · ${t.traded_on} · 含税 ${t.amount} · ${t.reviewed?"已复核":"待复核"} · ${t.anomaly_basis||"无异常依据"} · ${t.source}`)));card.append(details);}
    }
  });
  const submit=el("button","primary","确认材料并开始审计"), reset=el("button","","重新选择材料");
  reset.addEventListener("click",()=>{clearMaterials();hideError();dz.style.display="block";});
  submit.addEventListener("click",async()=>{
    if(submit.disabled||!materialCurrent(request,owner))return;
    hideError();
    const selected=materialCards.filter(c=>c.selected.checked);
    if(!selected.length) return showError("请至少选择一份有效材料。");
    if(mode.value==="merge" && !sameScope.checked) return showError("合并前请确认所有材料属于同一企业、同一核对期间。");
    if(selected.some(c=>(c.doc.kind==="pdf"||c.doc.review_required)&&!c.reviewed.checked)) return showError("请完成所选材料提取结果的核对确认。");
    const selections=selected.map(c=>({id:c.doc.id,company:c.getCompany(),reviewed:c.reviewed?.checked,
      rows:(c.doc.kind==="pdf"||c.doc.review_required)?c.rows.map(r=>({name:r.name.value,value:r.value.value.trim(),page:Number(r.page.value),detail:r.detail.value.trim()})):undefined}));
    submit.disabled=true; reset.disabled=true; $("busy").style.display="block";
    try {
      const result=await api("/api/materials/audit",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({token:draft.token,mode:mode.value,same_scope:sameScope.checked,company:company(),client_id:$("auditClient").value||null,selections})});
      if(!materialCurrent(request,owner)||!$("uploadPanel").classList.contains("active"))return;
      const box=$("batchResults"); box.replaceChildren(); box.hidden=false;
      box.append(el("h2","",`完成 ${result.results.length} 份审计，${result.errors.length} 组材料未通过`));
      if(result.errors.length)box.append(el("p","muted","成功组已独立保存；重传前请逐组核查历史档案，避免重复建档。"));
      result.errors.forEach(e=>box.append(el("p","material-note",e.files.join("、")+"："+e.detail)));
      result.results.forEach(item=>{const b=el("button","",item.audit.company.name+" · 查看报告");b.addEventListener("click",()=>renderResult(item.audit));box.append(b);});
      if(result.results.length) {
        root.hidden=true;
        $("auditPanel").insertBefore(box,$("coBar"));
        const again=el("button","","上传下一组材料");again.addEventListener("click",()=>$("btnNew").click());box.append(again);
        renderResult(result.results[0].audit);
      } else box.scrollIntoView({behavior:"smooth",block:"center"});
    } catch(err){if(materialCurrent(request,owner)&&$("uploadPanel").classList.contains("active"))showError(err.message);}
    finally{submit.disabled=false;reset.disabled=false;if(materialCurrent(request,owner))$("busy").style.display="none";}
  });
  root.append(submit,reset);
}

/* ---------- 渲染 ---------- */
function renderResult(j) {
  switchPanel("auditPanel");
  $("auditEmpty").style.display = "none";
  auditId = j.audit_id;
  lastResult = j;
  snapshotReturn = null;
  $("snapshotBack").hidden = true;
  loadRiskChanges(j.audit_id);
  $("coName").textContent = j.company.name;
  $("coTaxId").textContent = j.company.taxpayer_id;
  $("coIndustry").textContent = j.company.industry;
  $("coPeriod").textContent = j.company.period;
  $("coMetrics").textContent = j.metrics.length + " 项";
  renderMaterialEvidence(j.metrics);
  $("auditTime").textContent = "审计时间 " + j.audited_at;
  $("coBar").style.display = "block";

  $("nHit").textContent = j.summary.hit;
  $("nHigh").textContent = j.summary.high;
  $("nPass").textContent = j.summary.pass;
  $("nSkip").textContent = j.summary.skipped;
  $("statRow").style.display = "grid";
  $("filterBar").style.display = "block";
  $("actions").style.display = "flex";
  $("auditNarrativeSection").hidden = false;
  renderAuditNarrative(j.narrative || null);

  latestFindings = j.findings;
  const category = $("filterCategory"); category.textContent = ""; const allCategory = el("option",null,"全部"); allCategory.value=""; category.appendChild(allCategory);
  for (const value of Array.from(new Set(j.findings.map((f)=>f.category))).sort()) { const o=el("option",null,value); o.value=value; category.appendChild(o); }
  const tax = $("filterTax"); tax.textContent = ""; const allTax = el("option",null,"全部"); allTax.value=""; tax.appendChild(allTax);
  for (const value of Array.from(new Set(j.findings.map((f)=>f.tax_type))).sort()) { const o=el("option",null,value); o.value=value; tax.appendChild(o); }
  applyFilters();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function renderMaterialEvidence(metrics) {
  const box=$("materialEvidence");box.replaceChildren();
  const selected=(metrics||[]).filter(m=>/^(人力|个税|社保|公积金|合同)\./.test(m.name));
  box.hidden=!selected.length;
  if(!selected.length)return;
  box.append(el("summary","","人力与合同归集证据（"+selected.length+" 项）"),el("p","muted","以下为本次已保存的归集指标。四流完整仅表示已复核材料关联及金额勾稽完整，不等于税务合规；待完善不算通过。人力人数按证据所示核对月统计，不是期间人数累加。"));
  for(const metric of selected){const item=el("div","material-card");item.append(el("h3","",metric.name+"："+metric.value),el("p","muted",metric.detail||"未记录口径说明"),el("p","muted","来源："+(metric.source||"未记录")));box.append(item);}
}

function renderAuditNarrative(narrative) {
  const panel = $("auditNarrativePanel"), status = $("auditNarrativeStatus"), button = $("btnAuditNarrative");
  panel.replaceChildren();
  status.textContent = "";
  button.textContent = narrative ? "重新读取已保存内容" : "生成总体评价与建议";
  if (!narrative) {
    panel.appendChild(el("p", "muted", "尚未生成。确定性统计和逐条证据不受影响，生成后会同步进入 HTML/PDF 报告。"));
    return;
  }
  panel.appendChild(el("div", "audit-narrative-locked", `规则统计已锁定：命中 ${narrative.summary.hit} 项、通过 ${narrative.summary.pass} 项、未执行 ${narrative.summary.skipped} 项`));
  const addGroup = (title, items) => {
    panel.appendChild(el("h4", null, title));
    for (const item of items) {
      const block = el("div", "audit-narrative-paragraph");
      block.append(el("p", null, item.text), el("p", "audit-narrative-citations", "来源规则：" + item.citations.join("、")));
      panel.appendChild(block);
    }
  };
  addGroup("总体评价", narrative.overall_assessment);
  addGroup("处理建议", narrative.recommendations);
  panel.appendChild(el("p", "finding-ai-meta", `仅汇总，不改变规则结论 · ${narrative.model} · 证据版本 ${narrative.evidence_hash.slice(0,12)} · ${narrative.cached ? "已保存内容" : "本次生成"}`));
}

$("btnAuditNarrative").addEventListener("click", async () => {
  if (!auditId) return;
  const button = $("btnAuditNarrative"), status = $("auditNarrativeStatus");
  button.disabled = true;
  button.textContent = "正在基于 Finding 汇总……";
  status.textContent = "模型只负责报告文字，规则结论不会改变。";
  try {
    const narrative = await api("/api/audits/" + encodeURIComponent(auditId) + "/narrative", {method:"POST"});
    renderAuditNarrative(narrative);
    status.textContent = "总体评价已保存，并同步用于本次审计的 HTML/PDF 报告。";
  } catch (err) {
    status.textContent = err.message || String(err);
    button.textContent = "重试生成总体评价与建议";
  } finally {
    button.disabled = false;
  }
});

function applyFilters() {
  const filtered = latestFindings.filter((f) => (!$("filterStatus").value || f.status === $("filterStatus").value) && (!$("filterSeverity").value || f.severity === $("filterSeverity").value) && (!$("filterCategory").value || f.category === $("filterCategory").value) && (!$("filterTax").value || f.tax_type === $("filterTax").value));
  const hits = filtered.filter((f) => f.status === "hit");
  const skips = filtered.filter((f) => f.status === "skipped");
  const passes = filtered.filter((f) => f.status === "pass");

  $("hitCnt").textContent = "命中 " + hits.length + " 项，点击卡片展开完整证据链";
  renderCards($("hitList"), hits);
  renderCards($("skipList"), skips);
  renderCards($("passList"), passes);

  $("hitSection").style.display = hits.length ? "block" : "none";
  $("skipSection").style.display = skips.length ? "block" : "none";
  $("passSection").style.display = passes.length ? "block" : "none";
  $("passFoldText").textContent = "检查通过事项（" + passes.length + " 项）—— 已执行，未发现异常";
  $("passList").classList.remove("open");

}

for (const id of ["filterStatus","filterSeverity","filterCategory","filterTax"]) $(id).addEventListener("change",applyFilters);

/* 一张规则卡：head（可点击展开/收起）+ body（证据链） */
function renderCards(container, findings) {
  container.textContent = "";
  for (const f of findings) container.appendChild(findingCard(f));
}

function findingCard(f) {
  const card = el("div", "card");

  const head = el("div", "card-head");
  const sev = f.status === "hit" ? f.severity : (f.status === "skipped" ? "skip" : "pass");
  const sevText = f.status === "hit" ? f.severity_label : (f.status === "skipped" ? "未执行" : "通过");
  head.appendChild(el("span", "tag " + sev, sevText));
  head.appendChild(el("span", "rid", f.id));
  head.appendChild(el("span", "rname", f.name));
  if (f.status === "hit") head.appendChild(el("span", "concl", f.conclusion));
  head.appendChild(el("span", "arrow", "▶"));
  head.addEventListener("click", () => card.classList.toggle("open"));
  card.appendChild(head);

  const body = el("div", "card-body");

  /* 基本属性 */
  const kv = el("div", "kv");
  kv.appendChild(el("div", null, "风险类型：" + f.category + "　·　税种：" + f.tax_type + "　·　规则版本 v" + f.version + (f.effective_from ? "（" + f.effective_from + " 至 " + (f.effective_to || "持续") + "）" : "")));
  if (f.scope) kv.appendChild(el("div", null, "适用范围：" + f.scope));
  body.appendChild(kv);

  /* 未执行：原因置顶 */
  if (f.status === "skipped") {
    body.appendChild(el("div", "ev-title", "未执行原因（补齐以下材料后可复检）"));
    body.appendChild(el("div", "calc-box", f.skip_reason || f.conclusion));
  }

  /* 触发明细 */
  if (f.evidence && f.evidence.length) {
    body.appendChild(el("div", "ev-title", "触发明细（每项数值均标注取数来源）"));
    const tbl = el("table", "ev");
    const trh = el("tr");
    for (const h of ["项目", "数值", "取数来源"]) trh.appendChild(el("th", null, h));
    const thead = el("thead"); thead.appendChild(trh); tbl.appendChild(thead);
    const tb = el("tbody");
    for (const e of f.evidence) {
      const tr = el("tr", e.emphasis ? "emph" : null);
      tr.appendChild(el("td", null, e.label));
      tr.appendChild(el("td", "v num", e.value));
      tr.appendChild(el("td", "src", e.source));
      tb.appendChild(tr);
    }
    tbl.appendChild(tb);
    body.appendChild(tbl);
  }

  /* 计算过程 + 阈值 */
  if (f.calculation) {
    body.appendChild(el("div", "ev-title", "计算过程"));
    body.appendChild(el("div", "calc-box num", f.calculation));
  }
  if (f.threshold_desc) {
    body.appendChild(el("div", "ev-title", "判定阈值"));
    body.appendChild(el("div", "kv", f.threshold_desc + (f.threshold_basis ? "（" + f.threshold_basis + "）" : "")));
  }

  /* 法条依据 */
  if (f.legal_basis && f.legal_basis.length) {
    body.appendChild(el("div", "ev-title", "法律依据"));
    const ul = el("ul", "plain-list");
    for (const item of f.legal_basis) ul.appendChild(el("li", null, item));
    body.appendChild(ul);
  }

  /* 建议 */
  if (f.status === "hit" && f.suggestion) {
    body.appendChild(el("div", "ev-title", "整改建议"));
    body.appendChild(el("div", "calc-box", f.suggestion));
  }

  /* 命中 Finding 的受约束模型解读：规则结论始终由上方确定性证据卡负责。 */
  if (f.status === "hit") {
    const tools = el("div", "finding-ai-tools");
    const explain = el("button", "btn finding-ai-btn", "✦ AI 白话解读");
    explain.type = "button";
    const panel = el("div", "finding-ai-panel");
    panel.hidden = true;
    explain.addEventListener("click", async () => {
      if (!auditId) return;
      explain.disabled = true;
      explain.textContent = "正在基于证据解读……";
      panel.hidden = false;
      panel.replaceChildren(el("p", "muted", "模型只翻译当前 Finding，不参与风险判定。"));
      try {
        const result = await api("/api/audits/" + encodeURIComponent(auditId) + "/findings/" + encodeURIComponent(f.id) + "/interpretation", {method:"POST"});
        panel.replaceChildren();
        const locked = el("div", "finding-ai-locked", "规则引擎结论已锁定：命中（" + result.citation + "）");
        panel.appendChild(locked);
        panel.appendChild(el("div", "ev-title", "业务白话"));
        panel.appendChild(el("p", null, result.plain_language));
        panel.appendChild(el("div", "ev-title", "为什么触发"));
        panel.appendChild(el("p", null, result.why_flagged));
        panel.appendChild(el("div", "ev-title", "建议核对顺序"));
        const ol = el("ol", "finding-ai-steps");
        for (const step of result.review_steps) ol.appendChild(el("li", null, step));
        panel.appendChild(ol);
        panel.appendChild(el("p", "finding-ai-meta", "仅解释，不改变规则结论 · " + result.model + " · " + (result.cached ? "已保存解读" : "本次生成")));
        explain.textContent = "已生成 AI 白话解读";
      } catch (err) {
        panel.replaceChildren(el("div", "finding-ai-error", err.message || String(err)));
        explain.textContent = "重试 AI 白话解读";
      } finally {
        explain.disabled = false;
      }
    });
    tools.appendChild(explain);
    tools.appendChild(el("span", "finding-ai-note", "仅解释，不改变规则结论"));
    body.appendChild(tools);
    body.appendChild(panel);
  }

  /* 参考链接 */
  if (f.references && f.references.length) {
    body.appendChild(el("div", "ev-title", "官方参考"));
    const ul = el("ul", "plain-list");
    for (const url of f.references) {
      const li = el("li");
      const a = el("a", null, url); a.href = url; a.target = "_blank"; a.rel = "noopener";
      li.appendChild(a); ul.appendChild(li);
    }
    body.appendChild(ul);
  }

  card.appendChild(body);
  return card;
}

/* ---------- 折叠 / 导出 / 重来 ---------- */
$("passFold").addEventListener("click", () => {
  $("passFold").classList.toggle("open");
  $("passList").classList.toggle("open");
});

$("btnPdf").addEventListener("click", async () => {
  if (!auditId) return;
  const btn = $("btnPdf"); btn.disabled = true; btn.textContent = "正在生成 PDF……";
  try {
    const confirmed = currentUser && currentUser.role === "accountant" ? window.confirm("确认导出该客户报告并记录本次导出行为？") : true;
    if (!confirmed) return;
    const r = await fetch("/api/report/" + auditId + (currentUser && currentUser.role === "accountant" ? "?confirm=true" : ""));
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      throw new Error(j.detail || "PDF 生成失败");
    }
    const blob = await r.blob();
    const a = el("a"); a.href = URL.createObjectURL(blob);
    a.download = "税务风险审计报告.pdf";
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  } catch (err) {
    showError(err.message || String(err));
  } finally {
    btn.disabled = false; btn.textContent = "导出 A4 PDF 报告";
  }
});

$("btnNew").addEventListener("click", () => {
  switchPanel("uploadPanel");
  $("auditEmpty").style.display = "block";
  clearMaterials();
  auditId = null;
  lastResult = null;
  snapshotReturn = null;
  $("snapshotBack").hidden = true;
  latestFindings = [];
  $("coBar").style.display = $("statRow").style.display = $("actions").style.display = $("filterBar").style.display = "none";
  $("auditNarrativeSection").hidden = true;
  $("auditNarrativePanel").replaceChildren();
  $("auditNarrativeStatus").textContent = "";
  $("hitSection").style.display = $("skipSection").style.display = $("passSection").style.display = "none";
  $("hitList").textContent = $("skipList").textContent = $("passList").textContent = "";
  hideError();
  dz.style.display = "block";
  window.scrollTo({ top: 0, behavior: "smooth" });
});

/* ---------- P1 登录、档案、实训与管理 ---------- */
// Purge credentials saved by earlier versions; passwords are never stored in browser storage.
try { localStorage.removeItem("taxpearls.rememberedLogin.v1"); } catch {}
$("pwToggle").addEventListener("click", () => {
  const input = $("loginPass");
  const show = input.type === "password";
  input.type = show ? "text" : "password";
  document.querySelector("#pwToggle .pw-eye").style.display = show ? "none" : "";
  document.querySelector("#pwToggle .pw-eye-off").style.display = show ? "" : "none";
  $("pwToggle").setAttribute("aria-label", show ? "隐藏密码" : "显示密码");
  $("pwToggle").setAttribute("aria-pressed", String(show));
});

let pendingEmailToken="",emailRegistrationProof=null,authResetProof="",authViewVersion=0,authSubmitBusy=false;
async function bootstrap() {
  const params = new URLSearchParams(location.search);
  pendingEmailToken = new URLSearchParams(location.hash.slice(1)).get("email") || params.get("reset") || "";
  let pathInvite = "";
  if(location.pathname.startsWith("/p/")){
    try{pathInvite=decodeURIComponent(location.pathname.slice(3));}catch{history.replaceState(null,"","/");}
  }
  const inviteCode = (pathInvite || params.get("invite") || "").trim();
  if (pendingEmailToken) history.replaceState(null, "", "/");
  if (inviteCode) {
    history.replaceState(null, "", "/");
    $("signupCode").value = inviteCode;
    $("authForm").dataset.inviteCode = inviteCode;
    $("authForm").dataset.inviteFromUrl = "1";
  }
  const s = await api("/api/status");
  const setup = s.needs_setup;
  $("authForm").dataset.setup = setup ? "1" : "0";
  $("loginPass").autocomplete = setup ? "new-password" : "current-password";
  if (pendingEmailToken && !setup) return showAuthView("emailMagic");
  if (s.user) return enterApp(s.user);
  if (inviteCode && !setup) return showAuthView("signup");
  showAuthView("login");
}

const authViews = ["viewLogin", "viewForgot", "viewReset", "viewSignup", "viewEmailLogin", "viewEmailMagic"];
const authViewMap = {
  login: "viewLogin", forgot: "viewForgot", reset: "viewReset", signup: "viewSignup",
  emailLogin:"viewEmailLogin",emailMagic:"viewEmailMagic",
};
const authViewLabels = {
  login: ["登录税海拾珠", "登录", ""],
  emailLogin:["邮箱登录","验证并登录","在当前浏览器申请并使用验证码或邮件链接。"],
  emailMagic:["确认邮件验证","确认本人操作并继续","不会在打开链接时自动登录或修改密码。"],
  forgot: ["找回密码", "发送重置邮件", ""],
  reset: ["设置新密码", "设置新密码", ""],
  signup: ["邀请码开户", "注册", "使用平台或机构管理员提供的邀请码完成注册。"],
};
function showAuthView(view) {
  authViewVersion++;
  const form = $("authForm");
  const setup = form.dataset.setup === "1";
  const [title, submitText, step] = authViewLabels[view] || authViewLabels.login;
  $("loginUserLabel").textContent = setup ? "用户名" : "邮箱（兼容原用户名）";
  $("loginUser").placeholder = setup ? "设置管理员用户名" : "name@example.com";
  $("loginUser").inputMode = setup ? "text" : "email";
  $("loginRememberField").hidden = setup || view !== "login";
  $("loginRemember").disabled = setup || view !== "login";
  (view==="emailLogin"?$("emailLoginVerification"):$("viewSignup")).append($("emailVerificationFields"));
  authViews.forEach((id) => {
    const on = id === authViewMap[view];
    $(id).style.display = on ? "" : "none";
    $(id).querySelectorAll("input").forEach((i) => { i.required = on; });
  });
  $("forgotLink").style.display = ["login","emailLogin"].includes(view)&&!setup ? "" : "none";
  $("signupEntryLink").style.display = ["login","emailLogin"].includes(view)&&!setup ? "" : "none";
  $("emailLoginEntryLink").style.display = view==="login"&&!setup?"":"none";
  $("passwordLoginEntryLink").style.display = view==="emailLogin"&&!setup?"":"none";
  $("backToLogin").style.display = ["login","emailLogin"].includes(view) ? "none" : "";
  $("authStep").textContent = setup ? "" : step;
  $("authTitle").textContent = setup ? "初始化平台管理员" : title;
  $("authSubmit").textContent = setup ? "创建管理员" : submitText;
  $("authSubmit").disabled=authSubmitBusy;
  form.dataset.view = view;
  // Captcha gates email delivery, not final registration (which proves the email).
  $("captchaAnswer").required = false;
  const verified=view==="signup"&&!!emailRegistrationProof;
  $("emailVerificationFields").hidden=verified;$("signupEmailCode").required=!verified&&["signup","emailLogin"].includes(view);
  $("signupProofStatus").hidden=!verified;$("signupEmail").readOnly=verified;
  $("signupInviteField").hidden=verified&&emailRegistrationProof.has_invite;
  $("signupCode").required=view==="signup"&&!$("signupInviteField").hidden;
  if(verified){
    $("signupEmail").value=emailRegistrationProof.email;
    $("signupProofStatus").textContent=emailRegistrationProof.has_invite
      ?"邮箱已验证，将使用发起请求时绑定的邀请完成注册。请设置密码；刷新或关闭此页后需重新申请验证。"
      :"邮箱已验证，请填写邀请码并设置密码。刷新或关闭此页后需重新申请验证。";
    if(emailRegistrationProof.has_invite)$("signupCode").value="";
  }
  if(view==="emailLogin"||(view==="signup"&&!verified))loadCaptcha();
}
$("forgotLink").addEventListener("click", () => showAuthView("forgot"));
$("signupEntryLink").addEventListener("click", () => {emailRegistrationProof=null;showAuthView("signup");});
$("emailLoginEntryLink").addEventListener("click",()=>showAuthView("emailLogin"));
$("passwordLoginEntryLink").addEventListener("click",()=>showAuthView("login"));
$("backToLogin").addEventListener("click", () => {emailRegistrationProof=null;authResetProof="";pendingEmailToken="";showAuthView("login");});

let captchaBusy = false;
async function loadCaptcha() {
  if (captchaBusy) return;
  captchaBusy = true;
  updateSignupSendButton();
  const img = $("captchaImage");
  img.style.opacity = ".45";
  try {
    const result = await api("/api/auth/captcha");
    $("authForm").dataset.captchaId = result.captcha_id;
    img.src = result.image;
    img.alt = "人机验证码，看不清可点击刷新";
    $("captchaAnswer").value = "";
    $("captchaAnswer").disabled = false;
  } catch (err) {
    $("authForm").dataset.captchaId = "";
    img.removeAttribute("src");
    img.alt = "验证码加载失败，点击刷新 ↻";
    $("captchaAnswer").disabled = true;
  } finally { captchaBusy = false; img.style.opacity = "1"; updateSignupSendButton(); }
}
$("captchaImage").addEventListener("click", loadCaptcha);
$("captchaRefresh").addEventListener("click", loadCaptcha);

let sendCooldown = 0,signupSendBusy=false;
function emailAddressInput(){return $("authForm").dataset.view==="emailLogin"?$("emailLoginAddress"):$("signupEmail");}
function updateSignupSendButton(){
  const emailInput=emailAddressInput();
  $("signupSendBtn").disabled=signupSendBusy||captchaBusy||sendCooldown>0||!$("authForm").dataset.captchaId||!$("captchaAnswer").value.trim()||!emailInput.value.trim()||!emailInput.validity.valid;
}
$("signupEmail").addEventListener("input",updateSignupSendButton);
$("emailLoginAddress").addEventListener("input",updateSignupSendButton);
$("captchaAnswer").addEventListener("input",updateSignupSendButton);
function startCooldown(seconds) {
  sendCooldown = seconds;
  const btn = $("signupSendBtn");
  btn.disabled = true;
  const tick = () => {
    if (sendCooldown <= 0) { btn.textContent = "获取验证码"; updateSignupSendButton(); return; }
    btn.textContent = `重新发送(${sendCooldown}s)`;
    sendCooldown -= 1;
    setTimeout(tick, 1000);
  };
  tick();
}

$("signupSendBtn").addEventListener("click", async () => {
  if(signupSendBusy||sendCooldown>0)return;
  const form = $("authForm");
  const email = emailAddressInput().value.trim(),version=authViewVersion;
  const purpose=form.dataset.view==="emailLogin"?"login":"register";
  const captchaId = form.dataset.captchaId || "";
  const captchaAnswer = $("captchaAnswer").value.trim();
  if (!email) { showToast("请先填写邮箱。"); return; }
  if (!captchaId || !captchaAnswer) { showToast("请先完成人机验证。"); return; }
  signupSendBusy=true;updateSignupSendButton();
  try {
    const result = await api("/api/auth/email/start", {method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({email, captcha_id:captchaId, captcha_answer:captchaAnswer,purpose,invite_code:purpose==="register"?$("signupCode").value.trim():""})});
    if(version!==authViewVersion||email!==emailAddressInput().value.trim())return;
    if (result.exists) {
      $("signupEmailHint").textContent = "该邮箱已注册，请直接登录；忘记密码可用登录页的「忘记密码？」找回。";
      return;
    }
    $("signupEmailHint").textContent = result.message || "验证码已发送，10 分钟内有效，输错 5 次作废。";
    startCooldown(60);
    loadCaptcha();
  } catch (err) {
    if(version===authViewVersion){showToast(err.message);loadCaptcha();}
  } finally {signupSendBusy=false;updateSignupSendButton();}
});

$("authForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  if(authSubmitBusy)return;
  const form = e.currentTarget;
  const view = form.dataset.view || "login";
  const version=authViewVersion;
  authSubmitBusy=true;$("authSubmit").disabled=true;
  try {
    if(view==="emailMagic"){
      const result=await api("/api/auth/email/verify",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({token:pendingEmailToken})});
      if(version!==authViewVersion)return;
      pendingEmailToken="";
      if(result.purpose==="login"){enterApp(result.user);return;}
      if(result.purpose==="reset"){authResetProof=result.proof;showAuthView("reset");return;}
      emailRegistrationProof=result;showAuthView("signup");return;
    }
    if(view==="emailLogin"){
      const result=await api("/api/auth/email/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({email:$("emailLoginAddress").value.trim(),code:$("signupEmailCode").value.trim()})});
      if(version===authViewVersion)enterApp(result.user);return;
    }
    if (view === "forgot") {
      const result = await api("/api/auth/password/reset", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({email:$("resetEmail").value})});
      if(version===authViewVersion)showToast(result.message || "已提交，请查收邮箱。", "success");
      return;
    }
    if (view === "reset") {
      if ($("newPass").value !== $("newPass2").value) throw new Error("两次输入的新密码不一致。");
      await api("/api/auth/password/reset/confirm", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({token:authResetProof, password:$("newPass").value})});
      if(version!==authViewVersion)return;
      history.replaceState(null, "", location.pathname);
      authResetProof="";$("newPass").value="";$("newPass2").value="";
      showAuthView("login");
      showToast("密码已重置，请使用新密码登录。", "success");
      return;
    }
    if (view === "signup") {
      if ($("signupPass").value !== $("signupPass2").value) throw new Error("两次输入的密码不一致。");
      if (!emailRegistrationProof&&!$("signupEmailCode").value.trim()) throw new Error("请填写邮箱验证码。");
      await api("/api/register/complete", {method:"POST", headers:{"Content-Type":"application/json"},
        body:JSON.stringify({email:$("signupEmail").value.trim(), code:$("signupEmailCode").value.trim(),
                             invite_code:$("signupCode").value.trim(), password:$("signupPass").value,email_proof:emailRegistrationProof?.proof||""})});
      const user = await api("/api/me");
      if(version===authViewVersion)enterApp(user);
      return;
    }
    const setup = form.dataset.setup === "1";
    const body = {username: $("loginUser").value.trim(), password: $("loginPass").value, remember: !setup && $("loginRemember").checked};
    if (setup) body.display_name = body.username;
    await api(setup ? "/api/setup" : "/api/login", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
    if (setup) { form.dataset.setup = "0"; await api("/api/login", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({username:body.username,password:body.password})}); }
    const user = await api("/api/me");
    $("loginPass").value = "";
    if(version===authViewVersion)enterApp(user);
  } catch (err) { if(version===authViewVersion)showToast(err.message); }
  finally{authSubmitBusy=false;$("authSubmit").disabled=false;}
});

function enterApp(user) {
  $("loginRemember").checked = false;
  authViewVersion++;pendingEmailToken="";emailRegistrationProof=null;authResetProof="";
  for(const id of ["signupPass","signupPass2","newPass","newPass2","signupEmailCode","loginPass"])$(id).value="";
  trainingLoadRequest++; exerciseOpenRequest++; sandboxFeedbackRequest++; openAssignmentId=null;
  clearTimeout(exerciseDeadlineTimer); classroomUI.reset(); $("exerciseBox").style.display="none";
  $("exerciseGenerationResult").textContent=""; $("exerciseGenerationResult").hidden=true;
  currentUser = user;
  memberRequest++; memberSnapshot=null; $("memberOrg").textContent=""; $("userList").textContent="";
  clearMemberInvitations(); $("signupCode").value=""; delete $("authForm").dataset.inviteCode;
  $("memberQuotaStatus").textContent="尚未加载成员信息。"; $("memberQuotaEditor").hidden=true;
  $("authScreen").style.display = "none";
  $("appHeader").style.display = "block"; $("appMain").style.display = "block"; $("appFooter").style.display = "block";
  $("nav").style.display = "flex";
  $("currentUser").textContent = user.display_name + " · " + ({teacher:"教师",student:"学生",org_admin:"机构管理员",accountant:"会计",platform_admin:"平台管理员"}[user.role] || user.role);
  document.querySelectorAll(".training-nav").forEach((n) => n.style.display = ["teacher","student"].includes(user.role) ? "block" : "none");
  document.querySelectorAll(".admin-nav").forEach((n) => n.style.display = ["platform_admin","org_admin","teacher","accountant"].includes(user.role) ? "block" : "none");
  $("teacherBox").style.display = user.role === "teacher" ? "block" : "none";
  $("orgSettingsAdmin").style.display = user.role === "org_admin" ? "block" : "none";
  $("inviteAdmin").style.display = user.role === "platform_admin" ? "block" : "none";
  $("createUserRow").style.display = user.role === "org_admin" ? "flex" : "none";
  $("userAdmin").style.display = ["platform_admin","org_admin"].includes(user.role) ? "block" : "none";
  $("clientAdmin").style.display = user.role === "org_admin" ? "block" : "none";
  $("ruleAdmin").style.display = ["platform_admin","org_admin","teacher","accountant"].includes(user.role) ? "block" : "none";
  $("ruleAdmin").querySelector("h2").textContent = user.role === "platform_admin" ? "规则维护与版本发布" : "规则参数与试跑";
  $("ruleAdmin").querySelector("p.muted").textContent = user.role === "platform_admin" ? "平台维护规则定义、启停与版本，不读取或试跑客户审计。请依据已核对的变更发布。" : "在自己有权访问的历史材料上试跑草稿，不改变冻结审计；正式规则版本仅由平台管理员发布。";
  $("logAdmin").style.display = ["platform_admin","org_admin","teacher"].includes(user.role) ? "block" : "none";
  $("auditClientField").style.display = ["org_admin","accountant"].includes(user.role) ? "block" : "none";
  const businessUser = !["student","platform_admin"].includes(user.role);
  document.querySelector('[data-panel="auditPanel"]').style.display = businessUser ? "block" : "none";
  document.querySelectorAll(".business-nav, [data-panel='historyPanel'], [data-panel='notificationPanel']").forEach(n => n.style.display = businessUser ? "block" : "none");
  document.querySelector('[data-panel="orgReportPanel"]').style.display = user.role === "org_admin" ? "block" : "none";
  if (user.role === "student") switchPanel("trainingPanel"); else if(user.role === "platform_admin") switchPanel("adminPanel"); else switchPanel("dashboardPanel");
}

$("btnLogout").addEventListener("click", async () => { await api("/api/logout", {method:"POST"}); location.reload(); });
document.querySelectorAll("#nav [data-panel]").forEach((b) => b.addEventListener("click", () => switchPanel(b.dataset.panel)));

function noticeCurrent(request, owner) {
  return request === noticeRequest && currentUser?.id === owner && $("notificationPanel").classList.contains("active");
}
function noticeControls(loading = false) {
  const disabled = loading || noticeBusy || !noticeReady;
  for (const id of ["noticeCompleted", "noticeHigh", "noticeSave"]) $(id).disabled = disabled;
  $("noticeEmail").disabled = disabled || !noticeHasEmail;
  $("noticeRefresh").disabled = noticeBusy;
  document.querySelectorAll("#noticeList button, #noticeRecipients button, #noticeRecipients input").forEach(n => { n.disabled = disabled; });
}
async function noticeAction(action, message = "", openAudit = false) {
  if (noticeBusy || !noticeReady || !$("notificationPanel").classList.contains("active")) return;
  noticeBusy = true;
  noticeReady = false;
  const request = ++noticeRequest, owner = currentUser?.id;
  let interrupted = false;
  noticeControls();
  try {
    const result = await action();
    if (!noticeCurrent(request, owner)) { interrupted = true; return; }
    if (openAudit) { renderResult(result); switchPanel("auditPanel"); return; }
    noticeBusy = false;
    if (await loadNotifications()) $("noticeStatus").textContent += " " + message;
  } catch (error) {
    interrupted = !noticeCurrent(request, owner);
    if (noticeCurrent(request, owner)) {
      noticeReady = false;
      $("noticeStatus").textContent = "操作未确认，请刷新核对后再试：" + error.message;
    }
  } finally {
    noticeBusy = false;
    noticeControls();
    if (interrupted && currentUser?.id === owner && $("notificationPanel").classList.contains("active")) await loadNotifications();
  }
}
async function loadNotifications() {
  if (!currentUser || noticeBusy || !$("notificationPanel").classList.contains("active")) return false;
  const request = ++noticeRequest, owner = currentUser.id, admin = currentUser.role === "org_admin";
  noticeReady = false;
  noticeControls(true);
  $("noticeList").replaceChildren(); $("noticeRecipients").replaceChildren(); $("noticeRecipientBox").hidden = true;
  $("noticeStatus").textContent = "正在读取通知与订阅……";
  try {
    const [prefs, notices, members] = await Promise.all([api("/api/notifications/preferences"),
      api("/api/notifications"), admin ? api("/api/notifications/recipients") : Promise.resolve([])]);
    if (!noticeCurrent(request, owner)) return false;
    noticeHasEmail = prefs.has_email;
    $("noticeCompleted").checked = prefs.audit_completed;
    $("noticeHigh").checked = prefs.high_risk;
    $("noticeEmail").checked = prefs.email_enabled;
    $("noticeStatus").textContent = !prefs.has_email ? "账号未绑定邮箱，仅可接收站内通知。" :
      (prefs.delivery_enabled ? "邮件发送服务已启用。" : "邮件发送服务未启用：订阅仍会保存，邮件留在队列中。") + " 退订不撤回已受理邮件。";
    const box = $("noticeList"); box.textContent = "";
    const labels = {pending:"待发送",claimed:"发送中",accepted:"邮件已受理（非送达）",failed:"发送失败",uncertain:"发送结果未知",suppressed:"邮件已取消"};
    for (const item of notices) {
      const card = el("div", "sheet");
      card.append(el("h3", null, (item.event === "high_risk" ? "高风险提醒" : "审计完成") + (item.read_at ? " · 已读" : " · 未读")),
        el("p", "muted", item.created_at + " · " + item.audit_id),
        el("p", null, `风险 ${item.summary.hit_count} 项，高风险 ${item.summary.high_count} 项 · ${labels[item.email_status] || "仅站内通知"}`));
      for (const risk of item.summary.risks) card.append(el("p", null, risk.id + "：" + risk.name));
      const open = el("button", "btn pdf", "查看审计");
      open.onclick = () => noticeAction(async () => { const result = await api("/api/audits/" + item.audit_id); await api("/api/notifications/" + item.id + "/read", {method:"PUT"}); return result; }, "", true);
      card.append(open);
      if (!item.read_at) { const read = el("button", "btn re", "标为已读"); read.onclick = () => noticeAction(() => api("/api/notifications/" + item.id + "/read", {method:"PUT"})); card.append(read); }
      if (["failed", "uncertain"].includes(item.email_status)) { const retry = el("button", "btn re", "申请邮件重试"); retry.onclick = () => noticeAction(() => api("/api/notifications/" + item.id + "/retry", {method:"POST"}), "已申请重试，发送结果请刷新查看。"); card.append(retry); }
      box.append(card);
    }
    if (!box.childElementCount) box.append(el("p", "muted", "暂无通知。订阅只影响之后的审计。"));
    $("noticeRecipientBox").hidden = !admin;
    if (admin) {
      const recipients = $("noticeRecipients"); recipients.textContent = "";
      for (const member of members) {
        const row = el("div", "row"); row.append(el("span", null, member.display_name + " · " + member.org_id));
        const inputs = {};
        for (const [key, title] of [["audit_completed","审计完成"],["high_risk","高风险"]]) {
          const label = el("label"); const input = el("input"); input.type = "checkbox"; input.checked = member[key]; inputs[key] = input; label.append(input, document.createTextNode(title)); row.append(label);
        }
        row.append(el("span", "muted", member.email_enabled ? "本人已同意邮件" : "本人未开邮件"));
        const save = el("button", "btn re", "保存接收事件"); save.onclick = () => noticeAction(() => api("/api/notifications/recipients/" + member.id, {method:"PUT", headers:{"Content-Type":"application/json"}, body:JSON.stringify({audit_completed:inputs.audit_completed.checked, high_risk:inputs.high_risk.checked, email_enabled:member.email_enabled})}), "接收事件已保存。"); row.append(save); recipients.append(row);
      }
    }
    noticeReady = true;
    return true;
  } catch(e) { if (noticeCurrent(request, owner)) $("noticeStatus").textContent = "读取失败，请刷新重试：" + e.message; return false; }
  finally { if (noticeCurrent(request, owner)) noticeControls(); }
}
$("noticeRefresh").onclick = loadNotifications;
$("noticeSave").onclick = () => noticeAction(() => api("/api/notifications/preferences", {method:"PUT", headers:{"Content-Type":"application/json"}, body:JSON.stringify({audit_completed:$("noticeCompleted").checked, high_risk:$("noticeHigh").checked, email_enabled:$("noticeEmail").checked})}), "订阅已保存。");

let currentRiskChanges = null;
let riskChangesRequest = 0;
async function loadRiskChanges(id, baseline = "") {
  const request = ++riskChangesRequest;
  currentRiskChanges = null;
  $("riskChangesSection").hidden = false;
  $("riskBaseline").disabled = true;
  $("riskChangesStatus").textContent = "正在比较已冻结的审计快照……";
  $("riskChangesList").textContent = "";
  $("riskBaselineOpen").hidden = true;
  try {
    const result = await api("/api/audits/" + encodeURIComponent(id) + "/changes" + (baseline ? "?baseline_id=" + encodeURIComponent(baseline) : ""));
    if (auditId !== id || request !== riskChangesRequest) return;
    currentRiskChanges = result;
    const select = $("riskBaseline"); select.textContent = "";
    for (const candidate of result.baselines) { const option = el("option", null, candidate.period + " · " + candidate.audited_at + " · " + candidate.id.slice(0,8)); option.value = candidate.id; select.append(option); }
    select.disabled = result.status !== "ready";
    if (result.status !== "ready") { $("riskChangesStatus").textContent = result.message; return; }
    select.value = result.baseline.id;
    $("riskBaselineOpen").hidden = false;
    $("riskChangesStatus").textContent = `基期 ${result.baseline.period} → 本期 ${result.current.period}；新增 ${result.counts.new}、消失 ${result.counts.resolved}、偏离恶化 ${result.counts.worsened}、偏离改善 ${result.counts.improved}、无法比较 ${result.counts.incomparable}、口径变化 ${result.counts.rule_changed}。` + (result.gap ? " 两期不连续，中间变化未知。" : "");
    renderRiskChanges();
  } catch (error) { if (auditId === id && request === riskChangesRequest) { currentRiskChanges = null; $("riskChangesStatus").textContent = "跨期比较失败：" + error.message; } }
}
function renderRiskChanges() {
  const box = $("riskChangesList"); box.textContent = "";
  if (!currentRiskChanges || currentRiskChanges.status !== "ready") return;
  for (const item of currentRiskChanges.items) {
    const filter = $("riskChangeFilter").value;
    if (filter === "attention" && ["clear", "persistent", "incomparable"].includes(item.change)) continue;
    if (!["attention", "all"].includes(filter) && item.change !== filter) continue;
    const card = el("div", "sheet");
    card.append(el("h3", null, item.label + " · " + item.rule_id + " " + item.name),
      el("p", "muted", `基期 ${item.before_status} / v${item.before_version || "无"} → 本期 ${item.after_status} / v${item.after_version || "无"}`), el("p", null, item.reason));
    if (item.before_distance !== null && item.after_distance !== null) card.append(el("p", null, "偏离值 " + item.before_distance + " → " + item.after_distance));
    box.append(card);
  }
  if (!box.childElementCount) box.append(el("p", "muted", "所选类型无变化；可切换全部规则或无法比较查看完整状态。"));
}
$("riskBaseline").onchange = () => loadRiskChanges(auditId, $("riskBaseline").value);
$("riskChangeFilter").onchange = renderRiskChanges;
$("riskBaselineOpen").onclick = async () => {
  try {
    const origin = snapshotReturn || lastResult;
    renderResult(await api("/api/audits/" + encodeURIComponent(currentRiskChanges.baseline.id)));
    snapshotReturn = origin;
    $("snapshotBack").hidden = false;
  } catch(e) { showError(e.message); }
};
$("snapshotBack").onclick = () => { if (snapshotReturn) renderResult(snapshotReturn); };

let orgOverview = null, orgOverviewRequest = 0, orgSelectedClients = new Set();
// Use a dedicated entry, leaving the existing single-client workspace intact.
const orgReportNav = el("button","org-report-nav"); orgReportNav.dataset.panel = "orgReportPanel";
orgReportNav.setAttribute("aria-label","机构总览"); orgReportNav.title = "机构总览";
orgReportNav.append(document.querySelector('[data-panel="historyPanel"] svg').cloneNode(true),el("span","nav-label","机构总览"));
orgReportNav.style.display = "none";
document.querySelector('[data-panel="historyPanel"]').after(orgReportNav);
orgReportNav.onclick = () => switchPanel("orgReportPanel");
function orgSummary(data) {
  const t = data.totals;
  return `所选范围 ${t.clients} 家；有审计 ${t.audited} 家，无匹配审计 ${t.no_audit} 家；有高等级命中 ${t.high} 家、中等级命中 ${t.medium} 家、低等级命中 ${t.low} 家；全部检查通过 ${t.clear} 家，未命中但材料不足 ${t.incomplete} 家；命中 ${t.hit} 条 / 通过 ${t.pass} 条 / 未执行 ${t.skipped} 条。`;
}
function orgWarnings(data) {
  const notes = [];
  if (data.mixed_periods) notes.push("客户业务期间不同，仅汇总线索，不作同期财务比较。");
  if (data.mixed_rule_sets) notes.push("规则集合或定义不同，按冻结口径分别统计，不比较命中率。");
  const e=data.excluded_audits;
  notes.push(`机构历史未纳入候选：未关联当前客户档案 ${e.unlinked} 份、主体不匹配 ${e.identity_mismatch} 份、无法识别期间 ${e.invalid_period} 份。`);
  return notes.join(" ");
}
function updateOrgSelection() {
  $("orgReportStatus").textContent = `已选择 ${orgSelectedClients.size} 家客户；生成时再次读取当前快照，以生成后的清单为准。`;
  $("orgReportCreate").disabled = !orgSelectedClients.size;
  if (!orgOverview) return;
  const rows=orgOverview.rows.filter(row=>orgSelectedClients.has(row.client_id)),totals=Object.fromEntries(Object.keys(orgOverview.totals).map(key=>[key,0]));
  totals.clients=rows.length;
  for(const row of rows) { totals[row.state]++;if(row.audit_id)totals.audited++;for(const key of ["hit","pass","skipped"])totals[key]+=row.counts[key]; }
  const selected={...orgOverview,rows,totals,
    mixed_periods:new Set(rows.filter(row=>row.audit_id).map(row=>row.period_start+"/"+row.period_end)).size>1,
    mixed_rule_sets:new Set(rows.filter(row=>row.audit_id).map(row=>JSON.stringify([...row.rules].sort((a,b)=>a.id.localeCompare(b.id))))).size>1};
  $("orgReportSummary").textContent=orgSummary(selected);$("orgReportWarnings").textContent=orgWarnings(selected);
}
async function loadOrgOverview() {
  const request=++orgOverviewRequest;
  orgOverview=null; orgSelectedClients.clear(); $("orgReportClients").textContent="";
  $("orgReportCreate").disabled=true; $("orgReportStatus").textContent="正在读取机构总览……";
  try {
    const data=await api("/api/org/overview?period="+encodeURIComponent($("orgReportPeriod").value));
    if (request!==orgOverviewRequest) return;
    orgOverview=data; orgSelectedClients=new Set(data.rows.map(row=>row.client_id));
    const select=$("orgReportPeriod"), current=select.value; select.replaceChildren();
    const latest=el("option",null,"各客户最新业务期间"); latest.value=""; select.append(latest);
    for(const p of data.available_periods) { const option=el("option",null,p.label); option.value=p.value; select.append(option); }
    if(current && !data.available_periods.some(p=>p.value===current)) { const option=el("option",null,current);option.value=current;select.append(option); }
    select.value=current;
    $("orgReportSummary").textContent=orgSummary(data); $("orgReportWarnings").textContent=orgWarnings(data);
    for(const row of data.rows) {
      const card=el("div","data-item"), label=el("label"), check=el("input"); check.type="checkbox"; check.checked=true;
      check.onchange=()=>{ if(check.checked) orgSelectedClients.add(row.client_id);else orgSelectedClients.delete(row.client_id);updateOrgSelection(); };
      label.append(check,document.createTextNode(` ${row.name} · ${row.taxpayer_id} · ${row.period || "无匹配审计"} · ${row.state_label}`));
      const text=el("div"); text.append(label,el("p","muted",`负责会计 ${row.accountant} · 命中 ${row.counts.hit} / 通过 ${row.counts.pass} / 未执行 ${row.counts.skipped}`)); card.append(text);
      if(row.audit_id) { const open=el("button",null,"查看客户证据");open.onclick=async()=>{try{renderResult(await api("/api/audits/"+encodeURIComponent(row.audit_id)));switchPanel("auditPanel");}catch(e){showError(e.message);}};card.append(open); }
      $("orgReportClients").append(card);
    }
    if(!data.rows.length) $("orgReportClients").textContent="暂无客户档案，请在管理中建立客户。";
    updateOrgSelection(); await loadOrgReports();
  } catch(e) { if(request===orgOverviewRequest) { $("orgReportSummary").textContent="";$("orgReportWarnings").textContent="";$("orgReportStatus").textContent="读取失败："+e.message; } }
}
async function loadOrgReports() {
  const rows=await api("/api/org/reports"), box=$("orgReportSaved"); box.textContent="";
  for(const row of rows) { const card=el("div","data-item"), open=el("button",null,"查看汇总快照");
    card.append(el("div",null,`TP-ORG-${row.id.slice(0,12).toUpperCase()} · ${row.created_at}`),open);
    open.onclick=async()=>{try{showOrgReport(await api("/api/org/reports/"+encodeURIComponent(row.id)));}catch(e){showError(e.message);}};box.append(card);
  }
  if(!rows.length) box.textContent="尚未生成机构报告。";
}
function showOrgReport(record) {
  const box=$("orgReportDetail"), data=record.snapshot; box.hidden=false; box.textContent="";
  box.append(el("h3",null,data.report_no+" · "+data.branding.display_name),el("p",null,orgSummary(data)),el("p","muted",orgWarnings(data)));
  const hash=el("p","muted",`快照 SHA-256 ${record.snapshot_sha256} · HTML SHA-256 ${record.html_sha256}`); hash.style.overflowWrap="anywhere";box.append(hash);
  const html=el("a",null,"打开机构报告 HTML");html.href="/api/org/reports/"+encodeURIComponent(record.id)+"/html";html.target="_blank";html.rel="noopener";
  const pdf=el("button","btn pdf","导出机构 PDF");
  pdf.onclick=async()=>{pdf.disabled=true;pdf.textContent="正在导出……";try{
    const response=await fetch("/api/org/reports/"+encodeURIComponent(record.id)+"/pdf");
    if(!response.ok) { const error=await response.json();throw new Error(error.detail || "导出失败"); }
    const url=URL.createObjectURL(await response.blob()), download=el("a");download.href=url;download.download=data.report_no+".pdf";download.click();setTimeout(()=>URL.revokeObjectURL(url),60000);
  }catch(e){showError(e.message);}finally{pdf.disabled=false;pdf.textContent="导出机构 PDF";}};
  const actions=el("div","row");actions.append(html,pdf,htmlOriginalButton(html.href,data.report_no+".html"));box.append(actions);
  appendProtectionAction(box,data.protection);
  for(const row of data.rows) box.append(el("p",null,`${row.name} · ${row.period || "无匹配审计"} · ${row.state_label} · ${row.audit_id || "无审计编号"}`));
}
$("orgReportPeriod").onchange=loadOrgOverview;
$("orgReportRefresh").onclick=loadOrgOverview;
$("orgReportAll").onclick=()=>{if(orgOverview){orgSelectedClients=new Set(orgOverview.rows.map(row=>row.client_id));$("orgReportClients").querySelectorAll('input[type="checkbox"]').forEach(input=>input.checked=true);updateOrgSelection();}};
$("orgReportNone").onclick=()=>{orgSelectedClients.clear();$("orgReportClients").querySelectorAll('input[type="checkbox"]').forEach(input=>input.checked=false);updateOrgSelection();};
$("orgReportCreate").onclick=async()=>{const button=$("orgReportCreate");button.disabled=true;try{
  const result=await api("/api/org/reports",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({period:$("orgReportPeriod").value,client_ids:[...orgSelectedClients]})});
  showOrgReport(result);await loadOrgReports();$("orgReportStatus").textContent="已生成并冻结 "+result.snapshot.report_no+"，共 "+result.snapshot.totals.clients+" 家客户。";
}catch(e){showError(e.message);}finally{button.disabled=!orgSelectedClients.size;}};

let historyPage = 1, historyRequest = 0;
let verificationRequest=0;
function htmlOriginalButton(url,filename) {
  const button=el("button","btn re","下载 HTML 原件");
  button.onclick=async()=>{button.disabled=true;try{
    const response=await fetch(url);
    if(!response.ok){const error=await response.json();throw new Error(error.detail || "原件下载失败");}
    const objectUrl=URL.createObjectURL(await response.blob()),download=el("a");
    download.href=objectUrl;download.download=filename;download.click();setTimeout(()=>URL.revokeObjectURL(objectUrl),60000);
  }catch(e){showError(e.message);}finally{button.disabled=false;}};
  return button;
}
function appendProtectionAction(box,protection) {
  if(!protection) {box.append(el("p","muted","旧归档：未登记 D08 水印/追溯标识，不改写原文件。"));return;}
  const label=el("p","muted","追溯标识 "+protection.id);label.style.overflowWrap="anywhere";
  const open=el("button","btn re","核验报告原件");open.onclick=()=>{
    invalidateVerification();$("verificationId").value=protection.id;$("verificationFile").value="";
    switchPanel("historyPanel");$("reportVerification").scrollIntoView({block:"start"});lookupVerification();
  };box.append(label,open);
}
function invalidateVerification() {
  verificationRequest++;$("verificationStatus").textContent="";$("verificationDetails").textContent="";
  $("verificationLookup").disabled=$("verificationCheck").disabled=false;
}
function verificationDetails(data) {
  $("verificationDetails").textContent=`${data.report_no} · ${data.kind==="audit"?"审计 v"+data.version:"机构报告"}\n${data.customer_name} · 报告日期 ${data.report_date} · 归档 ${data.archived_at}\n标识 ${data.id}\nHTML SHA-256 ${data.html_sha256}\nPDF SHA-256 ${data.pdf_sha256 || "未导出存档；不能核验 PDF"}\n${data.notice}`;
}
async function performVerification(checkFile) {
  const request=++verificationRequest,id=$("verificationId").value.trim(),format=$("verificationFormat").value,file=$("verificationFile").files[0];
  $("verificationLookup").disabled=$("verificationCheck").disabled=true;
  $("verificationDetails").textContent="";$("verificationStatus").textContent=checkFile?"正在本机计算文件指纹……":"正在读取登记……";
  try {
    if(!/^TPV-[0-9a-f]{40}$/.test(id))throw new Error("请输入完整的 TPV 追溯标识；TPL 本地标识没有服务器登记。");
    let options;
    if(checkFile) {
      if(!file)throw new Error("请先选择待核验文件。");
      if(file.size>64*1024*1024)throw new Error("本机核验限制 64MB，请选择较小报告文件。");
      if(!crypto.subtle)throw new Error("文件核验需要 HTTPS 或 localhost 安全上下文。");
      const hash=await crypto.subtle.digest("SHA-256",await file.arrayBuffer());
      const sha256=Array.from(new Uint8Array(hash),byte=>byte.toString(16).padStart(2,"0")).join("");
      if(request!==verificationRequest)return;
      options={method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({format,sha256})};
    }
    const data=await api("/api/report-verification/"+encodeURIComponent(id),options);
    if(request!==verificationRequest)return;
    verificationDetails(data);
    $("verificationStatus").textContent=checkFile?(data.sha256_matches?"核验通过：所选文件与归档原件逐字节一致。":"核验不通过：所选文件与归档原件不一致，可能改动或选错版本。"):
      "此标识已登记；尚未核验任何文件，请选择文件后核验。";
  } catch(e) {if(request===verificationRequest)$("verificationStatus").textContent="核验失败："+e.message;}
  finally {if(request===verificationRequest)$("verificationLookup").disabled=$("verificationCheck").disabled=false;}
}
function lookupVerification(){return performVerification(false);}
$("verificationLookup").onclick=lookupVerification;$("verificationCheck").onclick=()=>performVerification(true);
$("verificationId").oninput=invalidateVerification;$("verificationFormat").onchange=invalidateVerification;$("verificationFile").onchange=invalidateVerification;
async function loadHistory(page = 1) {
  const request = ++historyRequest;
  $("historyPrev").disabled = $("historyNext").disabled = true;
  $("historyStatus").textContent = "正在检索……";
  try {
    const query = new URLSearchParams({q:$("historyQuery").value, period:$("historyPeriod").value,
      risk:$("historyRisk").value, page:String(page), page_size:"20"});
    if ($("historyDateFrom").value) query.set("date_from",$("historyDateFrom").value);
    if ($("historyDateTo").value) query.set("date_to",$("historyDateTo").value);
    const result = await api("/api/archive?" + query);
    if (request !== historyRequest) return;
    historyPage = result.page;
    const rows = result.items, box = $("historyList"); box.textContent = "";
    $("historyStatus").textContent = `共 ${result.total} 条，第 ${historyPage} / ${Math.max(1,Math.ceil(result.total/result.page_size))} 页`;
    $("historyPrev").disabled = historyPage <= 1;
    $("historyNext").disabled = historyPage * result.page_size >= result.total;
    if (!rows.length) box.appendChild(el("div","data-item","暂无历史审计。"));
    for (const row of rows) {
      const item = el("div","sheet"); const txt = el("div");
      txt.appendChild(el("div",null,row.company_name + " · " + row.period));
      txt.appendChild(el("div","muted",row.audited_at + "　命中 " + row.summary.hit + " / 通过 " + row.summary.pass + " / 未执行 " + row.summary.skipped));
      const btn = el("button","btn re","打开证据快照"); btn.addEventListener("click", async () => { try { renderResult(await api("/api/audits/" + encodeURIComponent(row.id))); switchPanel("auditPanel"); } catch(e) { showError(e.message); } });
      const versions = el("button","btn re",`报告版本（${row.report_versions}）`), versionBox = el("div"); versionBox.hidden = true;
      versions.onclick = async () => { versionBox.hidden = !versionBox.hidden; if (!versionBox.hidden) await loadReportVersions(row.id, versionBox, versions); };
      const actions = el("div","row"); actions.append(btn,versions);
      item.append(txt,actions,versionBox); box.appendChild(item);
    }
  } catch (err) { if (request === historyRequest) { $("historyList").textContent = ""; $("historyStatus").textContent = "检索失败：" + err.message; } }
}
async function loadReportVersions(id, box, toggle) {
  box.textContent = "正在读取报告版本……";
  try {
    const rows = await api("/api/audits/" + encodeURIComponent(id) + "/report-versions");
    if (!box.isConnected) return;
    box.textContent = ""; toggle.textContent = `报告版本（${rows.length}）`;
    const publish = el("button","btn re","归档当前报告");
    publish.onclick = async () => { publish.disabled = true; try { await api("/api/audits/" + encodeURIComponent(id) + "/report-versions",{method:"POST"}); await loadReportVersions(id,box,toggle); } catch(e) { publish.disabled = false; showError(e.message); } };
    box.append(publish,el("p","muted","相同内容复用已有版本；归档不重跑规则。校验和用于完整性核对，不代表第三方认证或防伪签名。"));
    if (!rows.length) box.append(el("p","muted","尚无归档报告；可归档当前报告，记录实际生成时间。"));
    for (const row of rows) {
      const card = el("div","sheet"), m = row.manifest;
      card.append(el("h3",null,`v${row.version} · ${m.report_title}`),el("p","muted",`归档 ${row.created_at} · 操作人 ${row.created_by} · ${m.org_name}`),
        el("p","muted",`报告编号 ${m.report_no} · 规则 ${m.rules.map(r=>r.id+" v"+r.version).join("、")}`));
      const hashes = el("p","muted",`HTML SHA-256 ${row.html_sha256}\n模板 SHA-256 ${m.template_sha256}\n审计 SHA-256 ${m.audit_sha256}\nPDF ${row.pdf_sha256 || "尚未导出；首次导出后冻结"}${m.narrative_model ? "\n叙述模型 "+m.narrative_model : "\n无 AI 叙述"}`);
      hashes.style.overflowWrap = "anywhere"; hashes.style.whiteSpace = "pre-line"; card.append(hashes);
      const html = el("a",null,"查看此版 HTML"); html.href = "/api/report/" + encodeURIComponent(id) + "/html?version=" + row.version; html.target = "_blank"; html.rel = "noopener";
      const pdf = el("a",null,"下载此版 PDF"); pdf.href = "/api/report/" + encodeURIComponent(id) + "?version=" + row.version + "&confirm=true";
      pdf.onclick = async (event) => {
        event.preventDefault();
        if (pdf.dataset.busy === "1") return;
        if (currentUser.role === "accountant" && !window.confirm("确认已核对报告内容，并明确报告用途后导出此版本？")) return;
        pdf.dataset.busy = "1"; pdf.textContent = "正在导出……";
        try {
          const response = await fetch(pdf.href);
          if (!response.ok) { const error = await response.json(); throw new Error(error.detail || error.error || "导出失败"); }
          const url = URL.createObjectURL(await response.blob()), download = el("a");
          const name = response.headers.get("Content-Disposition")?.match(/filename\*=UTF-8''(.+)$/);
          download.href = url; download.download = name ? decodeURIComponent(name[1]) : `审计报告-v${row.version}.pdf`;
          download.click(); setTimeout(()=>URL.revokeObjectURL(url),60000);
          if (box.isConnected) await loadReportVersions(id,box,toggle);
        } catch(e) { showError(e.message); }
        finally { pdf.dataset.busy = "0"; pdf.textContent = "下载此版 PDF"; }
      };
      const links = el("div","row"); links.append(html,pdf,htmlOriginalButton(html.href,`${m.report_no}-v${row.version}.html`)); card.append(links); box.append(card);
      appendProtectionAction(card,m.protection);
    }
  } catch(e) { if (box.isConnected) box.textContent = "版本读取失败：" + e.message; }
}
$("historySearch").onsubmit = event => { event.preventDefault(); loadHistory(1); };
$("historyReset").onclick = () => { $("historySearch").reset(); loadHistory(1); };
$("historyPrev").onclick = () => loadHistory(historyPage-1);
$("historyNext").onclick = () => loadHistory(historyPage+1);

let exerciseRuleRequest=0,exerciseRulesYear=null;
async function refreshExerciseRules() {
  const request=++exerciseRuleRequest,year=Number($("exerciseYear").value),select=$("exerciseTargetRule");
  select.textContent="";exerciseRulesYear=null;$("btnGenerateExercise").disabled=true;
  try {
    if(!Number.isInteger(year)||year<2000||year>2099)throw new Error("教学年度须为 2000–2099 的整数。");
    const data=await api("/api/exercises/rules?year="+year);
    if(request!==exerciseRuleRequest)return;
    for(const rule of data.rules){const option=el("option",null,rule.id+" "+rule.name+" · v"+rule.version);option.value=rule.id;option.dataset.version=rule.version;select.append(option);}
    exerciseRulesYear=year;$("btnGenerateExercise").disabled=!data.rules.length;
    $("exerciseGenerationStatus").textContent=data.rules.length?"本期规则已加载；生成后请核对完整答案再发布。":"本期没有已启用规则。";
  }catch(e){if(request===exerciseRuleRequest)$("exerciseGenerationStatus").textContent=e.message;}
}
function exerciseDownloadButton(url) {
  const button=el("button",null,"下载仿真材料（不含答案）");button.type="button";
  button.onclick=async()=>{
    button.disabled=true;
    try {
      const response=await fetch(url,{credentials:"same-origin"});
      if(!response.ok){const error=await response.json().catch(()=>({}));throw new Error(error.detail||"材料下载失败。");}
      const objectUrl=URL.createObjectURL(await response.blob()),link=el("a");
      link.href=objectUrl;link.download="exercise-material.xlsx";link.click();
      setTimeout(()=>URL.revokeObjectURL(objectUrl),60000);
    }catch(e){showError(e.message);}finally{button.disabled=false;}
  };
  return button;
}
function showGeneratedExercise(data) {
  const box=$("exerciseGenerationResult"),meta=data.metadata;box.hidden=false;box.textContent="";
  box.append(el("p",null,data.company_name+" · "+meta.year+" 年 · 目标 "+meta.requested_rule_id+" · v"+meta.rule_versions[meta.requested_rule_id]+" · 种子 "+meta.seed+" · 生成器 "+meta.generator_version));
  box.append(el("p","muted",meta.notice));
  box.append(exerciseDownloadButton("/api/exercises/"+encodeURIComponent(data.audit_id)+"/materials"));
  box.append(el("p",null,"完整标准答案："+meta.standard_answer.join("、")));
  const extra=meta.standard_answer.filter(id=>id!==meta.requested_rule_id);if(extra.length)box.append(el("p","muted","关联命中也须评分："+extra.join("、")+"。发布前请复核全部答案。"));
  const details=el("details"),summary=el("summary",null,"核对全部规则答案与计算过程");details.append(summary);box.append(details);
  for(const answer of data.answers){const item=el("div","data-item");item.append(el("p",null,answer.rule_id+" "+answer.name+" · "+({hit:"命中",pass:"通过",skipped:"未执行"}[answer.status])));item.append(el("div","calc-box",answer.calculation||answer.skip_reason));details.append(item);}
}
$("btnRefreshExerciseRules").onclick=refreshExerciseRules;
$("exerciseYear").oninput=()=>{exerciseRuleRequest++;exerciseRulesYear=null;$("exerciseTargetRule").textContent="";$("btnGenerateExercise").disabled=true;$("exerciseGenerationStatus").textContent="年度已变化，请刷新本期规则。";};
$("btnGenerateExercise").onclick=async()=>{
  const button=$("btnGenerateExercise"),option=$("exerciseTargetRule").selectedOptions[0];button.disabled=true;
  $("exerciseGenerationResult").hidden=true;$("exerciseGenerationStatus").textContent="正在生成并由审计引擎复核……";
  try{
    const year=Number($("exerciseYear").value),seed=Number($("exerciseSeed").value);
    if(!option||exerciseRulesYear!==year)throw new Error("请先刷新本期规则。");
    if(!Number.isInteger(seed)||seed<0||seed>2147483647)throw new Error("种子须为 0–2147483647 的整数。");
    const data=await api("/api/exercises",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({rule_id:option.value,expected_version:option.dataset.version,year,seed,level:$("exerciseLevel").value})});
    await loadAssignments();$("assignmentAudit").value=data.audit_id;showGeneratedExercise(data);
    $("exerciseGenerationStatus").textContent="案例已保存并选入发布栏；尚未发布给学生。";
  }catch(e){$("exerciseGenerationStatus").textContent="生成失败："+e.message;}
  finally{button.disabled=exerciseRulesYear!==Number($("exerciseYear").value)||!$("exerciseTargetRule").options.length;}
};
$("btnExerciseRecord").onclick=async()=>{try{showGeneratedExercise(await api("/api/exercises/"+encodeURIComponent($("assignmentAudit").value)));}catch(e){$("exerciseGenerationStatus").textContent=e.message;}};

const classroomUI = window.createTrainingClassroom({api,el,getUser:()=>currentUser,openExercise,refresh:()=>loadAssignments(),showError});
function updateExerciseDeadline(a) {
  clearTimeout(exerciseDeadlineTimer);
  const expired=a.deadline_passed || (a.deadline_at && Date.now()>=Date.parse(a.deadline_at));
  $("exerciseDeadlineStatus").textContent=(expired?"已截止，仅可查看材料与已有成绩":"截止："+classroomUI.dueText(a.deadline_at))+"；提交以服务器校验为准。";
  $("btnSubmitExercise").disabled=!a.can_submit || !!expired;
  if(a.deadline_at && !expired)exerciseDeadlineTimer=setTimeout(()=>{if(openAssignmentId===a.id)updateExerciseDeadline(a);},Math.min(60000,Math.max(100,Date.parse(a.deadline_at)-Date.now())));
}
async function loadAssignments() {
  const request=++trainingLoadRequest,uid=currentUser?.id,isTeacher=currentUser?.role==="teacher";
  classroomUI.invalidatePending();
  try {
    const [rows,classes,papers,students,audits,submissions]=await Promise.all([
      api("/api/assignments"),api("/api/classes"),api("/api/papers"),
      isTeacher?api("/api/classes/students"):[],isTeacher?api("/api/audits"):[],isTeacher?api("/api/submissions"):[]
    ]);
    if(request!==trainingLoadRequest||uid!==currentUser?.id)return;
    classroomUI.render({classes,papers,students,audits});
    if(openAssignmentId){const current=rows.find(a=>a.id===openAssignmentId);if(current)updateExerciseDeadline(current);else{openAssignmentId=null;exerciseOpenRequest++;sandboxFeedbackRequest++;clearTimeout(exerciseDeadlineTimer);$("exerciseBox").style.display="none";$("exerciseData").textContent="";$("scoreResult").textContent="";}}
    const box = $("assignmentList"); box.textContent = "";
    const singles=rows.filter(row=>!row.paper_id);
    for (const row of singles) {
      const item = el("div","training-card"); item.appendChild(el("h3",null,row.title));
      item.append(el("p","muted",classroomUI.status(row)+" · 截止："+classroomUI.dueText(row.deadline_at)));
      const btn = el("button",null,currentUser.role === "student" ? "开始 / 查看评分" : "查看");
      btn.addEventListener("click",() => openExercise(row.id)); item.appendChild(btn);
      const controls=classroomUI.singleControls(row);if(controls)item.append(controls);box.appendChild(item);
    }
    if (!singles.length) box.appendChild(el("div","data-item","暂无单案例作业；组卷题目请从上方试卷进入。"));
    $("reviewBox").style.display=isTeacher?"block":"none";const sb=$("submissionList");sb.textContent="";
    if (isTeacher) {
      const select = $("assignmentAudit"), previous=select.value; select.textContent = "";
      for (const row of audits) { const o = el("option",null,row.company_name + " · " + row.period); o.value=row.id; select.appendChild(o); }
      if(audits.some(a=>a.id===previous))select.value=previous;
      for(const s of submissions){const item=el("div","data-item");const shown=s.adjusted_score===null?s.score:s.adjusted_score;item.appendChild(el("span",null,s.display_name+" · "+s.title+" · "+shown+" 分"));const b=el("button",null,"复核调整");b.addEventListener("click",async()=>{try{const value=window.prompt("调整后分数（0-100）",String(shown));if(value===null)return;const feedback=window.prompt("教师评语",s.feedback||"")||"";await api("/api/submissions/"+s.id+"/review",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({adjusted_score:Number(value),feedback})});await loadAssignments();}catch(error){showError(error.message);}});item.appendChild(b);sb.appendChild(item);}if(!submissions.length)sb.appendChild(el("div","data-item","暂无学生提交。"));
      if(exerciseRulesYear===null)await refreshExerciseRules();
    }
  } catch (err) { if(request===trainingLoadRequest&&uid===currentUser?.id)showError(err.message); }
}

$("btnAssignment").addEventListener("click", async () => {
  const button=$("btnAssignment");if(button.disabled)return;button.disabled=true;
  try {
    await api("/api/assignments", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({title:$("assignmentTitle").value,audit_id:$("assignmentAudit").value,false_positive_penalty:Number($("assignmentPenalty").value),published:$("assignmentPublished").checked,class_id:$("assignmentClass").value||null,deadline_at:classroomUI.deadlineInput($("assignmentDeadline"))})});
    await loadAssignments();
  } catch (err) { showError(err.message); } finally {button.disabled=false;}
});

function paintSandboxFeedback(result) {
  const box=$("sandboxMessages");box.textContent="";
  for(const message of result.messages)box.append(el("p",message.level==="warning"?"calc-box":"muted",message.text));
  if(result.calculation) {
    const c=result.calculation;
    box.append(el("p","calc-box",c.left+"（"+c.left_value+"）"+(c.operation==="ratio"?" ÷ ":" − ")+c.right+"（"+c.right_value+"） = "+c.value+" "+c.unit+(c.percent!==undefined?"（"+c.percent+"%）":"")));
  }
  if(result.guide) {
    const guide=$("sandboxGuide");guide.textContent="";
    guide.append(el("h3",null,result.guide.rule_id+" 证据定位"),el("p","muted",result.guide.method));
    const located=sandboxEvidence.get(result.guide.rule_id)||new Set();
    for(const input of result.guide.inputs) {
      const label=el("label","sandbox-evidence"),check=el("input");check.type="checkbox";check.checked=located.has(input.name);check.disabled=!input.present;
      check.onchange=()=>{if(check.checked)located.add(input.name);else located.delete(input.name);sandboxEvidence.set(result.guide.rule_id,located);requestSandboxFeedback({action:"mark_risk",rule_id:result.guide.rule_id,evidence_metrics:[...located]});};
      label.append(check,document.createTextNode(" 已核对 "+input.name+"（"+input.unit+"）"),el("p","muted",input.source||"当前案例缺少此项原始指标"),el("p","muted",input.detail||input.description));guide.append(label);
    }
  }
}
async function requestSandboxFeedback(body) {
  const request=++sandboxFeedbackRequest,assignment=openAssignmentId;
  if(!assignment)return;
  $("sandboxMessages").textContent="正在核对操作与证据口径……";
  try {
    const result=await api("/api/assignments/"+encodeURIComponent(assignment)+"/feedback",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    if(request===sandboxFeedbackRequest&&assignment===openAssignmentId)paintSandboxFeedback(result);
  }catch(e){if(request===sandboxFeedbackRequest&&assignment===openAssignmentId)$("sandboxMessages").textContent=e.message;}
}
function buildSandboxTools(a) {
  $("sandboxTools")?.remove();
  const tools=el("section");tools.id="sandboxTools";
  tools.append(el("h3",null,"即时教学反馈"),el("p","muted","勾选风险后定位证据，或用原始指标试算。提示不判定答案正确性；定位和试算仅在本页使用，不改变材料或正式评分。"));
  const row=el("div","sandbox-controls");
  for(const [id,title] of [["sandboxLeft","左侧指标"],["sandboxRight","右侧指标"]]) {
    const field=el("label","field"),select=el("select");select.id=id;select.setAttribute("aria-label",title);select.append(el("option",null,"请选择"));select.options[0].value="";
    field.append(el("span",null,title),select);row.append(field);
    for(const metric of [...(a.metrics||[])].sort((x,y)=>x.name.localeCompare(y.name,"zh-CN"))){const o=el("option",null,metric.name);o.value=metric.name;select.append(o);}
  }
  tools.append(row);
  const options=el("div","sandbox-controls");
  for(const [id,title,values] of [["sandboxOperation","试算方式",[["difference","左侧 − 右侧"],["ratio","左侧 ÷ 右侧"]]],["sandboxPeriodMode","期间口径",[["same_period","同期间核对"],["year_on_year","同比：本期 / 上年同期"]]]]) {
    const field=el("label","field"),select=el("select");select.id=id;select.setAttribute("aria-label",title);
    for(const [value,text] of values){const o=el("option",null,text);o.value=value;select.append(o);}field.append(el("span",null,title),select);options.append(field);
  }
  const calc=el("button","btn pdf","核对并试算");calc.type="button";
  const calculate=()=>requestSandboxFeedback({action:"calculate",left:$("sandboxLeft").value,right:$("sandboxRight").value,operation:$("sandboxOperation").value,period_mode:$("sandboxPeriodMode").value});
  calc.onclick=calculate;options.append(calc);tools.append(options);
  for(const id of ["sandboxLeft","sandboxRight","sandboxOperation","sandboxPeriodMode"])tools.querySelector("#"+id).onchange=calculate;
  const messages=el("div");messages.id="sandboxMessages";messages.setAttribute("role","status");messages.setAttribute("aria-live","polite");messages.textContent="选择风险选项或原始指标后，在这里查看操作提示。";
  const guide=el("div");guide.id="sandboxGuide";tools.append(messages,guide);
  $("exerciseRules").after(tools);
}
async function openExercise(id) {
  const request=++exerciseOpenRequest,loadVersion=trainingLoadRequest;sandboxFeedbackRequest++;clearTimeout(exerciseDeadlineTimer);openAssignmentId=null;$("exerciseBox").style.display="none";
  try {
    const a = await api("/api/assignments/" + id);
    if(request!==exerciseOpenRequest||loadVersion!==trainingLoadRequest)return;
    openAssignmentId=id;sandboxRuleId=null;sandboxEvidence=new Map();$("exerciseBox").style.display="block";
    $("exerciseTitle").textContent=a.title; $("exerciseCompany").textContent=a.company.name + " · " + a.company.period;
    const data=$("exerciseData"); data.textContent="";
    if(a.generated_material_available)data.append(exerciseDownloadButton("/api/assignments/"+encodeURIComponent(id)+"/materials"));
    data.appendChild(el("div","ev-title","科目余额表"));
    const at=el("table","ev"), ah=el("tr"); for(const h of ["科目编码","科目名称","期初","借方","贷方","期末"])ah.appendChild(el("th",null,h));const ahr=el("thead");ahr.appendChild(ah);at.appendChild(ahr);const ab=el("tbody");for(const row of a.accounts){const tr=el("tr");for(const v of [row.code,row.name,row.opening,row.debit,row.credit,row.closing])tr.appendChild(el("td",null,v));ab.appendChild(tr);}at.appendChild(ab);data.appendChild(at);
    data.appendChild(el("div","ev-title","增值税申报")); const dt=el("table","ev"),db=el("tbody");for(const [k,v] of Object.entries(a.declarations)){const tr=el("tr");tr.append(el("td",null,k),el("td","v num",v));db.appendChild(tr);}dt.appendChild(db);data.appendChild(dt);
    data.append(el("div","ev-title","报表与补充底稿指标"),el("p","muted","金额单位元，比例用小数，人数为整数；历史实际期间见口径说明。仿真参考值不代表真实行业标准。"));
    const mt=el("table","ev"),mh=el("tr");for(const h of ["指标","数值","来源 / 口径"])mh.append(el("th",null,h));const mhead=el("thead");mhead.append(mh);mt.append(mhead);const mb=el("tbody");
    for(const metric of a.metrics||[]){const row=el("tr");row.append(el("td",null,metric.name),el("td","v num",metric.value),el("td",null,metric.source+"；"+metric.detail));mb.append(row);}mt.append(mb);data.append(mt);
    const box=$("exerciseRules"); box.textContent="";
    for (const r of a.rules) { const label=el("label"); const input=el("input"); input.type="checkbox"; input.value=r.id;
      input.onchange=()=>{if(input.checked){sandboxRuleId=r.id;requestSandboxFeedback({action:"mark_risk",rule_id:r.id,evidence_metrics:[...(sandboxEvidence.get(r.id)||[])]});}else if(sandboxRuleId===r.id){sandboxFeedbackRequest++;sandboxRuleId=null;$("sandboxGuide").textContent="";$("sandboxMessages").textContent="已取消该风险标注；正式答案以提交时勾选的项目为准。";}};
      label.append(input,document.createTextNode(" "+r.id+" "+r.name)); box.appendChild(label); }
    buildSandboxTools(a);
    $("btnSubmitExercise").style.display=currentUser.role === "student" ? "inline-block" : "none";
    updateExerciseDeadline(a);
    $("scoreResult").textContent="";
    if (a.submission) showScore(a.submission.details);
    if(a.submission?.adjusted_score!==null&&a.submission?.adjusted_score!==undefined)$("scoreResult").append(el("p","muted","教师复核："+a.submission.adjusted_score+" 分 · "+(a.submission.feedback||"")));
    $("exerciseBox").scrollIntoView({block:"start"});
  } catch (err) { if(request===exerciseOpenRequest&&loadVersion===trainingLoadRequest)showError(err.message); }
}

$("btnSubmitExercise").addEventListener("click", async () => {
  const assignment=openAssignmentId,button=$("btnSubmitExercise");if(!assignment||button.disabled)return;button.disabled=true;
  const ids=Array.from($("exerciseRules").querySelectorAll("input:checked")).map((n)=>n.value);
  try { const result=await api("/api/assignments/"+assignment+"/submit",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({selected_rule_ids:ids})});if(openAssignmentId===assignment)showScore(result);await loadAssignments(); }
  catch(err){if(openAssignmentId===assignment){showError(err.message);if(err.status===404||err.status===409)await loadAssignments();else button.disabled=false;}}
});

function showScore(result) {
  const box=$("scoreResult"); box.textContent=""; box.appendChild(el("div","score",result.score+" 分"));
  box.appendChild(el("div","muted","命中 "+result.correct.length+" 项，漏检 "+result.missed.length+" 项，误报 "+result.false_positives.length+" 项；误报扣 "+result.false_positive_deduction+" 分。"));
  for (const group of [["漏检明细",result.missed],["误报明细",result.false_positives]]) { if (!group[1].length) continue; box.appendChild(el("div","ev-title",group[0])); for (const d of group[1]) box.appendChild(el("div","calc-box",d.rule_id+" "+d.name+"："+d.explanation)); }
}

function showOrgSettings(settings,message="") {
  $("orgDisplayName").value=settings.display_name||"";
  $("orgReportTitle").value=settings.report_title||"";
  $("orgFooterText").value=settings.footer_text||"";
  const preview=$("orgLogoPreview");preview.hidden=!settings.has_logo;
  if(settings.has_logo)preview.src="/api/org/logo?v="+encodeURIComponent(settings.logo_updated_at||Date.now());
  else preview.removeAttribute("src");
  $("btnRemoveOrgLogo").disabled=!settings.has_logo;
  $("orgSettingsStatus").textContent=message||(settings.has_logo?"已配置机构 Logo。":"当前使用无 Logo 的文字抬头。");
}

async function loadOrgSettings(){showOrgSettings(await api("/api/org/settings"));}

function nextRuleVersion(version){const parts=version.split(".").map(Number);if(parts.some(n=>!Number.isInteger(n)))return version+".1";parts[parts.length-1]+=1;return parts.join(".");}
function setRuleDraft(path,value){let node=ruleDraftLogic;for(let i=0;i<path.length-1;i++)node=node[path[i]];node[path[path.length-1]]=value;}
function ruleFieldName(path){const key=path[path.length-1],names={threshold:"相对偏离阈值",tolerance:"差额容差",min:"区间下限",max:"区间上限",left:"左侧指标 / 基准",right:"右侧指标 / 基准",numerator:"分子指标",denominator:"分母指标",direction:"比较方向"};return names[key]||("参数 "+(Number.isInteger(key)?key+1:key));}

function renderRuleParamFields(){
  const box=$("ruleParamFields");box.textContent="";const metrics=Object.keys(editingRule.inputs);
  function walk(value,path=[]){
    if(Array.isArray(value)){value.forEach((item,index)=>walk(item,path.concat(index)));return;}
    if(value&&typeof value==="object"){for(const [key,item] of Object.entries(value)){if(key!=="type")walk(item,path.concat(key));}return;}
    const key=path[path.length-1],field=el("div","field"),label=el("label",null,ruleFieldName(path));let input;
    if(typeof value==="number"){
      input=el("input");input.type="number";input.step="any";input.value=String(value);input.addEventListener("input",()=>{if(input.value!==""&&Number.isFinite(Number(input.value)))setRuleDraft(path,Number(input.value));});
    }else if(key==="direction"){
      input=el("select");for(const [v,t] of [["absolute","绝对差"],["above","左侧高于右侧"],["below","左侧低于右侧"]]){const option=el("option",null,t);option.value=v;input.append(option);}input.value=value;input.addEventListener("change",()=>setRuleDraft(path,input.value));
    }else{
      input=el("select");for(const metric of metrics){const option=el("option",null,metric);option.value=metric;input.append(option);}input.value=value;input.addEventListener("change",()=>setRuleDraft(path,input.value));
    }
    input.setAttribute("aria-label",ruleFieldName(path));const hint=el("span","rule-param-path",path.join("."));field.append(label,input,hint);box.append(field);
  }
  walk(ruleDraftLogic);
}

function openRuleEditor(rule){
  ruleEditorRequest++;
  editingRule=rule;ruleDraftLogic=JSON.parse(JSON.stringify(rule.logic));$("ruleEditor").hidden=false;
  $("ruleEditorTitle").textContent=rule.id+" "+rule.name;$("ruleEditorMeta").textContent=rule.category+" · "+({high:"高",medium:"中",low:"低"}[rule.severity]||rule.severity)+"风险";
  $("ruleEditorState").textContent=rule.customized?"已配置":"基础规则";$("ruleCurrentVersion").value=rule.version;$("ruleNewVersion").value=nextRuleVersion(rule.version);$("ruleThresholdBasis").value=rule.threshold_basis;
  $("ruleEffectiveFrom").value="";$("ruleEffectiveTo").value="";
  $("ruleMetricHint").textContent="允许引用的指标："+Object.keys(rule.inputs).join("、");$("btnRuleSave").hidden=currentUser.role!=="platform_admin";
  $("btnRuleTrial").hidden=currentUser.role==="platform_admin";$("ruleTrialAudit").closest(".field").hidden=currentUser.role==="platform_admin";
  $("ruleEditorStatus").textContent=currentUser.role==="platform_admin"?"平台仅维护规则，不读取客户材料或试跑客户审计；请依据已核对的变更发布新版本。":"可调整并试跑草稿；正式保存由平台管理员完成。";$("ruleTrialResult").hidden=true;renderRuleParamFields();
  loadRuleVersionHistory(rule.id);
  ruleEditorControls();
  $("ruleEditor").scrollIntoView({behavior:"smooth",block:"nearest"});
}

async function loadRuleVersionHistory(ruleId){
  const request=++ruleHistoryRequest,editor=ruleEditorRequest,owner=currentUser?.id;
  const current=()=>request===ruleHistoryRequest&&ruleEditorCurrent(editor,owner)&&editingRule.id===ruleId;
  const box=$("ruleVersionHistory");box.textContent="";
  if(currentUser.role!=="platform_admin")return;
  try{
    const versions=await api("/api/rules/"+ruleId+"/versions");
    if(!current())return;
    box.append(el("h3",null,"版本历史"));
    if(!versions.length){box.append(el("p","muted","尚无自定义版本。"));return;}
    for(const item of versions){const interval=item.effective_from?item.effective_from+" 至 "+(item.effective_to||"持续"):"全期间兼容版本";
      box.append(el("div","data-item","v"+item.version+" · "+interval+" · 保存于 "+item.updated_at));}
  }catch(err){if(current())box.append(el("p","muted","版本历史加载失败："+err.message));}
}

function ruleEditorCurrent(request,owner){return request===ruleEditorRequest&&currentUser?.id===owner&&!!editingRule&&$("adminPanel").classList.contains("active");}
function ruleEditorControls(){
  $("ruleEditor").querySelectorAll("input:not([readonly]),select,textarea,button").forEach(n=>{n.disabled=ruleBusy;});
}
async function submitRuleDraft(publish){
  if(ruleBusy||!editingRule||!$("adminPanel").classList.contains("active"))return;
  const request=ruleEditorRequest,owner=currentUser.id,id=editingRule.id;
  try{
    const body=ruleDraftBody(),audit=$("ruleTrialAudit").value;
    if(!publish&&!audit)throw new Error("请先完成一份可访问的审计再试跑。");
    ruleBusy=true;ruleEditorControls();$("ruleTrialResult").hidden=true;
    const result=await api("/api/rules/"+id+(publish?"/parameters":"/trial"),{method:publish?"PUT":"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(publish?body:{...body,audit_id:audit})});
    if(!ruleEditorCurrent(request,owner))return;
    if(publish){
      openRuleEditor(result);
      $("ruleEditorStatus").textContent="已保存 "+result.id+" v"+result.version+"；审计将按完整所属期选择适用版本。";
      await loadAdmin();
    }else{showRuleTrial(result);$("ruleEditorStatus").textContent="试跑完成，历史审计未被修改。";}
  }catch(err){if(ruleEditorCurrent(request,owner)){
    $("ruleEditorStatus").textContent=(publish?"发布未确认，请刷新核对版本后再操作：":"试跑失败：")+err.message;
    showError(err.message);
  }}finally{ruleBusy=false;ruleEditorControls();}
}

function ruleDraftBody(){
  if(!editingRule)throw new Error("请先选择规则。");const version=$("ruleNewVersion").value.trim(),basis=$("ruleThresholdBasis").value.trim();
  if(!version)throw new Error("请填写新版本号。");if(!basis)throw new Error("请填写阈值依据。");
  if(Array.from($("ruleParamFields").querySelectorAll('input[type="number"]')).some(input=>input.value===""))throw new Error("数值参数不能为空。");
  const from=$("ruleEffectiveFrom").value||null,to=$("ruleEffectiveTo").value||null;
  if(to&&!from)throw new Error("请填写生效起始日。");if(from&&to&&to<from)throw new Error("终止日不能早于起始日。");
  return {expected_version:editingRule.version,new_version:version,logic:ruleDraftLogic,threshold_basis:basis,effective_from:from,effective_to:to};
}

function showRuleTrial(result){
  const box=$("ruleTrialResult");box.className="rule-trial-result "+result.status;box.textContent="";const labels={hit:"风险命中",pass:"检查通过",skipped:"材料不足"};
  box.append(el("h3",null,"试跑结果："+(labels[result.status]||result.status)+" · v"+result.version),el("p",null,result.conclusion));
  if(result.calculation)box.append(el("div","calc-box",result.calculation));if(result.skip_reason)box.append(el("p","muted",result.skip_reason));box.hidden=false;
}

let memberRequest=0,memberSnapshot=null;
function memberCurrent(uid,org){return currentUser?.id===uid&&$("memberOrg").value===org;}
function clearMemberInvitations(){
  $("memberInvitationPanel").hidden=true;$("memberInvitationList").textContent="";
  $("memberInvitationReveal").hidden=true;$("memberInvitationURL").textContent="";
  $("memberDomains").value="";$("memberDomainEnabled").checked=false;
}
function renderMemberInvitations(result,uid,org){
  $("memberInvitationPanel").hidden=false;
  $("btnIssueMemberInvite").disabled=false;$("btnSaveInvitationPolicy").disabled=false;
  $("memberDomainEnabled").checked=result.policy.enabled;$("memberDomains").value=result.policy.domains.join("\n");
  const names={accountant:"会计",teacher:"教师",student:"学生"};
  for(const link of result.links){
    const item=el("div","data-item"),text=el("div"),actions=el("div","row");
    text.append(el("div",null,names[link.role]+" · "+(link.active?"可用":"已停用")+" · 凭证 "+link.id),el("p","muted","累计注册 "+link.used_count+" 人 · 当前剩余席位 "+(result.quota.remaining??"未配置")+" · 最后使用 "+(link.last_used_at||"尚未使用")+" · 版本 "+link.revision));
    const refresh=el("button",null,"刷新链接"),toggle=el("button",null,link.active?"停用链接":"启用链接");
    refresh.addEventListener("click",()=>{
      if(memberCurrent(uid,org)&&confirm("刷新将立即作废旧链接，但不影响已注册成员。继续？"))
        mutateMemberInvitation("/invitations/"+encodeURIComponent(link.id)+"/refresh","POST",{revision:link.revision});
    });
    toggle.addEventListener("click",()=>{
      if(memberCurrent(uid,org)&&confirm(link.active?"停用后不能用该链接注册，现有账号不受影响。继续？":"重新启用会恢复当前链接的注册能力。继续？"))
        mutateMemberInvitation("/invitations/"+encodeURIComponent(link.id)+"/state","PUT",{revision:link.revision,active:!link.active});
    });
    actions.append(refresh,toggle);item.append(text,actions);$("memberInvitationList").append(item);
  }
  if(!result.links.length)$("memberInvitationList").append(el("p","muted","尚未签发成员邀请链接。"));
}
async function mutateMemberInvitation(suffix,method,body){
  const uid=currentUser?.id,org=$("memberOrg").value,request=memberRequest;
  if(!memberSnapshot||memberSnapshot.org_id!==org)return;
  $("memberInvitationPanel").querySelectorAll("button").forEach(button=>button.disabled=true);
  $("memberInvitationURL").textContent="";$("memberInvitationReveal").hidden=true;
  let result;
  try{result=await api("/api/members/"+encodeURIComponent(org)+suffix,{method,headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});}
  catch(err){if(memberCurrent(uid,org)&&request===memberRequest)showError(err.message);}
  if(!memberCurrent(uid,org)||request!==memberRequest)return;
  const reloadRequest=memberRequest+1;await loadMembers();
  if(result?.path&&memberCurrent(uid,org)&&memberRequest===reloadRequest&&memberSnapshot){
    $("memberInvitationURL").textContent=new URL(result.path,location.origin).href;
    $("memberInvitationReveal").hidden=false;$("btnCopyMemberInvite").disabled=false;$("btnHideMemberInvite").disabled=false;
  }
}
$("btnIssueMemberInvite").addEventListener("click",()=>mutateMemberInvitation("/invitations","POST",{role:$("memberInviteRole").value}));
$("btnSaveInvitationPolicy").addEventListener("click",()=>{
  if(!memberSnapshot?.invitations)return;
  mutateMemberInvitation("/invitation-policy","PUT",{enabled:$("memberDomainEnabled").checked,domains:$("memberDomains").value.split(/\r?\n/).map(s=>s.trim()).filter(Boolean),revision:memberSnapshot.invitations.policy.revision});
});
$("btnHideMemberInvite").addEventListener("click",()=>{$("memberInvitationURL").textContent="";$("memberInvitationReveal").hidden=true;});
$("btnCopyMemberInvite").addEventListener("click",async()=>{
  try{if($("memberInvitationURL").textContent)await navigator.clipboard.writeText($("memberInvitationURL").textContent);}
  catch{showError("复制失败，请手动选中并复制链接。");}
});
async function loadMembers(refreshOrganizations=false){
  const uid=currentUser?.id,role=currentUser?.role,request=++memberRequest;
  if(!["platform_admin","org_admin"].includes(role))return;
  memberSnapshot=null;$("btnSaveMemberQuota").disabled=true;$("memberQuotaStatus").textContent="正在读取成员与席位…";
  clearMemberInvitations();
  $("userList").textContent="";$("memberQuotaEditor").hidden=true;
  try{
    if(refreshOrganizations){
      const organizations=await api("/api/members/organizations");
      if(request!==memberRequest||currentUser?.id!==uid)return;
      const selected=$("memberOrg").value;$("memberOrg").textContent="";
      for(const org of organizations){const option=el("option",null,org.name+" · "+org.org_id);option.value=org.org_id;$("memberOrg").append(option);}
      if(organizations.some(org=>org.org_id===selected))$("memberOrg").value=selected;
      $("memberOrg").disabled=role!=="platform_admin";
    }
    const org=$("memberOrg").value;
    if(!org){$("memberQuotaStatus").textContent="暂无已开户机构；请先签发创始码，由持码人完成邮箱验证与开户。";return;}
    const [result,invites]=await Promise.all([api("/api/members/"+encodeURIComponent(org)),api("/api/members/"+encodeURIComponent(org)+"/invitations")]);
    if(request!==memberRequest||!memberCurrent(uid,org))return;
    memberSnapshot={...result,invitations:invites};renderMemberInvitations(invites,uid,org);
    const q=result.quota;
    $("memberQuotaStatus").textContent=q.seats===null?"在岗 "+q.used+" 人；席位尚未配置，创建或恢复成员前须由平台配置。":"席位 "+q.used+" / "+q.seats+"，剩余 "+q.remaining+(q.over_quota?"；现有成员超额，须先处理或调整配额。":"。");
    $("memberQuotaEditor").hidden=role!=="platform_admin";$("memberSeats").value=q.seats??"";$("btnSaveMemberQuota").disabled=false;
    const names={platform_admin:"平台管理员",org_admin:"机构管理员",accountant:"会计",teacher:"教师",student:"学生"};
    const sources={founder:"创始码开户",admin_created:"机构管理员创建",member_invitation:"机构邀请链接",legacy:"历史账号（来源未追溯）"};
    for(const person of result.members){
      const item=el("div","data-item"),text=el("div"),actions=el("div","row");
      text.append(el("div",null,person.display_name+" · "+person.username),el("p","muted",(names[person.role]||person.role)+" · 注册："+person.created_at+" · "+(sources[person.source_kind]||person.source_kind)));
      if(person.source_kind==="member_invitation")text.append(el("p","muted","来源凭证："+person.credential_id+" · "+(person.invitation_active?"当前可用":"当前停用")+" · 签发："+(person.invitation_created_at||"—")));
      actions.append(el("span","role-chip",person.active?"在岗":"已停用"));
      if(person.id!==uid&&(role==="platform_admin"||person.role!=="org_admin")){
        const button=el("button",null,person.active?"停用成员":"恢复成员");
        button.addEventListener("click",async()=>{
          if(!memberCurrent(uid,org))return;
          if(!window.confirm((person.active?"停用将撤销旧会话并释放席位，不删除历史业务。确认停用":"恢复需要可用席位，旧会话不会恢复。确认恢复")+"「"+person.display_name+"」？"))return;
          button.disabled=true;
          try{await api("/api/members/"+encodeURIComponent(org)+"/"+encodeURIComponent(person.id)+"/active",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({active:!person.active,expected_active:!!person.active})});}
          catch(err){if(memberCurrent(uid,org))showError(err.message);}
          finally{if(memberCurrent(uid,org))await loadMembers();}
        });actions.append(button);
      }
      item.append(text,actions);$("userList").append(item);
    }
  }catch(err){if(request===memberRequest&&currentUser?.id===uid){$("memberQuotaStatus").textContent="成员信息加载失败，请刷新重试。";showError(err.message);}}
}
$("memberOrg").addEventListener("change",()=>loadMembers());
$("btnRefreshMembers").addEventListener("click",()=>loadMembers(true));
$("btnSaveMemberQuota").addEventListener("click",async()=>{
  const uid=currentUser?.id,org=$("memberOrg").value,data=memberSnapshot,seats=Number($("memberSeats").value);
  if(currentUser?.role!=="platform_admin"||!data||data.org_id!==org)return;
  if(!Number.isInteger(seats)||seats<1||seats>200){showError("席位须为 1–200 的整数。");return;}
  $("btnSaveMemberQuota").disabled=true;
  try{await api("/api/members/"+encodeURIComponent(org)+"/quota",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({seats,revision:data.quota.revision})});}
  catch(err){if(memberCurrent(uid,org))showError(err.message);}
  finally{if(memberCurrent(uid,org))await loadMembers();}
});

async function loadAdmin() {
  const request=++adminRequest,owner=currentUser?.id;
  const current=()=>request===adminRequest&&currentUser?.id===owner&&$("adminPanel").classList.contains("active");
  try {
    let users=[];
    if (["platform_admin","org_admin"].includes(currentUser.role)) {
      if(currentUser.role==="org_admin") await loadOrgSettings();
      users=await api("/api/users"); await loadMembers(true);
      if(currentUser.role==="org_admin"){$("newRole").textContent="";const o=el("option",null,"会计");o.value="accountant";$("newRole").appendChild(o);}
      const accountants=users.filter(u=>u.role==="accountant"&&u.active&&u.org_id===currentUser.org_id),assigned=$("clientAccountant").value;
      $("clientAccountant").textContent="";const none=el("option",null,"暂不指派");none.value="";$("clientAccountant").append(none);
      for(const u of accountants){const o=el("option",null,u.display_name+" · "+u.username);o.value=u.id;$("clientAccountant").append(o);}
      if(accountants.some(u=>u.id===assigned))$("clientAccountant").value=assigned;
      const clients=currentUser.role==="org_admin"?await api("/api/clients"):[],cb=$("clientList");cb.textContent="";
      for(const client of clients){const item=el("div","data-item"),text=el("div");text.append(el("div",null,client.name),el("p","muted",client.taxpayer_id+" · 负责会计："+(client.accountant_name||client.accountant_username||"未指派")));item.append(text,el("span","role-chip","客户档案"));cb.append(item);}
      if(!clients.length)cb.append(el("div","data-item","暂无客户档案。"));
    }
    if(currentUser.role==="platform_admin"){
      const invites=await api("/api/invites"),ib=$("inviteList");ib.textContent="";
      const nowMs=Date.now();
      for(const inv of invites){
        const state=inv.revoked?"已吊销":inv.redeemed_by?"已注册":(new Date(inv.expires_at).getTime()<nowMs?"已过期":"未使用");
        const item=el("div","data-item"),text=el("div");
        const detail=["签发："+(inv.created_at||"—"),"有效期至："+inv.expires_at];
        if(inv.redeemed_email)detail.push("注册人："+inv.redeemed_email+(inv.redeemed_at?" · 注册时间："+inv.redeemed_at:""));
        text.append(el("div",null,inv.org_name),el("p","muted",detail.join(" · ")));
        item.append(text,el("span","role-chip",state));ib.append(item);
      }
      if(!invites.length)ib.append(el("div","data-item","暂无签发记录。"));
    }
    const rules=await api("/api/rules");
    if(!current())return;
    const audits=currentUser.role==="platform_admin"?[]:await api("/api/audits");
    if(!current())return;
    ruleCatalog=rules;const trialSelect=$("ruleTrialAudit"),selectedAudit=trialSelect.value;trialSelect.textContent="";
    for(const row of audits){const option=el("option",null,row.company_name+" · "+row.period+" · "+row.audited_at);option.value=row.id;trialSelect.append(option);}if(audits.some(row=>row.id===selectedAudit))trialSelect.value=selectedAudit;
    if(!audits.length){const option=el("option",null,"暂无可试跑的历史审计");option.value="";trialSelect.append(option);}
    const rb=$("ruleList");rb.textContent="";
    for(const r of ruleCatalog){const item=el("div","data-item"),text=el("div");text.append(el("div",null,r.id+" "+r.name+" · v"+r.version),el("p","muted",r.category+(r.customized?" · 已配置参数":" · 基础参数")));const actions=el("div","row"),chip=el("span","role-chip",r.enabled?"已启用":"已停用"),edit=el("button",null,currentUser.role==="platform_admin"?"配置规则":"配置 / 试跑");edit.addEventListener("click",()=>openRuleEditor(r));actions.append(chip,edit);if(currentUser.role==="platform_admin"){const toggle=el("button",null,r.enabled?"停用":"启用");toggle.addEventListener("click",async()=>{await api("/api/rules/"+r.id+"/state",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({enabled:!r.enabled})});await loadAdmin();});actions.append(toggle);}item.append(text,actions);rb.append(item);}
    // Keep unsaved drafts intact. Selecting a rule explicitly reloads its version.
    if(editingRule)loadRuleVersionHistory(editingRule.id);
    if(["platform_admin","org_admin","teacher"].includes(currentUser.role)){const logs=await api("/api/audit-log");const lb=$("logList");lb.textContent="";for(const l of logs.slice(0,80))lb.appendChild(el("div","data-item",l.created_at+" · "+l.action+" · "+l.target_type+" "+l.target_id));}
  } catch(err){if(current())showError(err.message);}
}

$("btnCreateUser").addEventListener("click",async()=>{const uid=currentUser?.id;$("btnCreateUser").disabled=true;try{await api("/api/users",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({username:$("newUsername").value,password:$("newPassword").value,display_name:$("newDisplayName").value,role:$("newRole").value})});if(currentUser?.id===uid){$("newPassword").value="";await loadAdmin();}}catch(err){if(currentUser?.id===uid)showError(err.message);}finally{$("btnCreateUser").disabled=false;}});
$("btnCreateInvite").addEventListener("click",async()=>{try{const org=$("inviteOrgName").value.trim();if(!org)throw new Error("请填写企业/高校名称。");const inv=await api("/api/invites",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({org_name:org})});$("inviteOrgName").value="";$("inviteRevealCode").textContent=inv.code;$("inviteReveal").hidden=false;await loadAdmin();}catch(err){showError(err.message);}});
$("btnCopyInvite").addEventListener("click",async()=>{try{await navigator.clipboard.writeText($("inviteRevealCode").textContent);showToast("邀请码已复制");}catch(err){showError("复制失败，请手动选中复制。");}});
$("btnHideInvite").addEventListener("click",()=>{$("inviteReveal").hidden=true;$("inviteRevealCode").textContent="";});
$("btnCreateClient").addEventListener("click",async()=>{try{const client=await api("/api/clients",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:$("clientName").value,taxpayer_id:$("clientTaxpayerId").value,accountant_id:$("clientAccountant").value||null})});selectedClientId=client.id;$("clientName").value="";$("clientTaxpayerId").value="";await loadAdmin();}catch(err){showError(err.message);}});
$("btnSaveOrgSettings").addEventListener("click",async()=>{try{const saved=await api("/api/org/settings",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({display_name:$("orgDisplayName").value,report_title:$("orgReportTitle").value,footer_text:$("orgFooterText").value})});showOrgSettings(saved,"报告抬头与页脚已保存。");}catch(err){showError(err.message);}});
$("btnUploadOrgLogo").addEventListener("click",async()=>{const file=$("orgLogoFile").files[0];if(!file)return showError("请选择 PNG 或 JPEG Logo。");try{const form=new FormData();form.append("file",file);const saved=await api("/api/org/logo",{method:"POST",body:form});$("orgLogoFile").value="";showOrgSettings(saved,"Logo 已上传并同步到报告模板。");}catch(err){showError(err.message);}});
$("btnRemoveOrgLogo").addEventListener("click",async()=>{if(!window.confirm("确认移除本机构报告 Logo？机构名称、标题和页脚不会改变。"))return;try{showOrgSettings(await api("/api/org/logo",{method:"DELETE"}),"Logo 已移除。");}catch(err){showError(err.message);}});
$("btnRuleTrial").addEventListener("click",()=>submitRuleDraft(false));
$("btnRuleSave").addEventListener("click",()=>submitRuleDraft(true));
for(const event of ["input","change"])$("ruleEditor").addEventListener(event,()=>{
  $("ruleTrialResult").hidden=true;
  $("ruleEditorStatus").textContent="草稿已修改；请重新试跑或核对后发布，旧试跑结果不再适用。";
});
