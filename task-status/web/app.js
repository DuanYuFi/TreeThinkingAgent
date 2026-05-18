const svg = document.querySelector("#graph");
const details = document.querySelector("#details");
const counts = document.querySelector("#counts");
const refreshButton = document.querySelector("#refreshButton");
const maxDepthInput = document.querySelector("#maxDepthInput");
const filters = Array.from(document.querySelectorAll(".toolbar input[type='checkbox']"));

let graphData = { nodes: [], edges: [], counts: {} };
let selectedId = null;
let hoveredId = null;
let focusedId = null;
let selectedEdgeId = null;
let edgeDraftSourceId = null;
let graphNodes = [];
let graphEdges = [];
let animationFrame = null;
let dragging = null;
let suppressNextClick = false;
const pinned = new Map();

refreshButton.addEventListener("click", loadGraph);
filters.forEach((filter) => filter.addEventListener("change", refreshGraphView));
maxDepthInput.addEventListener("input", refreshGraphView);
maxDepthInput.addEventListener("change", () => {
  maxDepthInput.value = normalizeMaxDepth(maxDepthInput.value);
  refreshGraphView();
});
svg.addEventListener("click", (event) => {
  if (isGraphSelectionTarget(event.target)) {
    return;
  }
  clearGraphSelection();
});
svg.addEventListener("pointerdown", (event) => {
  if (!hasGraphSelection() || isActiveGraphSelectionTarget(event.target)) {
    return;
  }
  event.preventDefault();
  event.stopPropagation();
  clearGraphSelection();
}, true);
document.addEventListener("pointerdown", (event) => {
  if (!hasGraphSelection() || event.target.closest?.(".details-panel, .toolbar, .topbar")) {
    return;
  }
  if (isActiveGraphSelectionTarget(event.target)) {
    return;
  }
  event.preventDefault();
  event.stopPropagation();
  clearGraphSelection();
}, true);
window.addEventListener("resize", () => {
  initializeGraphPositions({ preserveExisting: true });
  drawGraph();
});
window.addEventListener("keydown", (event) => {
  if (event.key !== " " || !selectedId || isInteractiveTarget(event.target)) {
    return;
  }
  const selectedNode = graphData.nodes.find((node) => node.id === selectedId);
  if (!selectedNode || isRoot(selectedNode)) {
    return;
  }
  event.preventDefault();
  togglePinNode(selectedId);
});

loadGraph();

async function loadGraph() {
  const response = await fetch("/api/graph");
  graphData = await response.json();
  if (graphData.error) {
    details.innerHTML = `<h2>Database unavailable</h2><p>${escapeHtml(graphData.error)}</p>`;
  }
  if (!graphData.nodes.some((node) => node.id === selectedId)) {
    selectedId = graphData.nodes[0]?.id ?? null;
  }
  if (!graphData.nodes.some((node) => node.id === hoveredId)) {
    hoveredId = null;
  }
  if (!graphData.nodes.some((node) => node.id === focusedId)) {
    focusedId = null;
  }
  if (!graphData.edges.some((edge) => edgeIdentity(edge) === selectedEdgeId)) {
    selectedEdgeId = null;
  }
  if (!graphData.nodes.some((node) => node.id === edgeDraftSourceId)) {
    edgeDraftSourceId = null;
  }
  renderCounts();
  initializeGraphPositions({ preserveExisting: true });
  renderDetails();
  startSimulation();
}

function refreshGraphView() {
  drawGraph();
  renderDetails();
}

function renderCounts() {
  const statuses = ["active", "blocked", "ready", "done", "candidate"];
  counts.innerHTML = statuses.map((status) => `
    <button class="count-item" type="button" data-status="${status}">
      <span>${status}</span>
      <strong>${Number(graphData.counts?.[status] ?? 0)}</strong>
    </button>
  `).join("");
  counts.querySelectorAll(".count-item").forEach((button) => {
    button.addEventListener("click", () => {
      const status = button.dataset.status;
      const checkbox = filters.find((item) => item.value === status);
      if (checkbox) {
        checkbox.checked = !checkbox.checked;
        refreshGraphView();
      }
    });
  });
}

function initializeGraphPositions({ preserveExisting }) {
  const width = svg.clientWidth || 900;
  const height = svg.clientHeight || 640;
  const previous = new Map(graphNodes.map((node) => [node.id, node]));
  const statusAngles = {
    active: -Math.PI / 2,
    blocked: -Math.PI / 7,
    ready: Math.PI / 7,
    done: Math.PI * 0.78,
    candidate: Math.PI * 1.2,
  };
  const radius = Math.max(130, Math.min(width, height) * 0.28);

  graphNodes = graphData.nodes.map((node, index) => {
    const existing = previous.get(node.id);
    const fixed = pinned.get(node.id);
    if (isRoot(node)) {
      return {
        ...node,
        x: width / 2,
        y: height / 2,
        vx: 0,
        vy: 0,
        fx: width / 2,
        fy: height / 2,
      };
    }
    if (preserveExisting && existing) {
      return { ...node, x: existing.x, y: existing.y, vx: existing.vx, vy: existing.vy, fx: fixed?.x, fy: fixed?.y };
    }
    const angle = statusAngles[node.status] ?? (index / Math.max(1, graphData.nodes.length)) * Math.PI * 2;
    const jitter = (index % 5) * 16;
    return {
      ...node,
      x: fixed?.x ?? width / 2 + Math.cos(angle) * (radius + jitter),
      y: fixed?.y ?? height / 2 + Math.sin(angle) * (radius + jitter),
      vx: 0,
      vy: 0,
      fx: fixed?.x,
      fy: fixed?.y,
    };
  });

  const nodeIds = new Set(graphNodes.map((node) => node.id));
  graphEdges = graphData.edges.filter((edge) => nodeIds.has(edge.source_id) && nodeIds.has(edge.target_id));
}

