import type { GraphEdge, GraphKind, GraphNode, GraphResult, TypeDef } from '../../shared/model/types';

export interface GraphSelection { node?: string; edge?: string }
const palette = ['#356eae', '#b7652c', '#7b58a1', '#27836d', '#b44865', '#69832f', '#307f98', '#986746', '#655bab', '#a37722', '#358262', '#a14a48'];
export const groupKey = (node: GraphNode, kind: GraphKind) => kind === 'knowledge' ? node.kind : node.type_key || node.kind;
export function groupColor(key: string) {
  let hash = 2166136261;
  for (const char of key) hash = Math.imul(hash ^ char.charCodeAt(0), 16777619);
  return palette[(hash >>> 0) % palette.length];
}
export const groupLabel = (node: GraphNode, kind: GraphKind, types: TypeDef[]) => kind === 'knowledge' ? ({ knowledge: '知识页', document: '文档' }[node.kind] ?? node.kind) : node.type_label || types.find(t => t.key === node.type_key)?.label || node.type_key;
export function boundedGraph(data: GraphResult): GraphResult {
  const nodes = data.nodes.slice(0, 500); const ids = new Set(nodes.map(n => n.id));
  return { ...data, nodes, edges: data.edges.filter(e => ids.has(e.source) && ids.has(e.target)).slice(0, 1000), truncated: data.truncated || data.nodes.length > 500 || data.edges.length > 1000 };
}
export function filterGraph(data: GraphResult, kind: GraphKind, types: TypeDef[], hiddenTypes: string[], search: string): GraphResult {
  const query = search.trim().toLocaleLowerCase();
  const nodes = data.nodes.filter(node => !hiddenTypes.includes(groupKey(node, kind)) && (!query || `${node.label} ${groupLabel(node, kind, types)} ${node.id}`.toLocaleLowerCase().includes(query)));
  const ids = new Set(nodes.map(n => n.id));
  return { ...data, nodes, edges: data.edges.filter(edge => ids.has(edge.source) && ids.has(edge.target)) };
}
export function selectionContext(data: GraphResult, selection: GraphSelection) {
  const ids = new Set(data.nodes.map(node => node.id)); const nodes = new Set<string>(); const edges = new Set<string>();
  if (selection.node && ids.has(selection.node)) {
    nodes.add(selection.node);
    for (const edge of data.edges) if (edge.source === selection.node || edge.target === selection.node) { nodes.add(edge.source); nodes.add(edge.target); edges.add(edge.id); }
  } else if (selection.edge) {
    const edge = data.edges.find(e => e.id === selection.edge);
    if (edge && ids.has(edge.source) && ids.has(edge.target)) { nodes.add(edge.source); nodes.add(edge.target); edges.add(edge.id); }
  }
  return { nodes, edges, active: nodes.size > 0 };
}
export function relationDefinition(edge: GraphEdge, data: GraphResult, types: TypeDef[]) {
  const source = data.nodes.find(n => n.id === edge.source);
  return types.find(t => t.key === source?.type_key)?.relations.find(r => r.key === edge.type_key);
}
export const topologyKey = (data: GraphResult) => JSON.stringify([data.nodes.map(n => n.id).sort(), data.edges.map(e => [e.id, e.source, e.target]).sort((a, b) => a[0].localeCompare(b[0]))]);

export interface EdgeLane { type: 'arrow' | 'curved'; curvature: number }
/** Stable visual lanes only: relation identities and directions are never merged. */
export function edgeLanes(edges: GraphEdge[]): Map<string, EdgeLane> {
  const pairs = new Map<string, GraphEdge[]>();
  for (const edge of edges) {
    // Coincident endpoints use the separate cubic SVG layer, not a quadratic edge.
    if (edge.source === edge.target) continue;
    const pair = JSON.stringify([edge.source, edge.target].sort());
    const group = pairs.get(pair) ?? []; group.push(edge); pairs.set(pair, group);
  }
  const lanes = new Map<string, EdgeLane>();
  for (const group of pairs.values()) {
    group.sort((a, b) => a.id.localeCompare(b.id));
    const step = Math.min(0.5, 1.4 / Math.max(1, group.length - 1));
    group.forEach((edge, index) => {
      // Reversing an arrow also reverses its normal; compensate to keep physical lanes distinct.
      const curvature = (index - (group.length - 1) / 2) * step * (edge.source < edge.target ? 1 : -1);
      lanes.set(edge.id, { type: curvature === 0 ? 'arrow' : 'curved', curvature });
    });
  }
  return lanes;
}

