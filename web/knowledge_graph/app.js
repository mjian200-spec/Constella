const app = {
  summary: null,
  graph: { nodes: [], edges: [] },
  selectedId: null,
  searchTimer: null,
  transform: { x: 0, y: 0, k: 1 },
};

const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? "").replace(/[&<>'"]/g, char => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
}[char]));

async function api(path) {
  const response = await fetch(path);
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}

function number(value) {
  return new Intl.NumberFormat("zh-CN").format(value || 0);
}

function kindLabel(kind) {
  return { concept: "概念", rule: "规则", state: "状态", transition: "迁移" }[kind] || kind;
}

function relationLabel(type) {
  return {
    IS_A: "属于", PART_OF: "组成于", ABOUT_CONCEPT: "对应概念",
    CONDITION_OF: "条件", ANTECEDENT_OF: "前件", HAS_CONSEQUENT: "后件",
    HAS_TRANSITION: "包含迁移", FROM_STATE: "起始状态", TO_STATE: "目标状态",
  }[type] || type;
}

function toast(message, bad = false) {
  const node = $("#toast");
  node.textContent = message;
  node.className = `toast visible${bad ? " bad" : ""}`;
  setTimeout(() => node.classList.remove("visible"), 2600);
}

async function loadSummary() {
  app.summary = await api("/api/summary");
  $("#dataset-id").textContent = app.summary.dataset_id;
  const nodes = app.summary.nodes;
  const matches = app.summary.concept_matches;
  const cards = [
    ["Concept", "概念", "concept"], ["Rule", "规则", "rule"],
    ["StateExpression", "状态表达式", "state"], ["StateTransition", "状态迁移", "transition"],
    ["matched", "已关联概念", "matched", matches], ["unmatched", "未匹配状态", "unmatched", matches],
  ];
  $("#metrics").innerHTML = cards.map(([key, label, tone, source]) => `
    <article class="metric ${tone}"><span>${esc(label)}</span><b>${number((source || nodes)[key])}</b></article>
  `).join("");
}

async function loadOverview() {
  setGraphLoading("正在组织概念结构…");
  const graph = await api("/api/graph/overview?limit=110");
  app.selectedId = null;
  $("#graph-title").textContent = graph.title;
  renderGraph(graph, true);
  renderInspector(null);
}

function setGraphLoading(message) {
  $("#graph").innerHTML = `<div class="loading"><span></span>${esc(message)}</div>`;
}

async function search() {
  const query = $("#query").value.trim();
  const kind = $("#kind").value;
  $("#result-meta").textContent = "检索中…";
  try {
    const payload = await api(`/api/search?q=${encodeURIComponent(query)}&kind=${encodeURIComponent(kind)}&limit=60`);
    renderResults(payload.results);
  } catch (error) {
    $("#result-meta").textContent = "检索失败";
    toast(error.message, true);
  }
}

function renderResults(results) {
  $("#result-meta").innerHTML = `找到 <b>${number(results.length)}</b> 项${results.length === 60 ? "，请缩小范围" : ""}`;
  $("#results").innerHTML = results.length ? results.map(item => `
    <button class="result ${item.id === app.selectedId ? "selected" : ""}" data-id="${esc(item.id)}" data-kind="${esc(item.kind)}">
      <span class="kind-dot ${esc(item.kind)}"></span>
      <span class="result-copy"><b>${esc(item.title)}</b><small>${esc(item.subtitle)}</small></span>
      <span class="type-tag">${esc(kindLabel(item.kind))}</span>
    </button>
  `).join("") : '<div class="empty-list">没有匹配结果</div>';
  document.querySelectorAll(".result").forEach(button => {
    button.onclick = () => loadEntity(button.dataset.kind, button.dataset.id);
  });
}

async function loadEntity(kind, id) {
  app.selectedId = id;
  setGraphLoading("正在展开相邻节点…");
  renderInspector({ loading: true });
  try {
    const detail = await api(`/api/entity/${encodeURIComponent(kind)}/${encodeURIComponent(id)}`);
    $("#graph-title").textContent = `${kindLabel(kind)}邻域 · ${detail.entity.title}`;
    renderGraph(detail, true);
    renderInspector(detail);
    document.querySelectorAll(".result").forEach(row => row.classList.toggle("selected", row.dataset.id === id));
  } catch (error) {
    toast(`无法读取节点：${error.message}`, true);
    await loadOverview();
  }
}

