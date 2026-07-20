const HIGH_RISK_FIELDS = {
  management: "是否涉及设备或系统对外提供管理和控制接口相关的代码",
  untrusted: "是否涉及对不可信来源数据进行解析或处理的代码",
  security: "是否涉及安全相关类代码(如，认证、授权、接入控制、加解密、密钥管理、日志审计、软件完整性保护等模块)",
  sensitive: "是否涉及个人数据或者敏感数据的代码",
  web: "是否涉及web相关处理",
  external: "是否外部暴露面",
};

const state = {
  valueAssets: [],
  highRiskModules: [],
  attackTrees: {
    attack_trees: [],
    analysis_gaps: [],
  },
};

const els = {
  summaryText: document.getElementById("summaryText"),
  valueAssetsInput: document.getElementById("valueAssetsInput"),
  highRiskModulesInput: document.getElementById("highRiskModulesInput"),
  attackTreesInput: document.getElementById("attackTreesInput"),
  valueAssetsBody: document.getElementById("valueAssetsBody"),
  highRiskModulesBody: document.getElementById("highRiskModulesBody"),
  internalNodesBody: document.getElementById("internalNodesBody"),
  attackTreesCanvas: document.getElementById("attackTreesCanvas"),
  valueAssetsCount: document.getElementById("valueAssetsCount"),
  highRiskModulesCount: document.getElementById("highRiskModulesCount"),
  internalNodesCount: document.getElementById("internalNodesCount"),
  attackTreesCount: document.getElementById("attackTreesCount"),
};

document.querySelectorAll(".tab").forEach((button) => {
  button.addEventListener("click", () => activateTab(button.dataset.tab));
});

els.valueAssetsInput.addEventListener("change", (event) => {
  readJsonFile(event.target.files[0], (data) => {
    state.valueAssets = Array.isArray(data) ? data : [];
    render();
  });
});

els.highRiskModulesInput.addEventListener("change", (event) => {
  readJsonFile(event.target.files[0], (data) => {
    state.highRiskModules = Array.isArray(data) ? data : [];
    render();
  });
});

els.attackTreesInput.addEventListener("change", (event) => {
  readJsonFile(event.target.files[0], (data) => {
    state.attackTrees = normalizeAttackTreePayload(data);
    render();
  });
});

els.attackTreesCanvas.addEventListener("click", (event) => {
  const toggle = event.target.closest(".node-toggle");
  if (!toggle) {
    return;
  }
  const node = toggle.closest(".tree-node");
  if (node) {
    const collapsed = node.classList.toggle("is-collapsed");
    toggle.setAttribute("aria-expanded", String(!collapsed));
  }
});

if (window.THREAT_ANALYSIS_DATA) {
  state.valueAssets = Array.isArray(window.THREAT_ANALYSIS_DATA.valueAssets)
    ? window.THREAT_ANALYSIS_DATA.valueAssets
    : [];
  state.highRiskModules = Array.isArray(window.THREAT_ANALYSIS_DATA.highRiskModules)
    ? window.THREAT_ANALYSIS_DATA.highRiskModules
    : [];
  state.attackTrees = normalizeAttackTreePayload(window.THREAT_ANALYSIS_DATA.attackTrees);
}

render();

function activateTab(tabId) {
  document.querySelectorAll(".tab").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.tab === tabId);
  });
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    panel.classList.toggle("is-active", panel.id === tabId);
  });
}

function readJsonFile(file, onLoad) {
  if (!file) {
    return;
  }
  const reader = new FileReader();
  reader.addEventListener("load", () => {
    try {
      onLoad(JSON.parse(String(reader.result)));
      clearError();
    } catch (error) {
      showError(`${file.name} 不是合法 JSON`);
    }
  });
  reader.addEventListener("error", () => showError(`${file.name} 读取失败`));
  reader.readAsText(file, "utf-8");
}

function render() {
  renderValueAssets();
  renderHighRiskModules();
  renderInternalNodes();
  renderAttackTrees();
  updateSummary();
}

