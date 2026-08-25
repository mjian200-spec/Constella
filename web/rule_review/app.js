const state = {
  summary: null,
  selected: null,
  detail: null,
  detailTab: "rules",
  outputTab: "candidate",
  ruleQuery: "",
  groupMode: "compact",
  autoRefreshInitialized: false,
};

const $ = query => document.querySelector(query);
const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;",
}[char]));
const short = (value, length = 105) => {
  const text = String(value ?? "").replace(/\s+/g, " ");
  return text.length > length ? `${text.slice(0, length)}…` : text;
};
const duration = seconds => {
  if (seconds == null || !Number.isFinite(seconds)) return "尚无估算";
  seconds = Math.max(0, Math.round(seconds));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor(seconds % 3600 / 60);
  const rest = seconds % 60;
  return hours ? `${hours}小时${minutes}分` : (minutes ? `${minutes}分${rest}秒` : `${rest}秒`);
};
const statusLabel = value => ({
  success: "成功", no_rule: "无规则", failed: "失败", pending: "等待",
  generating: "生成中", reflecting: "反思中", applying_reflection: "应用反思",
  parsing: "解析中", writing_graph: "写图中", appropriate: "合适",
  inappropriate: "不合适", unreviewed: "未审核", completed: "已完成", running: "运行中",
}[value] || value || "未知");

async function api(path, options) {
  const response = await fetch(path, {cache: "no-store", ...options});
  if (!response.ok) throw new Error(await response.text() || response.status);
  return response.json();
}

function renderMetrics(summary) {
  const counts = summary.counts || {};
  const stats = summary.result_stats || {};
  const reviews = summary.feedback_counts || {};
  const reviewed = (reviews.appropriate || 0) + (reviews.inappropriate || 0);
  const values = [
    [summary.package_count || 0, "上下文包", ""],
    [(stats.total_rules || 0).toLocaleString(), "最终规则", ""],
    [Number(stats.average_rules || 0).toFixed(1), "平均规则/包", ""],
    [stats.over_20 || 0, "超过 20 条", stats.over_20 ? "attention" : ""],
    [reviewed, `已人工审核 · ${reviews.unreviewed || 0} 待审`, ""],
    [counts.failed || 0, "处理失败", counts.failed ? "danger" : ""],
  ];
  $("#metrics").innerHTML = values.map(([value, label, tone]) => (
    `<div class="metric ${tone}"><b>${esc(value)}</b><span>${esc(label)}</span></div>`
  )).join("");
}

function renderProgress(summary) {
  const counts = summary.counts || {};
  const progress = summary.progress || {};
  const total = summary.package_count || 0;
  const done = progress.completed ?? ((counts.success || 0) + (counts.no_rule || 0) + (counts.failed || 0));
  const active = progress.active || 0;
  const queued = progress.queued ?? Math.max(0, total - done - active);
  const percent = total ? Math.round(done * 1000 / total) / 10 : 0;
  const completed = summary.run?.status === "completed";
  $("#progress").classList.toggle("completed", completed);
  $("#progress").innerHTML = `
    <div class="progress-head">
      <b>${completed ? "全量抽取已完成" : `完成 ${percent}%`} · ${done} / ${total}</b>
      <span>${completed
        ? `耗时 ${duration(progress.elapsed_seconds)} · ${Number(progress.throughput_per_minute || 0).toFixed(1)} 包/分钟`
        : `处理中 ${active} · 队列 ${queued} · ${Number(progress.throughput_per_minute || 0).toFixed(1)} 包/分钟`}</span>
    </div>
    <div class="progress-track" role="progressbar" aria-valuemin="0" aria-valuemax="${total}" aria-valuenow="${done}">
      <div class="progress-fill" style="width:${percent}%"></div>
    </div>
    ${completed ? "" : `<div class="meta">已运行 ${duration(progress.elapsed_seconds)} · 预计剩余 ${duration(progress.estimated_remaining_seconds)}</div>`}`;
}

function matchesRuleRange(count, range) {
  if (!range) return true;
  if (range === "0") return count === 0;
  if (range === "1-10") return count >= 1 && count <= 10;
  if (range === "11-20") return count >= 11 && count <= 20;
  if (range === "21-40") return count >= 21 && count <= 40;
  return count > 40;
}

