const state = { graph: null, packages: [], report: null, selected: {} };
const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;","\"":"&quot;"}[char]));
const text = (value) => typeof value === "string" ? value : JSON.stringify(value ?? "");
const short = (value, length = 120) => { const result = text(value).replace(/\s+/g, " "); return result.length > length ? `${result.slice(0, length)}…` : result; };
const roleLabels = {concept: "概念", rule: "规则", concept_and_rule: "概念+规则", noise: "噪声"};
const roleClass = (label) => label ? `role-${label}` : "role-unclassified";
const packageRole = (item) => item.attributes?.package_role || {};
const resourceUnderstanding = (item) => item?.attributes?.resource_understanding || {};

async function loadJson(path) {
  const response = await fetch(path, {cache: "no-store"});
  if (!response.ok) throw new Error(`${path}: ${response.status}`);
  return response.json();
}
async function loadJsonl(path) {
  const response = await fetch(path, {cache: "no-store"});
  if (!response.ok) throw new Error(`${path}: ${response.status}`);
  const body = await response.text();
  return body.trim() ? body.trim().split("\n").map(JSON.parse) : [];
}
function units() { return Object.values(state.graph.units || {}); }
function unit(id) { return state.graph.units?.[id]; }
function constraints() { return Object.values(state.graph.constraints || {}); }
function relationsFrom(id, type = null) { return state.graph.relations.filter((item) => item.source_id === id && (!type || item.type === type)); }
function pageOf(item) { return item?.source?.page == null ? "页码未知" : `第 ${item.source.page + 1} 页`; }
function unitCard(item, selected) { return `<button class="row ${selected ? "selected" : ""}" data-unit="${item.id}"><b>${escapeHtml(item.id)} <span class="badge">${escapeHtml(item.type)}</span></b><small>${escapeHtml(short(item.attributes?.caption || item.content))}</small></button>`; }
function renderSummary() {
  const roles = state.packages.reduce((result, item) => { const label = packageRole(item).label || "unclassified"; result[label] = (result[label] || 0) + 1; return result; }, {});
  const resourceRows = units().filter((item) => ["figure", "table", "formula"].includes(item.type)).map(resourceUnderstanding);
  const metrics = [
    [state.report.unit_count, "Unit"], [state.report.relation_count, "关联"], [state.report.constraint_count, "条件"],
    [state.report.context_package_count, "上下文包"], [roles.concept || 0, "概念包"], [roles.rule || 0, "规则包"],
    [roles.concept_and_rule || 0, "概念+规则"], [roles.noise || 0, "噪声包"],
    [resourceRows.filter((item) => item.status === "ok").length, "资源已理解"],
    [resourceRows.filter((item) => item.useful).length, "有用资源"],
    [resourceRows.filter((item) => item.status === "failed").length, "资源失败"],
    [state.report.elapsed_seconds?.toFixed(2), "构建秒数"],
  ];
  $("#summary").innerHTML = metrics.map(([value, label]) => `<div class="metric"><b>${escapeHtml(value ?? 0)}</b><span>${label}</span></div>`).join("");
}
function treeNode(node) {
  const leaves = node.units.map((item) => `<div class="leaf">${escapeHtml(item.content)} <span class="meta">${item.id}</span></div>`).join("");
  const children = [...node.children.entries()].map(([name, child]) => `<details open><summary>${escapeHtml(name)}</summary>${treeNode(child)}</details>`).join("");
  return leaves + children;
}
function renderStructure() {
  const titles = units().filter((item) => item.type === "title");
  const root = {children: new Map(), units: []};
  titles.forEach((item) => {
    let cursor = root;
    (item.attributes?.section_path || [text(item.content)]).forEach((part) => {
      if (!cursor.children.has(part)) cursor.children.set(part, {children: new Map(), units: []});
      cursor = cursor.children.get(part);
    });
    cursor.units.push(item);
  });
  $("#structure").innerHTML = `<div class="toolbar"><span class="hint">标题树：${titles.length} 个标题。展开节点可核验章节路径与恢复结果。</span></div><div class="tree">${treeNode(root) || "<div class=empty>没有标题。</div>"}</div>`;
}
function renderAssetDetail(item) {
  const incoming = state.graph.relations.filter((relation) => relation.target_id === item.id && relation.type === "MENTIONS");
  const caption = item.attributes?.caption || "（无题注）";
  const image = ["figure", "table"].includes(item.type) && item.source?.asset_path
    ? `<img class="asset-image" src="/assets/${encodeURIComponent(item.id)}" alt="${escapeHtml(caption)}" onerror="this.remove()">`
    : "";
  const tableBody = item.attributes?.table_body || item.content;
  // MinerU's table_body is generated HTML. It is part of the local build output,
  // so keep the table structure visible instead of displaying escaped markup.
  const body = item.type === "table"
    ? `<div class="content table-content">${typeof tableBody === "string" && tableBody.includes("<table") ? tableBody : escapeHtml(text(tableBody))}</div>`
    : `<div class="content">${escapeHtml(item.type === "figure" ? caption : text(item.content))}</div>`;
  const links = incoming.map((relation) => `<button data-jump="${relation.source_id}">${relation.source_id} · ${escapeHtml(relation.evidence.join(", ") || "显式关联")}</button>`).join("") || "<span class=meta>没有正文资产关联。</span>";
  const understanding = renderResourceUnderstanding(item);
  return `<h2>${item.id} <span class="badge">${item.type}</span></h2><p class="meta">${pageOf(item)} · ${escapeHtml((item.attributes?.section_path || []).join(" / ") || "无章节")}</p><p><b>题注：</b>${escapeHtml(caption)}</p>${understanding}${image}${body}<div class="group"><h3>引用它的正文 Unit（${incoming.length}）</h3><div class="links">${links}</div></div>`;
}
function renderResourceUnderstanding(item) {
  const result = resourceUnderstanding(item);
  if (!result.status || result.status === "model_not_run") return `<div class="semantic-card pending"><b>资源理解：</b>模型未运行</div>`;
  if (result.status === "failed") return `<div class="semantic-card failed"><b>资源理解失败：</b>${escapeHtml(result.error_type || "error")} · ${escapeHtml(result.reason || "")}</div>`;
  const kinds = (result.content_kinds || []).map((kind) => `<span class="badge">${escapeHtml(kind)}</span>`).join("");
  const symbols = (result.symbols || []).map((symbol) => `<div class="symbol-row"><code>${escapeHtml(symbol.symbol)}</code><span>${escapeHtml(symbol.meaning || "含义未知")}</span><span>${escapeHtml(symbol.unit || "无单位")}</span><small>${escapeHtml((symbol.evidence_unit_ids || []).join(", ") || "无证据ID")}</small></div>`).join("");
  return `<div class="semantic-card ${result.useful ? "useful" : "discarded"}"><div class="semantic-title"><b>模型文字化</b><span class="badge ${result.useful ? "ok" : "warn"}">${result.useful ? "有用" : "无用"}</span><span class="badge">图片：${escapeHtml(result.image_status || "不适用")}</span></div>${result.title ? `<h3>${escapeHtml(result.title)}</h3>` : ""}${result.description ? `<p>${escapeHtml(result.description)}</p>` : ""}${result.summary ? `<p><b>公式摘要：</b>${escapeHtml(result.summary)}</p>` : ""}${kinds ? `<div>${kinds}</div>` : ""}${symbols ? `<div class="symbols"><b>符号解释</b>${symbols}</div>` : ""}<p class="meta">${escapeHtml(result.configured_model || "模型未知")} · ${escapeHtml(result.prompt_id || "Prompt未知")}@${escapeHtml(result.prompt_version || "?")}</p></div>`;
}
function renderAssets() {
  const assetUnits = units().filter((item) => ["figure", "table", "formula"].includes(item.type));
  $("#assets").innerHTML = `<div class="toolbar"><select id="asset-type"><option value="">全部资产</option><option value="figure">图</option><option value="table">表</option><option value="formula">公式</option></select><select id="asset-useful"><option value="">全部理解状态</option><option value="useful">有用</option><option value="discarded">无用</option><option value="failed">失败</option><option value="pending">未运行</option></select><input id="asset-query" placeholder="按 Unit、题注、文字化内容或章节搜索"><span class="hint" id="asset-count"></span></div><div class="layout"><div class="list" id="asset-list"></div><article class="detail" id="asset-detail"><div class="empty">从左侧选择一项资产。</div></article></div>`;
  const refresh = () => {
    const query = $("#asset-query").value.trim().toLowerCase(); const type = $("#asset-type").value; const useful = $("#asset-useful").value;
    const filtered = assetUnits.filter((item) => { const result = resourceUnderstanding(item); const stateName = result.status === "failed" ? "failed" : result.status !== "ok" ? "pending" : result.useful ? "useful" : "discarded"; return (!type || item.type === type) && (!useful || useful === stateName) && (!query || JSON.stringify(item).toLowerCase().includes(query)); });
    $("#asset-count").textContent = `${filtered.length} / ${assetUnits.length} 项`;
    $("#asset-list").innerHTML = filtered.length ? filtered.map((item) => { const result = resourceUnderstanding(item); const status = result.status === "ok" ? (result.useful ? "有用" : "无用") : result.status === "failed" ? "失败" : "未运行"; return `<button class="row ${item.id === state.selected.asset ? "selected" : ""}" data-unit="${item.id}"><b>${escapeHtml(item.id)} <span class="badge">${escapeHtml(item.type)}</span><span class="badge ${result.useful ? "ok" : result.status === "failed" ? "warn" : ""}">${status}</span></b><small>${escapeHtml(short(result.title || result.description || item.attributes?.caption || item.content))}</small></button>`; }).join("") : $("#empty-template").innerHTML;
    $("#asset-list").querySelectorAll("[data-unit]").forEach((button) => button.onclick = () => { state.selected.asset = button.dataset.unit; refresh(); $("#asset-detail").innerHTML = renderAssetDetail(unit(state.selected.asset)); bindJumps(); });
    if (state.selected.asset && filtered.some((item) => item.id === state.selected.asset)) $("#asset-detail").innerHTML = renderAssetDetail(unit(state.selected.asset));
    bindJumps();
  };
  $("#asset-query").oninput = refresh; $("#asset-type").onchange = refresh; $("#asset-useful").onchange = refresh; refresh();
}
function selectAsset(assetId) {
  if (!unit(assetId)) return;
  state.selected.asset = assetId;
  document.querySelector('[data-tab="assets"]').click();
  renderAssets();
}
function renderPackageDetail(item) {
  const renderUnits = (ids) => ids.map((id) => { const current = unit(id); if (!current) return `<div class="content">${id}（未找到）</div>`; const understood = resourceUnderstanding(current); const semantic = understood.description || understood.summary; return `<div class="content"><b>${id}</b> <span class="badge">${current.type}</span><br>${escapeHtml(text(current.content))}${semantic ? `<div class="inline-semantic"><b>模型文字化：</b>${escapeHtml(semantic)}</div>` : ""}</div>`; }).join("") || "<span class=meta>无</span>";
  const renderConstraints = item.constraint_ids.map((id) => { const value = state.graph.constraints[id]; return value ? `<span class="badge">${escapeHtml(value.type)}：${escapeHtml(text(value.value))}</span>` : ""; }).join("") || "<span class=meta>无确定条件</span>";
  const assets = item.asset_part_ids.map((id) => `<button data-asset="${id}">${id} · ${escapeHtml(unit(id)?.attributes?.caption || unit(id)?.type || "未找到")}</button>`).join("") || "<span class=meta>无关联资产</span>";
  const route = (item.attributes?.routing_evidence || []).map((entry) => `<span class="badge">${escapeHtml(entry.role)} · ${escapeHtml(entry.pattern_id)} · ${entry.confidence}</span>`).join("");
  const classified = packageRole(item); const label = classified.label;
  const finalRoute = `<span class="badge role ${roleClass(label)}">${escapeHtml(roleLabels[label] || "未分类")}</span>${classified.status === "ok" ? `<span class="meta">${escapeHtml(classified.configured_model || "")} · ${escapeHtml(classified.prompt_id || "")}@${escapeHtml(classified.prompt_version || "?")}</span>` : `<span class="meta">${escapeHtml(classified.status || "unknown")}</span>`}`;
  return `<h2>${item.id} ${finalRoute}</h2><p class="meta">${escapeHtml((item.attributes?.section_path || []).join(" / ") || "无章节")}</p><div class="group"><h3>最终包角色</h3>${finalRoute}</div><div class="group"><h3>确定性路由证据</h3>${route || "<span class=meta>无</span>"}</div><div class="group"><h3>核心文本</h3>${renderUnits(item.core_unit_ids)}</div><div class="group"><h3>支撑内容</h3>${renderUnits(item.support_unit_ids)}</div><div class="group"><h3>确定条件</h3>${renderConstraints}</div><div class="group"><h3>完整资产</h3><div class="links">${assets}</div></div><div class="group"><h3>未解决条件冲突</h3>${item.unresolved_ids.length ? item.unresolved_ids.map((id) => `<span class="badge warn">${id}</span>`).join("") : "<span class=meta>无</span>"}</div>`;
}
function renderPackages() {
  $("#packages").innerHTML = `<div class="toolbar"><select id="package-role"><option value="">全部角色</option><option value="concept">概念</option><option value="rule">规则</option><option value="concept_and_rule">概念+规则</option><option value="noise">噪声</option><option value="unclassified">未分类/失败</option></select><input id="package-query" placeholder="按章节、正文、资源文字化或条件搜索"><span class="hint" id="package-count"></span></div><div class="layout"><div class="list" id="package-list"></div><article class="detail" id="package-detail"><div class="empty">从左侧选择一个上下文包。</div></article></div>`;
  const refresh = () => {
    const query = $("#package-query").value.trim().toLowerCase(); const wantedRole = $("#package-role").value;
    const filtered = state.packages.filter((item) => { const label = packageRole(item).label || "unclassified"; const ids = [...item.core_unit_ids, ...item.support_unit_ids, ...item.asset_part_ids]; const searchable = `${JSON.stringify(item)} ${ids.map((id) => JSON.stringify(unit(id) || {})).join(" ")}`.toLowerCase(); return (!wantedRole || label === wantedRole) && (!query || searchable.includes(query)); });
    $("#package-count").textContent = `${filtered.length} / ${state.packages.length} 包`;
    $("#package-list").innerHTML = filtered.length ? filtered.map((item) => { const core = unit(item.core_unit_ids[0]); const label = packageRole(item).label; return `<button class="row ${item.id === state.selected.package ? "selected" : ""}" data-package="${item.id}"><b>${item.id} <span class="badge role ${roleClass(label)}">${escapeHtml(roleLabels[label] || "未分类")}</span><span class="badge">${item.constraint_ids.length} 条件</span><span class="badge">${item.asset_part_ids.length} 资产</span></b><small>${escapeHtml(short(core?.content))}</small></button>`; }).join("") : $("#empty-template").innerHTML;
    $("#package-list").querySelectorAll("[data-package]").forEach((button) => button.onclick = () => { state.selected.package = button.dataset.package; refresh(); $("#package-detail").innerHTML = renderPackageDetail(state.packages.find((item) => item.id === state.selected.package)); bindJumps(); });
    if (state.selected.package && filtered.some((item) => item.id === state.selected.package)) $("#package-detail").innerHTML = renderPackageDetail(state.packages.find((item) => item.id === state.selected.package));
    bindJumps();
  };
  $("#package-query").oninput = refresh; $("#package-role").onchange = refresh; refresh();
}
function renderConditions() {
  const rows = constraints().map((item) => `<button class="row" data-condition="${item.id}"><b>${item.id} <span class="badge ${item.status === "conflict" ? "warn" : ""}">${escapeHtml(item.type)}</span></b><small>${escapeHtml(text(item.value))} · ${escapeHtml(item.source_id)} · ${escapeHtml(item.scope?.start_unit_id || "?")} → ${escapeHtml(item.scope?.end_unit_id || "?")}</small></button>`).join("");
  $("#conditions").innerHTML = `<div class="toolbar"><span class="hint">确定条件 ${constraints().filter((item) => item.status === "certain").length} 条；冲突 ${state.report.ambiguity_count} 条。此页只显示条件冲突，不将资产候选视为歧义。</span></div><div class="layout"><div class="list">${rows || $("#empty-template").innerHTML}</div><article class="detail" id="condition-detail"><div class="empty">从左侧选择一个条件。</div></article></div>`;
  $("#conditions").querySelectorAll("[data-condition]").forEach((button) => button.onclick = () => { const item = state.graph.constraints[button.dataset.condition]; $("#condition-detail").innerHTML = `<h2>${item.id}</h2><p><span class="badge">${escapeHtml(item.type)}</span> <span class="badge">${escapeHtml(item.status)}</span></p><div class="content">${escapeHtml(text(item.value))}</div><div class="group"><h3>来源与作用域</h3><p>${item.source_id}：${escapeHtml(text(unit(item.source_id)?.content))}</p><p class="meta">${item.scope?.start_unit_id || "?"} → ${item.scope?.end_unit_id || "?"}</p></div>`; });
}
function bindJumps() {
  document.querySelectorAll("[data-asset]").forEach((button) => button.onclick = () => selectAsset(button.dataset.asset));
  document.querySelectorAll("[data-jump]").forEach((button) => button.onclick = () => { const item = unit(button.dataset.jump); if (!item) return; alert(`${item.id}\n\n${text(item.content)}`); });
}
function activateTabs() { document.querySelectorAll(".tab").forEach((button) => button.onclick = () => { document.querySelectorAll(".tab,.panel").forEach((element) => element.classList.remove("active")); button.classList.add("active"); $(`#${button.dataset.tab}`).classList.add("active"); }); }
async function boot() {
  try {
    [state.graph, state.packages, state.report] = await Promise.all([loadJson("/data/document_graph.json"), loadJsonl("/data/context_packages.jsonl"), loadJson("/data/run_report.json")]);
    renderSummary(); renderStructure(); renderAssets(); renderPackages(); renderConditions(); activateTabs();
    $("#load-status").textContent = `已加载 ${state.report.unit_count} 个 Unit · ${state.report.context_package_count} 个文本包`;
  } catch (error) { $("#load-status").textContent = "加载失败"; document.querySelector("main").innerHTML = `<div class="empty">无法读取输出文件：${escapeHtml(error.message)}。请先运行构建脚本，并通过 serve_review.py 打开页面。</div>`; }
}
boot();