function renderInspector(detail) {
  const inspector = $("#inspector");
  if (detail?.loading) {
    inspector.innerHTML = '<div class="empty-inspector"><div class="spinner"></div><p>读取节点详情…</p></div>';
    return;
  }
  if (!detail) {
    inspector.innerHTML = '<div class="empty-inspector"><span class="constellation">✦</span><h2>选择一个节点</h2><p>查看概念定义、规则表达式、状态及其相邻关系。</p></div>';
    return;
  }
  const item = detail.entity;
  const p = item.properties;
  const aliases = (p.aliases || []).map(alias => `<span class="chip">${esc(alias)}</span>`).join("");
  const adjacent = detail.edges.filter(edge => edge.source === item.id || edge.target === item.id);
  const properties = Object.entries(p).filter(([key, value]) =>
    !["graph_id", "dataset_id", "aliases", "definition", "raw_expression"].includes(key) && value !== null && value !== "" && (!Array.isArray(value) || value.length)
  );
  inspector.innerHTML = `
    <div class="inspector-head">
      <span class="entity-kind ${esc(item.kind)}">${esc(kindLabel(item.kind))}</span>
      <h2>${esc(item.title)}</h2>
      <code>${esc(entityPublicId(item))}</code>
    </div>
    ${p.definition ? `<section class="definition"><span>定义</span><p>${esc(p.definition)}</p></section>` : ""}
    ${p.raw_expression ? `<section class="expression"><span>规则表达式</span><p>${esc(p.raw_expression)}</p></section>` : ""}
    ${aliases ? `<section class="inspector-section"><h3>别名</h3><div class="chips">${aliases}</div></section>` : ""}
    ${renderStats(detail.stats || {})}
    <section class="inspector-section">
      <h3>属性</h3>
      <dl class="properties">${properties.map(([key, value]) => `
        <div><dt>${esc(propertyLabel(key))}</dt><dd>${esc(Array.isArray(value) ? value.join("、") : value)}</dd></div>
      `).join("") || '<div class="muted">没有其他属性</div>'}</dl>
    </section>
    <section class="inspector-section">
      <h3>直接关系 <span>${adjacent.length}</span></h3>
      <div class="relation-list">${adjacent.slice(0, 30).map(edge => {
        const otherId = edge.source === item.id ? edge.target : edge.source;
        const other = detail.nodes.find(node => node.id === otherId);
        return `<button data-neighbor="${esc(otherId)}" data-neighbor-kind="${esc(other?.kind || "")}">
          <span>${esc(relationLabel(edge.type))}</span><b>${esc(other?.title || otherId)}</b>
        </button>`;
      }).join("") || '<div class="muted">没有直接关系</div>'}</div>
    </section>
  `;
  document.querySelectorAll("[data-neighbor]").forEach(button => {
    button.onclick = () => loadEntity(button.dataset.neighborKind, button.dataset.neighbor);
  });
}

function renderStats(stats) {
  const entries = Object.entries(stats);
  if (!entries.length) return "";
  const labels = { concept_neighbors: "概念邻居", state_usages: "关联状态", rule_usages: "关联规则", states: "状态", transitions: "迁移", rules: "关联规则" };
  return `<section class="detail-stats">${entries.map(([key, value]) => `<div><b>${number(value)}</b><span>${esc(labels[key] || key)}</span></div>`).join("")}</section>`;
}

function entityPublicId(item) {
  const p = item.properties;
  return p.concept_id || p.rule_id || p.state_id || p.transition_id || item.id;
}

function propertyLabel(key) {
  return {
    canonical_name: "规范名", concept_id: "概念 ID", definition_type: "定义类型", audit_status: "审计状态",
    origin_depth: "来源层级", source_package_ids: "来源包", evidence_ids: "证据单元",
    rule_id: "规则 ID", context_package_id: "上下文包", rule_group_id: "规则组", rule_index: "组内序号",
    relation: "关系", state_id: "状态 ID", object: "对象", raw_state: "原始状态",
    normalized_state: "标准状态", concept_match_status: "概念匹配", transition_id: "迁移 ID",
  }[key] || key;
}