function filteredPackages() {
  if (!state.summary) return [];
  const query = $("#query").value.trim().toLowerCase();
  const runFilter = $("#filter").value;
  const reviewFilter = $("#review-filter").value;
  const ruleFilter = $("#rule-filter").value;
  const rows = state.summary.packages.filter(item => {
    const haystack = `${item.id} ${(item.section_path || []).join(" ")} ${item.snippet || ""}`.toLowerCase();
    return (!runFilter || item.status === runFilter)
      && (!reviewFilter || item.review_status === reviewFilter)
      && matchesRuleRange(Number(item.rule_count || 0), ruleFilter)
      && (!query || haystack.includes(query));
  });
  const sort = $("#sort").value;
  rows.sort((left, right) => {
    if (sort === "rules-desc") return right.rule_count - left.rule_count || left.id.localeCompare(right.id);
    if (sort === "rules-asc") return left.rule_count - right.rule_count || left.id.localeCompare(right.id);
    if (sort === "review") {
      const order = {unreviewed: 0, inappropriate: 1, appropriate: 2};
      return order[left.review_status] - order[right.review_status] || left.id.localeCompare(right.id);
    }
    return left.id.localeCompare(right.id);
  });
  return rows;
}

function renderList() {
  const list = $("#list");
  const scrollTop = list.scrollTop;
  const rows = filteredPackages();
  const totalRules = rows.reduce((sum, item) => sum + Number(item.rule_count || 0), 0);
  $("#list-meta").innerHTML = `<b>${rows.length}</b> 个包 · <b>${totalRules.toLocaleString()}</b> 条规则`;
  list.innerHTML = rows.length ? rows.map(item => {
    const dense = item.rule_count > 40 ? "very-dense" : (item.rule_count > 20 ? "dense" : "");
    return `<button class="row ${item.id === state.selected ? "selected" : ""}" data-id="${esc(item.id)}">
      <span class="row-head"><b>${esc(item.id)}</b><span class="rule-count ${dense}">${item.rule_count} 条</span></span>
      <small class="path">${esc((item.section_path || []).join(" / ") || "未标注章节")}</small>
      <small>${esc(short(item.snippet))}</small>
      <span class="row-flags"><span class="badge ${esc(item.status)}">${esc(statusLabel(item.status))}</span><span class="badge ${esc(item.review_status)}">${esc(statusLabel(item.review_status))}</span>${item.asset_count ? `<span class="meta">${item.asset_count} 资源</span>` : ""}</span>
    </button>`;
  }).join("") : '<div class="empty">没有符合筛选条件的包。</div>';
  list.scrollTop = scrollTop;
  list.querySelectorAll("[data-id]").forEach(element => {
    element.onclick = () => selectPackage(element.dataset.id);
  });
}

function unitCard(unit) {
  const content = typeof unit.content === "string" ? unit.content : JSON.stringify(unit.content, null, 2);
  return `<div class="content-card"><div class="content-head"><b>${esc(unit.id)}</b><span class="badge">${esc(unit.type)}</span><span class="meta">第 ${Number(unit.source?.page ?? -1) + 1} 页 · ${esc(unit.source?.original_block_id || "")}</span></div><div class="content-body">${esc(content)}</div></div>`;
}

function assetCard(asset, packageId) {
  const unit = asset.unit;
  const content = typeof unit.content === "string" ? unit.content : JSON.stringify(unit.content, null, 2);
  let body = unit.type === "table" && content.includes("<table") ? content : `<pre>${esc(content)}</pre>`;
  if (asset.resolved_path) body = `<img loading="lazy" src="/asset/${encodeURIComponent(packageId)}/${encodeURIComponent(unit.id)}" alt="${esc(asset.caption || unit.id)}">${body}`;
  return `<div class="asset"><div class="content-head"><b>${esc(unit.id)}</b><span class="badge">${esc(unit.type)}</span></div><p class="meta">${esc(asset.caption || "")}</p>${body}</div>`;
}

function parseStatus(detail) {
  const current = detail.state || {};
  if (current.status === "success") return `<span class="badge success">可解析并已写入图</span><b>${(current.rule_ids || []).length} 条规则</b>`;
  if (current.status === "no_rule") return '<span class="badge no_rule">模型明确无规则</span>';
  if (current.status === "failed") return `<span class="badge failed">处理失败</span><span class="error">${esc(current.failure_code || "")} ${esc(current.failure_reason || "")}</span>`;
  return `<span class="badge ${esc(current.status || "pending")}">${esc(statusLabel(current.status || "pending"))}</span>`;
}