function startSimulation() {
  stopSimulation();
  let ticks = 0;
  const tick = () => {
    runForces();
    drawGraph();
    ticks += 1;
    if (ticks < 100 || dragging) {
      animationFrame = requestAnimationFrame(tick);
    }
  };
  tick();
}

function stopSimulation() {
  if (animationFrame) {
    cancelAnimationFrame(animationFrame);
    animationFrame = null;
  }
}

function runForces() {
  const width = svg.clientWidth || 900;
  const height = svg.clientHeight || 640;
  const centerX = width / 2;
  const centerY = height / 2;
  const byId = new Map(graphNodes.map((node) => [node.id, node]));
  const decompositionChildLayout = buildDecompositionChildLayout();

  for (const node of graphNodes) {
    if (isRoot(node)) {
      node.x = centerX;
      node.y = centerY;
      node.fx = centerX;
      node.fy = centerY;
      node.vx = 0;
      node.vy = 0;
      continue;
    }
    node.vx += (centerX - node.x) * 0.0018;
    node.vy += (centerY - node.y) * 0.0018;
    const statusTarget = statusTargetPoint(node.status, width, height);
    node.vx += (statusTarget.x - node.x) * 0.0015;
    node.vy += (statusTarget.y - node.y) * 0.0015;
  }

  for (let i = 0; i < graphNodes.length; i += 1) {
    for (let j = i + 1; j < graphNodes.length; j += 1) {
      const a = graphNodes[i];
      const b = graphNodes[j];
      let dx = b.x - a.x;
      let dy = b.y - a.y;
      let distanceSq = dx * dx + dy * dy;
      if (distanceSq < 0.01) {
        dx = 1;
        dy = 0;
        distanceSq = 1;
      }
      const distance = Math.sqrt(distanceSq);
      const force = Math.min(4, 2400 / distanceSq);
      const fx = (dx / distance) * force;
      const fy = (dy / distance) * force;
      a.vx -= fx;
      a.vy -= fy;
      b.vx += fx;
      b.vy += fy;
    }
  }

  for (const edge of graphEdges) {
    const source = byId.get(edge.source_id);
    const target = byId.get(edge.target_id);
    if (!source || !target) {
      continue;
    }
    const dx = target.x - source.x;
    const dy = target.y - source.y;
    const distance = Math.max(1, Math.sqrt(dx * dx + dy * dy));
    const desired = edge.relation === "decomposes_to" ? 190 : edge.relation === "depends_on" ? 240 : 280;
    const force = (distance - desired) * 0.012;
    const fx = (dx / distance) * force;
    const fy = (dy / distance) * force;
    if (!isRoot(source)) {
      source.vx += fx;
      source.vy += fy;
    }
    if (!isRoot(target)) {
      target.vx -= fx;
      target.vy -= fy;
    }
  }

  applyDecompositionBranchForces(decompositionChildLayout, byId, centerX, centerY);

  for (const node of graphNodes) {
    if (isRoot(node)) {
      node.x = centerX;
      node.y = centerY;
      node.fx = centerX;
      node.fy = centerY;
      node.vx = 0;
      node.vy = 0;
      continue;
    }
    if (node.fx !== undefined && node.fy !== undefined) {
      node.x = node.fx;
      node.y = node.fy;
      node.vx = 0;
      node.vy = 0;
      continue;
    }
    node.vx *= 0.82;
    node.vy *= 0.82;
    node.x = clamp(node.x + node.vx, 42, width - 42);
    node.y = clamp(node.y + node.vy, 42, height - 64);
  }
}

function buildDecompositionChildLayout() {
  const childrenByParent = new Map();
  for (const edge of graphEdges) {
    if (edge.relation !== "decomposes_to") {
      continue;
    }
    if (!childrenByParent.has(edge.source_id)) {
      childrenByParent.set(edge.source_id, []);
    }
    childrenByParent.get(edge.source_id).push(edge);
  }

  const childLayout = new Map();
  for (const edges of childrenByParent.values()) {
    edges.sort((a, b) => String(a.target_id).localeCompare(String(b.target_id)));
    edges.forEach((edge, index) => {
      childLayout.set(edgeKey(edge), {
        index,
        count: edges.length,
      });
    });
  }
  return childLayout;
}

