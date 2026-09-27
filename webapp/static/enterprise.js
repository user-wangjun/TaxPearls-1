"use strict";
/* Enterprise review owns a persisted batch, not the teaching in-memory draft.
   All server/material text is inserted using DOM text nodes, never HTML. */
function createEnterpriseMaterials({api, el, $, user, clear, showError, showResult, showReview=()=>{}}) {
  const base = "/api/enterprise/materials";
  let batch = null, generation = 0, listRequest = 0, busy = false, dirty = false, needsReload = false;
  let editor = null, save = null, confirm = null, supplement = null, status = null;
  let referenceRequest = 0, operationRequest = 0, operationRoot = null;
  const urls = new Set();
  const enabled = () => ["org_admin", "accountant"].includes(user()?.role);
  const endpoint = suffix => base + "/" + encodeURIComponent(batch.id) + suffix;
  const json = value => ({method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(value)});
  function stamp() {
    const value = generation, owner = user()?.id;
    return () => value === generation && owner === user()?.id && enabled();
  }
  function reset() {
    generation++; batch = null; busy = dirty = needsReload = false;
    referenceRequest++; $("auditMaterials").replaceChildren(); $("auditMaterials").hidden = true;
    operationRequest++;
    operationRoot?.replaceChildren(); operationRoot = null;
    $("busy").style.display = "none";
    editor = save = confirm = supplement = status = null;
    urls.forEach(URL.revokeObjectURL); urls.clear();
  }
  function controls() {
    $("busy").style.display = busy && !batch ? "block" : "none";
    if (editor) editor.disabled = busy;
    if (save) save.disabled = busy || needsReload;
    if (confirm) confirm.disabled = busy || dirty || needsReload || !batch?.analysis.can_confirm || batch.revision !== batch.analysis_revision;
    if (supplement) supplement.disabled = busy || dirty || needsReload;
    if (status) status.textContent = busy ? "正在处理，请稍候……" : needsReload ? "服务器版本可能已变化，请重新读取后再操作。" : dirty ? "存在未保存修改，请先保存并重新分析；当前提示属于上次分析。" : "修改已保存。正式检测只在确认后执行。";
  }
  function changed() { dirty = true; controls(); }
  async function perform(work) {
    if (busy) return;
    const valid = stamp(); busy = true; controls();
    try { await work(valid); }
    catch (error) {
      if (valid()) {
        if ([401,403,404].includes(error.status)) {
          reset(); $("materialReview").hidden = false;
          $("materialReview").replaceChildren(el("p", "material-note", "会话或材料访问权限已变化，请重新登录或返回材料列表。"));
          $("enterpriseHistory").replaceChildren(); $("dropzone").style.display = "block";
        } else { if (error.status === 409) needsReload = true; showError(error.message); }
      }
    } finally { if (valid()) { busy = false; controls(); } }
  }
  function button(text, action, primary = false) {
    const node = el("button", primary ? "primary" : "", text); node.type = "button"; node.onclick = action; return node;
  }
  function canLeave() { return !busy && (!dirty || window.confirm("尚有未保存修改，确定放弃这些修改并读取其他材料吗？")); }
  function validateFiles(files) {
    if (!files.length || files.length > 20 || files.some(f => !/\.(xlsx|xml|pdf|zip)$/i.test(f.name) || !f.size || f.size > 10 * 1024 * 1024) || files.reduce((sum,f)=>sum+f.size,0)>50*1024*1024) {
      throw new Error("请选择 1–20 份 xlsx/xml/pdf/zip 材料，单份非空且不超过 10MB，合计不超过 50MB。");
    }
  }
  function form(files) {
    validateFiles(files); const data = new FormData(); files.forEach(file=>data.append("files",file));
    data.append("extraction", $("extractionMode").value); return data;
  }
  async function upload(files) {
    if (!canLeave()) return;
    clear(); const valid = stamp(); $("dropzone").style.display = "none"; $("busy").style.display = "block";
    await perform(async () => {
      const data = form(files); if ($("auditClient").value) data.append("client_id", $("auditClient").value);
      const value = await api(base, {method:"POST",body:data});
      if (valid()) { batch = value; render(); $("materialReview").scrollIntoView({block:"start"}); refresh(); }
    });
    if (valid()) { $("busy").style.display = "none"; if (!batch) $("dropzone").style.display = "block"; }
  }
  async function open(id) {
    if (!canLeave()) return;
    clear(); $("dropzone").style.display = "block";
    await perform(async valid => {
      const value = await api(base + "/" + encodeURIComponent(id));
      if (valid()) { batch = value; render(); showReview(); $("materialReview").scrollIntoView({block:"start"}); }
    });
  }
  async function refresh() {
    const root = $("enterpriseHistory"), owner = user()?.id, request = ++listRequest;
    root.hidden = !enabled();
    $("materialRetentionHint").textContent = enabled() ? "企业原件加密留存；本次检测一家企业、一个主期间" : "教学核对草稿仅暂存在内存，不使用真实企业原件";
    if (!enabled()) { root.replaceChildren(); return; }
    const current = () => owner === user()?.id && request === listRequest && enabled();
    root.replaceChildren(el("h2", "", "继续已有材料批次"), el("p", "muted", "正在读取……"));
    try {
      const [rows, config] = await Promise.all([api(base), api(base + "/config")]);
      if (!current()) return;
      root.replaceChildren(el("h2", "", "继续已有材料批次"), el("p", config.retention_ready ? "muted" : "material-note", config.message));
      root.append(button("刷新材料列表", refresh));
      const operations = el("div");
      root.append(button("查看材料操作与失败记录", ()=>loadOperations(operations,current)),operations);
      if (!rows.length) root.append(el("p", "muted", "暂无材料批次。上传后可在这里恢复核对或查看旧结果。"));
      else root.append(el("p", "muted", "显示最近 100 个有权访问的批次；检测期间以材料核对页为准，不是上传时间。"));
      for (const row of rows) root.append(button(`${row.created_at} · 批次 ${row.id.slice(0,8)} · 版本 ${row.revision}`, ()=>open(row.id)));
    } catch (error) { if (current()) root.replaceChildren(el("p", "material-note", error.message), button("重试读取材料列表", refresh)); }
  }
  async function loadOperations(root, listCurrent) {
    operationRoot = root;
    const request=++operationRequest, valid=stamp();
    const current=()=>request===operationRequest&&valid()&&listCurrent();
    root.replaceChildren(el("p","muted","正在读取操作记录……"));
    try {
      const rows=await api("/api/enterprise/material-operations");
      if(!current())return;
      root.replaceChildren(el("h3","","材料操作记录"),el("p","muted","最近 100 条有权访问的请求。不含文件内容或凭证；未确认结果可能仍在处理或已中断，请先读取已有批次，勿盲目重复上传。访问登记不代表文件已送达。"));
      if(!rows.length)root.append(el("p","muted","暂无操作记录。"));
      const actions={upload:"上传",supplement:"补传",analyze:"修改分析",confirm:"确认检测",delete:"删除原件",download:"下载原件",view:"查看材料",confirmation_view:"查看确认快照"};
      const failures={authentication:"会话无效",permission:"权限变化",unavailable:"对象不可用",conflict:"版本或材料冲突",deleted:"原件已删除",too_large:"请求过大",invalid_input:"输入或配置不符合要求",busy:"处理繁忙",internal:"内部处理或响应异常"};
      for(const row of rows) {
        const card=el("section","material-card"), access=["download","view","confirmation_view"].includes(row.action);
        const state=row.state==="committed"?(access?"已登记访问":"业务已提交"):row.state==="failed"?"本次操作未提交":"未确认结果（处理中或中断）";
        card.append(el("h4","",`${actions[row.action]||"材料操作"} · ${state}`),el("p","muted",`${row.started_at} · 请求 ${row.id} · 操作人 ${row.actor_id}`));
        if(row.failure_code)card.append(el("p","material-note",failures[row.failure_code]||"处理异常"));
        if(row.state==="committed"&&(row.http_status>=400||!row.observed_at))card.append(el("p","material-note","业务或访问登记已提交，但未确认正常响应；请读取已有批次/结果核实，不要据此重复上传。"));
        if(row.batch_id)card.append(button("读取关联批次 · "+row.batch_id.slice(0,8),()=>open(row.batch_id)));
        root.append(card);
      }
    } catch(error) { if(current())root.replaceChildren(el("p","material-note",error.message),button("重试读取操作记录",()=>loadOperations(root,listCurrent))); }
  }
  function textDetails(parent, title, value) {
    const details = el("details"); details.append(el("summary", "", title), el("pre", "muted", typeof value === "string" ? value : JSON.stringify(value,null,2))); parent.append(details);
  }
  function renderReference(result) {
    const root=$("auditMaterials"), request=++referenceRequest, owner=user()?.id;
    root.replaceChildren();root.hidden=!enabled();if(root.hidden)return;
    const current=()=>request===referenceRequest&&owner===user()?.id&&enabled();
    const link=result.material_reference;
    if(!link){root.append(el("p","muted","此历史结果未保存材料确认版本关联；不将当前上传材料推定为其原始依据。"));return;}
    root.append(el("h3","","本结果的确认材料"),el("p","muted",`批次 ${link.batch_id} · 分析版本 ${link.analysis_revision} · 确认版本 ${link.confirmation_revision}`));
    const detail=el("div");
    const load=button("查看当时确认的材料",async()=>{
      if(load.disabled)return;load.disabled=true;detail.replaceChildren(el("p","muted","正在读取确认快照……"));
      try{
        const data=await api("/api/audits/"+encodeURIComponent(result.audit_id)+"/materials");if(!current())return;
        detail.replaceChildren();const snapshot=data.confirmation;
        if(!snapshot)throw new Error("未找到此结果的材料确认版本，不能用最新材料替代。");
        if(snapshot.audit_id!==result.audit_id||snapshot.batch_id!==link.batch_id||snapshot.analysis_revision!==link.analysis_revision||snapshot.confirmation_revision!==link.confirmation_revision)
          throw new Error("材料确认关联不一致，请重新读取结果。");
        detail.append(el("p","",`只读历史快照 · 确认人 ${snapshot.confirmed_by} · ${snapshot.confirmed_at}`),
          el("p","muted",`当前批次已到版本 ${snapshot.current_revision}；以下仍是本结果对应的分析版本 ${snapshot.analysis_revision}，不是最新材料。`),
          el("p","muted",`分析指纹 ${snapshot.analysis_sha256} · 提取程序 ${snapshot.parser_version||"历史未记录"}`));
        textDetails(detail,"当时的企业与主期间",snapshot.company);
        const purposes={current:"本期检测",history:"历史参考（不并入本期金额）",excluded:"当时已排除"};
        for(const doc of snapshot.documents){
          const card=el("section","material-card");card.append(el("h4","",doc.name),el("p","",purposes[doc.purpose]||doc.purpose),
            el("p","muted",`文件指纹 ${doc.fingerprint} · 原文企业 ${doc.material_company?.name||"未识别"} · 原文期间 ${doc.material_company?.period||"未识别"}`));
          textDetails(card,"当时的提取信息",doc.extraction||{});
          const original=snapshot.files.find(f=>f.id===doc.original_id);
          if(!original)card.append(el("p","material-note","历史未关联原件，不推定可用。"));
          else if(original.deleted_at)card.append(el("p","material-note",`原件当前已删除（${original.deleted_at}）；本结果与确认快照保留，但不能下载原件。`));
          else {
            const get=button("下载确认原件 · "+original.name,async()=>{
              if(get.disabled)return;get.disabled=true;
              try{await downloadOriginal(original,snapshot.batch_id,current);}
              catch(error){if(current()){detail.replaceChildren(el("p","material-note",error.message));load.textContent="重新核对原件状态";}}
              finally{if(current())get.disabled=false;}
            });card.append(get);
          }
          detail.append(card);
        }
        textDetails(detail,"当时采用的输入指标与来源",snapshot.metrics);
        textDetails(detail,"当时的用户修改与原文差异",snapshot.edits);
        textDetails(detail,"当时的检查范围与限制",{checks:snapshot.checks,feedback:snapshot.feedback});
        detail.append(button("继续此批次（读取最新版本）",()=>open(snapshot.batch_id)));
      }catch(error){if(current()){detail.replaceChildren(el("p","material-note",error.message));load.textContent="重试读取确认材料";}}
      finally{if(current())load.disabled=false;}
    });root.append(load,detail);
  }
  function input(parent, title, value, type = "text") {
    const label = el("label", "", title), control = el("input"); control.type = type;
    control.value = value ?? ""; control.maxLength = 200; control.setAttribute("aria-label",title);
    control.addEventListener("input",changed); label.append(control); parent.append(label); return control;
  }
  function render() {
    const root = $("materialReview"); root.hidden = false; root.classList.add("enterprise-review"); root.replaceChildren();
    $("dropzone").style.display = "none";
    root.append(el("h2", "", "企业材料核对"), el("p", "muted", `批次 ${batch.id.slice(0,8)} · 当前版本 ${batch.revision} · 分析版本 ${batch.analysis_revision}。一次检测一家企业、一个主期间，历史材料不并入本期金额。`));
    status = el("p", "material-note"); status.setAttribute("role","status"); root.append(status);
    const reload = button("重新读取已保存版本", ()=>open(batch.id));
    root.append(reload, button("另开检测", ()=>{if(canLeave()){clear();$("dropzone").style.display="block";refresh();}}));
    editor = el("fieldset"); root.append(editor);
    const scope = el("div", "material-company"), company = {};
    for (const [key,title,type] of [["name","企业名称（必填）"],["taxpayer_id","纳税人识别号（必填）"],["period_start","主期间起始日（必填）","date"],["period_end","主期间终止日（必填）","date"],["industry","所属行业（选填）"]]) company[key] = input(scope,title,batch.company[key],type);
    editor.append(scope,el("p", "muted", "无歧义信息会预填；可补填或修正，无须填写修改原因。用户值不代替原文事实，主体或期间冲突仍须处理。主期间支持完整月、季度、半年或年度。"));
    const selections = {};
    for (const doc of batch.documents) {
      const selection = batch.selections[doc.id] || {}, card = el("section", "material-card");
      card.append(el("h3", "", doc.name), el("p",doc.error?"material-note":"muted",doc.error||doc.summary)); editor.append(card);
      const label = el("label", "", "材料用途"), purpose = el("select"); purpose.setAttribute("aria-label",doc.name+" 材料用途");
      for (const [value,title] of [["current","本期检测"],["history","历史参考（不计入本期金额）"],["excluded","移出本次检测（保留原件与历史）"]]) { const option=el("option","",title);option.value=value;purpose.append(option); }
      purpose.value = selection.purpose || "current"; purpose.onchange = changed; label.append(purpose);card.append(label);
      card.append(el("p","muted",`原文企业：${doc.company.name||"未识别"} · 税号：${doc.company.taxpayer_id||"未识别"} · 业务期间：${doc.company.period||"未识别"}`));
      if (doc.extraction && Object.keys(doc.extraction).length) textDetails(card,"提取方式与来源信息",doc.extraction);
      for (const warning of doc.warnings || []) card.append(el("p","material-note",warning));
      const file = batch.files.find(f=>f.id===doc.original_id);
      if (file) {
        card.append(el("p","muted",`上传时间（非业务期间）：${file.uploaded_at} · 指纹 ${file.sha256}`));
        if (file.deleted_at) card.append(el("p","material-note",`原件已于 ${file.deleted_at} 删除；请明确移出本次检测后重新分析。`));
        else card.append(button("下载原件",()=>download(file)));
      }
      const editable = !doc.error && (doc.kind === "pdf" || doc.review_required);
      let readRows = null;
      if (editable) {
        textDetails(card,"查看提取原文",(doc.pages||[]).map(p=>(p.label||"第 "+p.page+" 页")+"\n"+(p.text||"无可提取文字，请查看原件。")).join("\n\n"));
        readRows = rowEditor(card,doc,selection.rows || doc.rows || []);
      } else if (doc.rows?.length) textDetails(card,"已读取指标与来源",doc.rows);
      const readStandard = doc.standard_fields?.length ? standardEditor(card, doc, selection.standard_edits || {}) : null;
      if(doc.kind === "xlsx" && !doc.error && !doc.review_required && !doc.standard_fields)
        card.append(el("p","material-note","这是旧版解析材料，保存并重新分析后可加载标准账表修正字段。"));
      for (const [key,title] of [["invoices","发票与交易日期"],["bank_transactions","银行流水（入账不直接作为收入）"],["bank_adjustments","银行调节底稿"],["period_series","历史指标及实际期间"]]) if(doc[key]?.length) textDetails(card,title,doc[key]);
      selections[doc.id] = () => ({purpose:purpose.value,...(readRows ? {rows:readRows()} : {}),
        ...(readStandard ? {standard_edits:readStandard()} : {})});
    }
    save = button("保存修改并重新分析",()=>perform(async valid=>{
      const data = {expected_revision:batch.revision, company:Object.fromEntries(Object.entries(company).map(([k,v])=>[k,v.value.trim()])), selections:Object.fromEntries(Object.entries(selections).map(([k,v])=>[k,v()]))};
      const value = await api(endpoint("/analyze"),json(data));
      if (valid()) {batch=value;dirty=false;needsReload=false;render();refresh();}
    })); editor.append(save);
    const feedback = batch.analysis.feedback;
    for (const [level,title] of [["blocking","必须处理"],["limited","缺失或条件不足，但可继续其他检查"],["suggested","建议补充 / 核对"]]) {
      const group = el("section", "enterprise-feedback " + level);group.append(el("h3","",`${title}（${feedback[level].length}）`));
      if (!feedback[level].length) group.append(el("p","muted","本次分析暂无此类提示。"));
      for (const note of feedback[level]) {
        const file = batch.documents.find(d=>d.id===note.file_id);
        const item = el("details"); item.open = level === "blocking";
        item.append(el("summary","",(file?file.name+" · ":"")+(note.impact||note.message)));
        item.append(el("p","",note.message));
        if(note.required) item.append(el("p","muted","需补充或处理："+note.required));
        group.append(item);
      } root.append(group);
    }
    if (batch.metrics?.length) {
      const metrics = el("details"); metrics.append(el("summary","",`本次检测输入指标与来源（${batch.metrics.length} 项）`));
      for(const metric of batch.metrics) metrics.append(el("p","",`${metric.name}：${metric.value}`),el("p","muted",`${metric.source} · ${metric.detail}`));
      root.append(metrics);
    }
    if (batch.analysis.supplement_differences?.length) textDetails(root,"补传识别差异（已保留原有填写值）",batch.analysis.supplement_differences);
    if (batch.analysis.edits?.length) textDetails(root,"用户值 / 原文差异与标准化记录",batch.analysis.edits);
    const coverage = el("details"); coverage.append(el("summary","",`本次检查范围（${batch.analysis.checks.filter(c=>c.ready).length} 项具备计算条件 / ${batch.analysis.checks.length} 项规则）`));
    for (const check of batch.analysis.checks) coverage.append(el("p","muted",`${check.rule_id} ${check.name} v${check.version} · ${check.ready?"具备计算条件，尚非检测结果":check.reasons.join("；")}`)); root.append(coverage);
    confirm = button("确认材料并开始检测",()=>perform(async valid=>{
      const url = endpoint("");
      const result = await api(url+"/confirm",json({expected_revision:batch.analysis_revision}));
      if (!valid()) return;
      try { const value=await api(url);if(valid()){batch=value;render();} }
      catch(error) {if(valid()){needsReload=true;showError("检测已保存，但材料列表未刷新："+error.message);}}
      if(valid()){showResult(result);refresh();}
    }),true); root.append(confirm);
    if (batch.revision !== batch.analysis_revision) root.append(el("p","muted","此分析已经确认或材料随后变化。可打开已保存结果，或保存修改并重新分析后再次确认。"));
    const supplementLabel = el("label","","补传材料（沿用当前批次，重新分析后须再次确认）");
    supplement = el("input"); supplement.type="file";supplement.multiple=true;supplement.accept=".xlsx,.xml,.pdf,.zip";supplement.setAttribute("aria-label","补传材料");
    supplement.onchange = () => {
      const files = Array.from(supplement.files);supplement.value="";if(!files.length)return;
      perform(async valid=>{
        const data=form(files);data.append("expected_revision",String(batch.revision));
        const value=await api(endpoint("/supplement"),{method:"POST",body:data});
        if(valid()){batch=value;dirty=false;render();refresh();}
      });
    }; supplementLabel.append(supplement);root.append(supplementLabel);
    if (batch.executions.length) {
      root.append(el("h3","","此批次已保存的检测结果（旧结果不覆盖）"));
      for (const execution of batch.executions) root.append(button(`打开分析版本 ${execution.analysis_revision} 的结果`,()=>perform(async valid=>{
        const result=await api("/api/audits/"+encodeURIComponent(execution.audit_id));if(valid())showResult(result);
      })));
    }
    const traceBox=el("div");root.append(button("查看材料版本与操作记录",()=>perform(async valid=>{
      const trace=await api(endpoint("/trace"));if(!valid())return;
      traceBox.replaceChildren();
      for(const version of trace.versions) textDetails(traceBox,`版本 ${version.revision} · ${version.kind} · ${version.created_at} · 操作人 ${version.created_by}`,version.detail);
      textDetails(traceBox,"上传、确认、检测与原件访问记录",trace.events);
    })),traceBox);
    if(user().role==="org_admin") {
      const management=el("details");management.append(el("summary","","原件保留与删除（机构管理员）"),el("p","muted","原件不自动到期删除。删除不影响历史结果，但可能使原件复盘不可用；备份中可能仍有副本。"));
      for(const file of batch.files.filter(f=>!f.deleted_at))management.append(button("查看删除影响："+file.name,()=>deletion(file,management)));
      root.append(management);
    }
    controls();
  }
  function rowEditor(parent,doc,values) {
    const wrap=el("div","material-table-wrap"), table=el("table","material-table"), head=el("tr"), body=el("tbody");
    ["标准指标","数值（元；比率用小数）","来源页 / 表编号","栏次与口径说明","操作"].forEach(t=>head.append(el("th","",t)));table.append(head,body);wrap.append(table);parent.append(wrap);
    let rows=[];
    function add(value={},isNew=false) {
      const tr=el("tr"),name=el("select"),amount=el("input"),page=el("input"),detail=el("input");
      for(const key of Array.from(new Set([...Object.keys(batch.fields),...values.map(r=>r.name)])).sort()) {const option=el("option","",key);option.value=key;name.append(option);}
      if(value.name)name.value=value.name;amount.value=value.value??"";amount.inputMode="decimal";amount.placeholder="空白表示缺失";
      page.type="number";page.min=1;page.max=doc.page_count;page.value=value.page||1;detail.value=value.detail||"";detail.maxLength=2000;
      for(const [node,label] of [[name,"标准指标"],[amount,"指标数值"],[page,"来源页码"],[detail,"口径说明"]]){node.setAttribute("aria-label",label);node.addEventListener("input",changed);}
      const row={name,amount,page,detail};rows.push(row);
      for(const node of [name,amount,page,detail,button("移除指标",()=>{rows=rows.filter(r=>r!==row);tr.remove();changed();})]){const td=el("td");td.append(node);tr.append(td);}
      if(value.ai_issues?.length)tr.cells[0].append(el("p","material-note",value.ai_issues.join("；")));
      if(value.ai_raw_value!==undefined)tr.cells[1].append(el("p","muted",`AI 候选原值（待核对）：${value.ai_raw_value??"缺失"} ${value.ai_unit||""}`));
      body.append(tr);if(isNew)changed();
    }
    values.forEach(v=>add(v));parent.append(button("添加未识别指标",()=>add({},true)));
    return ()=>rows.map(r=>({name:r.name.value,value:r.amount.value.trim(),page:Number(r.page.value),detail:r.detail.value.trim()}));
  }
  function standardEditor(parent, doc, existing) {
    const section = el("details"), fields = doc.standard_fields, values = {...existing};
    section.append(el("summary","",`核对 / 修正标准账表数值（${fields.length} 个字段）`),
      el("p","muted","仅修改本次检测输入，不改原件。按原表单位填写，空白表示缺失，0 表示明确的零；不需要填写修改原因。"));
    const filterLabel=el("label","","筛选账表字段"), filter=el("input");
    filter.type="search";filter.setAttribute("aria-label",doc.name+" 筛选账表字段");filterLabel.append(filter);section.append(filterLabel);
    const pageInfo=el("p","muted"), rows=el("div","enterprise-standard-fields"), navigation=el("div","actions");
    let page=0;
    const previous=button("上一页字段",()=>{page--;draw();}), next=button("下一页字段",()=>{page++;draw();});
    navigation.append(previous,next);section.append(pageInfo,rows,navigation);parent.append(section);
    function draw() {
      const query=filter.value.trim().toLowerCase();
      const selected=fields.filter(field=>(field.table+" "+field.label+" "+field.id).toLowerCase().includes(query));
      const pages=Math.max(1,Math.ceil(selected.length/40));page=Math.max(0,Math.min(page,pages-1));
      previous.disabled=page===0;next.disabled=page+1>=pages;
      function refreshCount(){pageInfo.textContent=`第 ${page+1}/${pages} 页 · 匹配 ${selected.length} 个字段 · 本文件修正 ${Object.keys(values).length} 项`;}
      refreshCount();
      rows.replaceChildren();
      for(const field of selected.slice(page*40,(page+1)*40)) {
        const row=el("div","material-card");
        row.append(el("p","",field.table+" · "+field.label),el("p","muted",`原文 ${field.id}：${field.value??"空白（缺失，不是零）"}`));
        const label=el("label","","用于检测的值"), control=el("input");control.type="text";control.inputMode="decimal";control.maxLength=100;
        control.setAttribute("aria-label",doc.name+" "+field.id+" 用于检测的值");
        control.value=Object.hasOwn(values,field.id)?(values[field.id]??""):(field.value??"");
        const marker=el("p","muted");
        function refreshMarker(){marker.textContent=Object.hasOwn(values,field.id)?"用户修正 / 补填，保存后重新计算":"沿用原文";}
        control.addEventListener("input",()=>{
          const value=control.value.trim();
          if(value===(field.value??""))delete values[field.id];else values[field.id]=value||null;
          changed();refreshMarker();refreshCount();
        });
        label.append(control);row.append(label,marker,button("恢复原文 · "+field.id,()=>{
          delete values[field.id];control.value=field.value??"";changed();refreshMarker();refreshCount();
        }));refreshMarker();rows.append(row);
      }
    }
    filter.addEventListener("input",()=>{page=0;draw();});draw();
    return ()=>({...values});
  }
  async function download(file) {
    await perform(valid=>downloadOriginal(file,batch.id,valid));
  }
  async function downloadOriginal(file,batchId,valid) {
      const response=await fetch(base+"/"+encodeURIComponent(batchId)+"/originals/"+encodeURIComponent(file.id));
      if(!response.ok){const body=await response.json().catch(()=>({}));const error=new Error(body.detail||"原件下载失败");error.status=response.status;throw error;}
      const blob=await response.blob();if(!valid())return;
      const url=URL.createObjectURL(blob);urls.add(url);const anchor=el("a");anchor.href=url;anchor.download=file.name;anchor.click();
      setTimeout(()=>{URL.revokeObjectURL(url);urls.delete(url);},30000);
  }
  async function deletion(file,parent) {
    if(dirty){showError("请先保存修改并重新分析，再查看删除影响。");return;}
    await perform(async valid=>{
      const url=endpoint("/originals/"+encodeURIComponent(file.id)),impact=await api(url+"/deletion-impact");if(!valid())return;
      const preview=el("section","enterprise-feedback blocking");
      preview.append(el("h3","","删除影响："+file.name),el("p","",impact.warning),el("p","muted",`关联 ${impact.versions} 个材料版本、${impact.audits.length} 份检测结果。删除后当前分析须重新核对。`));
      for(const audit of impact.audits)preview.append(el("p","muted",`结果 ${audit.audit_id} · 分析版本 ${audit.analysis_revision}`));
      preview.append(button("确认删除此原件",()=>perform(async validDelete=>{
        await api(url,{...json({expected_revision:impact.revision}),method:"DELETE"});
        if(!validDelete())return;needsReload=true;
        const value=await api(endpoint(""));if(validDelete()){batch=value;needsReload=false;render();refresh();}
      })),button("取消",()=>preview.remove()));parent.append(preview);
    });
  }
  return {enabled,reset,refresh,upload,open,renderReference};
}
