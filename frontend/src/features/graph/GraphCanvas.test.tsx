import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { GraphCanvas } from './GraphCanvas';
import { filterGraph, type Position } from './graph-model';
import type { GraphResult } from '../../shared/model/types';

const state = vi.hoisted(() => ({ workers: 0, connectedOnKill: false, sigma: undefined as any }));
vi.mock('@sigma/edge-curve', () => ({ EdgeCurvedArrowProgram: class {} }));
vi.mock('sigma', () => ({ default: class {
  container: HTMLElement; graph: any; settings: any = {}; callbacks: Record<string, Function> = {};
  camera = { ratio: 1, on: vi.fn(), animate: vi.fn(), setState: vi.fn() };
  refresh = vi.fn(() => this.callbacks.afterRender?.()); kill = vi.fn(() => { state.connectedOnKill = this.container.isConnected; });
  constructor(graph: any, container: HTMLElement) { this.container = container; this.graph = graph; state.sigma = this; }
  getMouseCaptor() { return { on: (event: string, callback: Function) => { this.callbacks[event] = callback; } }; }
  getTouchCaptor() { return { on: (event: string, callback: Function) => { this.callbacks[event] = callback; } }; }
  graphToViewport(point: { x: number; y: number }) { return point; }
  getBBox() { return { x: [0, 1], y: [0, 1] }; } getCustomBBox() { return null; } setCustomBBox() {}
  viewportToGraph(point: { x: number; y: number }) { return point; }
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

it('preserves the renderer and dragged anchors on expansion and restores coordinates for the same nodes', async () => {
  const positions = new Map<string, Position>();
  const props = { data, visible: data, kind: 'instances' as const, selection: {}, positions, cache: new Map<string, Position[]>(), onSelect: vi.fn(), onSelectEdge: vi.fn(), onClear: vi.fn() };
  const view = render(<GraphCanvas {...props} />);
  await waitFor(() => expect(state.sigma?.graph.order).toBe(3));
  const sigma = state.sigma;
  act(() => { sigma.callbacks.downNode({ node: 'a' }); sigma.callbacks.mousemovebody({ x: 7, y: 8, preventSigmaDefault() {}, original: { preventDefault() {}, stopPropagation() {} } }); sigma.callbacks.mouseup(); });
  const expanded = { ...data, nodes: [...data.nodes, { ...data.nodes[0], id: 'd' }], edges: [...data.edges, { ...data.edges[0], id: 'ad', target: 'd' }] };
  view.rerender(<GraphCanvas {...props} data={expanded} visible={expanded} />);
  await waitFor(() => expect(sigma.graph.order).toBe(4));
  expect(state.sigma).toBe(sigma); expect(sigma.kill).not.toHaveBeenCalled();
  expect(sigma.graph.getNodeAttribute('a', 'x')).toBe(7);
  const restored = new Map(positions); restored.set('a', { id: 'a', x: 11, y: 12 });
  view.rerender(<GraphCanvas {...props} data={expanded} visible={expanded} positions={restored} />);
  await waitFor(() => expect(sigma.graph.getNodeAttribute('a', 'x')).toBe(11));
  expect(state.sigma).toBe(sigma);
  view.unmount(); expect(sigma.kill).toHaveBeenCalledOnce(); expect(state.connectedOnKill).toBe(true);
});

it('renders parallel arrows separately and keeps their names and identity through selection', async () => {
  const parallel: GraphResult = { ...data, edges: [...data.edges, { ...data.edges[0], id: 'ab2', label: '使用' }, { ...data.edges[0], id: 'self', target: 'a', label: '自身' }] };
  const props = { data: parallel, visible: parallel, kind: 'instances' as const, selection: {}, cache: new Map<string, Position[]>(), onSelect: vi.fn(), onSelectEdge: vi.fn(), onClear: vi.fn() };
  const view = render(<GraphCanvas {...props} />);
  await waitFor(() => expect((screen.getByRole('button', { name: '适配可见图' }) as HTMLButtonElement).disabled).toBe(false));
  const sigma = state.sigma;
  fireEvent.click(screen.getByRole('button', { name: '关系名称' }));
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


it('distinguishes click jitter from a drag and supports one-finger movement without moving pinned nodes', async () => {
  const positions = new Map<string, Position>(); const onSelect = vi.fn(); const onOpen = vi.fn();
  const props = { onOpen, data, visible: data, kind: 'instances' as const, selection: {}, positions, cache: new Map<string, Position[]>(), onSelect, onSelectEdge: vi.fn(), onClear: vi.fn() };
  const view = render(<GraphCanvas {...props} />);
  await waitFor(() => expect(state.sigma?.graph.order).toBe(3));
  const sigma = state.sigma;
  const event = (x: number, y: number) => ({ x, y, preventSigmaDefault: vi.fn(), original: { preventDefault() {}, stopPropagation() {} } });
  const before = { ...positions.get('a')! };
  act(() => { sigma.callbacks.downNode({ node: 'a', event: event(10, 10) }); sigma.callbacks.mousemovebody(event(11, 11)); sigma.callbacks.mouseup(); sigma.callbacks.clickNode({ node: 'a' }); });
  expect(positions.get('a')).toEqual(before); expect(onSelect).toHaveBeenCalledOnce();
  const touch = { touches: [{ x: 40, y: 50 }], ...event(40, 50) };
  act(() => { sigma.callbacks.downNode({ node: 'a', event: event(10, 10) }); sigma.callbacks.touchmove(touch); sigma.callbacks.touchup(); });
  expect(positions.get('a')).toEqual({ id: 'a', x: 40, y: 50 }); expect(touch.preventSigmaDefault).toHaveBeenCalled();
  view.rerender(<GraphCanvas {...props} pins={['a']} />);
  act(() => { sigma.callbacks.downNode({ node: 'a', event: event(40, 50) }); sigma.callbacks.mousemovebody(event(80, 90)); sigma.callbacks.mouseup(); });
  expect(positions.get('a')).toEqual({ id: 'a', x: 40, y: 50 });
  act(() => sigma.callbacks.doubleClickNode({ node: 'a', preventSigmaDefault() {} }));
  expect(onOpen).not.toHaveBeenCalled();
  view.rerender(<GraphCanvas {...props} />);
  act(() => { sigma.callbacks.downNode({ node: 'a', event: event(40, 50) }); sigma.callbacks.touchmove({ ...touch, touches: [{ x: 60, y: 60 }, { x: 80, y: 80 }] }); sigma.callbacks.touchmove(touch); });
  expect(positions.get('a')).toEqual({ id: 'a', x: 40, y: 50 });
});

it('opens only visible unlocked nodes or edges on double click and prevents camera zoom', async () => {
  const onOpen = vi.fn(); const onOpenEdge = vi.fn(); const preventSigmaDefault = vi.fn();
  const props = { data, visible: data, kind: 'instances' as const, selection: {}, cache: new Map<string, Position[]>(), onSelect: vi.fn(), onSelectEdge: vi.fn(), onClear: vi.fn(), onOpen, onOpenEdge };
  const view = render(<GraphCanvas {...props} />);
  await waitFor(() => expect(state.sigma?.graph.order).toBe(3));
  const sigma = state.sigma;
  act(() => { sigma.callbacks.clickNode({ node: 'a' }); sigma.callbacks.doubleClickNode({ node: 'a', preventSigmaDefault }); sigma.callbacks.clickEdge({ edge: 'ab' }); sigma.callbacks.doubleClickEdge({ edge: 'ab', preventSigmaDefault }); });
  expect(onOpen).toHaveBeenCalledWith(data.nodes[0]); expect(onOpenEdge).toHaveBeenCalledWith(data.edges[0]); expect(preventSigmaDefault).toHaveBeenCalledTimes(2);
  view.rerender(<GraphCanvas {...props} disabled />);
  act(() => sigma.callbacks.doubleClickNode({ node: 'a', preventSigmaDefault }));
  view.rerender(<GraphCanvas {...props} visible={{ ...data, nodes: [], edges: [] }} />);
  act(() => { sigma.callbacks.clickNode({ node: 'a' }); sigma.callbacks.doubleClickNode({ node: 'a', preventSigmaDefault }); sigma.callbacks.clickEdge({ edge: 'ab' }); sigma.callbacks.doubleClickEdge({ edge: 'ab', preventSigmaDefault }); });
  expect(onOpen).toHaveBeenCalledOnce(); expect(onOpenEdge).toHaveBeenCalledOnce();
});

it('allows keyboard selection, positioning and opening while respecting pins and edit locks', async () => {
  const positions = new Map<string, Position>();
  const props = { data, visible: data, kind: 'instances' as const, selection: {}, positions, cache: new Map<string, Position[]>(), onSelect: vi.fn(), onSelectEdge: vi.fn(), onClear: vi.fn(), onOpen: vi.fn() };
  const view = render(<GraphCanvas {...props} />);
  await waitFor(() => expect(state.sigma?.graph.order).toBe(3));
  const canvas = screen.getByRole('group', { name: '授权子图画布' });
  fireEvent.keyDown(canvas, { key: 'ArrowRight' }); expect(props.onSelect).toHaveBeenCalledWith(data.nodes[0]);
  view.rerender(<GraphCanvas {...props} selection={{ node: 'a' }} />);
  fireEvent.keyDown(canvas, { key: 'ArrowRight' }); fireEvent.keyDown(canvas, { key: 'ArrowDown', shiftKey: true });
  expect(positions.get('a')).toEqual({ id: 'a', x: 16, y: 48 });
  fireEvent.keyDown(canvas, { key: 'Enter' }); expect(props.onOpen).toHaveBeenCalledWith(data.nodes[0]);
  fireEvent.keyDown(canvas, { key: 'Escape' }); expect(props.onClear).toHaveBeenCalledOnce();
  view.rerender(<GraphCanvas {...props} selection={{ node: 'a' }} pins={['a']} />);
  fireEvent.keyDown(canvas, { key: 'ArrowRight' }); expect(positions.get('a')?.x).toBe(16);
  view.rerender(<GraphCanvas {...props} selection={{ node: 'a' }} disabled />);
  fireEvent.keyDown(canvas, { key: 'ArrowRight' }); fireEvent.keyDown(canvas, { key: 'Enter' });
  expect(positions.get('a')?.x).toBe(16); expect(props.onOpen).toHaveBeenCalledOnce();
});
