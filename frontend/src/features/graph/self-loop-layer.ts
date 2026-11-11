import type Sigma from 'sigma';
import type { GraphEdge, GraphResult } from '../../shared/model/types';
import { selectionContext, type GraphSelection } from './graph-model';

const SVG = 'http://www.w3.org/2000/svg';
type Point = { x: number; y: number };
type State = { data: GraphResult; visible: GraphResult; selection: GraphSelection; dark?: boolean; showEdgeLabels?: boolean; onSelectEdge: (edge: GraphEdge) => void };

/** Original cubic geometry. No synthetic graph nodes or semantic edges are added. */
export function loopGeometry(center: Point, radius: number, lane: number, count = lane + 1) {
  const angle = [-Math.PI * 0.7, Math.PI * 0.7, -Math.PI * 0.3, Math.PI * 0.3][lane % 4];
  const reach = radius + 56 + Math.floor(lane / 4) * Math.min(28, 80 / Math.max(1, Math.ceil(count / 4) - 1));
  const point = (distance: number, delta: number): Point => ({ x: center.x + Math.cos(angle + delta) * distance, y: center.y + Math.sin(angle + delta) * distance });
  const start = point(radius + 1, -0.7); const end = point(radius + 1, 0.7);
  const c1 = point(reach, -0.75); const c2 = point(reach, 0.75);
  const tangent = Math.atan2(end.y - c2.y, end.x - c2.x);
  const back = { x: end.x - Math.cos(tangent) * 8, y: end.y - Math.sin(tangent) * 8 };
  const arrow = [end, { x: back.x - Math.sin(tangent) * 3.5, y: back.y + Math.cos(tangent) * 3.5 }, { x: back.x + Math.sin(tangent) * 3.5, y: back.y - Math.cos(tangent) * 3.5 }];
  const midpoint = { x: (start.x + 3 * c1.x + 3 * c2.x + end.x) / 8, y: (start.y + 3 * c1.y + 3 * c2.y + end.y) / 8 };
  return { path: `M ${start.x} ${start.y} C ${c1.x} ${c1.y} ${c2.x} ${c2.y} ${end.x} ${end.y}`, arrow: arrow.map(p => `${p.x},${p.y}`).join(' '), midpoint, label: { x: midpoint.x + Math.cos(angle) * 12, y: midpoint.y + Math.sin(angle) * 12 } };
}

export function selfLoopLanes(edges: GraphEdge[]) {
  const groups = new Map<string, GraphEdge[]>(); const lanes = new Map<string, number>();
  for (const edge of edges) if (edge.source === edge.target) { const group = groups.get(edge.source) ?? []; group.push(edge); groups.set(edge.source, group); }
  for (const group of groups.values()) group.sort((a, b) => a.id.localeCompare(b.id)).forEach((edge, index) => lanes.set(edge.id, index));
  return lanes;
}

function element<K extends keyof SVGElementTagNameMap>(tag: K, attrs: Record<string, string> = {}) {
  const node = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  return node;
}

