import { expect, it } from 'vitest';
import { boundedGraph, edgeLanes, filterGraph, groupColor, layoutGraph, selectionContext, topologyKey } from './graph-model';
import type { GraphResult } from '../../shared/model/types';

const data: GraphResult = {
  nodes: [{ id: 'a', label: '星图', type_key: 'project', type_label: '项目', kind: 'object', properties: {} }, { id: 'b', label: '星图', type_key: 'system', type_label: '系统', kind: 'object', properties: {} }, { id: 'c', label: '离线', type_key: 'system', kind: 'object', properties: {} }],
  edges: [{ id: 'ab', source: 'a', target: 'b', label: '使用', type_key: 'uses', kind: 'relation' }, { id: 'ab2', source: 'a', target: 'b', label: '负责', type_key: 'owns', kind: 'relation' }, { id: 'aa', source: 'a', target: 'a', label: '关联自身', type_key: 'self', kind: 'relation' }], truncated: false,
};

it('keeps separate physical lanes for forward and reverse relations, stable under response reordering', () => {
  const edges = [...data.edges, { ...data.edges[0], id: 'ba', source: 'b', target: 'a' }];
  const lanes = edgeLanes(edges);
  expect(lanes).toEqual(edgeLanes([...edges].reverse()));
  expect(lanes.has('aa')).toBe(false); // self references use the cubic SVG layer
  const physicalOffsets = edges.filter(e => e.source !== e.target).map(e => lanes.get(e.id)!.curvature * (e.source < e.target ? 1 : -1));
  expect(new Set(physicalOffsets).size).toBe(3);
  expect(Math.min(...physicalOffsets)).toBeLessThan(0); expect(Math.max(...physicalOffsets)).toBeGreaterThan(0);
  expect(edgeLanes([data.edges[0]]).get('ab')).toEqual({ type: 'arrow', curvature: 0 });
  expect(edges).toHaveLength(4);
});

it('filters the actual induced graph and retains parallel/self edges instead of collapsing business relations', () => {
  const search = filterGraph(data, 'instances', [], [], '星图');
  expect(search.nodes.map(n => n.id)).toEqual(['a', 'b']); expect(search.edges).toHaveLength(3);
  const hidden = filterGraph(data, 'instances', [], ['system'], '');
  expect(hidden.nodes.map(n => n.id)).toEqual(['a']); expect(hidden.edges.map(e => e.id)).toEqual(['aa']);
  expect(filterGraph(data, 'instances', [], [], '项目').nodes.map(n => n.id)).toEqual(['a']);
  expect(groupColor('project')).toBe(groupColor('project'));
});

it('guards removed selections and distinguishes a selected relation from other parallel edges', () => {
  expect([...selectionContext(data, { node: 'a' }).edges]).toEqual(['ab', 'ab2', 'aa']);
  expect([...selectionContext(data, { edge: 'ab2' }).edges]).toEqual(['ab2']);
  expect(selectionContext(data, { node: 'removed' }).active).toBe(false);
  const filtered = filterGraph(data, 'instances', [], ['system'], '');
  expect(selectionContext(filtered, { edge: 'ab' }).active).toBe(false);
});

it('uses real edges for deterministic finite coordinates; selection/labels are not layout dependencies', () => {
  const input = { ids: ['c', 'a', 'b'], edges: [{ source: 'a', target: 'b' }] };
  const points = layoutGraph(input);
  expect(points).toEqual(layoutGraph({ ...input, ids: [...input.ids].reverse() }));
  expect(points).toEqual(layoutGraph({ ...input, edges: [...input.edges, { source: 'b', target: 'a' }, ...input.edges] }));
  expect(points.every(p => Number.isFinite(p.x) && Number.isFinite(p.y))).toBe(true);
  expect(points).not.toEqual(layoutGraph({ ...input, edges: [] }));
  expect(topologyKey(data)).toBe(topologyKey({ ...data, nodes: [...data.nodes].reverse().map(n => ({ ...n, label: '新文字' })) }));
  expect(layoutGraph({ ids: ['one'], edges: [] })).toEqual([{ id: 'one', x: 0, y: 0 }]);
});

it('bounds visible work without adding dangling edges or claiming complete counts', () => {
  const nodes = Array.from({ length: 501 }, (_, i) => ({ ...data.nodes[0], id: String(i) }));
  const limited = boundedGraph({ ...data, nodes, edges: [{ ...data.edges[0], source: '0', target: '500' }] });
  expect(limited.nodes).toHaveLength(500); expect(limited.edges).toEqual([]); expect(limited.truncated).toBe(true);
});

it('keeps a linked pair legible among many isolated records without merging or losing nodes', () => {
  const ids = ['linked-a', 'linked-b', ...Array.from({ length: 26 }, (_, i) => `isolated-${i}`)];
  const input = { ids, edges: [{ source: 'linked-a', target: 'linked-b' }] };
  const points = layoutGraph(input); const byId = new Map(points.map(point => [point.id, point]));
  const a = byId.get('linked-a')!; const b = byId.get('linked-b')!;
  const span = Math.max(Math.max(...points.map(p => p.x)) - Math.min(...points.map(p => p.x)), Math.max(...points.map(p => p.y)) - Math.min(...points.map(p => p.y)));
  expect(Math.hypot(a.x - b.x, a.y - b.y) / span).toBeGreaterThan(0.15);
  expect(new Set(points.map(p => p.id)).size).toBe(28);
  expect(points).toEqual(layoutGraph({ ids: [...ids].reverse(), edges: input.edges }));
  for (const p of points.filter(p => p.id.startsWith('isolated'))) {
    expect(p.x < Math.min(a.x, b.x) || p.x > Math.max(a.x, b.x) || p.y < Math.min(a.y, b.y) || p.y > Math.max(a.y, b.y)).toBe(true);
  }
});

it('handles a fully disconnected bounded graph with distinct finite coordinates', () => {
  const points = layoutGraph({ ids: Array.from({ length: 500 }, (_, i) => `object-${i}`), edges: [] });
  expect(points).toHaveLength(500);
  expect(new Set(points.map(p => `${p.x}:${p.y}`)).size).toBe(500);
  expect(points.every(p => Number.isFinite(p.x) && Number.isFinite(p.y))).toBe(true);
});