function applyDecompositionBranchForces(childLayout, byId, centerX, centerY) {
  for (const edge of graphEdges) {
    if (edge.relation !== "decomposes_to") {
      continue;
    }

    const source = byId.get(edge.source_id);
    const target = byId.get(edge.target_id);
    if (!source || !target || isRoot(target)) {
      continue;
    }

    const parentDx = source.x - centerX;
    const parentDy = source.y - centerY;
    const parentDistance = Math.sqrt(parentDx * parentDx + parentDy * parentDy);
    if (parentDistance < 70) {
      continue;
    }

    const nx = parentDx / parentDistance;
    const ny = parentDy / parentDistance;
    const tx = -ny;
    const ty = nx;
    const layout = childLayout.get(edgeKey(edge)) ?? { index: 0, count: 1 };
    const rawSpread = (layout.index - (layout.count - 1) / 2) * 72;
    const tangentSpread = clamp(rawSpread, -190, 190);
    const outwardGap = 160 + Math.min(120, Math.max(0, parentDistance - 130) * 0.35);
    const targetX = source.x + nx * outwardGap + tx * tangentSpread;
    const targetY = source.y + ny * outwardGap + ty * tangentSpread;
    const controlledSource = source.fx !== undefined && source.fy !== undefined;
    const strength = controlledSource ? 0.026 : 0.014;

    target.vx += (targetX - target.x) * strength;
    target.vy += (targetY - target.y) * strength;
  }
}

function edgeKey(edge) {
  return `${edge.source_id}->${edge.target_id}`;
}

function buildViewState() {
  const visibleByFilter = new Set(
    graphData.nodes
      .filter((node) => nodePassesFilters(node))
      .map((node) => node.id),
  );
  return {
    visibleByFilter,
    focusMask: buildFocusMask(focusedId),
  };
}

function nodePassesFilters(node) {
  const selectedStatuses = new Set(filters.filter((item) => item.checked).map((item) => item.value));
  const statusPasses = !selectedStatuses.size || selectedStatuses.has(node.status) || isRoot(node);
  const maxDepth = Number.parseInt(queryMaxDepth(maxDepthInput.value), 10);
  const distance = Number(node.root_distance);
  const depthPasses = Number.isFinite(distance) && distance <= maxDepth;
  return statusPasses && depthPasses;
}

function shouldDimNode(node, viewState) {
  if (!viewState.visibleByFilter.has(node.id)) {
    return true;
  }
  return viewState.focusMask.active && !viewState.focusMask.nodes.has(node.id);
}

function shouldDimEdge(edge, viewState) {
  if (!viewState.visibleByFilter.has(edge.source_id) || !viewState.visibleByFilter.has(edge.target_id)) {
    return true;
  }
  return viewState.focusMask.active && !viewState.focusMask.edges.has(edgeIdentity(edge));
}

function buildFocusMask(id) {
  if (!id) {
    return { active: false, nodes: new Set(), edges: new Set() };
  }

  const nodes = new Set([id]);
  const edges = new Set();
  for (const edge of graphData.edges) {
    if (edge.source_id === id || edge.target_id === id) {
      nodes.add(edge.source_id);
      nodes.add(edge.target_id);
      edges.add(edgeIdentity(edge));
    }
  }

  const path = shortestPathToRoot(id);
  path.nodes.forEach((nodeId) => nodes.add(nodeId));
  path.edges.forEach((edgeId) => edges.add(edgeId));
  return { active: true, nodes, edges };
}

function shortestPathToRoot(startId) {
  const roots = new Set(graphData.nodes.filter((node) => isRoot(node)).map((node) => node.id));
  if (!startId || roots.has(startId)) {
    return { nodes: new Set(startId ? [startId] : []), edges: new Set() };
  }

  const adjacency = new Map();
  for (const node of graphData.nodes) {
    adjacency.set(node.id, []);
  }
  for (const edge of graphData.edges) {
    if (!adjacency.has(edge.source_id) || !adjacency.has(edge.target_id)) {
      continue;
    }
    adjacency.get(edge.source_id).push({ nodeId: edge.target_id, edgeId: edgeIdentity(edge) });
    adjacency.get(edge.target_id).push({ nodeId: edge.source_id, edgeId: edgeIdentity(edge) });
  }

  const queue = [startId];
  const visited = new Set([startId]);
  const previous = new Map();
  let rootId = null;
  for (let index = 0; index < queue.length; index += 1) {
    const current = queue[index];
    if (roots.has(current)) {
      rootId = current;
      break;
    }
    for (const next of adjacency.get(current) ?? []) {
      if (visited.has(next.nodeId)) {
        continue;
      }
      visited.add(next.nodeId);
      previous.set(next.nodeId, { nodeId: current, edgeId: next.edgeId });
      queue.push(next.nodeId);
    }
  }

  if (!rootId) {
    return { nodes: new Set([startId]), edges: new Set() };
  }

  const nodes = new Set([rootId]);
  const edges = new Set();
  let current = rootId;
  while (current !== startId) {
    const step = previous.get(current);
    if (!step) {
      break;
    }
    edges.add(step.edgeId);
    nodes.add(step.nodeId);
    current = step.nodeId;
  }
  return { nodes, edges };
}

function edgeIdentity(edge) {
  return String(edge.id ?? `${edge.source_id}->${edge.target_id}:${edge.relation}`);
}

function statusTargetPoint(status, width, height) {
  const targets = {
    active: { x: width * 0.5, y: height * 0.42 },
    blocked: { x: width * 0.72, y: height * 0.35 },
    ready: { x: width * 0.68, y: height * 0.62 },
    done: { x: width * 0.34, y: height * 0.64 },
    candidate: { x: width * 0.28, y: height * 0.32 },
  };
  return targets[status] ?? { x: width / 2, y: height / 2 };
}