/** SVG is an interaction/geometry layer over Sigma's own normalized viewport. */
export function attachSelfLoopLayer(sigma: Sigma, host: HTMLElement, current: () => State) {
  const svg = element('svg', { class: 'graph-self-loops', 'aria-label': '环形关系', role: 'group' });
  const entries = new Map<string, { group: SVGGElement; hit: SVGPathElement; halo: SVGPathElement; line: SVGPathElement; arrow: SVGPolygonElement; text: SVGTextElement; title: SVGTitleElement; measuredWidth?: number }>();
  host.appendChild(svg);
  let disposed = false;
  let previousSize = '';
  const refresh = () => {
    if (disposed) return;
    const state = current(); const { width, height } = sigma.getDimensions();
    const size = `${width}:${height}`;
    if (size !== previousSize) { for (const entry of entries.values()) entry.measuredWidth = undefined; previousSize = size; }
    const labels: { entry: NonNullable<ReturnType<typeof entries.get>>; point: Point }[] = [];
    svg.setAttribute('viewBox', `0 0 ${Math.max(1, width)} ${Math.max(1, height)}`);
    svg.setAttribute('width', String(width)); svg.setAttribute('height', String(height));
    const lanes = selfLoopLanes(state.data.edges); const context = selectionContext(state.visible, state.selection);
    const counts = new Map<string, number>();
    for (const edge of state.data.edges) if (edge.source === edge.target) counts.set(edge.source, (counts.get(edge.source) ?? 0) + 1);
    const nodes = new Map(state.visible.nodes.map(n => [n.id, n]));
    const visible = new Map(state.visible.edges.filter(e => e.source === e.target && nodes.has(e.source)).map(e => [e.id, e]));
    for (const [id, entry] of entries) if (!visible.has(id)) { entry.group.remove(); entries.delete(id); }
    for (const [id, edge] of visible) {
      let entry = entries.get(id);
      if (!entry) {
        const group = element('g', { class: 'graph-self-loop', role: 'button', tabindex: '0', 'data-edge-id': id });
        const title = element('title'); const halo = element('path', { class: 'loop-halo', fill: 'none' });
        const line = element('path', { class: 'loop-line', fill: 'none' });
        const hit = element('path', { class: 'loop-hit', fill: 'none', stroke: 'transparent', 'stroke-width': '16' });
        const arrow = element('polygon', { class: 'loop-arrow' });
        const text = element('text', { class: 'loop-label', 'text-anchor': 'middle', 'dominant-baseline': 'central' });
        group.append(title, halo, line, arrow, text, hit); svg.appendChild(group);
        const choose = () => { const latest = current(); const match = latest.visible.edges.find(item => item.id === id && item.source === item.target); if (match) latest.onSelectEdge(match); };
        group.addEventListener('click', event => { event.stopPropagation(); choose(); });
        group.addEventListener('pointerdown', event => event.stopPropagation());
        group.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); event.stopPropagation(); choose(); } });
        entry = { group, hit, halo, line, arrow, text, title }; entries.set(id, entry);
      }
      const node = sigma.getNodeDisplayData(edge.source);
      if (!node || node.hidden) { entry.group.style.display = 'none'; continue; }
      const center = sigma.framedGraphToViewport(node); const radius = sigma.scaleSize(node.size);
      if (![center.x, center.y, radius].every(Number.isFinite) || center.x < 0 || center.x > width || center.y < 0 || center.y > height) { entry.group.style.display = 'none'; continue; }
      entry.group.style.display = '';
      const geometry = loopGeometry(center, radius, lanes.get(id) ?? 0, counts.get(edge.source));
      const related = context.edges.has(id); const selected = state.selection.edge === id;
      const color = state.dark ? context.active ? related ? '#9dd7b5' : '#354e42' : '#668878' : context.active ? related ? '#285d6c' : '#d6dfdf' : '#607d88';
      for (const path of [entry.line, entry.hit, entry.halo]) path.setAttribute('d', geometry.path);
      entry.line.setAttribute('stroke', color); entry.line.setAttribute('stroke-width', related ? '2.7' : '1.6');
      entry.arrow.setAttribute('points', geometry.arrow); entry.arrow.setAttribute('fill', color);
      entry.group.setAttribute('aria-pressed', String(selected));
      const label = `${nodes.get(edge.source)!.label} — ${edge.label} → ${nodes.get(edge.source)!.label}`;
      entry.group.setAttribute('aria-label', `环形关系：${label}`); entry.title.textContent = label;
      // Full names remain in the title, accessible name, inspector and relation list.
      const maxCharacters = Math.max(1, Math.min(20, Math.floor((width - 24) / 12)));
      const characters = Array.from(edge.label); const shortLabel = characters.length > maxCharacters ? characters.slice(0, maxCharacters - 1).join('') + '…' : edge.label;
      if (entry.text.textContent !== shortLabel) { entry.text.textContent = shortLabel; entry.measuredWidth = undefined; }
      labels.push({ entry, point: geometry.label });
      const showLabel = selected || (!context.active && !!state.showEdgeLabels) || (state.selection.node === edge.source && visible.size <= 8);
      entry.group.classList.toggle('show-label', showLabel);
    }
    // Batch layout reads after all geometry writes; selection/panning reuse unchanged widths.
    for (const { entry } of labels) if (entry.measuredWidth === undefined) entry.measuredWidth = typeof entry.text.getComputedTextLength === 'function' ? entry.text.getComputedTextLength() : Array.from(entry.text.textContent ?? '').length * 12;
    for (const { entry, point } of labels) {
      const measured = entry.measuredWidth!;
      entry.text.setAttribute('x', String(Math.max(measured / 2 + 12, Math.min(width - measured / 2 - 12, point.x))));
      entry.text.setAttribute('y', String(Math.max(12, Math.min(height - 12, point.y))));
    }
  };
  sigma.on('afterRender', refresh);
  return { refresh, destroy: () => { if (disposed) return; disposed = true; sigma.off('afterRender', refresh); svg.remove(); entries.clear(); } };
}
