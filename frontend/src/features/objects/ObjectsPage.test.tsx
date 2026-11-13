import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { loadObjectEditSession, ObjectEditor } from './ObjectsPage';
import { ObjectRelations } from './ObjectRelations';
import type { TypeDef } from '../../shared/model/types';

it('sends the read revision, retains the draft on conflict and never sends source-owned properties', async () => {
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation((_url, options) => Promise.resolve(options?.method === 'PATCH'
    ? new Response(JSON.stringify({ error: { code: 'conflict', message: '另一个用户已保存' } }), { status: 409 })
    : new Response(JSON.stringify({ items: [], total: 0 }))));
  const types: TypeDef[] = [{ key: 'project', label: '项目', properties: [{ key: 'name', label: '名称', data_type: 'string', required: true }, { key: 'external', label: '来源', data_type: 'string', source_owned: true }], relations: [] }];
  const onSaved = vi.fn();
  render(<ObjectEditor object={{ id: 'object-1', project_id: 'p1', type_key: 'project', properties: { name: '原名', external: '只读' }, revision: 4 }} types={types} modelReleaseId="r1" projectId="p1" canWrite onSaved={onSaved} onClose={vi.fn()} onDirty={vi.fn()} />);
  fireEvent.change(screen.getByLabelText('名称 *'), { target: { value: '本地修改' } });
  fireEvent.click(screen.getByRole('button', { name: '保存对象' }));
  await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('版本冲突'));
  expect((screen.getByLabelText('名称 *') as HTMLInputElement).value).toBe('本地修改');
  const writes = fetcher.mock.calls.filter(([, options]) => options?.method === 'PATCH');
  expect(JSON.parse(String(writes[0][1]?.body))).toEqual({ expected_revision: 4, expected_model_release_id: 'r1', properties: { name: '本地修改' } });
  expect(onSaved).not.toHaveBeenCalled(); expect(writes).toHaveLength(1);
});

it('refuses mismatched or missing model metadata on open and reads a fresh schema instead of a page cache', async () => {
  const type = { key: 'item', label: '新单位', properties: [], relations: [] };
  const object = { id: 'o', project_id: 'p', type_key: 'item', properties: {}, revision: 1, model_release_id: 'r2', model_version: 2 };
  let release: string | undefined = 'r1';
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async url => new Response(JSON.stringify(String(url).includes('/ontology/types') ? { items: [type], total: 1, release_id: release, version: 2 } : object)));
  await expect(loadObjectEditSession('p', 'o')).rejects.toMatchObject({ status: 409 });
  release = undefined; await expect(loadObjectEditSession('p', 'o')).rejects.toThrow('模型基线');
  release = 'r2'; expect(await loadObjectEditSession('p', 'o')).toEqual({ object, types: [type], modelReleaseId: 'r2', modelVersion: 2 });
  expect(fetcher.mock.calls.filter(([url]) => String(url).includes('/ontology/types'))).toHaveLength(3);
});

it('freezes create fields and the model baseline across parent refresh and retains input on model conflict', async () => {
  const types: TypeDef[] = [{ key: 'item', label: '物料', properties: [{ key: 'count', label: '数量', data_type: 'integer', unit: '件' }], relations: [] }];
  const writes: unknown[] = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, options) => { writes.push(JSON.parse(String(options?.body))); return new Response(JSON.stringify({ error: { code: 'model_release_conflict', message: '模型已发布新版本，请重新打开并比较' } }), { status: 409 }); });
  const props = { projectId: 'p', types, modelReleaseId: 'r1', canWrite: true, onSaved: vi.fn(), onClose: vi.fn(), onDirty: vi.fn() };
  const view = render(<ObjectEditor {...props} />);
  fireEvent.change(screen.getByLabelText('数量（件）'), { target: { value: '10' } });
  view.rerender(<ObjectEditor {...props} modelReleaseId="r2" types={[{ ...types[0], properties: [{ ...types[0].properties[0], unit: '箱' }] }]} />);
  expect(screen.queryByLabelText('数量（箱）')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '保存对象' }));
  await screen.findByRole('alert');
  expect(writes).toEqual([{ project_id: 'p', type_key: 'item', expected_model_release_id: 'r1', properties: { count: '10' }, evidence: [] }]);
  expect((screen.getByLabelText('数量（件）') as HTMLInputElement).value).toBe('10');
  expect(props.onSaved).not.toHaveBeenCalled();
});