function drawGraph() {
  const width = svg.clientWidth || 900;
  const height = svg.clientHeight || 640;
  const byId = new Map(graphNodes.map((node) => [node.id, node]));
  const viewState = buildViewState();
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.innerHTML = `
    <defs>
      <radialGradient id="nodeGlow" cx="50%" cy="45%" r="65%">
        <stop offset="0%" stop-color="rgba(255,255,255,0.28)"></stop>
        <stop offset="100%" stop-color="rgba(255,255,255,0)"></stop>
      </radialGradient>
      <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
        <path d="M 0 0 L 10 5 L 0 10 z" fill="#7f9188"></path>
      </marker>
      <marker id="arrow-highlight" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">
        <path d="M 0 0 L 10 5 L 0 10 z" fill="#f4d06f"></path>
      </marker>
    </defs>
  `;

  svg.appendChild(makeSvg("rect", {
    x: "0",
    y: "0",
    width: String(width),
    height: String(height),
    class: "graph-hitbox",
  }));
  drawConstellations(width, height);

  if (!graphNodes.length) {
    const text = makeSvg("text", { x: width / 2, y: height / 2, "text-anchor": "middle", class: "empty" });
    text.textContent = "No inquiries match the current filters.";
    svg.appendChild(text);
    return;
  }

  const edgeLayer = makeSvg("g", { class: "edge-layer" });
  const labelLayer = makeSvg("g", { class: "edge-label-layer" });
  const nodeLayer = makeSvg("g", { class: "node-layer" });
  svg.append(edgeLayer, labelLayer, nodeLayer);

  for (const edge of graphEdges) {
    const source = byId.get(edge.source_id);
    const target = byId.get(edge.target_id);
    if (!source || !target) {
      continue;
    }
    const dimmedEdge = shouldDimEdge(edge, viewState);
    const connectedToHover = !dimmedEdge && isConnectedToHoveredNode(edge.source_id, edge.target_id);
    const selectedEdge = edgeIdentity(edge) === selectedEdgeId;
    edgeLayer.appendChild(makeSvg("line", {
      x1: source.x,
      y1: source.y,
      x2: target.x,
      y2: target.y,
      class: `edge-hit ${dimmedEdge ? "edge-hit-dimmed" : ""}`,
      "data-edge-id": edgeIdentity(edge),
    }));
    const edgeClass = [
      "edge",
      `edge-${edge.relation}`,
      selectedEdge ? "edge-selected" : "",
      dimmedEdge ? "edge-dimmed" : "",
      hoverEdgeClass(connectedToHover, dimmedEdge),
    ].filter(Boolean).join(" ");
    const visualEdge = makeSvg("line", {
      x1: source.x,
      y1: source.y,
      x2: target.x,
      y2: target.y,
      class: edgeClass,
      "data-edge-id": edgeIdentity(edge),
      "data-source-id": edge.source_id,
      "data-target-id": edge.target_id,
      "marker-end": connectedToHover ? "url(#arrow-highlight)" : "url(#arrow)",
    });
    visualEdge.addEventListener("click", (event) => {
      event.stopPropagation();
      selectEdge(edgeIdentity(edge));
    });
    edgeLayer.lastChild.addEventListener("click", (event) => {
      event.stopPropagation();
      selectEdge(edgeIdentity(edge));
    });
    edgeLayer.appendChild(visualEdge);

    const label = makeSvg("text", {
      x: (source.x + target.x) / 2,
      y: (source.y + target.y) / 2 - 7,
      "text-anchor": "middle",
      class: [
        "edge-label",
        selectedEdge ? "edge-label-selected" : "",
        dimmedEdge ? "edge-label-dimmed" : "",
        hoverLabelClass(connectedToHover, dimmedEdge),
      ].filter(Boolean).join(" "),
      "data-edge-id": edgeIdentity(edge),
      "data-source-id": edge.source_id,
      "data-target-id": edge.target_id,
    });
    label.textContent = relationLabel(edge.relation);
    label.addEventListener("click", (event) => {
      event.stopPropagation();
      selectEdge(edgeIdentity(edge));
    });
    labelLayer.appendChild(label);
  }

  for (const node of graphNodes) {
    const radius = nodeRadius(node);
    const dimmedNode = shouldDimNode(node, viewState);
    const group = makeSvg("g", {
      class: `node ${isRoot(node) ? "node-root" : ""} ${node.id === selectedId ? "selected" : ""} ${node.id === focusedId ? "focused" : ""} ${node.id === hoveredId ? "hovered" : ""} ${dimmedNode ? "node-dimmed" : ""} ${node.fx !== undefined && !isRoot(node) ? "pinned" : ""}`,
      transform: `translate(${node.x},${node.y})`,
      tabindex: "0",
      role: "button",
      "aria-label": node.description ? `${node.content} ${node.description}` : node.content,
      "data-node-id": node.id,
    });
    group.addEventListener("pointerenter", () => setHoveredNode(node.id));
    group.addEventListener("pointerleave", () => clearHoveredNode(node.id));
    group.addEventListener("focus", () => setHoveredNode(node.id));
    group.addEventListener("blur", () => clearHoveredNode(node.id));
    group.addEventListener("click", (event) => {
      event.stopPropagation();
      if (event.currentTarget.classList.contains("node-dimmed")) {
        clearGraphSelection();
        return;
      }
      if (suppressNextClick) {
        event.preventDefault();
        suppressNextClick = false;
        return;
      }
      selectNode(node.id);
    });
    group.addEventListener("dblclick", (event) => {
      event.preventDefault();
      suppressNextClick = false;
      selectNode(node.id);
    });
    if (!isRoot(node)) {
      group.addEventListener("pointerdown", (event) => startDrag(event, node.id));
    }
    group.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        event.stopPropagation();
        selectNode(node.id);
        return;
      }
      if (event.key === " ") {
        event.preventDefault();
        event.stopPropagation();
        selectNode(node.id);
        togglePinNode(node.id);
      }
    });

    const title = makeSvg("title", {});
    title.textContent = node.description ? `${node.content}\n${node.description}` : node.content;
    group.appendChild(title);

    group.appendChild(makeSvg("circle", {
      r: String(radius + 9),
      class: `node-halo status-${node.status}`,
    }));
    group.appendChild(makeSvg("circle", {
      r: String(radius),
      class: `node-core status-${node.status}`,
    }));
    group.appendChild(makeSvg("circle", {
      r: String(Math.max(8, radius * 0.58)),
      class: "node-gloss",
    }));

    const label = makeSvg("text", {
      y: String(nodeLabelY(node, radius, height)),
      "text-anchor": "middle",
      class: "node-title",
    });
    const titleLine = makeSvg("tspan", { x: "0", dy: "0" });
    titleLine.textContent = truncate(node.content, 30);
    label.appendChild(titleLine);
    if (node.description) {
      const descriptionLine = makeSvg("tspan", { x: "0", dy: "15", class: "node-description-line" });
      descriptionLine.textContent = truncate(node.description, isRoot(node) ? 38 : 24);
      label.appendChild(descriptionLine);
    }
    group.appendChild(label);

    const idLabel = makeSvg("text", {
      y: "4",
      "text-anchor": "middle",
      class: "node-id",
    });
    idLabel.textContent = shortId(node.id);
    group.appendChild(idLabel);

    nodeLayer.appendChild(group);
  }
}