function tabButton(id, label, count = null) {
  return `<button class="tab ${state.detailTab === id ? "active" : ""}" data-detail-tab="${id}">${label}${count == null ? "" : ` <span>${count}</span>`}</button>`;
}

function renderContext(detail) {
  const resolved = detail.resolved;
  return `<div class="tab-panel">
    <section class="panel-section"><h3>核心正文</h3>${resolved.core_units.map(unitCard).join("") || '<div class="empty compact">无核心正文</div>'}</section>
    <details class="panel-section" open><summary>支撑内容 <span>${resolved.support_units.length}</span></summary>${resolved.support_units.map(unitCard).join("") || '<div class="empty compact">无支撑内容</div>'}</details>
    <details class="panel-section" open><summary>标题与上下文约束 <span>${resolved.constraints.length}</span></summary><div class="constraint-list">${resolved.constraints.map(item => `<span class="constraint"><b>${esc(item.type)}</b>${esc(item.value)}</span>`).join("") || '<span class="meta">无明确约束</span>'}</div></details>
    <details class="panel-section"><summary>关联图片、表格与公式 <span>${resolved.assets.length}</span></summary><div class="assets">${resolved.assets.map(asset => assetCard(asset, detail.package.id)).join("") || '<div class="empty compact">无关联资源</div>'}</div></details>
  </div>`;
}

function outputButton(id, label) {
  return `<button class="subtab ${state.outputTab === id ? "active" : ""}" data-output-tab="${id}">${label}</button>`;
}

function renderOutputs(detail) {
  const outputs = detail.model_outputs || {};
  const labels = {generate: "第一次生成", reflect: "反思修改指令", candidate: "最终 DSL"};
  const value = outputs[state.outputTab] || "尚未产生";
  return `<div class="tab-panel">
    <div class="notice">反思区展示的是对初稿的修改、删除或补充指令；最终结果以“最终 DSL”为准。</div>
    <div class="subtabs">${outputButton("generate", labels.generate)}${outputButton("reflect", labels.reflect)}${outputButton("candidate", labels.candidate)}</div>
    <div class="output-head"><b>${labels[state.outputTab]}</b><button class="copy-button" data-copy-output="${state.outputTab}">复制</button></div>
    <pre class="model-output">${esc(value)}</pre>
  </div>`;
}

function statePills(items) {
  if (!items?.length) return '<span class="condition none">C: 无</span>';
  return items.map(item => `<span class="condition"><b>${esc(item.object)}</b><span>${esc(item.raw_state)}</span></span>`).join("");
}

function groupedRules(rules) {
  const groups = new Map();
  for (const rule of rules) {
    const id = rule.rule_group_id || "未分组";
    if (!groups.has(id)) groups.set(id, []);
    groups.get(id).push(rule);
  }
  return [...groups.entries()];
}

function renderRuleGroups(detail) {
  const allRules = detail.ruleset?.rules || [];
  const query = state.ruleQuery.trim().toLowerCase();
  const groups = groupedRules(allRules);
  const filteredGroups = groups.map(([id, rules]) => [id, rules.filter(rule => !query || JSON.stringify(rule).toLowerCase().includes(query))])
    .filter(([, rules]) => rules.length);
  const collapseLarge = allRules.length > 30;
  const groupHtml = filteredGroups.map(([groupId, rules], index) => {
    const open = state.groupMode === "expanded" || (!collapseLarge && state.groupMode !== "collapsed") || (collapseLarge && index === 0 && state.groupMode === "compact");
    const cards = rules.map(rule => `<article class="rule-card">
      <div class="rule-card-head"><span>#${rule.rule_index}</span><b>${esc(rule.relation || "未命名关系")}</b><code>${esc(rule.id)}</code></div>
      <div class="conditions"><span class="field-label">约束</span>${statePills(rule.conditions)}</div>
      <div class="expression">${esc(rule.raw_expression || "")}</div>
    </article>`).join("");
    return `<details class="rule-group" ${open ? "open" : ""}><summary><b>${esc(groupId)}</b><span>${rules.length} 条规则</span></summary><div class="rule-grid">${cards}</div></details>`;
  }).join("");
  return `<div class="rule-tools">
      <input id="rule-query" type="search" value="${esc(state.ruleQuery)}" placeholder="在当前包的条件、对象、状态、关系中搜索">
      <button data-group-mode="expanded">全部展开</button><button data-group-mode="collapsed">全部折叠</button>
      <span class="meta">显示 ${filteredGroups.reduce((sum, [, rules]) => sum + rules.length, 0)} / ${allRules.length} 条</span>
    </div>
    ${groupHtml || '<div class="empty">没有匹配的结构化规则。</div>'}
    <details class="raw-json"><summary>查看原始结构化 JSON</summary><pre>${esc(JSON.stringify(allRules, null, 2))}</pre></details>`;
}

