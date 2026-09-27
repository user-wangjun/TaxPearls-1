"use strict";
const workspace = document.querySelector("#appMain > .wrap");
const navToggle = document.getElementById("navToggle");
// 标题换行或缩放后，侧栏仍紧贴页头底边。
new ResizeObserver(([entry]) => {
  const height = entry.target.getBoundingClientRect().height;
  document.body.style.setProperty("--app-header-height", `${height}px`);
}).observe(document.getElementById("appHeader"));
function setNavCollapsed(collapsed) {
  workspace.classList.toggle("nav-collapsed", collapsed);
  navToggle.setAttribute("aria-expanded", String(!collapsed));
  const label = collapsed ? "展开侧边栏" : "收起侧边栏";
  navToggle.setAttribute("aria-label", label);
  navToggle.title = label;
}
try { setNavCollapsed(localStorage.getItem("taxpearls.navCollapsed") === "true"); } catch (_) {}
navToggle.addEventListener("click", () => {
  const collapsed = !workspace.classList.contains("nav-collapsed");
  setNavCollapsed(collapsed);
  try { localStorage.setItem("taxpearls.navCollapsed", String(collapsed)); } catch (_) {}
});
let dashboardData = {records:[],history:[],clients:[]}, knowledgeRules = [], selectedKnowledge = "";
/* ---------- 数字滚动（count-up）----------
   进视口才播；prefers-reduced-motion 时直接落终值。
   缓动 ease-out cubic：开头快、结尾慢停，符合"结果导向"。 */
const REDUCED_MOTION = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
function formatNumber(target,decimals){
  return Number(target).toLocaleString("zh-CN",{minimumFractionDigits:decimals,maximumFractionDigits:decimals});
}
function countUpText(node,target,{decimals=0,duration=950}={}){
  if(!Number.isFinite(target)){node.textContent=String(target);return;}
  const started=performance.now(),ease=t=>1-Math.pow(1-t,3);
  (function tick(now){
    const p=Math.min(((now||performance.now())-started)/duration,1);
    node.textContent=formatNumber(target*ease(p),decimals);
    if(p<1)requestAnimationFrame(tick);
  })();
}
function countUpWhenVisible(watch,node,target,{decimals=0,duration=950,delay=0}={}){
  if(REDUCED_MOTION||!("IntersectionObserver" in window)){
    node.textContent=formatNumber(Number.isFinite(target)?target:0,decimals);return;
  }
  const io=new IntersectionObserver(entries=>{
    if(entries.some(e=>e.isIntersecting)){io.disconnect();setTimeout(()=>countUpText(node,target,{decimals,duration}),delay);}
  },{threshold:.35});
  io.observe(watch);
}
const companyKey = r => r.taxpayer_id || r.company_name;
const metricSpecs = [["营业收入","营业收入"],["营业成本","营业成本"],["净利润","利润表.净利润"],["增值税应纳税额","增值税.应纳税额"]];
const metricOf = (r,key) => r.metrics[key];
const amount = m => m ? Number(m.value).toLocaleString("zh-CN", {minimumFractionDigits:2,maximumFractionDigits:2}) : "未提供";
function option(select,value,label) { const o=el("option",null,label);o.value=value;select.append(o); }
/* ---------- 自绘下拉：弹层最多显示 10 项，超出出滚动条 ----------
   原生 <select> 的弹出列表由浏览器渲染，无法限制可见条数。
   做法：隐藏原生 select（仍是唯一数据源），套一层按钮 + 列表面板；
   select 的 options 被重建时由 MutationObserver 自动同步，调用方零改动。 */
