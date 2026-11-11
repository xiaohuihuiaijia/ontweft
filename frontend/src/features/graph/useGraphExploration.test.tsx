import { act, fireEvent, render, renderHook, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { ApiError } from '../../shared/api/api';
import type { GraphResult } from '../../shared/model/types';
import { useGraphExploration } from './useGraphExploration';

const graph = (label: string, truncated = false): GraphResult => ({ release_id: 'r1', version: 1, nodes: [{ id: 'a', label, type_key: 'item', kind: 'object', properties: {} }], edges: [], truncated });

it('retains branch collapse and pagination after refreshing edited values', async () => {
  const initial = graph('Old');
  const neighbor = { ...initial.nodes[0], id: 'b', label: 'Neighbor' };
  vi.spyOn(globalThis, 'fetch')
    .mockResolvedValueOnce(new Response(JSON.stringify({ ...initial, nodes: [...initial.nodes, neighbor], next_cursor: 'page-2' })))
    .mockResolvedValueOnce(new Response(JSON.stringify({ ...graph('Edited'), nodes: [...graph('Edited').nodes, { ...neighbor, label: 'Updated neighbor' }] })));
  const { result } = renderHook(() => useGraphExploration('p', 'u', initial, true, 200, vi.fn()));
  await act(async () => { await result.current.expand('a'); });
  await act(async () => { await result.current.refresh(); });
  expect(result.current.slices.a.next_cursor).toBe('page-2');
  expect(result.current.data.nodes.find(node => node.id === 'b')?.label).toBe('Updated neighbor');
  act(() => result.current.collapse('a'));
  expect(result.current.data.nodes).toEqual(graph('Edited').nodes);
});

it('uses the newest authorized node values and keeps server truncation visible', async () => {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ ...graph('Updated', true), next_cursor: null })));
  const { result } = renderHook(() => useGraphExploration('p', 'u', graph('Old'), true, 200, vi.fn()));
  await act(async () => { await result.current.expand('a'); });
  expect(result.current.data.nodes[0].label).toBe('Updated');
  expect(result.current.data.truncated).toBe(true);
});

it('preserves positions, pins and expanded nodes while the same graph reloads', async () => {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ ...graph('Updated'), nodes: [...graph('Updated').nodes, { ...graph('Neighbor').nodes[0], id: 'b' }], next_cursor: null })));
  const { result, rerender } = renderHook(({ initial }: { initial: GraphResult | undefined }) => useGraphExploration('p', 'u', initial, true, 200, vi.fn()), { initialProps: { initial: graph('Old') as GraphResult | undefined } });
  await act(async () => { await result.current.expand('a'); result.current.togglePin('a'); result.current.positions.current.set('a', { id: 'a', x: 12, y: 34 }); });
  rerender({ initial: undefined });
  rerender({ initial: graph('Refreshed') });
  expect(result.current.pins).toEqual(['a']);
  expect(result.current.positions.current.get('a')).toEqual({ id: 'a', x: 12, y: 34 });
  expect(result.current.data.nodes.map(n => n.id)).toContain('b');
  rerender({ initial: { ...graph('New model'), release_id: 'r2' } });
  expect(result.current.pins).toEqual([]);
  expect(result.current.data.nodes.map(n => n.id)).toEqual(['a']);
});

it('does not commit a late neighborhood response after the model changes', async () => {
  let finish!: (response: Response) => void;
  vi.spyOn(globalThis, 'fetch').mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  const onError = vi.fn();
  const { result, rerender } = renderHook(({ initial }) => useGraphExploration('p', 'u', initial, true, 200, onError), { initialProps: { initial: graph('Old') } });
  let pending!: Promise<void>;
  act(() => { pending = result.current.expand('a'); });
  rerender({ initial: { ...graph('New model'), release_id: 'r2' } });
  await act(async () => { finish(new Response(JSON.stringify({ ...graph('Late'), next_cursor: null }))); await pending; });
  expect(result.current.slices).toEqual({});
  expect(result.current.busy).toBe(false);
});

it('reports truncation when restoring a bounded saved view', async () => {
  localStorage.setItem('ontweft.graph.v1:u:p', JSON.stringify({ ids: ['a'], pins: [], positions: [] }));
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify(graph('Saved', true))));
  function View() { const state = useGraphExploration('p', 'u', graph('Old'), true, 200, vi.fn()); return <>{state.controls}<output>{String(state.data.truncated)}</output></>; }
  render(<View />);
  fireEvent.click(screen.getByRole('button', { name: '恢复个人视图' }));
  expect(await screen.findByText('true')).toBeTruthy();
  localStorage.removeItem('ontweft.graph.v1:u:p');
});


it('clears authorized search hits and expanded content after access is denied', async () => {
  const initial = graph('Initial'); let denied = false;
  vi.spyOn(globalThis, 'fetch').mockImplementation(async url => {
    if (denied) return new Response(JSON.stringify({ error: { message: '权限已撤销' } }), { status: 403 });
    return new Response(JSON.stringify(String(url).includes('/search/objects') ? { items: [{ id: 'secret', type_key: 'item', properties: { name: '授权检索内容' } }], index: { status: 'ready' }, has_more: false } : { ...graph('Expanded secret'), next_cursor: null }));
  });
  function View() { const state = useGraphExploration('p', 'u', initial, true, 200, vi.fn()); return <>{state.controls}<button onClick={() => state.expand('a')}>展开</button><output>{state.data.nodes.map(n => n.label).join(',')}</output></>; }
  render(<View />);
  fireEvent.change(screen.getByLabelText('全库对象搜索'), { target: { value: '授权' } });
  fireEvent.click(screen.getByRole('button', { name: '搜索' }));
  await screen.findByRole('button', { name: '授权检索内容 · 加入画布' });
  fireEvent.click(screen.getByRole('button', { name: '展开' }));
  await screen.findByText('Expanded secret');
  denied = true;
  fireEvent.click(screen.getByRole('button', { name: '展开' }));
  await act(async () => {});
  expect(screen.queryByText('Expanded secret')).toBeNull();
  expect(screen.queryByRole('button', { name: '授权检索内容 · 加入画布' })).toBeNull();
});

it('invalidates pending neighborhoods when the initial graph loses access', async () => {
  let finish!: (response: Response) => void;
  vi.spyOn(globalThis, 'fetch').mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  const initial = graph('Initial');
  const { result, rerender } = renderHook(({ readError }: { readError?: unknown }) => useGraphExploration('p', 'u', initial, true, 200, vi.fn(), false, readError), { initialProps: {} });
  let pending!: Promise<void>;
  act(() => { pending = result.current.expand('a'); });
  rerender({ readError: new ApiError(403, '权限已撤销') });
  await act(async () => { finish(new Response(JSON.stringify(graph('Late secret')))); await pending; });
  expect(result.current.data.nodes).toEqual([]);
  expect(result.current.slices).toEqual({});
});