function drawConstellations(width, height) {
  const layer = makeSvg("g", { class: "constellation-layer" });
  const points = [
    [0.18, 0.22], [0.28, 0.74], [0.42, 0.18], [0.62, 0.78],
    [0.76, 0.24], [0.86, 0.58], [0.12, 0.54], [0.52, 0.5],
  ];
  for (const [x, y] of points) {
    layer.appendChild(makeSvg("circle", {
      cx: width * x,
      cy: height * y,
      r: "1.3",
      class: "star",
    }));
  }
  svg.appendChild(layer);
}

function startDrag(event, id) {
  const node = graphNodes.find((item) => item.id === id);
  if (!node || isRoot(node)) {
    return;
  }
  if (shouldDimNode(node, buildViewState())) {
    event.preventDefault();
    event.stopPropagation();
    clearGraphSelection();
    return;
  }
  event.preventDefault();
  selectedId = id;
  const point = svgPoint(event);
  dragging = {
    id,
    pointerId: event.pointerId,
    startX: point.x,
    startY: point.y,
    originalFx: node.fx,
    originalFy: node.fy,
    wasPinned: pinned.has(id),
    started: false,
  };
  svg.setPointerCapture(event.pointerId);
  svg.addEventListener("pointermove", dragMove);
  svg.addEventListener("pointerup", endDrag);
  svg.addEventListener("pointercancel", endDrag);
  renderDetails();
}

function dragMove(event) {
  if (!dragging || event.pointerId !== dragging.pointerId) {
    return;
  }
  const node = graphNodes.find((item) => item.id === dragging.id);
  if (!node) {
    return;
  }
  const point = svgPoint(event);
  if (!dragging.started) {
    const dx = point.x - dragging.startX;
    const dy = point.y - dragging.startY;
    if (Math.sqrt(dx * dx + dy * dy) < 4) {
      return;
    }
    dragging.started = true;
    startSimulation();
  }
  node.fx = point.x;
  node.fy = point.y;
}

function endDrag(event) {
  if (!dragging || event.pointerId !== dragging.pointerId) {
    return;
  }
  const finishedDrag = dragging;
  svg.releasePointerCapture(event.pointerId);
  svg.removeEventListener("pointermove", dragMove);
  svg.removeEventListener("pointerup", endDrag);
  svg.removeEventListener("pointercancel", endDrag);
  const node = graphNodes.find((item) => item.id === finishedDrag.id);
  dragging = null;
  if (finishedDrag.started) {
    if (node && finishedDrag.wasPinned) {
      pinned.set(node.id, { x: node.fx, y: node.fy });
    } else if (node) {
      node.fx = undefined;
      node.fy = undefined;
    }
    suppressNextClick = true;
    startSimulation();
    renderDetails();
  } else {
    if (node && shouldDimNode(node, buildViewState())) {
      clearGraphSelection();
      return;
    }
    if (node && finishedDrag.wasPinned) {
      node.fx = finishedDrag.originalFx;
      node.fy = finishedDrag.originalFy;
    } else if (node) {
      node.fx = undefined;
      node.fy = undefined;
    }
    selectNode(finishedDrag.id);
  }
}

function svgPoint(event) {
  const point = svg.createSVGPoint();
  point.x = event.clientX;
  point.y = event.clientY;
  const transformed = point.matrixTransform(svg.getScreenCTM().inverse());
  return {
    x: transformed.x,
    y: transformed.y,
  };
}

