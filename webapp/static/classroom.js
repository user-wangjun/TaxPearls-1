"use strict";

// All case/roster text is rendered as text, never interpreted as HTML.
window.createTrainingClassroom = function ({api, el, getUser, openExercise, refresh, showError}) {
  const $ = id => document.getElementById(id);
  const mistakeBook = window.createMistakeBook({api, el, getUser});
  let data = {classes: [], students: [], audits: [], papers: []};
  let editingClass = null, draft = [], selectedPaper = null, paperRequest = 0;
  let statisticsRequest = 0;
  let profileRequest = 0;
  const teacher = () => getUser()?.role === "teacher";
  const dueText = value => value ? new Date(value).toLocaleString() : "无截止时间";
  const status = item => !item.published ? "草稿 / 已撤回" : item.deadline_passed ? "已截止" : "已发布";
  const localInput = value => {
    if (!value) return "";
    const date = new Date(value);
    const pad = n => String(n).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth()+1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  };
  function deadlineInput(input) {
    if (!input.value) return null;
    const date = new Date(input.value);
    if (!Number.isFinite(date.getTime())) throw new Error("请填写有效截止时间，或留空表示无截止。");
    return date.toISOString();
  }
  function fillSelect(id, entries, fallback) {
    const select = $(id), saved = select.value;
    select.textContent = "";
    for (const [value, text] of entries) {
      const option = el("option", null, text); option.value = value; select.append(option);
    }
    select.value = entries.some(([value]) => value === saved) ? saved : fallback;
  }
  async function mutation(button, action, fieldset = null) {
    const uid = getUser()?.id;
    if (button.disabled) return;
    button.disabled = true; if (fieldset) fieldset.disabled = true;
    try { await action(uid); }
    catch (error) { if (getUser()?.id === uid) showError(error.message); }
    finally { if (getUser()?.id === uid) { button.disabled = false; if (fieldset) fieldset.disabled = false; } }
  }
  function roster(preserve = false) {
    const box = $("classRoster");
    const included = new Set(preserve ? [...box.querySelectorAll("input:checked")].map(n=>n.value) : editingClass?.students.map(s => s.id) || []);
    box.textContent = "";
    // Include existing inactive members so saving does not silently remove them.
    const people = new Map(data.students.map(s => [s.id, s]));
    for (const person of editingClass?.students || []) if (!people.has(person.id)) people.set(person.id, person);
    for (const person of people.values()) {
      const label = el("label"), check = el("input"); check.type = "checkbox"; check.value = person.id;
      check.checked = included.has(person.id);
      label.append(check, document.createTextNode(` ${person.display_name}（${person.username}）`)); box.append(label);
    }
    if (!people.size) box.append(el("p", "muted", "暂无有效学生账号；需先由管理员创建账号。空班级不会自动公开作业。"));
  }
  function editClass() {
    const selected = data.classes.find(c => c.id === $("classEditor").value);
    editingClass = selected ? {...selected} : null;
    $("className").value = selected?.name || "";
    $("classEditStatus").textContent = selected ? `编辑名册 v${selected.revision}；移出成员会撤回其班级作业访问，历史提交仍保留。` : "新建班级：可先保存空班级，再编辑名册。";
    roster();
  }
  $("classEditor").onchange = editClass;
  $("btnReloadRoster").onclick = async () => { await refresh(); editClass(); };
  $("btnResetClass").onclick = () => { $("classEditor").value = ""; editClass(); };
  $("btnSaveClass").onclick = () => mutation($("btnSaveClass"), async uid => {
    const body = {name: $("className").value.trim(), student_ids: [...$("classRoster").querySelectorAll("input:checked")].map(n => n.value)};
    if (!body.name) throw new Error("请填写班级名称。");
    const existing = editingClass;
    if (existing) body.revision = existing.revision;
    const result = await api(existing ? "/api/classes/" + existing.id : "/api/classes", {
      method: existing ? "PUT" : "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)
    });
    if (getUser()?.id !== uid) return;
    await refresh(); $("classEditor").value = result.id; editClass();
  }, $("classEditorControls"));

  function draftRows() {
    const box = $("paperDraft"); box.textContent = "";
    draft.forEach((item, index) => {
      const card = el("div", "training-card");
      card.append(el("p", null, `第 ${index+1} 题 · ${item.label} · ${item.points} 分 · 每项误报扣 ${item.false_positive_penalty} 分`));
      const row = el("div", "training-actions");
      for (const [label, move] of [["上移", -1], ["下移", 1]]) {
        const button = el("button", null, label); button.type = "button";
        button.disabled = index + move < 0 || index + move >= draft.length;
        button.onclick = () => { [draft[index], draft[index+move]] = [draft[index+move], draft[index]]; draftRows(); }; row.append(button);
      }
      const remove = el("button", null, "移除本题"); remove.type = "button";
      remove.onclick = () => { draft.splice(index, 1); draftRows(); }; row.append(remove); card.append(row); box.append(card);
    });
    if (!draft.length) box.append(el("p", "muted", "尚未选择案例。请先核对案例完整答案，再添加到试卷。"));
    $("paperComposeStatus").textContent = `已选 ${draft.length}/30 题，总分值 ${draft.reduce((n, item) => n+item.points, 0)}；保存后题目与分值冻结，改变组成须另建新卷。`;
  }
  $("btnAddPaperCase").onclick = () => {
    try {
      const audit = data.audits.find(a => a.id === $("paperAudit").value);
      if (!audit) throw new Error("请选择仿真案例。");
      if (draft.length >= 30) throw new Error("每卷最多 30 个案例。");
      if (draft.some(i => i.audit_id === audit.id)) throw new Error("同一案例不能重复加入试卷。");
      const points = Number($("paperPoints").value), penalty = Number($("paperPenalty").value);
      if (!Number.isFinite(points) || points <= 0 || points > 1000000) throw new Error("题目分值须大于 0 且不超过 1000000。");
      if (!Number.isFinite(penalty) || penalty < 0 || penalty > 100) throw new Error("每项误报扣分须为 0–100。");
      draft.push({audit_id: audit.id, points, false_positive_penalty: penalty, label: `${audit.company_name} · ${audit.period} · ${audit.id.slice(-6)}`}); draftRows();
    } catch (error) { showError(error.message); }
  };
  $("btnClearPaper").onclick = () => { draft = []; draftRows(); };
  function savePaper(button, published) {
    return mutation(button, async uid => {
      const title = $("paperTitle").value.trim(), scope = $("paperClass").value;
      if (!title || !draft.length) throw new Error("请填写试卷标题并至少添加一个案例。");
      if (scope === "__choose__") throw new Error("请明确选择发布班级或全机构学生。");
      const body = {title, class_id: scope || null, deadline_at: deadlineInput($("paperDeadline")), published,
        items: draft.map(({label, ...item}) => item)};
      await api("/api/papers", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
      if (getUser()?.id !== uid) return;
      draft = []; draftRows(); await refresh();
      $("paperComposeStatus").textContent = published ? "试卷已发布；所选范围的学生可见。" : "试卷草稿已保存，学生不可见；可在下方预览后发布。";
    }, $("paperComposerControls"));
  }
  $("btnSavePaperDraft").onclick = () => savePaper($("btnSavePaperDraft"), false);
  $("btnPublishPaper").onclick = () => savePaper($("btnPublishPaper"), true);
  $("btnRefreshTraining").onclick = () => refresh();

  const percentage = value => value === null ? "—" : `${value}%`;
  const scoreText = value => value === null ? "—" : `${value} 分`;
  function statisticsTable(title, headings, rows) {
    const section = el("section"), scroll = el("div", "training-stats-scroll"), table = el("table");
    table.append(el("caption", null, title));
    const head = el("thead"), header = el("tr"), body = el("tbody");
    for (const text of headings) { const th = el("th", null, text); th.scope = "col"; header.append(th); }
    head.append(header); table.append(head, body);
    for (const cells of rows) {
      const row = el("tr");
      for (const cell of cells) row.append(el("td", null, String(cell)));
      body.append(row);
    }
    scroll.tabIndex = 0; scroll.setAttribute("aria-label", title + "，可横向滚动");
    scroll.append(table); section.append(scroll);
    if (!rows.length) section.append(el("p", "muted", "暂无可统计记录；没有记录不表示全部正确。"));
    return section;
  }
  function clearStatistics(message = "请选择班级查看学情。") {
    statisticsRequest++;
    $("classStatisticsResult").textContent = "";
    $("classStatisticsStatus").textContent = message;
    $("btnRefreshClassStatistics").disabled = false;
  }
  async function loadStatistics() {
    clearStatistics();
    const id = $("statisticsClass").value, uid = getUser()?.id, request = statisticsRequest;
    if (!teacher() || !id) return;
    const historical = $("statisticsWithdrawn").checked;
    $("classStatisticsStatus").textContent = "正在读取班级学情…";
    $("btnRefreshClassStatistics").disabled = true;
    try {
      const result = await api(`/api/classes/${encodeURIComponent(id)}/statistics?include_withdrawn=${historical}`);
      if (request !== statisticsRequest || uid !== getUser()?.id || !teacher()) return;
      const box = $("classStatisticsResult"), t = result.totals;
      $("classStatisticsStatus").textContent = `${result.class.name} · 名册 v${result.class.revision} · 有效学生 ${result.active_students} 人 · ${result.questions.length} 个案例题`;
      box.append(el("p", "training-progress", `应交 ${t.expected} 份 · 已交 ${t.submitted} 份 · 未交 ${t.unsubmitted} 份 · 有效 ${t.valid} 份 · 不可用 ${t.invalid} 份`));
      box.append(el("p", "muted", result.scope), el("p", "muted", result.method));
      box.append(el("p", "muted", `本次排除：无提交的草稿/撤回作业 ${t.excluded_unpublished_empty} 题、未勾选纳入的有提交撤回作业 ${t.excluded_withdrawn} 题、所选作业中已移出/停用/不再适用学生的提交 ${t.excluded_submissions} 份。历史数据未删除。`));
      box.append(statisticsTable("逐题正确率排名（薄弱题优先）", ["名次", "案例题", "状态", "应交 / 已交 / 未交", "全对 / 有效", "全对率", "当前均分 / 自动均分", "人工调分份数"],
        result.questions.map(q => [q.rank ?? "未排名", q.title, q.published ? "已发布（含已截止）" : "已撤回", `${q.expected} / ${q.submitted} / ${q.unsubmitted}`, `${q.exact_correct} / ${q.valid}`, percentage(q.accuracy), `${scoreText(q.mean_score)} / ${scoreText(q.automatic_mean_score)}`, q.adjusted_count])));
      for (const q of result.questions) for (const warning of q.warnings) box.append(el("p", "training-stat-warning", `${q.title}：${warning}`));
      box.append(statisticsTable("班级薄弱项分布（风险类别）", ["类别", "漏检", "误报", "材料不足仍标风险", "薄弱项占比", "漏检率", "误报率"],
        result.categories.map(c => [c.category, c.missed, c.false_positive, c.unsupported, percentage(c.error_share), percentage(c.miss_rate), percentage(c.false_positive_rate)])));
      box.append(statisticsTable("逐规则薄弱项（同编号不同口径分列）", ["规则 / 版本 / 口径", "漏检 / 命中机会", "漏检率", "误报 / 通过机会", "误报率", "材料不足仍标风险 / 未执行机会"],
        result.rules.map(r => [`${r.rule_id} ${r.name} · v${r.version} · ${r.definition_id.slice(0,8)}`, `${r.missed} / ${r.hit_opportunities}`, percentage(r.miss_rate), `${r.false_positive} / ${r.pass_opportunities}`, percentage(r.false_positive_rate), `${r.unsupported} / ${r.skipped}`])));
    } catch (error) {
      if (request === statisticsRequest && uid === getUser()?.id) $("classStatisticsStatus").textContent = "读取失败：" + error.message;
    } finally {
      if (request === statisticsRequest && uid === getUser()?.id) $("btnRefreshClassStatistics").disabled = false;
    }
  }
  $("statisticsClass").onchange = loadStatistics;
  $("statisticsWithdrawn").onchange = loadStatistics;
  $("btnRefreshClassStatistics").onclick = loadStatistics;

  const comparisonText = {first:"首次记录，暂无对照", same_basis:"同口径相邻时点", changed_basis:"口径/题目组成变化，不连线", gap:"该类记录有间隔，不连线", incomplete:"记录不完整，不比较"};
  const changeText = value => value === null ? "—" : `${value > 0 ? "+" : ""}${value} 个百分点`;
  function clearProfile(message = "请选择班级和学生查看画像。") {
    profileRequest++;
    $("studentProfileResult").textContent = "";
    $("studentProfileStatus").textContent = message;
    $("btnRefreshProfile").disabled = false;
  }
  function profileStudents(resetSelection = false) {
    const group = data.classes.find(c => c.id === $("profileClass").value);
    const active = new Set(data.students.map(s => s.id));
    if (resetSelection) $("profileStudent").value = "";
    fillSelect("profileStudent", [["", "请选择学生"], ...(group?.students || []).filter(s => active.has(s.id)).map(s => [s.id, `${s.display_name}（${s.username}）`])], "");
  }
  function profileChart(points, category) {
    const wrap = el("div", "training-stats-scroll profile-chart");
    const svgNode = (tag, attributes = {}, text = null) => {
      const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
      for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
      if (text !== null) node.textContent = text;
      return node;
    };
    const svg = svgNode("svg", {viewBox:"0 0 720 240", role:"img", "aria-label":`${category}跨作业漏检率与误报率；详细数值见下方表格`});
    svg.append(svgNode("title", {}, `${category} · 橙色漏检率，蓝色误报率；数值越低越好，断线表示不能直接比较`));
    for (const value of [0, 50, 100]) {
      const y = 190 - value * 1.5;
      svg.append(svgNode("line", {x1:48, x2:690, y1:y, y2:y, stroke:"#dbe4ef"}), svgNode("text", {x:4,y:y+4,fill:"#475569","font-size":12}, value + "%"));
    }
    const xAt = i => points.length < 2 ? 369 : 48 + 642 * i / (points.length-1);
    for (const [metric, color, label] of [["miss_rate", "#b45309", "漏检率"], ["false_positive_rate", "#2563eb", "误报率"]]) {
      let previous = null;
      points.forEach((point, index) => {
        const entry = point.categories.find(c => c.category === category), rate = entry?.[metric];
        if (rate === null || rate === undefined) { previous = null; return; }
        const x = xAt(index), y = 190 - rate * 1.5;
        if (previous && entry.comparison === "same_basis") svg.append(svgNode("line", {x1:previous.x,y1:previous.y,x2:x,y2:y,stroke:color,"stroke-width":2,"data-profile-series":metric}));
        const dot = svgNode("circle", {cx:x,cy:y,r:4,fill:color});
        dot.append(svgNode("title", {}, `时点 ${index+1} · ${dueText(point.submitted_at)} · ${label} ${rate}% · ${comparisonText[entry.comparison]}`)); svg.append(dot);
        previous = {x,y};
      });
    }
    points.forEach((point, index) => {
      if (index === 0 || index === points.length-1 || index % Math.max(1,Math.ceil(points.length/8)) === 0)
        svg.append(svgNode("text", {x:xAt(index),y:218,"text-anchor":"middle",fill:"#475569","font-size":12}, `时点 ${index+1}`));
    });
    wrap.append(svg); return wrap;
  }
  function paintProfile(result) {
    const box = $("studentProfileResult"), t = result.totals;
    const recordsById = new Map(result.records.map(r => [r.id, r]));
    $("studentProfileStatus").textContent = `${result.student.display_name} · ${result.class ? result.class.name + "（本班范围）" : "本人当前授权范围"}`;
    box.append(el("p", "training-progress", `适用 ${t.assignments} 份作业 · 已交 ${t.submitted} · 未交 ${t.unsubmitted} · 有效 ${t.valid} · 不可用 ${t.invalid}`));
    box.append(el("p", "muted", `不同案例 ${t.unique_cases} 份，重复案例提交 ${t.repeated_cases} 份；当前均分 ${scoreText(t.mean_score)}，自动均分 ${scoreText(t.automatic_mean_score)}，人工调分 ${t.adjusted} 份。`));
    box.append(el("p", "muted", result.scope));
    const method = el("details"); method.append(el("summary", null, "画像口径与比较限制"), el("p", "muted", result.method)); box.append(method);
    if (t.invalid) box.append(el("p", "training-stat-warning", `有 ${t.invalid} 份记录不可用（其中 ${t.undated} 份时间不可用）；请核查明细，不据此判断能力。`));
    box.append(statisticsTable("跨作业薄弱项（风险类别）", ["类别", "出现漏检的作业 / 涉及作业", "漏检 / 命中机会", "漏检率", "出现误报的作业", "误报 / 通过机会", "误报率", "材料不足仍标风险"],
      result.categories.map(c => [c.category, `${c.missed_cases} / ${c.case_count}${c.missed_cases >= 2 ? " · 多次漏检" : ""}`, `${c.missed} / ${c.hit_opportunities}`, percentage(c.miss_rate), `${c.false_positive_cases}${c.false_positive_cases >= 2 ? " · 多次误报" : ""}`, `${c.false_positive} / ${c.pass_opportunities}`, percentage(c.false_positive_rate), c.unsupported])));
    const choice = el("select"), label = el("label", null, "演进风险类别");
    choice.id = "profileCategory"; label.htmlFor = choice.id;
    for (const item of result.categories) { const option = el("option", null, item.category); option.value = item.category; choice.append(option); }
    const trend = el("div"); trend.id = "profileTrend";
    const renderTrend = () => {
      trend.textContent = "";
      if (!choice.value) { trend.append(el("p", "muted", "尚无有效提交，暂无能力演进记录。")); return; }
      const points = result.timeline, category = choice.value;
      trend.append(el("p", "muted", "橙色：漏检率；蓝色：误报率。数值越低越好。断线表示口径/组成变化或记录不完整；相同时间合并。难度未标定，变化不等于能力提升。"));
      if (points.filter(p => p.categories.some(c => c.category === category)).length < 2)
        trend.append(el("p", "muted", "不足两个有效时点，暂不判断跨作业趋势。"));
      trend.append(profileChart(points, category));
      trend.append(statisticsTable("按最新提交时间演进（本机时区）", ["时点 / 保存时间", "作业", "漏检 / 机会", "漏检率 / 变化", "误报 / 机会", "误报率 / 变化", "比较依据"], points.map((p,i) => {
        const c = p.categories.find(c => c.category === category);
        return [`${i+1} · ${dueText(p.submitted_at)}`, p.records.map(id => recordsById.get(id)?.title || id).join("；"), c ? `${c.missed} / ${c.hit_opportunities}` : "—", c ? `${percentage(c.miss_rate)} / ${changeText(c.miss_rate_change)}` : "—", c ? `${c.false_positive} / ${c.pass_opportunities}` : "—", c ? `${percentage(c.false_positive_rate)} / ${changeText(c.false_positive_rate_change)}` : "—", c ? comparisonText[c.comparison] : "无该类有效记录"];
      })));
    };
    const choiceField = el("div", "field"); choiceField.append(label, choice);
    choice.onchange = renderTrend; box.append(choiceField, trend); renderTrend();
    const rules = el("details"); rules.append(el("summary", null, "逐规则累计表现（按冻结口径分列）"));
    rules.append(statisticsTable("逐规则累计表现", ["规则 / 版本 / 口径", "漏检 / 命中机会", "误报 / 通过机会", "材料不足仍标风险 / 未执行"], result.rules.map(r => [`${r.rule_id} ${r.name} · v${r.version} · ${r.definition_id.slice(0,8)}`, `${r.missed} / ${r.hit_opportunities}`, `${r.false_positive} / ${r.pass_opportunities}`, `${r.unsupported} / ${r.skipped}`]))); box.append(rules);
    const records = el("details"); records.append(el("summary", null, `作业与冻结表现明细（${result.records.length} 份）`));
    for (const record of result.records) {
      const card = el("div", "training-card"); card.append(el("h3", null, record.title));
      card.append(el("p", "muted", `${record.published ? "已发布" : "已撤回"} · 最新保存：${record.submitted_at ? dueText(record.submitted_at) : "时间不可用"}`));
      card.append(el("p", record.valid ? null : "training-stat-warning", record.valid ? `当前评分 ${scoreText(record.score)} · 自动评分 ${scoreText(record.automatic_score)} · 漏检 ${record.missed} · 误报 ${record.false_positive} · 材料不足仍标风险 ${record.unsupported}` : record.warning));
      if (record.valid) { const view = el("button", null, "查看这份作业"); view.type = "button"; view.onclick = () => openExercise(record.id); card.append(view); }
      records.append(card);
    }
    box.append(records);
  }
  async function loadProfile() {
    clearProfile();
    const uid = getUser()?.id, request = profileRequest, role = getUser()?.role;
    let url = "/api/training/profile";
    if (role === "teacher") {
      const cid = $("profileClass").value, sid = $("profileStudent").value;
      if (!cid || !sid) return;
      url = `/api/classes/${encodeURIComponent(cid)}/students/${encodeURIComponent(sid)}/profile?include_withdrawn=${$("profileWithdrawn").checked}`;
    } else if (role !== "student") return;
    $("studentProfileStatus").textContent = "正在读取跨作业画像…";
    $("btnRefreshProfile").disabled = true;
    try {
      const result = await api(url);
      if (request !== profileRequest || uid !== getUser()?.id) return;
      paintProfile(result);
    } catch (error) {
      if (request === profileRequest && uid === getUser()?.id) $("studentProfileStatus").textContent = "读取失败：" + error.message;
    } finally {
      if (request === profileRequest && uid === getUser()?.id) $("btnRefreshProfile").disabled = false;
    }
  }
  $("profileClass").onchange = () => { profileStudents(true); loadProfile(); };
  $("profileStudent").onchange = loadProfile;
  $("profileWithdrawn").onchange = loadProfile;
  $("btnRefreshProfile").onclick = loadProfile;

  function settingsControls(item, kind) {
    const details = el("details"), summary = el("summary", null, "发布与截止设置"); details.append(summary);
    const row = el("div", "training-actions"), label = el("label", "training-deadline", "截止时间（本机时区，留空为不限）");
    const input = el("input"); input.type = "datetime-local"; input.step = "1"; input.value = localInput(item.deadline_at);
    input.setAttribute("aria-label", item.title + " 截止时间"); label.append(input); row.append(label);
    const update = (button, published) => mutation(button, async uid => {
      await api(`/api/${kind}/${item.id}${kind === "assignments" ? "/settings" : ""}`, {
        method: "PUT", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({revision: item.revision, published, deadline_at: deadlineInput(input)})
      });
      if (getUser()?.id === uid) await refresh();
    });
    const publish = el("button", null, item.published ? "撤回发布" : "发布"), save = el("button", null, "保存截止时间");
    publish.type = save.type = "button"; publish.onclick = () => update(publish, !item.published); save.onclick = () => update(save, item.published);
    row.append(publish, save); details.append(row); return details;
  }
  function paintPaper(item) {
    const box = $("paperDetail"); box.hidden = false; box.textContent = "";
    box.append(el("h2", null, item.title), el("p", "muted", `${status(item)} · 截止：${dueText(item.deadline_at)} · 截止以服务器提交校验为准`));
    if (item.progress) {
      const p = item.progress;
      box.append(el("p", "training-progress", `已完成 ${p.completed}/${p.case_count} 题 · 已得 ${p.earned_points}/${p.total_points} 分值 · 总评分：${p.score === null ? "待全部题目完成" : p.score + " 分"}`));
    }
    for (const question of item.items) {
      const card = el("div", "training-card");
      card.append(el("p", null, `第 ${question.position} 题 · ${question.points} 分值 · ${question.title}`));
      if (item.progress) card.append(el("p", "muted", question.score === null ? "未提交" : `本题评分：${question.score} 分`));
      const button = el("button", null, "打开本题 / 查看评分"); button.type = "button";
      button.onclick = () => openExercise(question.id); card.append(button); box.append(card);
    }
  }
  async function openPaper(id) {
    const request = ++paperRequest, uid = getUser()?.id;
    selectedPaper = null; $("paperDetail").hidden = true; $("paperDetail").textContent = "";
    try {
      const item = await api("/api/papers/" + id);
      if (request !== paperRequest || uid !== getUser()?.id) return;
      selectedPaper = id; paintPaper(item); $("paperDetail").scrollIntoView({block: "start"});
    } catch (error) { if (request === paperRequest && uid === getUser()?.id) showError(error.message); }
  }
  function render(next) {
    data = next;
    $("classManager").hidden = $("paperComposer").hidden = !teacher();
    $("classStatistics").hidden = !teacher();
    $("studentProfile").hidden = !["student","teacher"].includes(getUser()?.role);
    $("profileTeacherControls").hidden = !teacher();
    const list = $("classList"); list.textContent = "";
    for (const item of data.classes) list.append(el("p", null, `${item.name} · ${item.students?.length ?? item.member_count} 人`));
    if (!data.classes.length) list.append(el("p", "muted", teacher() ? "暂无自己管理的班级。" : "尚未加入班级；全机构公开作业仍可访问。"));
    if (teacher()) {
      const choices = data.classes.map(c => [c.id, c.name]);
      fillSelect("statisticsClass", [["", "请选择班级"], ...choices], "");
      fillSelect("profileClass", [["", "请选择班级"], ...choices], "");
      profileStudents();
      loadStatistics();
      fillSelect("classEditor", [["", "新建班级"], ...choices], "");
      fillSelect("assignmentClass", [["", "全机构学生（不限制班级）"], ...choices], "");
      fillSelect("paperClass", [["__choose__", "请选择发布范围"], ["", "全机构学生（不限制班级）"], ...choices], "__choose__");
      const synthetic = data.audits.filter(a => a.company_name.includes("仿真") || a.company_name.includes("纯合成测试") || (a.taxpayer_id || "").toUpperCase().includes("TEST"));
      fillSelect("paperAudit", [["", "请选择仿真案例"], ...synthetic.map(a => [a.id, `${a.company_name} · ${a.period} · ${a.id.slice(-6)}`])], "");
      if (!editingClass) roster(true);
      else if (data.classes.find(c => c.id === editingClass.id)?.revision !== editingClass.revision)
        $("classEditStatus").textContent = "名册版本已变化；保留当前编辑内容。请刷新名册后重新编辑，保存不会覆盖新版本。";
    }
    const papers = $("paperList"); papers.textContent = "";
    loadProfile();
    mistakeBook.load();
    for (const item of data.papers) {
      const card = el("div", "training-card");
      const scope = item.class_id ? data.classes.find(c => c.id === item.class_id)?.name || "所属班级" : "全机构学生";
      card.append(el("h3", null, item.title), el("p", "muted", `${status(item)} · ${scope} · ${item.items.length} 题 · 截止：${dueText(item.deadline_at)}`));
      if (item.progress) card.append(el("p", "training-progress", `已完成 ${item.progress.completed}/${item.progress.case_count} 题 · 总评分：${item.progress.score === null ? "未完成" : item.progress.score + " 分"}`));
      const view = el("button", null, "打开试卷 / 查看进度"); view.type = "button"; view.onclick = () => openPaper(item.id); card.append(view);
      if (teacher()) card.append(settingsControls(item, "papers")); papers.append(card);
    }
    if (!data.papers.length) papers.append(el("p", "muted", "暂无可访问的组卷作业。"));
    if (selectedPaper) {
      const item = data.papers.find(p => p.id === selectedPaper);
      if (item) paintPaper(item);
      else { selectedPaper = null; paperRequest++; $("paperDetail").hidden = true; $("paperDetail").textContent = ""; }
    }
  }
  function reset() {
    mistakeBook.reset();
    paperRequest++; selectedPaper = null; editingClass = null; draft = [];
    data = {classes: [], students: [], audits: [], papers: []};
    clearStatistics(); $("classStatistics").hidden = true;
    clearProfile(); $("studentProfile").hidden = true;
    $("profileClass").textContent = $("profileStudent").textContent = ""; $("profileWithdrawn").checked = false;
    $("statisticsClass").textContent = ""; $("statisticsWithdrawn").checked = false;
    for (const id of ["classRoster", "classList", "paperList", "paperDetail"]) $(id).textContent = "";
    $("paperDetail").hidden = true; $("classManager").hidden = $("paperComposer").hidden = true;
    $("classEditorControls").disabled = $("paperComposerControls").disabled = false;
    $("className").value = ""; $("paperClass").value = "__choose__"; $("paperDeadline").value = "";
    draftRows();
  }
  return {render, reset, deadlineInput, dueText, status, invalidatePending:()=>{paperRequest++;mistakeBook.invalidate("作业列表更新中，请稍候或刷新错题本。");clearStatistics("作业列表更新中，请稍候或刷新学情。");clearProfile("作业列表更新中，请稍候或刷新画像。");},
    singleControls: item => teacher() && item.created_by === getUser()?.id ? settingsControls(item, "assignments") : null};
};