it('sends the frozen model baseline for relation creation and deletion and keeps the chosen target on conflict', async () => {
  const writes: { method: string; body: unknown }[] = [];
  vi.spyOn(window, 'confirm').mockReturnValue(true);
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, options) => {
    if (options?.method && options.method !== 'GET') { writes.push({ method: options.method, body: JSON.parse(String(options.body)) }); return new Response(JSON.stringify({ error: { code: 'model_release_conflict', message: '模型已变化，请重新打开对象比较' } }), { status: 409 }); }
    return new Response(JSON.stringify({ items: String(url).includes('/relations?') ? [{ id: 'rel', revision: 3, source_id: 'o', target_id: 'b', type_key: 'owns' }] : String(url).includes('/objects?') ? [{ id: 'b', properties: { name: '目标' } }] : [], total: 1 }));
  });
  render(<ObjectRelations object={{ id: 'o', type_key: 'item', project_id: 'p', revision: 2, properties: {} }} type={{ key: 'item', label: '物料', properties: [], relations: [{ key: 'owns', label: '负责', target_type: 'item', cardinality: 'many' }] }} modelReleaseId="r1" canWrite onChanged={vi.fn()} />);
  fireEvent.click(screen.getByText('关系与目标对象'));
  await screen.findByRole('option', { name: '目标' });
  fireEvent.change(screen.getByLabelText('目标对象'), { target: { value: 'b' } });
  fireEvent.click(screen.getByRole('button', { name: '新增关系' }));
  await screen.findByRole('alert');
  expect((screen.getByLabelText('目标对象') as HTMLSelectElement).value).toBe('b');
  fireEvent.click(screen.getByRole('button', { name: '删除人工关系' }));
  await waitFor(() => expect(writes).toHaveLength(2));
  expect(writes[0]).toMatchObject({ method: 'POST', body: { expected_model_release_id: 'r1' } });
  expect(writes[1]).toEqual({ method: 'DELETE', body: { expected_revision: 3, expected_model_release_id: 'r1' } });
});

it('edits large integers as exact strings without resending unchanged small integers', async () => {
  let submitted: unknown;
  const object = { id: 'o', project_id: 'p', type_key: 'item', properties: { code: '9223372036854775807', quantity: 42 }, revision: 2 };
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, options) => {
    if (options?.method === 'PATCH') { submitted = JSON.parse(String(options.body)); return new Response(JSON.stringify({ ...object, properties: { ...object.properties, code: '9223372036854775808' }, revision: 3 })); }
    return new Response(JSON.stringify({ items: [], total: 0 }));
  });
  const types: TypeDef[] = [{ key: 'item', label: '项目', properties: [{ key: 'code', label: '精确编号', data_type: 'integer' }, { key: 'quantity', label: '数量', data_type: 'integer' }], relations: [] }];
  render(<ObjectEditor object={object} types={types} modelReleaseId="r1" projectId="p" canWrite onSaved={vi.fn()} onClose={vi.fn()} onDirty={vi.fn()} />);
  const input = screen.getByLabelText('精确编号') as HTMLInputElement;
  expect(input.type).toBe('text'); expect(input.value).toBe('9223372036854775807');
  fireEvent.change(input, { target: { value: '+09223372036854775808' } });
  fireEvent.click(screen.getByRole('button', { name: '保存对象' }));
  await screen.findByText('已保存至平台，未向外部系统写回。');
  expect(submitted).toEqual({ expected_revision: 2, expected_model_release_id: 'r1', properties: { code: '9223372036854775808' } });
  expect(input.value).toBe('9223372036854775808');
});

it.each([
  ['conflict', '来源冲突'], ['incomplete', '数据不完整'], ['withdrawn', '来源已撤回'],
] as const)('keeps %s objects read-only and preserves independently retained manual values', async (dataStatus, label) => {
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => new Response(JSON.stringify({ items: [], total: 0 })));
  const types: TypeDef[] = [{ key: 'item', label: '物料', properties: [{ key: 'note', label: '人工备注', data_type: 'string' }], relations: [{ key: 'owns', label: '负责', target_type: 'item', cardinality: 'many' }] }];
  render(<ObjectEditor object={{ id: 'o', project_id: 'p', type_key: 'item', properties: { note: '仍保留的人工说明' }, revision: 2, data_status: dataStatus }} types={types} modelReleaseId="r1" projectId="p" canWrite onSaved={vi.fn()} onClose={vi.fn()} onDirty={vi.fn()} />);
  expect(screen.getByText(`数据状态：${label}`).closest('[role="status"]')).not.toBeNull();
  const note = screen.getByLabelText('人工备注') as HTMLInputElement;
  expect(note.disabled).toBe(true); expect(note.value).toBe('仍保留的人工说明');
  expect(screen.queryByRole('button', { name: '保存对象' })).toBeNull();
  fireEvent.click(screen.getByText('关系与目标对象'));
  await screen.findByText('暂无已保存关系。');
  expect(screen.queryByRole('button', { name: '新增关系' })).toBeNull();
  expect(fetcher.mock.calls.every(([, options]) => !options?.method || options.method === 'GET')).toBe(true);
});