function renderRules(detail) {
  if (!detail.ruleset) return '<div class="empty">尚无成功解析的结构化规则。</div>';
  return `<div class="tab-panel">${renderRuleGroups(detail)}</div>`;
}

function renderFeedback(detail) {
  const feedback = detail.feedback;
  return `<section class="feedback" id="feedback-panel">
    <div class="feedback-heading"><div><h3>人工反馈</h3><p>${feedback ? `已保存为 <span class="badge ${esc(feedback.verdict)}">${esc(statusLabel(feedback.verdict))}</span> <span class="meta">${esc(feedback.saved_at)}</span>` : '<span class="meta">尚未审核这个包</span>'}</p></div><span id="feedback-status" class="meta"></span></div>
    <div class="verdict-options">
      <label class="verdict appropriate"><input type="radio" name="verdict" value="appropriate" ${feedback?.verdict === "appropriate" ? "checked" : ""}> 抽取结果合适</label>
      <label class="verdict inappropriate"><input type="radio" name="verdict" value="inappropriate" ${feedback?.verdict === "inappropriate" ? "checked" : ""}> 抽取结果不合适</label>
    </div>
    <div id="correction-fields" class="correction-fields ${feedback?.verdict === "inappropriate" ? "visible" : ""}">
      <label><b>参考答案</b><span class="meta">不合适时必填，填写修正后的完整 DSL</span><textarea id="standard" spellcheck="false">${esc(feedback?.standard_result || "")}</textarea></label>
      <label><b>问题说明</b><textarea id="note">${esc(feedback?.note || "")}</textarea></label>
    </div>
    <div class="feedback-actions"><button id="save-feedback" class="primary">保存反馈</button><button id="save-next" type="button">保存并打开下一个未审核包</button></div>
  </section>`;
}

function bindDetailActions() {
  document.querySelectorAll("[data-detail-tab]").forEach(button => {
    button.onclick = () => { state.detailTab = button.dataset.detailTab; renderDetail(); };
  });
  document.querySelectorAll("[data-output-tab]").forEach(button => {
    button.onclick = () => { state.outputTab = button.dataset.outputTab; renderDetail(); };
  });
  document.querySelectorAll("[data-group-mode]").forEach(button => {
    button.onclick = () => { state.groupMode = button.dataset.groupMode; renderDetail(); };
  });
  const ruleQuery = $("#rule-query");
  if (ruleQuery) ruleQuery.oninput = event => {
    state.ruleQuery = event.target.value;
    const position = event.target.selectionStart;
    const container = event.target.closest(".tab-panel");
    container.innerHTML = renderRuleGroups(state.detail);
    bindDetailActions();
    $("#rule-query")?.focus();
    $("#rule-query")?.setSelectionRange(position, position);
  };
  document.querySelectorAll("[data-copy-output]").forEach(button => {
    button.onclick = async () => {
      await navigator.clipboard.writeText(state.detail.model_outputs[button.dataset.copyOutput] || "");
      button.textContent = "已复制";
    };
  });
  $("#previous-package").onclick = () => navigatePackage(-1);
  $("#next-package").onclick = () => navigatePackage(1);
  $("#refresh-detail").onclick = () => selectPackage(state.selected, true);
  document.querySelectorAll('input[name="verdict"]').forEach(input => {
    input.onchange = () => $("#correction-fields").classList.toggle("visible", input.value === "inappropriate" && input.checked);
  });
  $("#save-feedback").onclick = () => saveFeedback(false);
  $("#save-next").onclick = () => saveFeedback(true);
}