function renderValueAssets() {
  els.valueAssetsCount.textContent = `${state.valueAssets.length} 项`;
  replaceRows(
    els.valueAssetsBody,
    state.valueAssets,
    ["资产名", "资产类别", "资产描述", "攻击损失", "判断为价值资产的原因"],
    "暂无价值资产",
  );
}

function renderHighRiskModules() {
  els.highRiskModulesCount.textContent = `${state.highRiskModules.length} 项`;
  replaceRows(
    els.highRiskModulesBody,
    state.highRiskModules,
    [
      "模块名称",
      "代码目录",
      "面临威胁",
      HIGH_RISK_FIELDS.management,
      HIGH_RISK_FIELDS.untrusted,
      HIGH_RISK_FIELDS.security,
      HIGH_RISK_FIELDS.sensitive,
      HIGH_RISK_FIELDS.web,
      HIGH_RISK_FIELDS.external,
      "判断为高风险模块的原因",
    ],
    "暂无高风险模块",
    {
      [HIGH_RISK_FIELDS.management]: yesNoCell,
      [HIGH_RISK_FIELDS.untrusted]: yesNoCell,
      [HIGH_RISK_FIELDS.security]: yesNoCell,
      [HIGH_RISK_FIELDS.sensitive]: yesNoCell,
      [HIGH_RISK_FIELDS.web]: yesNoCell,
      [HIGH_RISK_FIELDS.external]: yesNoCell,
    },
  );
}

function renderInternalNodes() {
  const nodes = collectInternalNodes(state.attackTrees.attack_trees);
  els.internalNodesCount.textContent = `${nodes.length} 项`;
  replaceRows(
    els.internalNodesBody,
    nodes,
    ["node_name", "description"],
    "暂无内部节点",
  );
}

function renderAttackTrees() {
  const trees = state.attackTrees.attack_trees;
  els.attackTreesCount.textContent = `${trees.length} 棵`;
  els.attackTreesCanvas.replaceChildren();
  if (!trees.length) {
    const empty = document.createElement("div");
    empty.className = "tree";
    empty.appendChild(emptyBlock("暂无攻击树"));
    els.attackTreesCanvas.appendChild(empty);
    return;
  }

  trees.forEach((tree, index) => {
    els.attackTreesCanvas.appendChild(createTreeView(tree, index));
  });
}

function replaceRows(tbody, rows, fields, emptyText, renderers = {}) {
  tbody.replaceChildren();
  if (!rows.length) {
    const row = document.createElement("tr");
    row.className = "empty-row";
    const cell = document.createElement("td");
    cell.colSpan = fields.length;
    cell.textContent = emptyText;
    row.appendChild(cell);
    tbody.appendChild(row);
    return;
  }

  rows.forEach((item) => {
    const row = document.createElement("tr");
    fields.forEach((field) => {
      const cell = document.createElement("td");
      const renderer = renderers[field];
      if (renderer) {
        cell.appendChild(renderer(item[field]));
      } else {
        cell.textContent = formatValue(item[field]);
      }
      row.appendChild(cell);
    });
    tbody.appendChild(row);
  });
}

function yesNoCell(value) {
  const badge = document.createElement("span");
  const text = formatValue(value) || "否";
  badge.className = `badge ${text === "是" ? "yes" : "no"}`;
  badge.textContent = text;
  return badge;
}

function collectInternalNodes(trees) {
  const seen = new Set();
  const nodes = [];
  trees.forEach((tree) => {
    (tree.nodes || []).forEach((node) => {
      if (node.node_type !== "内部节点") {
        return;
      }
      const name = node.node_name || node.module_name || "";
      const key = `${name}\n${node.description || ""}`;
      if (seen.has(key)) {
        return;
      }
      seen.add(key);
      nodes.push({
        node_name: name,
        description: node.description || "",
      });
    });
  });
  return nodes;
}