function selectNode(id) {
  selectedId = id;
  focusedId = id;
  selectedEdgeId = null;
  drawGraph();
  renderDetails();
}

function selectEdge(id) {
  const edgeElement = svg.querySelector(`.edge[data-edge-id="${cssEscape(id)}"], .edge-hit[data-edge-id="${cssEscape(id)}"]`);
  if (edgeElement?.classList.contains("edge-dimmed") || edgeElement?.classList.contains("edge-hit-dimmed")) {
    clearGraphSelection();
    return;
  }
  selectedEdgeId = id;
  selectedId = null;
  focusedId = null;
  drawGraph();
  renderDetails();
}

function clearGraphSelection() {
  if (!hasGraphSelection() && hoveredId === null) {
    return;
  }
  selectedId = null;
  focusedId = null;
  selectedEdgeId = null;
  edgeDraftSourceId = null;
  hoveredId = null;
  drawGraph();
  renderDetails();
}

function hasGraphSelection() {
  return selectedId !== null || focusedId !== null || selectedEdgeId !== null || edgeDraftSourceId !== null;
}

function isGraphSelectionTarget(target) {
  if (target.closest?.(".node-dimmed, .edge-dimmed, .edge-hit-dimmed, .edge-label-dimmed")) {
    return false;
  }
  return Boolean(target.closest?.(".node, .edge, .edge-hit, .edge-label"));
}

function isActiveGraphSelectionTarget(target) {
  return Boolean(target.closest?.(
    ".node:not(.node-dimmed), .edge:not(.edge-dimmed), .edge-label:not(.edge-label-dimmed)",
  ));
}

function cssEscape(value) {
  return window.CSS?.escape ? window.CSS.escape(String(value)) : String(value).replaceAll('"', '\\"');
}

function setHoveredNode(id) {
  if (hoveredId === id) {
    return;
  }
  hoveredId = id;
  applyHoverState();
}

function clearHoveredNode(id) {
  if (hoveredId !== id) {
    return;
  }
  hoveredId = null;
  applyHoverState();
}

function applyHoverState() {
  svg.querySelectorAll(".edge").forEach((edgeElement) => {
    const dimmed = edgeElement.classList.contains("edge-dimmed");
    const connected = !dimmed && isConnectedToHoveredElement(edgeElement);
    edgeElement.classList.toggle("edge-highlighted", connected);
    edgeElement.classList.toggle("edge-hover-muted", hoveredId !== null && !connected && !dimmed);
    edgeElement.setAttribute("marker-end", connected ? "url(#arrow-highlight)" : "url(#arrow)");
  });
  svg.querySelectorAll(".edge-label").forEach((labelElement) => {
    const dimmed = labelElement.classList.contains("edge-label-dimmed");
    const connected = !dimmed && isConnectedToHoveredElement(labelElement);
    labelElement.classList.toggle("edge-label-highlighted", connected);
    labelElement.classList.toggle("edge-label-hover-muted", hoveredId !== null && !connected && !dimmed);
  });
  svg.querySelectorAll(".node").forEach((nodeElement) => {
    nodeElement.classList.toggle("hovered", nodeElement.getAttribute("data-node-id") === hoveredId);
  });
}

function isConnectedToHoveredElement(element) {
  return isConnectedToHoveredNode(
    element.getAttribute("data-source-id"),
    element.getAttribute("data-target-id"),
  );
}

function isConnectedToHoveredNode(sourceId, targetId) {
  return hoveredId !== null && (sourceId === hoveredId || targetId === hoveredId);
}

function hoverEdgeClass(connectedToHover, dimmed) {
  if (hoveredId === null) {
    return "";
  }
  if (dimmed) {
    return "";
  }
  return connectedToHover ? "edge-highlighted" : "edge-hover-muted";
}

function hoverLabelClass(connectedToHover, dimmed) {
  if (hoveredId === null) {
    return "";
  }
  if (dimmed) {
    return "";
  }
  return connectedToHover ? "edge-label-highlighted" : "edge-label-hover-muted";
}

function togglePinNode(id) {
  const node = graphNodes.find((item) => item.id === id);
  if (node && isRoot(node)) {
    return;
  }
  if (pinned.has(id)) {
    unpinNode(id);
  } else {
    pinNode(id);
  }
}

function pinNode(id) {
  const node = graphNodes.find((item) => item.id === id);
  if (!node || isRoot(node)) {
    return;
  }
  node.fx = node.x;
  node.fy = node.y;
  pinned.set(id, { x: node.fx, y: node.fy });
  drawGraph();
  renderDetails();
}

function unpinNode(id) {
  const node = graphNodes.find((item) => item.id === id);
  if (node && isRoot(node)) {
    return;
  }
  pinned.delete(id);
  if (node) {
    node.fx = undefined;
    node.fy = undefined;
  }
  startSimulation();
  renderDetails();
}