function renderDetail() {
  const detail = state.detail;
  if (!detail) return;
  const resolved = detail.resolved;
  const ruleCount = detail.ruleset?.rules?.length || 0;
  const tabs = `${tabButton("rules", "结构化规则", ruleCount)}${tabButton("context", "原始上下文", resolved.core_units.length + resolved.support_units.length)}${tabButton("outputs", "模型输出", 3)}`;
  const panel = state.detailTab === "context" ? renderContext(detail) : (state.detailTab === "outputs" ? renderOutputs(detail) : renderRules(detail));
  $("#detail").innerHTML = `
    <div class="detail-header">
      <div><div class="eyebrow">上下文包</div><h2>${esc(detail.package.id)}</h2><p>${esc((resolved.section_path || []).join(" / ") || "未标注章节")}</p></div>
      <div class="detail-actions"><button id="previous-package" title="上一个（←）">← 上一个</button><button id="next-package" title="下一个（→）">下一个 →</button><button id="refresh-detail">刷新</button></div>
    </div>
    <div class="package-status">${parseStatus(detail)}${ruleCount > 20 ? `<span class="badge attention">高密度结果 · ${ruleCount} 条</span>` : ""}</div>
    <nav class="tabs">${tabs}</nav>
    ${panel}
    ${renderFeedback(detail)}`;
  bindDetailActions();
}

async function selectPackage(id, force = false) {
  if (!force && id === state.selected && state.detail) return;
  state.selected = id;
  state.detail = null;
  state.ruleQuery = "";
  state.groupMode = "compact";
  renderList();
  $("#detail").innerHTML = '<div class="empty">读取包资源与抽取记录…</div>';
  try {
    state.detail = await api(`/api/package/${encodeURIComponent(id)}`);
    history.replaceState(null, "", `#${encodeURIComponent(id)}`);
    renderDetail();
  } catch (error) {
    $("#detail").innerHTML = `<div class="error-block">读取失败：${esc(error.message)}</div>`;
  }
}

function navigatePackage(offset, unreviewedOnly = false) {
  const rows = filteredPackages();
  if (!rows.length) return;
  let index = rows.findIndex(item => item.id === state.selected);
  for (let attempts = 0; attempts < rows.length; attempts += 1) {
    index = (index + offset + rows.length) % rows.length;
    if (!unreviewedOnly || rows[index].review_status === "unreviewed") {
      selectPackage(rows[index].id);
      return;
    }
  }
}

async function saveFeedback(openNext) {
  const verdict = document.querySelector('input[name="verdict"]:checked')?.value;
  const status = $("#feedback-status");
  if (!verdict) { status.textContent = "请先选择判断。"; return; }
  const standardResult = $("#standard")?.value || "";
  const note = $("#note")?.value || "";
  if (verdict === "inappropriate" && !standardResult.trim()) {
    status.textContent = "不合适时必须填写参考答案。";
    $("#standard").focus();
    return;
  }
  status.textContent = "正在保存…";
  try {
    await api("/api/feedback", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({context_package_id: state.selected, verdict, standard_result: standardResult, note}),
    });
    await refreshSummary(false);
    if (openNext) navigatePackage(1, true);
    else await selectPackage(state.selected, true);
  } catch (error) {
    status.textContent = `保存失败：${error.message}`;
  }
}

async function refreshSummary(showTimestamp = true) {
  try {
    state.summary = await api("/api/summary");
    renderMetrics(state.summary);
    renderProgress(state.summary);
    renderList();
    const run = state.summary.run || {};
    const completed = run.status === "completed";
    $("#run-badge").className = `run-badge ${completed ? "completed" : "running"}`;
    $("#run-badge").textContent = statusLabel(run.status || "未开始");
    $("#status").textContent = `${run.model ? run.model.split("/").pop() : "未指定模型"}${showTimestamp ? ` · ${new Date().toLocaleTimeString()}` : ""}`;
    if (!state.autoRefreshInitialized) {
      $("#live").checked = !completed;
      state.autoRefreshInitialized = true;
    }
    if (!state.selected) {
      const hashId = decodeURIComponent(location.hash.slice(1));
      const initial = state.summary.packages.find(item => item.id === hashId)?.id || filteredPackages()[0]?.id;
      if (initial) selectPackage(initial);
    }
  } catch (error) {
    $("#status").textContent = `读取失败：${error.message}`;
  }
}

for (const selector of ["#query", "#filter", "#review-filter", "#rule-filter", "#sort"]) {
  $(selector).addEventListener(selector === "#query" ? "input" : "change", renderList);
}
$("#refresh").onclick = () => refreshSummary();
document.addEventListener("keydown", event => {
  if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) return;
  if (event.key === "ArrowLeft") navigatePackage(-1);
  if (event.key === "ArrowRight") navigatePackage(1);
});
setInterval(() => {
  if ($("#live").checked && state.summary?.run?.status !== "completed") refreshSummary();
}, 10000);
refreshSummary();
