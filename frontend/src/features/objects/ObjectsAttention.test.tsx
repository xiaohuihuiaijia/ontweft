import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { ObjectsPage } from './ObjectsPage';

const types = { items: [{ key: 'item', label: '物料', properties: [{ key: 'name', label: '名称', data_type: 'string' }], relations: [] }], total: 1, release_id: 'r1', version: 1 };
const active = { id: 'a', project_id: 'p', type_key: 'item', revision: 1, model_release_id: 'r1', data_status: 'active', properties: { name: '有效对象' } };
const incomplete = { ...active, id: 'i', data_status: 'incomplete', properties: { name: '需核对对象' } };
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
const props = { projectId: 'p', canWrite: true, canReadContributions: true, onDirty: vi.fn() };

it('preserves type/text filters, resets pagination and opens attention rows in the existing read-only editor without widening ObjectQuery', async () => {
  const queries: Record<string, unknown>[] = [];
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, options) => {
    const url = new URL(String(input), 'http://localhost');
    if (url.pathname === '/api/ontology/types') return json(types);
    if (url.pathname === '/api/query') { queries.push(JSON.parse(String(options?.body))); return json({ items: [active], total: 1, release_id: 'r1', version: 1 }); }
    if (url.pathname === '/api/objects/i') return json(incomplete);
    if (url.pathname === '/api/objects') return json({ items: [url.searchParams.get('data_status') === 'attention' ? incomplete : active], total: 26 });
    return json({ items: [], total: 0 });
  });
  render(<ObjectsPage {...props} />);
  await screen.findByText('有效对象');
  fireEvent.change(screen.getByLabelText('对象类型'), { target: { value: 'item' } });
  fireEvent.change(screen.getByLabelText('搜索对象'), { target: { value: '检查' } });
  fireEvent.click(screen.getByRole('button', { name: '查询' }));
  await screen.findByText('有效对象');
  fireEvent.click(screen.getByRole('button', { name: '下一页' }));
  await waitFor(() => expect(fetcher.mock.calls.some(([url]) => String(url).includes('offset=25'))).toBe(true));
  fireEvent.change(screen.getByLabelText('数据范围'), { target: { value: 'attention' } });
  await screen.findByText('需核对对象');
  expect(screen.queryByText('有效对象')).toBeNull(); expect(screen.getByText('数据不完整')).not.toBeNull();
  const listCalls = fetcher.mock.calls.map(([value]) => new URL(String(value), 'http://localhost')).filter(url => url.pathname === '/api/objects');
  const latest = listCalls.at(-1)!;
  expect(Object.fromEntries(latest.searchParams)).toMatchObject({ data_status: 'attention', type_key: 'item', q: '检查', offset: '0' });
  fireEvent.click(screen.getByRole('button', { name: '打开' }));
  await screen.findByText('数据状态：数据不完整');
  expect((screen.getByLabelText('名称') as HTMLInputElement).disabled).toBe(true);
  expect(screen.queryByRole('button', { name: '保存对象' })).toBeNull();
  expect(screen.getByRole('button', { name: '来源贡献（展开）' })).not.toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '关闭侧栏' }));
  fireEvent.click(screen.getByRole('tab', { name: '类型化筛选' }));
  expect(screen.queryByLabelText('数据范围')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '执行类型化查询' }));
  await screen.findByText('有效对象');
  expect(queries).toHaveLength(1); expect(queries[0]).not.toHaveProperty('data_status');
  expect(screen.queryByText('需核对对象')).toBeNull();
});

it('hides attention controls without source permission and drops late attention data when permission disappears', async () => {
  let resolveAttention: (response: Response) => void = () => {};
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(input => {
    const path = String(input);
    if (path.includes('/ontology/types')) return Promise.resolve(json(types));
    if (path.includes('data_status=attention')) return new Promise<Response>(resolve => { resolveAttention = resolve; });
    return Promise.resolve(json({ items: [active], total: 1 }));
  });
  const view = render(<ObjectsPage {...props} canReadContributions={false} />);
  await screen.findByText('有效对象');
  expect(screen.queryByLabelText('数据范围')).toBeNull();
  expect(fetcher.mock.calls.every(([url]) => !String(url).includes('data_status=attention'))).toBe(true);
  view.rerender(<ObjectsPage {...props} />);
  fireEvent.change(screen.getByLabelText('数据范围'), { target: { value: 'attention' } });
  expect(screen.queryByText('有效对象')).toBeNull();
  await waitFor(() => expect(fetcher.mock.calls.some(([url]) => String(url).includes('data_status=attention'))).toBe(true));
  view.rerender(<ObjectsPage {...props} canReadContributions={false} />);
  await screen.findByText('有效对象');
  await act(async () => resolveAttention(json({ items: [incomplete], total: 1 })));
  expect(screen.queryByText('需核对对象')).toBeNull(); expect(screen.queryByLabelText('数据范围')).toBeNull();
});

it('clears diagnostic rows after a server-side permission refusal', async () => {
  let denied = false;
  vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
    const path = String(input);
    if (path.includes('/ontology/types')) return json(types);
    if (path.includes('data_status=attention')) return denied ? json({ error: { message: '没有来源管理权限' } }, 403) : json({ items: [incomplete], total: 1 });
    return json({ items: [active], total: 1 });
  });
  render(<ObjectsPage {...props} />);
  await screen.findByText('有效对象');
  fireEvent.change(screen.getByLabelText('数据范围'), { target: { value: 'attention' } });
  await screen.findByText('需核对对象');
  denied = true; fireEvent.click(screen.getByRole('button', { name: '刷新' }));
  await screen.findByText('没有来源管理权限');
  expect(screen.queryByText('需核对对象')).toBeNull(); expect(screen.queryByRole('button', { name: '打开' })).toBeNull();
});
