import { fireEvent } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import type Sigma from 'sigma';
import { attachSelfLoopLayer, loopGeometry, selfLoopLanes } from './self-loop-layer';
import type { GraphEdge, GraphResult } from '../../shared/model/types';

const edges: GraphEdge[] = ['self-a', 'self-b', 'self-c', 'self-d', 'self-e'].map((id, index) => ({ id, source: 'node', target: 'node', label: `自定义关系 ${index}`, type_key: id, kind: 'relation' }));
const data: GraphResult = { nodes: [{ id: 'node', label: '业务节点', type_key: 'task', kind: 'object', properties: {} }], edges, truncated: false };

it('assigns stable distinct loops with a directional arrow and bounded radial growth', () => {
  expect(selfLoopLanes(edges)).toEqual(selfLoopLanes([...edges].reverse()));
  const paths = edges.map((_, i) => loopGeometry({ x: 150, y: 150 }, 10, i, edges.length));
  expect(new Set(paths.map(p => p.path)).size).toBe(5);
  for (const shape of paths) { expect(shape.path).not.toContain('NaN'); expect(shape.arrow.split(' ')).toHaveLength(3); }
  const crowded = loopGeometry({ x: 150, y: 150 }, 10, 999, 1000);
  expect(Math.hypot(crowded.midpoint.x - 150, crowded.midpoint.y - 150)).toBeLessThan(160);
});

it('keeps exact relation identity across selection, zoom, filters and response ordering; removes subscriptions', () => {
  const host = document.createElement('div'); document.body.append(host);
  let offset = 0; const callbacks = new Map<string, () => void>();
  const sigma = { getDimensions: () => ({ width: 360, height: 280 }), getNodeDisplayData: () => ({ x: .5, y: .5, size: 10 }), framedGraphToViewport: () => ({ x: 150 + offset, y: 150 }), scaleSize: (size: number) => size, on: (event: string, callback: () => void) => callbacks.set(event, callback), off: (event: string) => callbacks.delete(event) };
  const select = vi.fn(); let current = { data, visible: data, selection: {} as { edge?: string }, onSelectEdge: select };
  const layer = attachSelfLoopLayer(sigma as unknown as Sigma, host, () => current); layer.refresh();
  const group = host.querySelector('[data-edge-id="self-b"]')!;
  const path = group.querySelector('.loop-line')!.getAttribute('d');
  fireEvent.keyDown(group, { key: 'Enter' }); expect(select).toHaveBeenLastCalledWith(edges[1]);
  fireEvent.keyDown(group, { key: ' ' }); expect(select).toHaveBeenLastCalledWith(edges[1]);
  current = { ...current, selection: { edge: 'self-b' }, data: { ...data, edges: [...edges].reverse() }, visible: { ...data, edges: [edges[1]] } };
  layer.refresh(); expect(host.querySelectorAll('[role="button"]')).toHaveLength(1);
  expect(group.getAttribute('aria-pressed')).toBe('true'); expect(group.querySelector('.loop-line')!.getAttribute('d')).toBe(path);
  offset = 30; callbacks.get('afterRender')!(); expect(group.querySelector('.loop-line')!.getAttribute('d')).not.toBe(path);
  current = { ...current, visible: { ...data, nodes: [], edges: [] } }; layer.refresh();
  expect(host.querySelectorAll('[role="button"]')).toHaveLength(0);
  fireEvent.click(group); expect(select).toHaveBeenCalledTimes(2); // stale event cannot select a filtered edge
  layer.destroy(); expect(callbacks.size).toBe(0); expect(host.children).toHaveLength(0); host.remove();
});

it('reuses label widths during camera movement and remeasures changed labels or viewport sizes', () => {
  const host = document.createElement('div'); document.body.append(host);
  let width = 360; let offset = 0;
  const sigma = { getDimensions: () => ({ width, height: 280 }), getNodeDisplayData: () => ({ x: .5, y: .5, size: 10 }), framedGraphToViewport: () => ({ x: 150 + offset, y: 150 }), scaleSize: (size: number) => size, on: vi.fn(), off: vi.fn() };
  let current = { data, visible: data, selection: {}, onSelectEdge: vi.fn() };
  const layer = attachSelfLoopLayer(sigma as unknown as Sigma, host, () => current); layer.refresh();
  const text = host.querySelector('.loop-label')!;
  const measure = vi.fn(() => 96); Object.defineProperty(text, 'getComputedTextLength', { value: measure });
  width = 320; layer.refresh(); expect(measure).toHaveBeenCalledTimes(1);
  const x = text.getAttribute('x'); offset = 20; layer.refresh();
  expect(measure).toHaveBeenCalledTimes(1); expect(text.getAttribute('x')).not.toBe(x);
  const changed = { ...data, edges: [{ ...edges[0], label: '已调整的关系名' }, ...edges.slice(1)] };
  current = { ...current, data: changed, visible: changed }; layer.refresh();
  expect(measure).toHaveBeenCalledTimes(2); expect(text.textContent).toBe('已调整的关系名');
  width = 280; layer.refresh(); expect(measure).toHaveBeenCalledTimes(3);
  layer.destroy(); host.remove();
});


it('keeps selected self-loops legible against the dark graph canvas', () => {
  const host = document.createElement('div'); document.body.append(host);
  const sigma = { getDimensions: () => ({ width: 360, height: 280 }), getNodeDisplayData: () => ({ x: .5, y: .5, size: 10 }), framedGraphToViewport: () => ({ x: 150, y: 150 }), scaleSize: (size: number) => size, on: vi.fn(), off: vi.fn() };
  const layer = attachSelfLoopLayer(sigma as unknown as Sigma, host, () => ({ data, visible: data, dark: true, selection: { edge: edges[0].id }, onSelectEdge: vi.fn() }));
  layer.refresh();
  const selected = host.querySelector('[data-edge-id="self-a"]')!;
  expect(selected.querySelector('.loop-line')!.getAttribute('stroke')).toBe('#9dd7b5');
  expect(selected.querySelector('.loop-arrow')!.getAttribute('fill')).toBe('#9dd7b5');
  layer.destroy(); host.remove();
});