const DROPDOWN_VISIBLE_ITEMS = 10;
function enhanceSelect(select){
  if (select.closest(".dropdown")) return;
  const wrap = document.createElement("div"); wrap.className = "dropdown";
  select.parentNode.insertBefore(wrap, select); wrap.append(select);
  select.style.display = "none";
  const toggle = document.createElement("button");
  toggle.type = "button"; toggle.className = "dropdown-toggle";
  toggle.setAttribute("aria-haspopup", "listbox"); toggle.setAttribute("aria-expanded", "false");
  const menu = document.createElement("div");
  menu.className = "dropdown-menu"; menu.setAttribute("role", "listbox");
  wrap.append(toggle, menu);
  const close = () => { wrap.classList.remove("open"); toggle.setAttribute("aria-expanded", "false"); };
  toggle.addEventListener("click", e => {
    e.stopPropagation();
    const willOpen = !wrap.classList.contains("open");
    document.querySelectorAll(".dropdown.open").forEach(d => { d.classList.remove("open"); d.querySelector(".dropdown-toggle")?.setAttribute("aria-expanded", "false"); });
    if (willOpen) {
      wrap.classList.add("open"); toggle.setAttribute("aria-expanded", "true");
      menu.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: "nearest" });
    }
  });
  menu.addEventListener("click", e => {
    e.stopPropagation();
    const item = e.target.closest(".dropdown-item"); if (!item) return;
    if (select.value !== item.dataset.value) { select.value = item.dataset.value; select.dispatchEvent(new Event("change", { bubbles: true })); }
    close(); refresh();
  });
  document.addEventListener("click", close);
  document.addEventListener("keydown", e => { if (e.key === "Escape") close(); });
  function refresh() {
    toggle.textContent = select.selectedOptions[0]?.textContent ?? "";
    menu.replaceChildren(...Array.from(select.options).map(o => {
      const b = document.createElement("button");
      b.type = "button"; b.className = "dropdown-item"; b.dataset.value = o.value;
      b.textContent = o.textContent; b.setAttribute("role", "option");
      b.setAttribute("aria-selected", String(o.value === select.value));
      return b;
    }));
  }
  new MutationObserver(refresh).observe(select, { childList: true });
  refresh();
}
enhanceSelect($("dashboardCompany"));
enhanceSelect($("dashboardPeriod"));
function action(label,fn) { const b=el("button","btn re",label);b.addEventListener("click",fn);return b; }
async function openAudit(id) { try {renderResult(await api("/api/audits/"+id));} catch(e){showError(e.message);} }
function startClientUpload(){
  const client=dashboardData.clients.find(c=>c.taxpayer_id===$("dashboardCompany").value);
  selectedClientId=client?.id||"";switchPanel("uploadPanel");
}
$("dashboardUpload").addEventListener("click",startClientUpload);
let dashboardRequest = 0;
async function loadDashboard(append = false) {
  const ticket = ++dashboardRequest, userId = currentUser.id;
  const body=$("dashboardBody");
  if (!append) body.replaceChildren(el("div","empty","正在读取企业业务数据……"));
  try {
    const company = $("dashboardCompany").value;
    const page = append ? dashboardData.page + 1 : 1;
    const data = await api("/api/dashboard?page=" + page + (company ? "&company=" + encodeURIComponent(company) : ""));
    if (ticket !== dashboardRequest || currentUser?.id !== userId) return;
    dashboardData = {...data, records:append ? [...dashboardData.records, ...data.records] : data.records};
    const select=$("dashboardCompany");select.textContent="";
    for (const item of data.companies) option(select,item.key,item.name);
    select.value=data.company;
    if (!data.companies.length) option(select,"","暂无企业数据");
    updatePeriods();
  } catch(e){if(ticket===dashboardRequest)body.replaceChildren(el("div","empty","数据加载失败："+e.message),action("重新加载",()=>loadDashboard()));}
}