export interface LayoutInput { ids: string[]; edges: { source: string; target: string }[]; positions?: Position[] }
export interface Position { id: string; x: number; y: number }
/** Independent bounded spring/repulsion layout. Edges affect placement, never business semantics. */
export function layoutGraph(input: LayoutInput): Position[] {
  // Existing coordinates are anchors; expanding a neighborhood only relaxes
  // newly added nodes. Initial disconnected graphs retain component packing.
  if (input.positions?.length) return springLayout(input);
  const ids = [...new Set(input.ids)].sort().slice(0, 500);
  const adjacency = new Map(ids.map(id => [id, new Set<string>()]));
  const edges = input.edges.slice(0, 1000).filter(edge => adjacency.has(edge.source) && adjacency.has(edge.target));
  for (const edge of edges) { adjacency.get(edge.source)!.add(edge.target); adjacency.get(edge.target)!.add(edge.source); }
  const unseen = new Set(ids); const components: string[][] = [];
  for (const id of ids) {
    if (!unseen.delete(id)) continue;
    const members = [id];
    for (let index = 0; index < members.length; index++) for (const next of adjacency.get(members[index])!) if (unseen.delete(next)) members.push(next);
    components.push(members.sort());
  }
  if (components.length <= 1) return springLayout({ ids, edges });
  // Solve each connected component independently: isolated records cannot shrink
  // a useful linked pair to a coincident dot in the normalized global viewport.
  const blocks = components.map(members => {
    const included = new Set(members);
    const points = springLayout({ ids: members, edges: edges.filter(edge => included.has(edge.source)) });
    const minX = Math.min(...points.map(p => p.x)); const maxX = Math.max(...points.map(p => p.x));
    const minY = Math.min(...points.map(p => p.y)); const maxY = Math.max(...points.map(p => p.y));
    const scale = members.length === 1 ? 1 : Math.sqrt(members.length) * 1.8 / Math.max(maxX - minX, maxY - minY, 0.01);
    const width = Math.max(1, (maxX - minX) * scale); const height = Math.max(1, (maxY - minY) * scale);
    return { id: members[0], points, width, height, scale, centerX: (minX + maxX) / 2, centerY: (minY + maxY) / 2 };
  }).sort((a, b) => b.height - a.height || a.id.localeCompare(b.id));
  const gap = 0.8;
  const rowWidth = Math.max(...blocks.map(block => block.width), Math.sqrt(blocks.reduce((sum, block) => sum + (block.width + gap) * (block.height + gap), 0)) * 1.25);
  let x = 0; let y = 0; let rowHeight = 0;
  const result: Position[] = [];
  for (const block of blocks) {
    if (x > 0 && x + block.width > rowWidth) { x = 0; y += rowHeight + gap; rowHeight = 0; }
    result.push(...block.points.map(p => ({ id: p.id, x: x + block.width / 2 + (p.x - block.centerX) * block.scale, y: y + block.height / 2 + (p.y - block.centerY) * block.scale })));
    x += block.width + gap; rowHeight = Math.max(rowHeight, block.height);
  }
  return result.sort((a, b) => a.id.localeCompare(b.id));
}

function springLayout(input: LayoutInput): Position[] {
  const ids = [...new Set(input.ids)].sort().slice(0, 500); const n = ids.length;
  const retained = new Map((input.positions ?? []).map(point => [point.id, point]));
  if (!n) return []; if (n === 1) return [retained.get(ids[0]) ?? { id: ids[0], x: 0, y: 0 }];
  const index = new Map(ids.map((id, i) => [id, i]));
  const points = ids.map((id, i) => {
    if (retained.has(id)) return { ...retained.get(id)! };
    const anchors = input.edges.filter(edge => edge.source === id || edge.target === id).map(edge => retained.get(edge.source === id ? edge.target : edge.source)).filter((point): point is Position => !!point);
    const angle = i * Math.PI * (3 - Math.sqrt(5)); const radius = anchors.length ? 0.2 : 1.5 * Math.sqrt((i + 1) / n);
    return { id, x: (anchors.length ? anchors.reduce((sum, point) => sum + point.x, 0) / anchors.length : 0) + Math.cos(angle) * radius, y: (anchors.length ? anchors.reduce((sum, point) => sum + point.y, 0) / anchors.length : 0) + Math.sin(angle) * radius };
  });
  const pairs = new Set<string>();
  const edges = input.edges.slice(0, 1000).map(e => [index.get(e.source), index.get(e.target)]).filter((e): e is [number, number] => {
    if (e[0] === undefined || e[1] === undefined || e[0] === e[1]) return false;
    // More relation types do not imply stronger attraction or business importance.
    const pair = [e[0], e[1]].sort((a, b) => a - b).join(':');
    if (pairs.has(pair)) return false; pairs.add(pair); return true;
  });
  const k = Math.sqrt(6 / n); const steps = Math.max(40, Math.min(140, Math.floor(20000 / n)));
  for (let step = 0; step < steps; step++) {
    const force = points.map(p => ({ x: -p.x * 0.05, y: -p.y * 0.05 }));
    for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) {
      const dx = points[i].x - points[j].x; const dy = points[i].y - points[j].y;
      const dist = Math.max(0.01, Math.hypot(dx, dy)); const power = k * k / (dist * dist);
      force[i].x += dx * power; force[i].y += dy * power; force[j].x -= dx * power; force[j].y -= dy * power;
    }
    for (const [i, j] of edges) {
      const dx = points[i].x - points[j].x; const dy = points[i].y - points[j].y; const power = Math.hypot(dx, dy) / k;
      force[i].x -= dx * power; force[i].y -= dy * power; force[j].x += dx * power; force[j].y += dy * power;
    }
    const temperature = 0.16 * (1 - step / steps) + 0.003;
    for (let i = 0; i < n; i++) { if (retained.has(points[i].id)) continue; const length = Math.hypot(force[i].x, force[i].y); const factor = length ? Math.min(length, temperature) / length : 0; points[i].x += force[i].x * factor; points[i].y += force[i].y * factor; }
  }
  return points;
}
