/* Pure deterministic layout for the workflow editor's node document. */
(function (root) {
  'use strict';

  const WIDTH = 170;
  const HEIGHT = 64;
  const STEP_X = 230;
  const STEP_Y = 110;
  const COLUMNS = Math.floor((3000 - WIDTH) / STEP_X) + 1;
  const ROWS = Math.floor((3000 - HEIGHT) / STEP_Y) + 1;

  function invalid(message) {
    throw new Error('画布布局失败：' + message);
  }

  function layout(document) {
    if (!document || !Array.isArray(document.nodes) || !Array.isArray(document.edges)) {
      invalid('流程节点或连接格式无效');
    }
    const nodes = document.nodes;
    if (!nodes.length) invalid('空流程无法布局');
    if (nodes.length > 200) invalid('最多支持200个节点');

    const byId = new Map();
    for (const node of nodes) {
      if (!node || typeof node.id !== 'string' || !node.id) invalid('节点标识无效');
      if (byId.has(node.id)) invalid('存在重复节点标识：' + node.id);
      byId.set(node.id, node);
    }

    const incoming = new Map(nodes.map((node) => [node.id, 0]));
    const outgoing = new Map(nodes.map((node) => [node.id, []]));
    for (const edge of document.edges) {
      if (!edge || !byId.has(edge.source) || !byId.has(edge.target)) {
        invalid('连接引用了不存在的节点');
      }
      outgoing.get(edge.source).push(edge.target);
      incoming.set(edge.target, incoming.get(edge.target) + 1);
    }

    // Kahn's algorithm also computes the longest predecessor path as layer.
    const ready = nodes.filter((node) => incoming.get(node.id) === 0).map((node) => node.id);
    const layers = new Map(nodes.map((node) => [node.id, 0]));
    const ordered = [];
    for (let cursor = 0; cursor < ready.length; cursor += 1) {
      const id = ready[cursor];
      ordered.push(id);
      for (const next of outgoing.get(id)) {
        layers.set(next, Math.max(layers.get(next), layers.get(id) + 1));
        incoming.set(next, incoming.get(next) - 1);
        if (incoming.get(next) === 0) ready.push(next);
      }
    }
    if (ordered.length !== nodes.length) invalid('流程连接存在循环');

    const grouped = new Map();
    for (const id of ordered) {
      const layer = layers.get(id);
      if (!grouped.has(layer)) grouped.set(layer, []);
      grouped.get(layer).push(id);
    }

    // Pack layer-preferred cells into a bounded grid. This keeps ordinary DAGs
    // left-to-right while folding deep chains and broad levels back into rows.
    const occupied = new Set();
    const positions = new Map();
    const cellCount = COLUMNS * ROWS;
    for (const [layer, ids] of grouped) {
      for (let index = 0; index < ids.length; index += 1) {
        const preferredColumn = layer % COLUMNS;
        const preferredRow = Math.floor(layer / COLUMNS) + index;
        let best = -1;
        let bestDistance = Infinity;
        for (let cell = 0; cell < cellCount; cell += 1) {
          if (occupied.has(cell)) continue;
          const column = cell % COLUMNS;
          const row = Math.floor(cell / COLUMNS);
          const distance = Math.abs(column - preferredColumn) + Math.abs(row - preferredRow);
          if (distance < bestDistance) {
            best = cell;
            bestDistance = distance;
            if (distance === 0) break;
          }
        }
        if (best < 0) invalid('画布空间不足');
        occupied.add(best);
        positions.set(ids[index], { x: (best % COLUMNS) * STEP_X,
          y: Math.floor(best / COLUMNS) * STEP_Y });
      }
    }

    return Object.assign({}, document, {
      nodes: nodes.map((node) => Object.assign({}, node, {
        position: positions.get(node.id),
      })),
    });
  }

  const api = { layout };
  root.AgentLineLayout = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