function renderGraph(payload, reset = false) {
  app.graph = { nodes: payload.nodes || [], edges: payload.edges || [] };
  const host = $("#graph");
  host.innerHTML = "";
  if (!app.graph.nodes.length) {
    host.innerHTML = '<div class="loading">当前节点没有可展示的邻域。</div>';
    return;
  }
  const width = Math.max(host.clientWidth, 640);
  const height = Math.max(host.clientHeight, 520);
  const positions = forceLayout(app.graph.nodes, app.graph.edges, width, height);
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.innerHTML = `<defs><marker id="arrow" viewBox="0 0 10 10" refX="18" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z"></path></marker></defs>`;
  const viewport = document.createElementNS(svg.namespaceURI, "g");
  viewport.classList.add("viewport");
  svg.appendChild(viewport);

  const edgeLayer = document.createElementNS(svg.namespaceURI, "g");
  edgeLayer.classList.add("edges");
  viewport.appendChild(edgeLayer);
  for (const edge of app.graph.edges) {
    const a = positions.get(edge.source), b = positions.get(edge.target);
    if (!a || !b) continue;
    const group = document.createElementNS(svg.namespaceURI, "g");
    group.classList.add("edge", edge.type.toLowerCase());
    const line = document.createElementNS(svg.namespaceURI, "line");
    line.setAttribute("x1", a.x); line.setAttribute("y1", a.y);
    line.setAttribute("x2", b.x); line.setAttribute("y2", b.y);
    line.setAttribute("marker-end", "url(#arrow)");
    group.appendChild(line);
    if (app.graph.edges.length <= 70) {
      const label = document.createElementNS(svg.namespaceURI, "text");
      label.setAttribute("x", (a.x + b.x) / 2); label.setAttribute("y", (a.y + b.y) / 2 - 4);
      label.textContent = relationLabel(edge.type);
      group.appendChild(label);
    }
    edgeLayer.appendChild(group);
  }

  const nodeLayer = document.createElementNS(svg.namespaceURI, "g");
  nodeLayer.classList.add("nodes");
  viewport.appendChild(nodeLayer);
  for (const node of app.graph.nodes) {
    const position = positions.get(node.id);
    const group = document.createElementNS(svg.namespaceURI, "g");
    group.classList.add("graph-node", node.kind);
    if (node.id === app.selectedId || node.focus) group.classList.add("focus");
    group.setAttribute("transform", `translate(${position.x},${position.y})`);
    group.setAttribute("tabindex", "0");
    group.setAttribute("role", "button");
    group.setAttribute("aria-label", `${kindLabel(node.kind)} ${node.title}`);
    const shape = document.createElementNS(svg.namespaceURI, node.kind === "rule" ? "rect" : "circle");
    if (node.kind === "rule") {
      shape.setAttribute("x", -13); shape.setAttribute("y", -13); shape.setAttribute("width", 26); shape.setAttribute("height", 26); shape.setAttribute("rx", 6);
    } else {
      shape.setAttribute("r", node.kind === "concept" ? 12 + Math.min(node.degree || 0, 8) : 10);
    }
    group.appendChild(shape);
    const label = document.createElementNS(svg.namespaceURI, "text");
    label.setAttribute("y", 27);
    label.textContent = truncate(node.title, 18);
    group.appendChild(label);
    const activate = () => loadEntity(node.kind, node.id);
    group.onclick = event => { event.stopPropagation(); activate(); };
    group.onkeydown = event => { if (event.key === "Enter" || event.key === " ") activate(); };
    nodeLayer.appendChild(group);
  }
  host.appendChild(svg);
  bindPanZoom(svg, viewport, reset);
}

function truncate(value, length) {
  const text = String(value || "");
  return text.length > length ? `${text.slice(0, length - 1)}…` : text;
}

function hash(value) {
  let result = 2166136261;
  for (const char of value) result = Math.imul(result ^ char.charCodeAt(0), 16777619);
  return result >>> 0;
}

