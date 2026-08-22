const state = { graph: null, packages: [], report: null, selected: {} };
const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;","\"":"&quot;"}[char]));
const text = (value) => typeof value === "string" ? value : JSON.stringify(value ?? "");
const short = (value, length = 120) => { const result = text(value).replace(/\s+/g, " "); return result.length > length ? `${result.slice(0, length)}…` : result; };

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
  const metrics = [
    [state.report.unit_count, "Unit"], [state.report.relation_count, "关联"], [state.report.constraint_count, "条件"],
    [state.report.context_package_count, "文本包"], [state.report.ambiguity_count, "条件冲突"], [state.report.elapsed_seconds?.toFixed(2), "构建秒数"],
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
  const image = item.type === "figure" && item.source?.asset_path ? `<img class="asset-image" src="/assets/${item.id}" alt="${escapeHtml(caption)}" onerror="this.remove()">` : "";
  const body = item.type === "table" ? `<div class="content table-content">${escapeHtml(item.attributes?.table_body || item.content)}</div>` : `<div class="content">${escapeHtml(item.type === "figure" ? caption : text(item.content))}</div>`;
  const links = incoming.map((relation) => `<button data-jump="${relation.source_id}">${relation.source_id} · ${escapeHtml(relation.evidence.join(", ") || "显式关联")}</button>`).join("") || "<span class=meta>没有正文资产关联。</span>";
  return `<h2>${item.id} <span class="badge">${item.type}</span></h2><p class="meta">${pageOf(item)} · ${escapeHtml((item.attributes?.section_path || []).join(" / ") || "无章节")}</p><p><b>题注：</b>${escapeHtml(caption)}</p>${image}${body}<div class="group"><h3>引用它的正文 Unit（${incoming.length}）</h3><div class="links">${links}</div></div>`;
}
function renderAssets() {
  const assetUnits = units().filter((item) => ["figure", "table", "formula"].includes(item.type));
  $("#assets").innerHTML = `<div class="toolbar"><select id="asset-type"><option value="">全部资产</option><option value="figure">图</option><option value="table">表</option><option value="formula">公式</option></select><input id="asset-query" placeholder="按 Unit、题注、内容或章节搜索"><span class="hint" id="asset-count"></span></div><div class="layout"><div class="list" id="asset-list"></div><article class="detail" id="asset-detail"><div class="empty">从左侧选择一项资产。</div></article></div>`;
  const refresh = () => {
    const query = $("#asset-query").value.trim().toLowerCase(); const type = $("#asset-type").value;
    const filtered = assetUnits.filter((item) => (!type || item.type === type) && (!query || JSON.stringify(item).toLowerCase().includes(query)));
    $("#asset-count").textContent = `${filtered.length} / ${assetUnits.length} 项`;
    $("#asset-list").innerHTML = filtered.length ? filtered.map((item) => unitCard(item, item.id === state.selected.asset)).join("") : $("#empty-template").innerHTML;
    $("#asset-list").querySelectorAll("[data-unit]").forEach((button) => button.onclick = () => { state.selected.asset = button.dataset.unit; refresh(); $("#asset-detail").innerHTML = renderAssetDetail(unit(state.selected.asset)); bindJumps(); });
    if (state.selected.asset && filtered.some((item) => item.id === state.selected.asset)) $("#asset-detail").innerHTML = renderAssetDetail(unit(state.selected.asset));
    bindJumps();
  };
  $("#asset-query").oninput = refresh; $("#asset-type").onchange = refresh; refresh();
}
function renderPackageDetail(item) {
  const renderUnits = (ids) => ids.map((id) => unit(id) ? `<div class="content"><b>${id}</b> <span class="badge">${unit(id).type}</span><br>${escapeHtml(text(unit(id).content))}</div>` : `<div class="content">${id}（未找到）</div>`).join("") || "<span class=meta>无</span>";
  const renderConstraints = item.constraint_ids.map((id) => { const value = state.graph.constraints[id]; return value ? `<span class="badge">${escapeHtml(value.type)}：${escapeHtml(text(value.value))}</span>` : ""; }).join("") || "<span class=meta>无确定条件</span>";
  const assets = item.asset_part_ids.map((id) => `<button data-asset="${id}">${id} · ${escapeHtml(unit(id)?.attributes?.caption || unit(id)?.type || "未找到")}</button>`).join("") || "<span class=meta>无关联资产</span>";
  const route = (item.attributes?.routing_evidence || []).map((entry) => `<span class="badge">${escapeHtml(entry.role)} · ${escapeHtml(entry.pattern_id)} · ${entry.confidence}</span>`).join("");
  return `<h2>${item.id}</h2><p class="meta">${escapeHtml((item.attributes?.section_path || []).join(" / ") || "无章节")}</p><div class="group"><h3>路由证据</h3>${route || "<span class=meta>无</span>"}</div><div class="group"><h3>核心文本</h3>${renderUnits(item.core_unit_ids)}</div><div class="group"><h3>支撑内容</h3>${renderUnits(item.support_unit_ids)}</div><div class="group"><h3>确定条件</h3>${renderConstraints}</div><div class="group"><h3>完整资产</h3><div class="links">${assets}</div></div><div class="group"><h3>未解决条件冲突</h3>${item.unresolved_ids.length ? item.unresolved_ids.map((id) => `<span class="badge warn">${id}</span>`).join("") : "<span class=meta>无</span>"}</div>`;
}
function renderPackages() {
  $("#packages").innerHTML = `<div class="toolbar"><input id="package-query" placeholder="按章节、文本、规则或条件搜索"><span class="hint" id="package-count"></span></div><div class="layout"><div class="list" id="package-list"></div><article class="detail" id="package-detail"><div class="empty">从左侧选择一个文本包。</div></article></div>`;
  const refresh = () => {
    const query = $("#package-query").value.trim().toLowerCase();
    const filtered = state.packages.filter((item) => !query || JSON.stringify(item).toLowerCase().includes(query) || item.core_unit_ids.some((id) => text(unit(id)?.content).toLowerCase().includes(query)));
    $("#package-count").textContent = `${filtered.length} / ${state.packages.length} 包`;
    $("#package-list").innerHTML = filtered.length ? filtered.map((item) => { const core = unit(item.core_unit_ids[0]); return `<button class="row ${item.id === state.selected.package ? "selected" : ""}" data-package="${item.id}"><b>${item.id} <span class="badge">${item.constraint_ids.length} 条件</span><span class="badge">${item.asset_part_ids.length} 资产</span></b><small>${escapeHtml(short(core?.content))}</small></button>`; }).join("") : $("#empty-template").innerHTML;
    $("#package-list").querySelectorAll("[data-package]").forEach((button) => button.onclick = () => { state.selected.package = button.dataset.package; refresh(); $("#package-detail").innerHTML = renderPackageDetail(state.packages.find((item) => item.id === state.selected.package)); bindJumps(); });
    if (state.selected.package && filtered.some((item) => item.id === state.selected.package)) $("#package-detail").innerHTML = renderPackageDetail(state.packages.find((item) => item.id === state.selected.package));
    bindJumps();
  };
  $("#package-query").oninput = refresh; refresh();
}
function renderConditions() {
  const rows = constraints().map((item) => `<button class="row" data-condition="${item.id}"><b>${item.id} <span class="badge ${item.status === "conflict" ? "warn" : ""}">${escapeHtml(item.type)}</span></b><small>${escapeHtml(text(item.value))} · ${escapeHtml(item.source_id)} · ${escapeHtml(item.scope?.start_unit_id || "?")} → ${escapeHtml(item.scope?.end_unit_id || "?")}</small></button>`).join("");
  $("#conditions").innerHTML = `<div class="toolbar"><span class="hint">确定条件 ${constraints().filter((item) => item.status === "certain").length} 条；冲突 ${state.report.ambiguity_count} 条。此页只显示条件冲突，不将资产候选视为歧义。</span></div><div class="layout"><div class="list">${rows || $("#empty-template").innerHTML}</div><article class="detail" id="condition-detail"><div class="empty">从左侧选择一个条件。</div></article></div>`;
  $("#conditions").querySelectorAll("[data-condition]").forEach((button) => button.onclick = () => { const item = state.graph.constraints[button.dataset.condition]; $("#condition-detail").innerHTML = `<h2>${item.id}</h2><p><span class="badge">${escapeHtml(item.type)}</span> <span class="badge">${escapeHtml(item.status)}</span></p><div class="content">${escapeHtml(text(item.value))}</div><div class="group"><h3>来源与作用域</h3><p>${item.source_id}：${escapeHtml(text(unit(item.source_id)?.content))}</p><p class="meta">${item.scope?.start_unit_id || "?"} → ${item.scope?.end_unit_id || "?"}</p></div>`; });
}
function bindJumps() {
  document.querySelectorAll("[data-asset]").forEach((button) => button.onclick = () => { state.selected.asset = button.dataset.asset; document.querySelector('[data-tab="assets"]').click(); });
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