function createTreeView(tree, index) {
  const wrapper = document.createElement("article");
  wrapper.className = "tree";

  const header = document.createElement("header");
  header.className = "tree-header";
  const title = document.createElement("strong");
  title.textContent = tree.value_asset?.asset_name || tree.tree_id || `攻击树 ${index + 1}`;
  const meta = document.createElement("span");
  meta.textContent = tree.tree_id || `#${index + 1}`;
  header.append(title, meta);

  const body = document.createElement("div");
  body.className = "tree-body";
  const roots = getRootNodes(tree);
  if (!roots.length) {
    body.appendChild(emptyBlock("暂无可展示节点"));
  } else {
    const list = document.createElement("ul");
    const inbound = buildInboundIndex(tree.edges || []);
    const nodesById = buildNodeIndex(tree.nodes || []);
    roots.forEach((root) => {
      list.appendChild(createTreeNode(root, nodesById, inbound, new Set()));
    });
    body.appendChild(list);
  }

  wrapper.append(header, body);
  return wrapper;
}

function createTreeNode(node, nodesById, inbound, visited) {
  const item = document.createElement("li");
  const card = document.createElement("div");
  card.className = `tree-node ${nodeClass(node)}`;

  const button = document.createElement("button");
  button.className = "node-toggle";
  button.type = "button";

  const title = document.createElement("span");
  title.className = "node-title";
  title.textContent = node.node_name || node.module_name || node.node_id;
  const type = document.createElement("span");
  type.className = "node-type";
  type.textContent = node.node_type || "";
  button.append(title, type);

  const description = document.createElement("div");
  description.className = "node-description";
  description.textContent = node.description || "";
  card.append(button, description);
  item.appendChild(card);

  const childIds = inbound.get(String(node.node_id)) || [];
  const nextVisited = new Set(visited);
  nextVisited.add(String(node.node_id));
  const children = childIds
    .filter((id) => !nextVisited.has(id))
    .map((id) => nodesById.get(id))
    .filter(Boolean);

  if (children.length) {
    button.setAttribute("aria-expanded", "true");
    const list = document.createElement("ul");
    children.forEach((child) => {
      list.appendChild(createTreeNode(child, nodesById, inbound, nextVisited));
    });
    item.appendChild(list);
  } else {
    button.disabled = true;
  }

  return item;
}

function getRootNodes(tree) {
  const nodes = tree.nodes || [];
  const roots = nodes.filter((node) => node.node_type === "根节点");
  return roots.length ? roots : nodes.slice(0, 1);
}

function buildNodeIndex(nodes) {
  const index = new Map();
  nodes.forEach((node) => index.set(String(node.node_id), node));
  return index;
}

function buildInboundIndex(edges) {
  const index = new Map();
  edges.forEach((edge) => {
    const target = String(edge.target_node_id);
    const source = String(edge.source_node_id);
    if (!index.has(target)) {
      index.set(target, []);
    }
    index.get(target).push(source);
  });
  return index;
}

function nodeClass(node) {
  if (node.node_type === "根节点") {
    return "root";
  }
  if (node.node_type === "叶子节点") {
    return "leaf";
  }
  return "internal";
}

function normalizeAttackTreePayload(data) {
  if (Array.isArray(data)) {
    return {
      attack_trees: data,
      analysis_gaps: [],
    };
  }
  return {
    attack_trees: Array.isArray(data?.attack_trees) ? data.attack_trees : [],
    analysis_gaps: Array.isArray(data?.analysis_gaps) ? data.analysis_gaps : [],
  };
}

function formatValue(value) {
  if (value == null) {
    return "";
  }
  if (Array.isArray(value)) {
    return value.map(formatValue).filter(Boolean).join(", ");
  }
  if (typeof value === "object") {
    return JSON.stringify(value, null, 2);
  }
  return String(value);
}

function emptyBlock(text) {
  const block = document.createElement("div");
  block.className = "empty-block";
  const message = document.createElement("div");
  message.textContent = text;
  block.appendChild(message);
  return block;
}

function updateSummary() {
  const internalNodes = collectInternalNodes(state.attackTrees.attack_trees);
  els.summaryText.textContent = [
    `${state.valueAssets.length} 个价值资产`,
    `${state.highRiskModules.length} 个高风险模块`,
    `${internalNodes.length} 个内部节点`,
    `${state.attackTrees.attack_trees.length} 棵攻击树`,
  ].join(" / ");
}

function showError(message) {
  els.summaryText.textContent = message;
  els.summaryText.classList.add("error");
}

function clearError() {
  els.summaryText.classList.remove("error");
}