function forceLayout(nodes, edges, width, height) {
  const positions = new Map();
  const radius = Math.min(width, height) * .35;
  nodes.forEach((node, index) => {
    const angle = (index / nodes.length) * Math.PI * 2 + (hash(node.id) % 100) / 240;
    positions.set(node.id, { x: width / 2 + Math.cos(angle) * radius, y: height / 2 + Math.sin(angle) * radius, vx: 0, vy: 0 });
  });
  const linked = edges.map(edge => [positions.get(edge.source), positions.get(edge.target)]).filter(pair => pair[0] && pair[1]);
  for (let step = 0; step < 150; step++) {
    const cooling = 1 - step / 170;
    for (let i = 0; i < nodes.length; i++) {
      const a = positions.get(nodes[i].id);
      for (let j = i + 1; j < nodes.length; j++) {
        const b = positions.get(nodes[j].id);
        let dx = a.x - b.x, dy = a.y - b.y;
        const distance2 = Math.max(dx * dx + dy * dy, 90);
        const force = 680 / distance2;
        const distance = Math.sqrt(distance2); dx /= distance; dy /= distance;
        a.vx += dx * force; a.vy += dy * force; b.vx -= dx * force; b.vy -= dy * force;
      }
    }
    for (const [a, b] of linked) {
      const dx = b.x - a.x, dy = b.y - a.y;
      const distance = Math.max(Math.sqrt(dx * dx + dy * dy), 1);
      const force = (distance - 105) * .0028;
      a.vx += dx / distance * force; a.vy += dy / distance * force;
      b.vx -= dx / distance * force; b.vy -= dy / distance * force;
    }
    for (const node of nodes) {
      const p = positions.get(node.id);
      p.vx += (width / 2 - p.x) * .0008; p.vy += (height / 2 - p.y) * .0008;
      p.vx *= .84; p.vy *= .84;
      p.x = Math.max(45, Math.min(width - 45, p.x + p.vx * cooling * 2.4));
      p.y = Math.max(45, Math.min(height - 45, p.y + p.vy * cooling * 2.4));
    }
  }
  return positions;
}

function bindPanZoom(svg, viewport, reset) {
  if (reset) app.transform = { x: 0, y: 0, k: 1 };
  let dragging = false, last = null;
  const apply = () => viewport.setAttribute("transform", `translate(${app.transform.x},${app.transform.y}) scale(${app.transform.k})`);
  svg.onwheel = event => {
    event.preventDefault();
    app.transform.k = Math.max(.35, Math.min(3.2, app.transform.k * (event.deltaY < 0 ? 1.12 : .89)));
    apply();
  };
  svg.onpointerdown = event => { if (event.target === svg) { dragging = true; last = { x: event.clientX, y: event.clientY }; svg.setPointerCapture(event.pointerId); } };
  svg.onpointermove = event => {
    if (!dragging) return;
    app.transform.x += event.clientX - last.x; app.transform.y += event.clientY - last.y;
    last = { x: event.clientX, y: event.clientY }; apply();
  };
  svg.onpointerup = () => { dragging = false; };
  apply();
}

function adjustZoom(factor) {
  const viewport = $("#graph .viewport");
  if (!viewport) return;
  app.transform.k = Math.max(.35, Math.min(3.2, app.transform.k * factor));
  viewport.setAttribute("transform", `translate(${app.transform.x},${app.transform.y}) scale(${app.transform.k})`);
}

function fitGraph() {
  app.transform = { x: 0, y: 0, k: 1 };
  const viewport = $("#graph .viewport");
  if (viewport) viewport.setAttribute("transform", "translate(0,0) scale(1)");
}

function bindControls() {
  $("#query").oninput = () => { clearTimeout(app.searchTimer); app.searchTimer = setTimeout(search, 220); };
  $("#kind").onchange = search;
  $("#overview").onclick = loadOverview;
  $("#fit").onclick = fitGraph;
  $("#zoom-in").onclick = () => adjustZoom(1.2);
  $("#zoom-out").onclick = () => adjustZoom(.82);
  document.querySelectorAll("[data-kind]").forEach(button => {
    button.onclick = () => { $("#kind").value = button.dataset.kind; search(); };
  });
}

async function boot() {
  bindControls();
  try {
    await Promise.all([loadSummary(), loadOverview()]);
    await search();
  } catch (error) {
    $("#graph").innerHTML = `<div class="fatal"><b>无法读取知识图谱</b><span>${esc(error.message)}</span></div>`;
    toast(error.message, true);
  }
}

boot();
