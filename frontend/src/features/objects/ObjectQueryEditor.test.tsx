import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { compileObjectQuery, newQueryDraft, type QueryDraft } from './ObjectQueryEditor';
import { ObjectsPage } from './ObjectsPage';
import type { TypeList } from '../../shared/model/types';

const types: TypeList = { items: [{ key: 'item', label: '库存项', properties: [
  { key: 'name', label: '名称', data_type: 'string' }, { key: 'code', label: '精确编号', data_type: 'integer' },
  { key: 'amount', label: '金额', data_type: 'number' }, { key: 'active', label: '启用', data_type: 'boolean' },
  { key: 'metadata', label: '自由 JSON', data_type: 'json' },
], relations: [] }], total: 1, release_id: 'release-1', version: 1 };
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });

it('compiles exact typed values and explicit null without accepting JSON or unreadable fields', () => {
  const draft: QueryDraft = { ...newQueryDraft(types), filters: [
    { field: 'code', op: 'gte', value: '+009223372036854775807', nullValue: false },
    { field: 'active', op: 'in', value: 'false\ntrue', nullValue: false },
    { field: 'name', op: 'ne', value: '', nullValue: true },
    { field: 'name', op: 'contains', value: '%_literal', nullValue: false },
  ] };
  expect(compileObjectQuery('p', draft, types).filters).toEqual([
    { field: 'code', op: 'gte', value: '9223372036854775807' }, { field: 'active', op: 'in', value: [false, true] },
    { field: 'name', op: 'ne', value: null }, { field: 'name', op: 'contains', value: '%_literal' },
  ]);
  for (const field of ['metadata', 'private_salary']) expect(() => compileObjectQuery('p', { ...draft, filters: [{ field, op: 'eq', value: '{}', nullValue: false }] }, types)).toThrow('不可用');
  expect(() => compileObjectQuery('p', { ...draft, sortField: 'metadata' }, types)).toThrow('排序字段');
});