function renderDetails() {
  const selectedEdge = graphData.edges.find((edge) => edgeIdentity(edge) === selectedEdgeId);
  if (selectedEdge) {
    renderEdgeDetails(selectedEdge);
    return;
  }

  const node = graphData.nodes.find((item) => item.id === selectedId);
  if (!node) {
    details.innerHTML = "<h2>No node selected</h2><p>Click a node to inspect it.</p>";
    return;
  }

  const incoming = graphData.edges.filter((edge) => edge.target_id === node.id);
  const outgoing = graphData.edges.filter((edge) => edge.source_id === node.id);
  const root = isRoot(node);
  const isPinned = pinned.has(node.id);
  const sourceNode = edgeDraftSourceId ? graphData.nodes.find((item) => item.id === edgeDraftSourceId) : null;
  const movementLabel = root ? "fixed root" : isPinned ? "pinned" : "floating";
  const actionMarkup = root
    ? '<button class="detail-action" type="button" disabled>Root is fixed</button>'
    : `<button class="detail-action" type="button" data-action="toggle-pin">${isPinned ? "Release position" : "Pin position"}</button>`;
  details.innerHTML = `
    <h2>${escapeHtml(node.content)}</h2>
    <p class="node-description-detail">${escapeHtml(node.description || "No short explanation recorded.")}</p>
    <div class="meta">
      <span class="pill status-pill ${escapeHtml(node.status)}">${escapeHtml(node.status)}</span>
      ${root ? '<span class="pill root-pill">root</span>' : ''}
      <span class="pill">importance ${formatScore(node.importance)}</span>
      <span class="pill">confidence ${formatScore(node.confidence)}</span>
      <span class="pill">root distance ${formatRootDistance(node.root_distance)}</span>
      <span class="pill">${movementLabel}</span>
      <span class="pill">${escapeHtml(node.id)}</span>
    </div>
    <div class="detail-actions">
      ${actionMarkup}
      <button class="detail-action" type="button" data-action="start-edge">Use as edge source</button>
      ${sourceNode ? '<button class="detail-action subtle" type="button" data-action="clear-edge-source">Clear edge source</button>' : ''}
    </div>
    <form class="edit-form" data-form="node-content">
      <label for="nodeContentInput">Content</label>
      <textarea id="nodeContentInput" name="content" rows="4">${escapeHtml(node.content)}</textarea>
      <label for="nodeDescriptionInput">Short explanation</label>
      <input id="nodeDescriptionInput" name="description" type="text" maxlength="50" value="${escapeHtml(node.description || "")}">
      <button class="detail-action primary" type="submit">Save node</button>
    </form>
    ${renderCreateEdgeForm(sourceNode, node)}
    <div class="answer">
      <h3>Answer</h3>
      <p>${node.answer ? escapeHtml(node.answer) : "No answer recorded."}</p>
    </div>
    <div class="relations">
      <h3>Outgoing</h3>
      ${renderRelationList(outgoing, "target_id")}
    </div>
    <div class="relations">
      <h3>Incoming</h3>
      ${renderRelationList(incoming, "source_id")}
    </div>
  `;
  const toggleButton = details.querySelector('[data-action="toggle-pin"]');
  if (toggleButton) {
    toggleButton.addEventListener("click", () => togglePinNode(node.id));
  }
  const startEdgeButton = details.querySelector('[data-action="start-edge"]');
  if (startEdgeButton) {
    startEdgeButton.addEventListener("click", () => {
      edgeDraftSourceId = node.id;
      renderDetails();
    });
  }
  const clearEdgeSourceButton = details.querySelector('[data-action="clear-edge-source"]');
  if (clearEdgeSourceButton) {
    clearEdgeSourceButton.addEventListener("click", () => {
      edgeDraftSourceId = null;
      renderDetails();
    });
  }
  const editNodeForm = details.querySelector('[data-form="node-content"]');
  if (editNodeForm) {
    editNodeForm.addEventListener("submit", (event) => {
      event.preventDefault();
      const formData = new FormData(editNodeForm);
      updateSelectedNodeContent(
        node.id,
        formData.get("content"),
        formData.get("description"),
      );
    });
  }
  const createEdgeForm = details.querySelector('[data-form="create-edge"]');
  if (createEdgeForm) {
    createEdgeForm.addEventListener("submit", (event) => {
      event.preventDefault();
      createSelectedEdge(new FormData(createEdgeForm));
    });
  }
}

function renderEdgeDetails(edge) {
  const source = graphData.nodes.find((node) => node.id === edge.source_id);
  const target = graphData.nodes.find((node) => node.id === edge.target_id);
  details.innerHTML = `
    <h2>Selected edge</h2>
    <div class="meta">
      <span class="pill">${escapeHtml(relationLabel(edge.relation))}</span>
      <span class="pill">edge ${escapeHtml(edgeIdentity(edge))}</span>
    </div>
    <div class="edge-summary">
      <p><strong>Source</strong>: ${escapeHtml(source?.content ?? edge.source_id)}</p>
      <p><strong>Target</strong>: ${escapeHtml(target?.content ?? edge.target_id)}</p>
      <p><strong>Note</strong>: ${edge.note ? escapeHtml(edge.note) : "No note."}</p>
    </div>
    <div class="detail-actions">
      <button class="detail-action danger" type="button" data-action="delete-edge">Delete edge</button>
    </div>
  `;
  const deleteButton = details.querySelector('[data-action="delete-edge"]');
  deleteButton.addEventListener("click", () => deleteSelectedEdge(edge));
}

