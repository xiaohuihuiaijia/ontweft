import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { ObjectFulltext, type FulltextIndex } from './ObjectFulltext';
import { ObjectsPage } from './ObjectsPage';
import type { TypeList } from '../../shared/model/types';
import { ApiError } from '../../shared/api/api';

const model: TypeList = { items: [{ key: 'item', label: '物料', properties: [{ key: 'name', label: '名称', data_type: 'string' }], relations: [] }], total: 1, release_id: 'r1', version: 1 };
const index: FulltextIndex = { kind: 'object_fulltext', generation: 'g1', tokenizer_version: 'cn-1', status: 'ready' };
const object = { id: 'o1', project_id: 'p', type_key: 'item', revision: 1, properties: { name: '库存原料' }, data_status: 'active', model_release_id: 'r1', model_version: 1 };
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
const result = (extra: object = {}) => ({ items: [object], total: null, has_more: true, limit: 20, offset: 0, query: { text: '库存 原料', terms: ['库存', '原料'], mode: 'all_terms' }, release_id: 'r1', model_version: 1, index, matches: { o1: { fields: ['name'] } }, ...extra });
const props = { projectId: 'p', types: model, loadingTypes: false, refreshTypes: vi.fn(), opening: false, onOpen: vi.fn(), refreshKey: 0 };

it('uses the model GET baseline and existing object table/editor, keeping pagination on applied keywords and safe fields', async () => {
  const bodies: Record<string, unknown>[] = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, options) => {
    const path = String(input);
    if (path.includes('/ontology/types')) return json(model);
    if (path.includes('/search/status')) return json(index);
    if (path === '/api/search/objects') { const body = JSON.parse(String(options?.body)); bodies.push(body); return json(result({ offset: body.offset, index: { ...index, status: 'updating' } })); }
    if (path.includes('/objects/o1?')) return json(object);
    return json({ items: [], total: 0 });
  });
  render(<ObjectsPage projectId="p" canWrite={false} onDirty={vi.fn()} />);
  fireEvent.click(screen.getByRole('tab', { name: '全文检索' }));
  await screen.findByText('当前状态：对象全文索引已就绪');
  expect(bodies).toHaveLength(0); expect((screen.getByRole('button', { name: '执行全文检索' }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.change(screen.getByLabelText('全文关键词'), { target: { value: '库存 原料' } });
  fireEvent.change(screen.getByLabelText('全文对象类型'), { target: { value: 'item' } });
  fireEvent.click(screen.getByRole('button', { name: '执行全文检索' }));
  await screen.findByText('库存原料');
  expect(bodies[0]).toEqual({ count_total: false, project_id: 'p', q: '库存 原料', type_key: 'item', limit: 20, offset: 0, expected_release_id: 'r1', expected_model_version: 1 });
  expect(screen.getByText(/已返回的安全结果仅是当前可检索部分/)).not.toBeNull();
  expect(screen.getByText('授权命中字段')).not.toBeNull(); expect(screen.getByRole('cell', { name: '名称' })).not.toBeNull();
  fireEvent.change(screen.getByLabelText('全文关键词'), { target: { value: '尚未执行的词' } });
  fireEvent.click(screen.getByRole('button', { name: '下一页全文结果' }));
  await waitFor(() => expect(bodies).toHaveLength(2));
  expect(bodies[1]).toMatchObject({ q: '库存 原料', offset: 20, expected_release_id: 'r1' });
  fireEvent.click(await screen.findByRole('button', { name: '打开' }));
  await screen.findByRole('complementary', { name: '对象详情' });
  expect((screen.getByLabelText('名称') as HTMLInputElement).value).toBe('库存原料');
});

it('retains keywords on 409 and requires a fresh model read and explicit rebind before another query', async () => {
  let version = 1; const bodies: Record<string, unknown>[] = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, options) => {
    const path = String(input);
    if (path.includes('/ontology/types')) return json({ ...model, release_id: `r${version}`, version });
    if (path.includes('/search/status')) return json(index);
    if (path === '/api/search/objects') { const body = JSON.parse(String(options?.body)); bodies.push(body); if (bodies.length === 1) { version = 2; return json({ error: { message: '全文查询模型已发布新版本' } }, 409); } return json(result({ release_id: 'r2', model_version: 2 })); }
    return json({ items: [], total: 0 });
  });
  render(<ObjectsPage projectId="p" canWrite={false} onDirty={vi.fn()} />);
  fireEvent.click(screen.getByRole('tab', { name: '全文检索' }));
  await screen.findByText('当前状态：对象全文索引已就绪');
  fireEvent.change(screen.getByLabelText('全文关键词'), { target: { value: '保留中文条件' } });
  fireEvent.click(screen.getByRole('button', { name: '执行全文检索' }));
  await screen.findByText('全文查询模型已发布新版本');
  expect((screen.getByLabelText('全文关键词') as HTMLInputElement).value).toBe('保留中文条件');
  expect((screen.getByRole('button', { name: '执行全文检索' }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: '读取最新模型，保留关键词' }));
  await waitFor(() => expect((screen.getByRole('button', { name: '按当前模型重新绑定全文查询' }) as HTMLButtonElement).disabled).toBe(false));
  expect(bodies).toHaveLength(1);
  fireEvent.click(screen.getByRole('button', { name: '按当前模型重新绑定全文查询' }));
  expect(bodies).toHaveLength(1); fireEvent.click(screen.getByRole('button', { name: '执行全文检索' }));
  await screen.findByText('库存原料'); expect(bodies[1]).toMatchObject({ expected_release_id: 'r2', expected_model_version: 2 });
});

