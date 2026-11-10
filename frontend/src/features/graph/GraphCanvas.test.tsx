import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { GraphCanvas } from './GraphCanvas';
import { filterGraph, type Position } from './graph-model';
import type { GraphResult } from '../../shared/model/types';

const state = vi.hoisted(() => ({ workers: 0, sigma: undefined as any }));
vi.mock('@sigma/edge-curve', () => ({ EdgeCurvedArrowProgram: class {} }));
vi.mock('sigma', () => ({ default: class {
  graph: any; settings: any = {}; callbacks: Record<string, Function> = {};
  camera = { ratio: 1, on: vi.fn(), animate: vi.fn(), setState: vi.fn() };
  refresh = vi.fn(() => this.callbacks.afterRender?.()); kill = vi.fn();
  constructor(graph: any) { this.graph = graph; state.sigma = this; }
  getGraph() { return this.graph; } getCamera() { return this.camera; }
  getNodeDisplayData(id: string) { return this.graph.getNodeAttributes(id); }
  getDimensions() { return { width: 600, height: 400 }; }
  framedGraphToViewport(point: { x: number; y: number }) { return { x: 160 + point.x * 300, y: 160 + point.y * 200 }; }
  scaleSize(size: number) { return size; }
  setSettings(settings: unknown) { this.settings = settings; }
  on(event: string, callback: Function) { this.callbacks[event] = callback; }
  off(event: string, callback: Function) { if (this.callbacks[event] === callback) delete this.callbacks[event]; }
} }));
const data: GraphResult = { nodes: ['a', 'b', 'c'].map((id, i) => ({ id, label: id, type_key: i === 2 ? 'other' : 'asset', kind: 'object', properties: {} })), edges: [{ id: 'ab', source: 'a', target: 'b', label: '负责', kind: 'relation', type_key: 'owns' }], truncated: false };

beforeEach(() => {
  state.workers = 0; state.sigma = undefined;
  vi.stubGlobal('matchMedia', () => ({ matches: true, addEventListener() {}, removeEventListener() {} }));
  vi.stubGlobal('Worker', class { onmessage?: (event: unknown) => void; constructor() { state.workers++; } postMessage(input: { ids: string[] }) { queueMicrotask(() => this.onmessage?.({ data: input.ids.map((id, i) => ({ id, x: i / 3, y: i / 3 })) })); } terminate() {} });
});

it('refreshes selection and visible reducers without restarting layout and respects reduced motion controls', async () => {
  const cache = new Map<string, Position[]>(); const onSelect = vi.fn();
  const props = { data, visible: data, kind: 'instances' as const, selection: {}, cache, onSelect, onSelectEdge: vi.fn(), onClear: vi.fn() };
  const view = render(<GraphCanvas {...props} />);
  await waitFor(() => expect((screen.getByRole('button', { name: '适配可见图' }) as HTMLButtonElement).disabled).toBe(false));
  view.rerender(<GraphCanvas {...props} selection={{ node: 'a' }} />);
  const sigma = state.sigma;
  expect(sigma.settings.nodeReducer('a', { x: 7, y: 8 }).highlighted).toBe(true);
  expect(sigma.settings.nodeReducer('a', { x: 7, y: 8 }).x).toBe(7);
  expect(sigma.settings.nodeReducer('c', {}).label).toBe('');
  expect(sigma.settings.edgeReducer('ab', {}).label).toBe('负责');
  expect(state.workers).toBe(1); expect(sigma.refresh).toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '定位所选' }));
  expect(sigma.camera.setState).toHaveBeenCalled(); expect(sigma.camera.animate).not.toHaveBeenCalled();
  view.rerender(<GraphCanvas {...props} visible={filterGraph(data, 'instances', [], ['asset'], '')} />);
  expect(sigma.settings.nodeReducer('a', {}).hidden).toBe(true); expect(sigma.settings.edgeReducer('ab', {}).hidden).toBe(true);
  sigma.callbacks.clickNode({ node: 'a' }); expect(onSelect).not.toHaveBeenCalled();
  sigma.callbacks.clickNode({ node: 'c' }); expect(onSelect).toHaveBeenCalledWith(data.nodes[2]);
  expect(state.workers).toBe(1);
});

