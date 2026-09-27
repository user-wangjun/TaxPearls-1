"use strict";

window.createMistakeBook = function ({api, el, getUser}) {
  const $ = id => document.getElementById(id);
  const kinds = {missed: "漏检", false_positive: "误报", unsupported: "材料不足仍标风险"};
  let request = 0, cases = [];
  const student = () => getUser()?.role === "student";
  const date = value => new Date(value).toLocaleString();
  const current = (version, uid) => version === request && uid === getUser()?.id && student();
  function clearDetail() { $("mistakeDetail").textContent = ""; $("mistakeDetail").hidden = true; }
  function invalidate(message = "") {
    request++; cases = []; clearDetail(); $("mistakeList").textContent = "";
    $("mistakeStatus").textContent = message;
  }
  function reset() {
    invalidate(); $("mistakeBook").hidden = true;
    $("mistakeState").value = $("mistakeKind").value = "";
  }
  function table(headings, rows) {
    const wrap = el("div", "training-stats-scroll"), table = el("table", "ev"), head = el("thead"), tr = el("tr");
    wrap.tabIndex = 0; wrap.setAttribute("role", "region"); wrap.setAttribute("aria-label", headings.join("、") + "，可横向滚动");
    headings.forEach(h => tr.append(el("th", null, h))); head.append(tr); table.append(head);
    const body = el("tbody");
    rows.forEach(values => { const row = el("tr"); values.forEach(v => row.append(el("td", null, String(v ?? "—")))); body.append(row); });
    table.append(body); wrap.append(table); return wrap;
  }
  function paintList() {
    const box = $("mistakeList"); box.textContent = "";
    const state = $("mistakeState").value, kind = $("mistakeKind").value;
    const selected = cases.filter(c => (!state || c.status === state) && (!kind || c.errors.some(e => e.kind === kind)));
    for (const item of selected) {
      const card = el("div", "training-card");
      card.append(el("h3", null, item.title));
      if (!item.available) card.append(el("p", "muted", "原材料或评分口径缺失／变化，暂不可重练，请核查备份。"));
      else {
        card.append(el("p", "muted", `${item.status === "corrected" ? "本轮已订正（不代表已掌握）" : "待订正"} · 累计 ${item.errors.length} 个错项 · ${item.practice_count} 次独立重练`));
        for (const error of item.errors) card.append(el("p", null, `${kinds[error.kind]} · ${error.rule_id} ${error.name} · ${error.category} · v${error.version}；正式错答 ${error.formal_count} 次 / 重练错答 ${error.practice_count} 次`));
        const button = el("button", null, "查看错因 / 独立重练"); button.type = "button";
        button.onclick = () => open(item.id); card.append(button);
      }
      box.append(card);
    }
    if (!selected.length) box.append(el("p", "muted", cases.length ? "没有符合筛选条件的错题。" : "暂无可访问错题。新提交会自动留存；旧记录可用上方按钮同步。"));
  }
  async function load(sync = false) {
    invalidate("正在读取错题本……"); $("mistakeBook").hidden = !student();
    if (!student()) return;
    const version = request, uid = getUser().id;
    try {
      const imported = sync ? await api("/api/training/mistakes/sync", {method: "POST"}) : null;
      if (!current(version, uid)) return;
      const data = await api("/api/training/mistakes");
      if (!current(version, uid)) return;
      cases = data.cases; $("mistakeMethod").textContent = data.method;
      $("mistakeStatus").textContent = `${cases.length} 个可访问案例。` + (imported ? ` 旧记录同步：新增 ${imported.added}，更新 ${imported.updated}，未变 ${imported.unchanged}，不可用 ${imported.invalid}。` : "");
      paintList();
    } catch (error) { if (current(version, uid)) $("mistakeStatus").textContent = "读取失败：" + error.message; }
  }
  function score(result) {
    const box = el("div", "calc-box");
    box.append(el("h3", null, `独立重练 ${result.score} 分（不计入正式成绩）`));
    box.append(el("p", null, result.missed.length || result.false_positives.length ? "仍有错项，可重新阅读材料后再练。" : "本轮全部订正；一次答对不等于已掌握。"));
    for (const error of [...result.missed, ...result.false_positives]) box.append(el("p", null, `${error.rule_id} ${error.name}：${error.explanation}`));
    return box;
  }
  function paintDetail(item, result = null) {
    const box = $("mistakeDetail"); box.hidden = false; box.textContent = "";
    box.append(el("h2", null, "独立重练 · " + item.title));
    box.append(el("p", "muted", `${item.material.company.name} · ${item.material.company.period}；整案例重新判断，所有选项初始为空。不会提交或覆盖原作业，截止后仍可在获授权范围内重练。`));
    const close = el("button", null, "关闭重练"); close.type = "button"; close.onclick = () => { request++; clearDetail(); }; box.append(close);
    if (result) box.append(score(result));
    else if (item.latest_practice) {
      const previous = el("details"); previous.append(el("summary", null, "上次独立重练结果 · " + date(item.latest_practice.created_at)), score(item.latest_practice.result)); box.append(previous);
    }
    const review = el("details"); review.append(el("summary", null, "查看已留存错因、计算与依据（展开后含答案提示）"));
    for (const finding of item.review) {
      review.append(el("h3", null, finding.rule_id), el("p", null, finding.conclusion || finding.skip_reason),
        el("p", null, finding.calculation + "；" + finding.threshold), el("p", "muted", "依据：" + finding.legal_basis));
    }
    box.append(review, el("h3", null, "科目余额表"), table(["科目编码", "科目名称", "期初", "借方", "贷方", "期末"], item.material.accounts.map(a => [a.code, a.name, a.opening, a.debit, a.credit, a.closing])));
    box.append(el("h3", null, "申报与补充指标"), el("p", "muted", "金额单位元，比例为小数；来源与业务口径见下表。仿真材料不是实际企业数据。"),
      table(["申报项目", "数值"], Object.entries(item.material.declarations)),
      table(["指标", "数值", "来源 / 口径"], item.material.metrics.map(m => [m.name, m.value, m.source + "；" + m.detail])));
    const form = el("fieldset"), legend = el("legend", null, "重新勾选本案例命中的风险"), choices = el("div", "check-grid training-checks"); form.append(legend, choices);
    for (const rule of item.rules) {
      const label = el("label"), input = el("input"); input.type = "checkbox"; input.value = rule.rule_id;
      label.append(input, document.createTextNode(` ${rule.rule_id} ${rule.name}`)); choices.append(label);
    }
    const submit = el("button", "btn pdf", "提交独立重练"), message = el("p", "muted"); submit.type = "button"; message.setAttribute("role", "status");
    form.append(submit); box.append(form, message);
    submit.onclick = async () => {
      if (form.disabled) return;
      const answers = [...choices.querySelectorAll("input:checked")].map(n => n.value).sort();
      const requestId = crypto.randomUUID();
      const version = ++request, uid = getUser()?.id; form.disabled = true; message.textContent = "正在保存独立重练……";
      try {
        const result = await api(`/api/training/mistakes/${encodeURIComponent(item.id)}/practice`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({revision: item.revision, request_id: requestId, selected_rule_ids: answers})});
        if (!current(version, uid)) return;
        const [updated, listing] = await Promise.all([api("/api/training/mistakes/" + encodeURIComponent(item.id)), api("/api/training/mistakes")]);
        if (!current(version, uid)) return;
        cases = listing.cases; paintList(); paintDetail(updated, result); $("mistakeDetail").scrollIntoView({block: "start"});
      } catch (error) {
        if (!current(version, uid)) return;
        // Remove retained material after an uncertain permission/conflict error.
        clearDetail(); cases = []; paintList(); $("mistakeStatus").textContent = "重练未确认完成：" + error.message + "。请刷新错题本查看已保存结果后重新打开；不要据此认定成绩已保存。";
      } finally { if (current(version, uid)) form.disabled = false; }
    };
  }
  async function open(id) {
    const version = ++request, uid = getUser()?.id; clearDetail(); $("mistakeStatus").textContent = "正在读取重练材料……";
    try {
      const item = await api("/api/training/mistakes/" + encodeURIComponent(id));
      if (!current(version, uid)) return;
      paintDetail(item); $("mistakeStatus").textContent = "独立重练已打开。"; $("mistakeDetail").scrollIntoView({block: "start"});
    } catch (error) { if (current(version, uid)) { cases = []; paintList(); $("mistakeStatus").textContent = "无法打开：" + error.message; } }
  }
  $("btnRefreshMistakes").onclick = () => load();
  $("btnSyncMistakes").onclick = () => load(true);
  // Re-read after cancelling a pending practice/open, so changing filters never
  // strands an in-flight list or displays a pre-submission correction status.
  for (const id of ["mistakeState", "mistakeKind"]) $(id).onchange = () => load();
  return {load, reset, invalidate};
};