it.each(['not_ready', 'failed'] as const)('does not equate %s zero results with absence, render HTML, or fall back to contains', async status => {
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => String(input).includes('/search/status') ? json({ ...index, status }) : json(result({ items: [], total: 0, query: { text: '<img src=x onerror=alert(1)>', terms: ['<script>'], mode: 'all_terms' }, index: { ...index, status } })));
  const view = render(<ObjectFulltext {...props} />);
  fireEvent.change(screen.getByLabelText('全文关键词'), { target: { value: '中文无结果' } });
  fireEvent.click(screen.getByRole('button', { name: '执行全文检索' }));
  await screen.findByText('当前暂无可返回的全文结果');
  expect(screen.queryByText('没有符合全文条件的对象')).toBeNull();
  expect(screen.getByText(/零条结果不能证明没有匹配对象/)).not.toBeNull();
  expect(view.container.querySelector('img,script')).toBeNull();
  expect(fetcher.mock.calls.every(([url]) => String(url).includes('/search/'))).toBe(true);
});

it('discards late project results and clears visible rows on a new 403 without retries', async () => {
  let resolveOld: (response: Response) => void = () => {}; let denied = false;
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation((input, options) => {
    if (String(input).includes('/search/status')) return Promise.resolve(json(index));
    const body = JSON.parse(String(options?.body));
    if (body.project_id === 'old') return new Promise<Response>(resolve => { resolveOld = resolve; });
    return Promise.resolve(denied ? json({ error: { message: '全文读取权限已撤销' } }, 403) : json(result()));
  });
  const view = render(<ObjectFulltext {...props} projectId="old" />);
  fireEvent.change(screen.getByLabelText('全文关键词'), { target: { value: '旧项目' } }); fireEvent.click(screen.getByRole('button', { name: '执行全文检索' }));
  await waitFor(() => expect(fetcher.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(1));
  view.rerender(<ObjectFulltext {...props} projectId="new" />);
  await act(async () => resolveOld(json(result({ items: [{ ...object, properties: { name: '旧项目秘密' } }] }))));
  expect(screen.queryByText('旧项目秘密')).toBeNull(); expect((screen.getByLabelText('全文关键词') as HTMLInputElement).value).toBe('');
  fireEvent.change(screen.getByLabelText('全文关键词'), { target: { value: '库存' } }); fireEvent.click(screen.getByRole('button', { name: '执行全文检索' }));
  await screen.findByText('库存原料'); denied = true;
  fireEvent.click(screen.getByRole('button', { name: '重新执行上次全文查询' }));
  await screen.findByText('全文读取权限已撤销'); expect(screen.queryByText('库存原料')).toBeNull();
  expect(fetcher.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(3);
});

it('shows server tokenizer budget failures without changing or resubmitting the query', async () => {
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async input => String(input).includes('/search/status') ? json(index) : json({ error: { message: '分词超过12个，请缩小查询' } }, 422));
  render(<ObjectFulltext {...props} />);
  fireEvent.change(screen.getByLabelText('全文关键词'), { target: { value: '很多中文业务词' } }); fireEvent.click(screen.getByRole('button', { name: '执行全文检索' }));
  await screen.findByText('分词超过12个，请缩小查询');
  expect((screen.getByLabelText('全文关键词') as HTMLInputElement).value).toBe('很多中文业务词');
  expect(fetcher.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(1);
});

it('immediately clears search output when model read permission is lost, and permits only reviewed query offset bounds', async () => {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async input => String(input).includes('/search/status') ? json(index) : json(result({ total: 2000, offset: 1000 })));
  const view = render(<ObjectFulltext {...props} />);
  fireEvent.change(screen.getByLabelText('全文关键词'), { target: { value: '分页边界' } }); fireEvent.click(screen.getByRole('button', { name: '执行全文检索' }));
  await screen.findByText('库存原料');
  expect((screen.getByRole('button', { name: '下一页全文结果' }) as HTMLButtonElement).disabled).toBe(true);
  view.rerender(<ObjectFulltext {...props} types={undefined} modelError={new ApiError(403, '模型读取权限撤销')} />);
  expect(screen.queryByText('库存原料')).toBeNull(); expect(screen.queryByText('本次可读匹配 2000 条')).toBeNull();
  expect((screen.getByRole('button', { name: '执行全文检索' }) as HTMLButtonElement).disabled).toBe(true);
});

it('lets source maintainers rebuild a failed index with the frozen model and clears results without rerunning preserved conditions', async () => {
  let searches = 0; let currentIndex: FulltextIndex = { ...index, status: 'failed' }; const writes: unknown[] = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, options) => {
    const path = String(input);
    if (path.includes('/ontology/types')) return json(model);
    if (path.includes('/search/status')) return json(currentIndex);
    if (path === '/api/search/objects') { searches++; return json(result({ index: currentIndex })); }
    if (path === '/api/search/rebuild') { writes.push(JSON.parse(String(options?.body))); currentIndex = { ...index, generation: 'g2', status: 'updating' }; return json(currentIndex, 202); }
    return json({ items: [], total: 0 });
  });
  render(<ObjectsPage projectId="p" canWrite={false} canReadContributions onDirty={vi.fn()} />);
  fireEvent.click(screen.getByRole('tab', { name: '全文检索' }));
  await screen.findByText('当前状态：全文索引处理失败，不能确认结果完整性');
  fireEvent.change(screen.getByLabelText('全文关键词'), { target: { value: '库存 原料' } });
  fireEvent.change(screen.getByLabelText('全文对象类型'), { target: { value: 'item' } });
  fireEvent.click(screen.getByRole('button', { name: '执行全文检索' })); await screen.findByText('库存原料');
  fireEvent.click(screen.getByRole('button', { name: '重建对象全文索引' }));
  await screen.findByText(/对象全文索引重建已在后台排队/);
  expect(writes).toEqual([{ project_id: 'p', expected_release_id: 'r1', expected_model_version: 1 }]);
  expect(searches).toBe(1); expect(screen.queryByText('库存原料')).toBeNull();
  expect((screen.getByLabelText('全文关键词') as HTMLInputElement).value).toBe('库存 原料');
  expect((screen.getByLabelText('全文对象类型') as HTMLSelectElement).value).toBe('item');
  expect(screen.getByText(/旧结果已清除，请主动重新查询，排队不代表重建完成/)).not.toBeNull();
  await screen.findByText('当前状态：全文索引更新中，结果暂不完整');
});