it('queries and pages through the existing authorized object table with a frozen release and exact integers', async () => {
  const calls: unknown[] = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, options) => {
    if (String(url).includes('/ontology/types')) return response(types);
    if (String(url) === '/api/query') {
      const body = JSON.parse(String(options?.body)); calls.push(body);
      return response({ items: [{ id: body.offset ? 'o2' : 'o1', type_key: 'item', revision: 1, properties: { name: body.offset ? '第二页对象' : '精确对象', code: '9223372036854775807' } }], total: 26, release_id: 'release-1', version: 1 });
    }
    return response({ items: [], total: 0 });
  });
  render(<ObjectsPage projectId="p" canWrite={false} onDirty={vi.fn()} />);
  await waitFor(() => expect((screen.getByRole('tab', { name: '类型化筛选' }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('tab', { name: '类型化筛选' }));
  fireEvent.click(screen.getByRole('button', { name: '添加筛选条件' }));
  expect(screen.queryByRole('option', { name: /自由 JSON/ })).toBeNull();
  fireEvent.change(screen.getByLabelText('条件 1 字段'), { target: { value: 'code' } });
  fireEvent.change(screen.getByLabelText('条件 1 比较'), { target: { value: 'gte' } });
  fireEvent.change(screen.getByLabelText('条件 1 值', { exact: false }), { target: { value: '9223372036854775807' } });
  fireEvent.change(screen.getByLabelText('排序字段'), { target: { value: 'code' } });
  fireEvent.change(screen.getByLabelText('排序方向'), { target: { value: 'desc' } });
  fireEvent.click(screen.getByRole('button', { name: '执行类型化查询' }));
  await screen.findByText('精确对象');
  expect(calls[0]).toEqual({ project_id: 'p', type_key: 'item', filters: [{ field: 'code', op: 'gte', value: '9223372036854775807' }], sort_field: 'code', sort_desc: true, limit: 25, offset: 0, release_id: 'release-1' });
  fireEvent.click(screen.getByRole('button', { name: '下一页' }));
  await screen.findByText('第二页对象');
  expect(calls[1]).toEqual({ ...(calls[0] as object), offset: 25 });
  expect(screen.getByRole('button', { name: '打开' })).toBeTruthy();
});

it('retains the query draft on 409 and only rebinds after an explicit current-model decision', async () => {
  let release = 1; const queries: { release_id: string }[] = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, options) => {
    if (String(url).includes('/ontology/types')) return response({ ...types, release_id: `release-${release}`, version: release });
    if (String(url) === '/api/query') {
      const body = JSON.parse(String(options?.body)); queries.push(body);
      if (queries.length === 1) { release = 2; return response({ error: { message: '查询本体版本已过期，请刷新' } }, 409); }
      return response({ items: [], total: 0, release_id: 'release-2', version: 2 });
    }
    return response({ items: [], total: 0 });
  });
  render(<ObjectsPage projectId="p" canWrite={false} onDirty={vi.fn()} />);
  await waitFor(() => expect((screen.getByRole('tab', { name: '类型化筛选' }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('tab', { name: '类型化筛选' }));
  fireEvent.click(screen.getByRole('button', { name: '添加筛选条件' }));
  fireEvent.change(screen.getByLabelText('条件 1 字段'), { target: { value: 'name' } });
  fireEvent.change(screen.getByLabelText('条件 1 值', { exact: true }), { target: { value: '保留条件' } });
  fireEvent.click(screen.getByRole('button', { name: '暂存本页查询草稿' }));
  fireEvent.change(screen.getByLabelText('条件 1 值', { exact: true }), { target: { value: '未暂存条件' } });
  vi.spyOn(window, 'confirm').mockReturnValue(true);
  fireEvent.click(screen.getByRole('button', { name: '恢复本页暂存草稿' }));
  expect((screen.getByLabelText('条件 1 值', { exact: true }) as HTMLInputElement).value).toBe('保留条件');
  fireEvent.click(screen.getByRole('button', { name: '执行类型化查询' }));
  await screen.findByText('查询本体版本已过期，请刷新');
  expect(queries).toHaveLength(1);
  fireEvent.click(screen.getByRole('button', { name: '读取最新模型，保留草稿' }));
  await screen.findByText('模型发布版本已变化');
  expect((screen.getByRole('button', { name: '执行类型化查询' }) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByLabelText('条件 1 值', { exact: true }) as HTMLInputElement).value).toBe('保留条件');
  expect(queries).toHaveLength(1);
  fireEvent.click(screen.getByRole('button', { name: '按当前发布重新绑定草稿' }));
  expect(queries).toHaveLength(1);
  fireEvent.click(screen.getByRole('button', { name: '执行类型化查询' }));
  await screen.findByText('没有符合条件的对象');
  expect(queries.map(query => query.release_id)).toEqual(['release-1', 'release-2']);
});

it('discards a late typed-query response after switching back to text search', async () => {
  let resolveQuery!: (response: Response) => void;
  vi.spyOn(globalThis, 'fetch').mockImplementation(async url => {
    if (String(url).includes('/ontology/types')) return response(types);
    if (String(url) === '/api/query') return new Promise<Response>(resolve => { resolveQuery = resolve; });
    return response({ items: [{ id: 'public', type_key: 'item', properties: { name: '当前文本结果' }, revision: 1 }], total: 1 });
  });
  render(<ObjectsPage projectId="p" canWrite={false} onDirty={vi.fn()} />);
  await screen.findByText('当前文本结果');
  fireEvent.click(screen.getByRole('tab', { name: '类型化筛选' }));
  fireEvent.click(screen.getByRole('button', { name: '执行类型化查询' }));
  await waitFor(() => expect(resolveQuery).toBeTruthy());
  fireEvent.click(screen.getByRole('tab', { name: '字段包含' }));
  await screen.findByText('当前文本结果');
  await act(async () => resolveQuery(response({ items: [{ id: 'old', type_key: 'item', properties: { name: '不应落入新列表的旧结果' }, revision: 1 }], total: 1, release_id: 'release-1', version: 1 })));
  expect(screen.queryByText('不应落入新列表的旧结果')).toBeNull();
});