it('catches Worker constructor failure and exposes an honest list fallback', async () => {
  vi.stubGlobal('Worker', class { constructor() { throw new Error('CSP denied'); } });
  render(<GraphCanvas data={data} visible={data} kind="instances" selection={{}} cache={new Map()} onSelect={() => {}} onSelectEdge={() => {}} onClear={() => {}} />);
  await screen.findByText('浏览器未能启动图布局，请使用节点和关系列表。');
  expect(screen.getByText('下方节点与关系列表仍可使用。')).toBeTruthy();
  expect((screen.getByRole('button', { name: '放大图谱' }) as HTMLButtonElement).disabled).toBe(true);
});

it('renders parallel arrows separately and keeps their names and identity through selection', async () => {
  const parallel: GraphResult = { ...data, edges: [...data.edges, { ...data.edges[0], id: 'ab2', label: '使用' }, { ...data.edges[0], id: 'self', target: 'a', label: '自身' }] };
  const props = { data: parallel, visible: parallel, kind: 'instances' as const, selection: {}, cache: new Map<string, Position[]>(), onSelect: vi.fn(), onSelectEdge: vi.fn(), onClear: vi.fn() };
  const view = render(<GraphCanvas {...props} />);
  await waitFor(() => expect((screen.getByRole('button', { name: '适配可见图' }) as HTMLButtonElement).disabled).toBe(false));
  const sigma = state.sigma;
  const first = sigma.graph.getEdgeAttributes('ab'); const second = sigma.graph.getEdgeAttributes('ab2');
  expect(first.type).toBe('curved'); expect(second.type).toBe('curved'); expect(first.curvature).not.toBe(second.curvature);
  expect(sigma.graph.hasEdge('self')).toBe(false); // separate SVG geometry, no fake graph nodes
  const self = await screen.findByRole('button', { name: '环形关系：a — 自身 → a' });
  fireEvent.click(self); expect(props.onSelectEdge).toHaveBeenCalledWith(parallel.edges[2]);
  expect(sigma.settings.edgeReducer('ab', first).label).toBe('负责');
  expect(sigma.settings.edgeReducer('ab2', second).label).toBe('使用');
  view.rerender(<GraphCanvas {...props} selection={{ edge: 'ab2' }} />);
  expect(sigma.settings.edgeReducer('ab', first).label).toBe('');
  expect(sigma.settings.edgeReducer('ab2', second).curvature).toBe(second.curvature);
  sigma.callbacks.clickEdge({ edge: 'ab2' }); expect(props.onSelectEdge).toHaveBeenCalledWith(parallel.edges[1]);
  expect(state.workers).toBe(1);
  view.rerender(<GraphCanvas {...props} visible={{ ...parallel, nodes: parallel.nodes.slice(1), edges: [] }} />);
  expect(screen.queryByRole('button', { name: '环形关系：a — 自身 → a' })).toBeNull();
  view.unmount(); expect(sigma.callbacks.afterRender).toBeUndefined();
});

it('discards a terminated topology worker response and handles runtime failure without a false ready canvas', async () => {
  const workers: any[] = [];
  vi.stubGlobal('Worker', class { onmessage?: Function; onerror?: Function; terminate = vi.fn(); constructor() { workers.push(this); } postMessage() {} });
  const props = { data, visible: data, kind: 'instances' as const, selection: {}, cache: new Map<string, Position[]>(), onSelect: vi.fn(), onSelectEdge: vi.fn(), onClear: vi.fn() };
  const view = render(<GraphCanvas {...props} />);
  const changed = { ...data, nodes: data.nodes.slice(0, 2) };
  view.rerender(<GraphCanvas {...props} data={changed} visible={changed} />);
  expect(workers[0].terminate).toHaveBeenCalled();
  act(() => workers[0].onmessage({ data: data.nodes.map((node, i) => ({ id: node.id, x: i, y: i })) }));
  expect(state.sigma).toBeUndefined(); expect(props.cache.size).toBe(0);
  act(() => workers[1].onerror());
  await screen.findByText('图布局未能完成，请使用节点和关系列表。');
  expect((screen.getByRole('button', { name: '适配可见图' }) as HTMLButtonElement).disabled).toBe(true);
});