function renderCreateEdgeForm(sourceNode, targetNode) {
  if (!sourceNode) {
    return "";
  }
  if (sourceNode.id === targetNode.id) {
    return `
      <div class="edit-form edge-draft">
        <label>New edge</label>
        <p>Source selected: ${escapeHtml(shortId(sourceNode.id))}. Click another node to choose target.</p>
      </div>
    `;
  }
  return `
    <form class="edit-form edge-draft" data-form="create-edge">
      <label>New edge</label>
      <input type="hidden" name="source_id" value="${escapeHtml(sourceNode.id)}">
      <input type="hidden" name="target_id" value="${escapeHtml(targetNode.id)}">
      <p><strong>${escapeHtml(shortId(sourceNode.id))}</strong> → <strong>${escapeHtml(shortId(targetNode.id))}</strong></p>
      <select name="relation">
        <option value="decomposes_to">decomposes</option>
        <option value="depends_on">depends</option>
        <option value="relates_to">relates</option>
      </select>
      <input name="note" type="text" placeholder="Optional note">
      <button class="detail-action primary" type="submit">Create edge</button>
    </form>
  `;
}

async function updateSelectedNodeContent(nodeId, content, description) {
  try {
    await requestJson(`/api/nodes/${encodeURIComponent(nodeId)}`, {
      method: "PATCH",
      body: JSON.stringify({ content, description }),
    });
    selectedId = nodeId;
    await loadGraph();
  } catch (error) {
    showDetailError(error);
  }
}

async function createSelectedEdge(formData) {
  try {
    const payload = {
      source_id: formData.get("source_id"),
      target_id: formData.get("target_id"),
      relation: formData.get("relation"),
      note: formData.get("note"),
    };
    const result = await requestJson("/api/edges", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    selectedId = null;
    focusedId = null;
    edgeDraftSourceId = null;
    selectedEdgeId = String(result.id);
    await loadGraph();
  } catch (error) {
    showDetailError(error);
  }
}

async function deleteSelectedEdge(edge) {
  try {
    await requestJson(`/api/edges/${encodeURIComponent(edgeIdentity(edge))}`, {
      method: "DELETE",
    });
    selectedEdgeId = null;
    await loadGraph();
  } catch (error) {
    showDetailError(error);
  }
}

async function requestJson(url, options) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || `Request failed: ${response.status}`);
  }
  return payload;
}

function showDetailError(error) {
  const message = error instanceof Error ? error.message : String(error);
  const existing = details.querySelector(".form-error");
  if (existing) {
    existing.textContent = message;
    return;
  }
  details.insertAdjacentHTML("afterbegin", `<p class="form-error">${escapeHtml(message)}</p>`);
}

function renderRelationList(edges, otherKey) {
  if (!edges.length) {
    return "<p>No relations.</p>";
  }
  return `<ul>${edges.map((edge) => {
    const other = graphData.nodes.find((node) => node.id === edge[otherKey]);
    const label = other ? other.content : edge[otherKey];
    const note = edge.note ? `, ${escapeHtml(edge.note)}` : "";
    return `<li><strong>${escapeHtml(relationLabel(edge.relation))}</strong>: ${escapeHtml(label)}${note}</li>`;
  }).join("")}</ul>`;
}

function nodeRadius(node) {
  if (isRoot(node)) {
    return 34 + Math.round(Number(node.importance || 0) * 10);
  }
  return 17 + Math.round(Number(node.importance || 0) * 12);
}

function nodeLabelY(node, radius, height) {
  const lineCount = node.description ? 2 : 1;
  const belowY = radius + 20;
  const belowBottom = node.y + belowY + (lineCount - 1) * 15 + 5;
  const aboveY = -radius - (lineCount === 2 ? 44 : 28);
  const aboveTop = node.y + aboveY - 12;
  if (belowBottom <= height - 8 || aboveTop < 8) {
    return belowY;
  }
  return aboveY;
}

function isRoot(node) {
  return Number(node?.is_root || 0) === 1;
}

function relationLabel(relation) {
  return {
    decomposes_to: "decomposes",
    depends_on: "depends",
    relates_to: "relates",
  }[relation] ?? relation;
}

function shortId(id) {
  const node = graphNodes.find((item) => item.id === id);
  if (node && isRoot(node)) {
    return "ROOT";
  }
  return String(id || "").replace("inq_", "").slice(0, 4);
}

function makeSvg(name, attributes) {
  const element = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (const [key, value] of Object.entries(attributes)) {
    element.setAttribute(key, value);
  }
  return element;
}

function truncate(text, maxLength) {
  if (!text || text.length <= maxLength) {
    return text || "";
  }
  return `${text.slice(0, maxLength - 1)}…`;
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function formatScore(value) {
  return Number(value || 0).toFixed(2);
}

function formatRootDistance(value) {
  if (value === null || value === undefined) {
    return "unknown";
  }
  return String(value);
}

function normalizeMaxDepth(value) {
  const parsed = Number.parseInt(value, 10);
  if (Number.isNaN(parsed) || parsed < 0) {
    return "0";
  }
  return String(parsed);
}

function queryMaxDepth(value) {
  if (String(value).trim() === "") {
    return "0";
  }
  return normalizeMaxDepth(value);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function isInteractiveTarget(target) {
  const tagName = target?.tagName?.toLowerCase();
  return (
    tagName === "input"
    || tagName === "textarea"
    || tagName === "select"
    || tagName === "button"
    || tagName === "a"
    || target?.isContentEditable
  );
}