it('hides rebuild without source permission, requires explicit rebind on 409 and clears protected results on rebuild 403', async () => {
  const writes: unknown[] = []; let revision = 1;
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, options) => {
    if (String(input).includes('/search/status')) return json(index);
    if (String(input) === '/api/search/rebuild') { writes.push(JSON.parse(String(options?.body))); return json({ error: { message: writes.length === 1 ? '重建模型基线过期' : '来源维护权限已撤销' } }, writes.length === 1 ? 409 : 403); }
    return json(result({ release_id: `r${revision}`, model_version: revision }));
  });
  const view = render(<ObjectFulltext {...props} />);
  expect(screen.queryByRole('button', { name: '重建对象全文索引' })).toBeNull();
  view.rerender(<ObjectFulltext {...props} canManage />);
  fireEvent.change(screen.getByLabelText('全文关键词'), { target: { value: '保留检索词' } });
  fireEvent.click(screen.getByRole('button', { name: '执行全文检索' })); await screen.findByText('库存原料');
  fireEvent.click(screen.getByRole('button', { name: '重建对象全文索引' })); await screen.findByText('重建模型基线过期');
  expect((screen.getByRole('button', { name: '重建对象全文索引' }) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByLabelText('全文关键词') as HTMLInputElement).value).toBe('保留检索词'); expect(writes).toHaveLength(1);
  revision = 2; view.rerender(<ObjectFulltext {...props} canManage types={{ ...model, release_id: 'r2', version: 2 }} />);
  fireEvent.click(screen.getByRole('button', { name: '按当前模型重新绑定全文查询' })); expect(writes).toHaveLength(1);
  fireEvent.click(screen.getByRole('button', { name: '执行全文检索' })); await screen.findByText('库存原料');
  fireEvent.click(screen.getByRole('button', { name: '重建对象全文索引' })); await screen.findByText('来源维护权限已撤销');
  expect(writes[1]).toEqual({ project_id: 'p', expected_release_id: 'r2', expected_model_version: 2 });
  expect(screen.queryByText('库存原料')).toBeNull(); expect(screen.queryByRole('button', { name: '重建对象全文索引' })).toBeNull();
  expect(writes).toHaveLength(2);
});