it('closing a nested source page does not submit an unsaved object', async () => {
  const object = { id: 'o', project_id: 'p', type_key: 'item', properties: { note: '原文' }, revision: 1, evidence: [{ document_id: 'doc', page: 1 }] };
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, options) => {
    if (options?.method === 'PATCH') return new Response(JSON.stringify({ ...object, revision: 2 }));
    if (String(url).includes('/documents/')) return new Response(JSON.stringify({ items: [{ page_number: 1, label: '合成来源', text: '合成原件', status: 'parsed', has_image: false, elements: [] }], total: 1, revision: 1, content_hash: 'hash' }));
    return new Response(JSON.stringify({ items: [], total: 0 }));
  });
  const onSaved = vi.fn();
  render(<ObjectEditor object={object} types={[{ key: 'item', label: '物料', properties: [{ key: 'note', label: '备注', data_type: 'string' }], relations: [] }]} modelReleaseId="r" projectId="p" canWrite onSaved={onSaved} onClose={() => {}} onDirty={() => {}} />);
  fireEvent.change(screen.getByLabelText('备注'), { target: { value: '尚未确认的修改' } });
  fireEvent.click(screen.getByText('来源证据'));
  fireEvent.click(screen.getByRole('button', { name: '查看原文 · 第 1 页' }));
  await screen.findByText('合成原件');
  fireEvent.click(within(screen.getByRole('complementary', { name: '文档来源页' })).getByRole('button', { name: '关闭侧栏' }));
  await waitFor(() => expect(screen.queryByRole('complementary', { name: '文档来源页' })).toBeNull());
  expect(fetcher.mock.calls.filter(([, options]) => options?.method === 'PATCH')).toHaveLength(0);
  expect(onSaved).not.toHaveBeenCalled();
  expect((screen.getByLabelText('备注') as HTMLInputElement).value).toBe('尚未确认的修改');
});

it('keeps a relation condition draft mounted when paging or closing the enclosing object', async () => {
  const object = { id: 'o', project_id: 'p', type_key: 'item', properties: {}, revision: 1, evidence: [] };
  const relationDefinition = { key: 'fits', label: '适用于', target_type: 'item', cardinality: 'many' as const, properties: [{ key: 'condition', label: '适用条件', data_type: 'string' as const }] };
  const relation = { id: 'edge', revision: 1, source_id: 'o', target_id: 'target', label: '适用于', type_key: 'fits', model_release_id: 'r', properties: { condition: '室内' }, evidence: [], definition: relationDefinition };
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
    const url = new URL(String(input), 'http://test');
    const data = url.pathname === '/api/relations'
      ? { items: Number(url.searchParams.get('offset')) === 25 ? [{ ...relation, id: 'edge26', target_id: '目标26' }] : [relation], total: 26 }
      : url.pathname === '/api/relations/edge' ? relation
      : url.pathname === '/api/objects/o' || url.pathname === '/api/objects/target' ? object
      : { items: [], total: 0 };
    return new Response(JSON.stringify(data));
  });
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
  const close = vi.fn();
  render(<ObjectEditor object={object} types={[{ key: 'item', label: '物料', properties: [], relations: [relationDefinition] }]} modelReleaseId="r" projectId="p" canWrite role="owner" initialRelationKey="fits" onSaved={vi.fn()} onClose={close} onDirty={vi.fn()} />);
  fireEvent.click(await screen.findByRole('button', { name: '条件与证据' }));
  fireEvent.click(await screen.findByRole('button', { name: '编辑条件与证据' }));
  await waitFor(() => expect((screen.getByLabelText('适用条件') as HTMLInputElement).disabled).toBe(false));
  fireEvent.change(screen.getByLabelText('适用条件'), { target: { value: '待核对的室外条件' } });
  const next = within(screen.getByRole('group', { name: '对象关系分页' })).getByRole('button', { name: '下一页' }) as HTMLButtonElement;
  expect(next.disabled).toBe(true);
  fireEvent.click(next);
  expect(fetcher.mock.calls.some(([url]) => String(url).includes('offset=25'))).toBe(false);
  fireEvent.click(screen.getByRole('button', { name: '关闭侧栏' }));
  expect(confirm).toHaveBeenCalledOnce();
  expect(close).not.toHaveBeenCalled();
  expect((screen.getByLabelText('适用条件') as HTMLInputElement).value).toBe('待核对的室外条件');
  fireEvent.click(screen.getByRole('button', { name: '取消修改' }));
  await waitFor(() => expect(next.disabled).toBe(false));
  fireEvent.click(next);
  await screen.findByText('→ fits · 目标26');
});