function updatePeriods(){
  const select=$("dashboardPeriod"),previous=select.value;select.textContent="";
  const rows=dashboardData.records.filter(r=>companyKey(r)===$("dashboardCompany").value);
  for(const r of rows)option(select,r.period,r.period);
  if(rows.some(r=>r.period===previous))select.value=previous;
  if(!rows.length)option(select,"","暂无期间");
  renderDashboard();
}
$("dashboardCompany").addEventListener("change",()=>loadDashboard());
$("dashboardPeriod").addEventListener("change",renderDashboard);
function section(title){const s=el("div","sheet");s.append(el("h2",null,title));return s;}
function renderDashboard(){
  const box=$("dashboardBody");box.textContent="";
  if(dashboardData.has_more) box.append(action("加载更早期间（已载入 "+dashboardData.records.length+" / "+dashboardData.total_periods+"）",()=>loadDashboard(true)));
  if(dashboardData.history_truncated) box.append(el("p","muted","最近审计显示最新 100 次；完整记录可在报告归档中检索。"));
  const rows=dashboardData.records.filter(r=>companyKey(r)===$("dashboardCompany").value);
  const r=rows.find(r=>r.period===$("dashboardPeriod").value);
  $("dashboardUpdated").textContent=r?"最近更新 "+r.audited_at:"";
  if(!r){const client=dashboardData.clients.find(c=>c.taxpayer_id===$("dashboardCompany").value),s=section(client?"为该客户完成首次审计":"从第一份企业材料开始");s.append(el("p","empty",client?"客户档案已建立。上传匹配该纳税人识别号的材料后，这里会展示经营指标、风险概况和历史记录。":"上传并完成审计后，这里会展示经营指标、风险概况和历史记录。"),action(client?"上传该客户材料":"上传材料",startClientUpload));box.append(s);return;}
  const kpis=el("div","kpi-grid");
  metricSpecs.forEach(([label,key],i)=>{
    const m=metricOf(r,key),c=el("div","kpi");c.append(el("p","muted",label));
    const v=el("div","value");
    if(m){
      const num=el("span",null,"0");num.style.fontVariantNumeric="tabular-nums";
      v.append(num,el("span","unit","元"));
      countUpWhenVisible(v,num,Number(m.value),{decimals:2,delay:i*90});
    } else v.textContent=amount(m);
    c.append(v);kpis.append(c);
  });box.append(kpis);
  const grid=el("div","dash-grid"),trend=section("经营数据趋势"),risk=section("风险分布");
  trend.append(el("p","muted","按业务所属期间展示 · 每期采用最新审计数据"));
  renderTrend(trend,rows);
  renderRiskDistribution(risk,r);grid.append(trend,risk);box.append(grid);
  const overview=section("审计概况"),stats=el("div","summary-grid");
  [["hit","风险命中"],["high","其中高风险"],["pass","检查通过"],["skipped","未执行"]].forEach(([key,label],i)=>{
    const c=el("div"),strong=el("strong",null,"0");
    strong.style.fontVariantNumeric="tabular-nums";
    c.append(strong,el("span","muted",label));stats.append(c);
    countUpWhenVisible(strong,strong,r.summary[key]??0,{delay:i*70});
  });overview.append(stats);
  const executed=r.summary.hit+r.summary.pass;overview.append(el("p","muted","检查通过率："+(executed?(r.summary.pass/executed*100).toFixed(1)+"%":"暂无已执行检查")+" · 分母为已执行规则数，未执行项单独列示。"));box.append(overview);
  const attention=section("需关注事项");
  for(const f of r.risks.slice().sort((a,b)=>(a.status==="skipped")-(b.status==="skipped")).slice(0,5)){
    const item=el("div","data-item"),txt=el("div");txt.append(el("div",null,f.name),el("p","muted",f.status==="skipped"?f.reason:"风险等级："+({high:"高",medium:"中",low:"低"}[f.severity])));item.append(txt,action("查看证据",()=>openAudit(r.id)));attention.append(item);
  }
  if(!r.risks.length)attention.append(el("p","empty","本次检查没有命中或未执行事项。"));box.append(attention);
  const recent=section("最近审计"),history=dashboardData.history.filter(a=>companyKey(a)===companyKey(r));
  for(const row of history.slice(0,6)){const item=el("div","data-item"),txt=el("div");txt.append(el("div",null,row.company_name+" · "+row.period),el("p","muted",row.audited_at+" · 命中 "+row.summary.hit+" 项"));item.append(txt,action("查看报告",()=>openAudit(row.id)));recent.append(item);}box.append(recent);
  box.append(el("p","source-note","同一企业、同一期间仅取最新成功审计的数据；历史版本仍保留在历史档案中。"));
}
function renderRiskDistribution(box,record){
  const levels=[
    ["high","高风险","#df6267","建议优先核对原始凭证与申报数据，确认差异原因。"],
    ["medium","中风险","#e6a447","建议逐项检查账表勾稽关系，补充差异说明。"],
    ["low","低风险","#5789e8","建议纳入日常复核，留存相关核对记录。"]
  ];
  const hits=record.risks.filter(f=>f.status==="hit"),total=hits.length;
  const layout=el("div","risk-distribution"),chart=el("div","risk-donut");
  box.classList.add("risk-chart-panel");
  const tooltip=el("div","risk-hover-card");tooltip.id="riskHoverInfo";tooltip.setAttribute("role","tooltip");tooltip.hidden=true;box.append(tooltip);
  function hideTooltip(){tooltip.hidden=true;box.querySelectorAll(".risk-segment.is-highlighted").forEach(n=>n.classList.remove("is-highlighted"));}
  function placeTooltip(event,target){
    const bounds=box.getBoundingClientRect(),anchor=target.getBoundingClientRect();
    const x=event&&Number.isFinite(event.clientX)?event.clientX:anchor.left+anchor.width/2;
    const y=event&&Number.isFinite(event.clientY)?event.clientY:anchor.top+anchor.height/2;
    const width=tooltip.offsetWidth,height=tooltip.offsetHeight;
    tooltip.style.left=Math.max(8,Math.min(x-bounds.left+16,bounds.width-width-8))+"px";
    tooltip.style.top=Math.max(8,(y+height+16>innerHeight?y-bounds.top-height-12:y-bounds.top+16))+"px";
  }
  function bindTooltip(target,label,color,findings,percent,segment){
    target.setAttribute("tabindex","0");target.setAttribute("aria-describedby",tooltip.id);
    const show=event=>{
      hideTooltip();tooltip.replaceChildren();const heading=el("div","risk-hover-heading"),name=el("strong","risk-label",label);name.style.setProperty("--risk-color",color);
      heading.append(name,el("span",null,findings.length+" 项 · "+percent));tooltip.append(heading);
      if(findings.length){const list=el("ul","risk-hover-findings");for(const f of findings.slice(0,5))list.append(el("li",null,f.id+" · "+f.name));tooltip.append(list);if(findings.length>5)tooltip.append(el("p","muted","另有 "+(findings.length-5)+" 项，可在风险证据中查看。"));}
      else tooltip.append(el("p","muted","当前没有该等级的命中事项。"));
      tooltip.hidden=false;segment?.classList.add("is-highlighted");placeTooltip(event,target);
    };
    target.addEventListener("pointerenter",show);target.addEventListener("pointermove",e=>{if(!tooltip.hidden)placeTooltip(e,target);});target.addEventListener("pointerleave",hideTooltip);
    target.addEventListener("focus",()=>show(null));target.addEventListener("blur",hideTooltip);target.addEventListener("click",show);target.addEventListener("keydown",e=>{if(e.key==="Escape")hideTooltip();});
  }
  const svg=svgNode("svg",{viewBox:"0 0 180 180",role:"img","aria-label":total?"风险分布，共 "+total+" 项命中":"暂无命中风险"});
  svg.append(svgNode("circle",{cx:90,cy:90,r:70,fill:"none",stroke:"#eef2f7","stroke-width":32}));
  const legend=el("div","risk-breakdown"),details=el("div","risk-details");
  let offset=0,segCount=0;
  for(const [key,label,color,advice] of levels){
    const findings=hits.filter(f=>f.severity===key),count=findings.length,share=total?count/total*100:0;
    const percent=Number(share.toFixed(1))+"%";
    let segment=null;
    if(count){
      segment=svgNode("circle",{cx:90,cy:90,r:70,fill:"none",stroke:color,"stroke-width":32,pathLength:100,"stroke-dasharray":share+" "+(100-share),"stroke-dashoffset":-offset,transform:"rotate(-90 90 90)",class:"risk-segment","aria-label":label+"："+count+" 项，占 "+percent});
      if(!REDUCED_MOTION){
        /* 描边生长：先归零，进帧后过渡到目标弧长，多段依次错峰展开。 */
        segment.style.strokeDasharray="0 100";
        segment.style.transition="stroke-dasharray .8s cubic-bezier(0.22,1,0.36,1) "+(segCount*140)+"ms, opacity .15s";
        requestAnimationFrame(()=>requestAnimationFrame(()=>{segment.style.strokeDasharray=share+" "+(100-share);}));
      }
      bindTooltip(segment,label,color,findings,percent,segment);svg.append(segment);offset+=share;segCount++;
    }
    const row=el("div","risk-legend-row"),labelNode=el("span","risk-label",label);labelNode.style.setProperty("--risk-color",color);
    row.append(labelNode,el("strong",null,count+" 项"),el("span","risk-percent",percent));legend.append(row);
    bindTooltip(row,label,color,findings,percent,segment);
    if(count){
      const detail=el("div","risk-detail"),heading=el("strong","risk-label",label+" · "+count+" 项");heading.style.setProperty("--risk-color",color);
      detail.append(heading,el("p","risk-findings",findings.slice(0,2).map(f=>f.name).join("；")+(count>2?"等 "+count+" 项":"")),el("p","muted",advice));details.append(detail);
    }
  }
  const totalText=svgNode("text",{x:90,y:88,"text-anchor":"middle",class:"risk-total"},"0");
  totalText.style.fontVariantNumeric="tabular-nums";
  svg.append(totalText,svgNode("text",{x:90,y:112,"text-anchor":"middle",class:"risk-center-label"},"命中事项"));
  if(total)countUpWhenVisible(svg,totalText,total,{duration:800});
  chart.append(svg);layout.append(chart,legend);box.append(layout);
  if(total)box.append(details,action("查看风险证据",()=>openAudit(record.id)));
  else box.append(el("p","risk-empty","当前没有命中的风险事项。"));
  const skipped=record.summary.skipped||0;
  box.append(el("p","source-note","占比按已命中事项数量计算。"+(skipped?"另有 "+skipped+" 项检查未执行，需补齐材料后复核。":"")));
}
function svgNode(tag,attrs,text){const n=document.createElementNS("http://www.w3.org/2000/svg",tag);for(const [k,v]of Object.entries(attrs))n.setAttribute(k,v);if(text!==undefined)n.textContent=text;return n;}
// Only connect comparable, explicitly recognized period granularities.
function periodInfo(value){
  const range=value.match(/^(\d{4})-(\d{2})-(\d{2})\s*(?:至|~|—)\s*(\d{4})-(\d{2})-(\d{2})$/);
  if(range){
    const [,ys,ms,ds,ye,me,de]=range.map(Number),months=(ye-ys)*12+me-ms+1;
    const full=ds===1&&ms>=1&&ms<=12&&me>=1&&me<=12&&de===new Date(Date.UTC(ye,me,0)).getUTCDate();
    if(full&&months===1)return{group:"month",order:ys*12+ms};
    if(full&&months===3&&[1,4,7,10].includes(ms))return{group:"quarter",order:ys*4+Math.ceil(ms/3)};
    if(full&&months===6&&[1,7].includes(ms))return{group:"half",order:ys*2+Math.ceil(ms/6)};
    if(full&&months===12&&ms===1)return{group:"year",order:ys};
    return null;
  }
  let m=value.match(/^(\d{4})[-年/.](\d{1,2})月?$/);if(m&&+m[2]>=1&&+m[2]<=12)return {group:"month",order:+m[1]*12+ +m[2]};
  m=value.match(/^(\d{4})[- ]?Q([1-4])$/i);if(m)return{group:"quarter",order:+m[1]*4+ +m[2]};
  m=value.match(/^(\d{4})[- ]?H([12])$/i);if(m)return{group:"half",order:+m[1]*2+ +m[2]};
  m=value.match(/^(\d{4})年?$/);return m?{group:"year",order:+m[1]}:null;
}
function emptyChart(title,sub){
  /* 空状态占位图：与真实图表同尺寸同网格，明示"这里是空的"。 */
  const svg=svgNode("svg",{viewBox:"0 0 620 240",class:"chart",role:"img","aria-label":title});
  for(let i=0;i<4;i++)svg.append(svgNode("line",{x1:65,y1:30+i*53.3,x2:590,y2:30+i*53.3,stroke:"#edf0f6"}));
  svg.append(svgNode("text",{x:327,y:112,"text-anchor":"middle",class:"chart-empty-title"},title));
  svg.append(svgNode("text",{x:327,y:138,"text-anchor":"middle",class:"chart-empty-sub"},sub));
  return svg;
}
function renderTrend(box,rows){
  const selected=periodInfo($("dashboardPeriod").value);
  const data=selected?rows.filter(r=>periodInfo(r.period)?.group===selected.group).sort((a,b)=>periodInfo(a.period).order-periodInfo(b.period).order).slice(-8):[];
  if(data.length<2){box.append(emptyChart("暂无可比较的跨期数据","同一企业至少两个相同粒度的明确期间，才能连线比较"));return;}
  const svg=svgNode("svg",{viewBox:"0 0 620 240",class:"chart",role:"img","aria-label":"营业收入与营业成本趋势，单位元"});
  const values=data.flatMap(r=>["营业收入","营业成本"].map(k=>metricOf(r,k)).filter(Boolean).map(m=>Number(m.value)));
  if(!values.length){box.append(emptyChart("所选期间没有收入或成本数据","上传含利润表的材料后，这里会展示趋势折线图"));return;}
  const low=Math.min(0,...values),high=Math.max(1,...values),x=i=>65+i*520/(data.length-1),y=v=>190-(v-low)/(high-low)*160;
  for(let i=0;i<4;i++){const v=low+(high-low)*i/3,yy=y(v);svg.append(svgNode("line",{x1:65,y1:yy,x2:590,y2:yy,stroke:"#edf0f6"}),svgNode("text",{x:57,y:yy+4,"text-anchor":"end",class:"chart-label"},(v/10000).toFixed(1)+"万"));}
  /* 折线生长动画 + 悬停浮卡：线段 dashoffset 扫描展开、数据点沿线点亮；悬停出浮卡 + 竖向参考线。 */
  const SWEEP=800;
  box.classList.add("trend-chart-panel");
  const tooltip=el("div","risk-hover-card");tooltip.hidden=true;
  const guide=svgNode("line",{y1:22,y2:196,stroke:"#b9c7dd","stroke-dasharray":"3 4",class:"trend-guide"});
  svg.append(guide);
  function placeTrendTip(event,target){
    const bounds=box.getBoundingClientRect(),anchor=target.getBoundingClientRect();
    const px=event&&Number.isFinite(event.clientX)?event.clientX:anchor.left+anchor.width/2;
    const py=event&&Number.isFinite(event.clientY)?event.clientY:anchor.top+anchor.height/2;
    const w=tooltip.offsetWidth,h=tooltip.offsetHeight;
    tooltip.style.left=Math.max(8,Math.min(px-bounds.left+16,bounds.width-w-8))+"px";
    tooltip.style.top=Math.max(8,(py+h+16>innerHeight?py-bounds.top-h-12:py-bounds.top+16))+"px";
  }
  function bindTrendDot(hit,dot,p){
    const show=e=>{
      tooltip.replaceChildren();
      const heading=el("div","risk-hover-heading"),name=el("strong","risk-label",p.key);
      name.style.setProperty("--risk-color",p.color);
      heading.append(name,el("span",null,p.periodLabel));
      tooltip.append(heading,el("p","trend-hover-value",p.valueText));
      tooltip.hidden=false;
      guide.setAttribute("x1",p.x);guide.setAttribute("x2",p.x);
      dot.classList.add("is-active");guide.classList.add("is-active");
      placeTrendTip(e,hit);
    };
    const hide=()=>{tooltip.hidden=true;dot.classList.remove("is-active");guide.classList.remove("is-active");};
    hit.addEventListener("pointerenter",show);
    hit.addEventListener("pointermove",e=>{if(!tooltip.hidden)placeTrendTip(e,hit);});
    hit.addEventListener("pointerleave",hide);
    hit.addEventListener("focus",()=>show(null));
    hit.addEventListener("blur",hide);
  }
  for(const [key,color]of [["营业收入","#2865e8"],["营业成本","#6bbda9"]]){
    const pts=data.map((r,i)=>{const m=metricOf(r,key);return m?{x:x(i),y:y(Number(m.value)),i,periodLabel:r.period,valueText:amount(m)+" 元"}:null;});
    const segs=[];
    for(let j=0;j<pts.length-1;j++)if(pts[j]&&pts[j+1])segs.push([pts[j],pts[j+1]]);
    const spacing=segs.length?SWEEP/segs.length:0,dur=Math.round(spacing+150);
    segs.forEach((seg,k)=>{
      const line=svgNode("line",{x1:seg[0].x,y1:seg[0].y,x2:seg[1].x,y2:seg[1].y,stroke:color,"stroke-width":3,"stroke-linecap":"round"});
      svg.append(line);
      if(!REDUCED_MOTION){
        const len=Math.hypot(seg[1].x-seg[0].x,seg[1].y-seg[0].y);
        line.style.strokeDasharray=len+" "+len;line.style.strokeDashoffset=len;
        line.style.transition="stroke-dashoffset "+dur+"ms linear "+Math.round(k*spacing)+"ms";
        requestAnimationFrame(()=>requestAnimationFrame(()=>{line.style.strokeDashoffset="0";}));
      }
    });
    for(const p of pts){
      if(!p)continue;
      const dot=svgNode("circle",{cx:p.x,cy:p.y,r:5,fill:color,class:"trend-dot"});
      svg.append(dot);
      const hit=svgNode("circle",{cx:p.x,cy:p.y,r:14,tabindex:0,class:"trend-hit","aria-label":p.periodLabel+" "+key+"："+p.valueText});
      bindTrendDot(hit,dot,{x:p.x,key,color,periodLabel:p.periodLabel,valueText:p.valueText});
      svg.append(hit);
      if(!REDUCED_MOTION){
        dot.style.opacity="0";
        dot.style.transition="opacity .25s ease "+Math.round(p.i/(data.length-1)*SWEEP+180)+"ms";
        requestAnimationFrame(()=>requestAnimationFrame(()=>{dot.style.opacity="1";}));
        dot.addEventListener("transitionend",()=>{dot.style.opacity="";dot.style.transition="";},{once:true});
      }
    }
  }
  data.forEach((r,i)=>{const p=periodInfo(r.period),labels={month:()=>Math.floor((p.order-1)/12)+"-"+String((p.order-1)%12+1).padStart(2,"0"),quarter:()=>Math.floor((p.order-1)/4)+"Q"+((p.order-1)%4+1),half:()=>Math.floor((p.order-1)/2)+"H"+((p.order-1)%2+1),year:()=>String(p.order)};svg.append(svgNode("text",{x:x(i),y:224,"text-anchor":"middle",class:"chart-label"},labels[p.group]()));});
  const legend=el("div","legend");for(const [text,color]of [["营业收入","#2865e8"],["营业成本","#6bbda9"]]){const n=el("span",null,text);n.style.setProperty("--color",color);legend.append(n);}box.append(svg,legend,el("p","source-note","缺失指标不连线；仅比较相同期间粒度，最多展示最近 8 期。"),tooltip);
}
