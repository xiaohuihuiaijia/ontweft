import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { ContributionDecisions } from './ContributionDecisions';
import { ObjectContributions } from './ObjectContributions';
import { ObjectEditor } from '../ObjectsPage';

const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
const decision = { id: 'decision1', revision: 1, operation: 'select', reason: '核对来源', actor_id: 'user1', created_at: '2026-10-29T10:00:00Z', selected_contribution_id: 'c1', selected_contribution_revision: 3 };
const candidate = { contribution_id: 'c1', revision: 3, identity_id: 'identity1', source_id: 'source1', source_name: '仓储台账', resource_id: 'inventory', record_id: 'record1', release_id: 'r1', mapping_key: '库存来源甲', active: true, value: '9223372036854775807' };
const context = {
  object_id: 'o', expected_revision: 7, expected_model_release_id: 'r1', expected_contribution_fingerprint: 'fingerprint1', data_status: 'conflict',
  fields: [{ field_key: 'stock', label: '库存数量', data_type: 'integer', unit: '件', can_decide: true, state: 'conflict', current_present: false, decision: null, candidates: [candidate, { ...candidate, contribution_id: 'c2', source_id: 'source2', mapping_key: '库存来源乙', value: '9223372036854775808' }, { ...candidate, contribution_id: 'c3', mapping_key: '已撤回来源', active: false }] }, { field_key: 'code', label: '产品编码', data_type: 'string', can_decide: true, state: 'unopposed', current_present: true, current_value: 'CODE-1', decision: null, candidates: [] }],
};
const preview = { preview_fingerprint: 'preview1', field_key: 'stock', operation: 'select', before: { present: false }, after: { present: true, value: '9223372036854775807' }, data_status_before: 'conflict', data_status_after: 'active', remaining_visible_conflicts: [], scope: 'platform_only' };
const entity = { id: 'o', project_id: 'p', type_key: 'item', revision: 8, model_release_id: 'r1', data_status: 'active' as const, properties: { stock: '9223372036854775807', note: '人工说明' } };
const props = () => ({ objectId: 'o', objectRevision: 7, modelReleaseId: 'r1', canDecide: true, onDirty: vi.fn(), onDecided: vi.fn(), onDenied: vi.fn() });
const open = async () => { fireEvent.click(screen.getByRole('button', { name: '查看字段冲突与裁决' })); await screen.findByRole('heading', { name: '库存数量（件） · 来源冲突' }); };
const choose = () => { fireEvent.click(screen.getByRole('radio', { name: '选择库存来源甲修订3' })); fireEvent.change(screen.getByLabelText(/^裁决理由/), { target: { value: '已核对入库记录，采纳甲来源。' } }); };

it('freezes the complete server context and preview, preserves exact values and never submits its enclosing object form', async () => {
  const writes: { path: string; body: Record<string, unknown> }[] = [];
  let committed = false; const submitForm = vi.fn(); const callbacks = props();
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, options) => {
    if (options?.method === 'POST') { writes.push({ path: String(url), body: JSON.parse(String(options.body)) }); if (String(url).endsWith('/preview')) return json(preview); committed = true; return json({ decision, object: entity }); }
    return json(committed ? { ...context, expected_revision: 8, data_status: 'active', fields: [{ ...context.fields[0], state: 'selected', current_present: true, current_value: candidate.value, decision }] } : context);
  });
  render(<form onSubmit={event => { event.preventDefault(); submitForm(); }}><ContributionDecisions {...callbacks} /></form>);
  await open();
  expect(screen.queryByRole('button', { name: '产品编码 · 无冲突' })).toBeNull();
  expect((screen.getByRole('radio', { name: '选择已撤回来源修订3' }) as HTMLInputElement).disabled).toBe(true);
  expect(screen.queryByText(/9223372036854775807/)).toBeNull();
  expect(screen.getAllByText('仓储台账').length).toBeGreaterThan(0);
  fireEvent.click(screen.getAllByRole('button', { name: '查看授权候选值' })[0]);
  expect(screen.getByText(/9223372036854775807/)).not.toBeNull();
  choose(); expect(callbacks.onDirty).toHaveBeenLastCalledWith(true);
  fireEvent.click(screen.getByRole('button', { name: '预览裁决影响' }));
  const view = await screen.findByLabelText('裁决影响预览');
  expect(within(view).getByText(/9223372036854775807/)).not.toBeNull();
  expect(writes[0].body).toEqual({ expected_revision: 7, expected_model_release_id: 'r1', expected_contribution_fingerprint: 'fingerprint1', field_key: 'stock', operation: 'select', selected_contribution_id: 'c1', reason: '已核对入库记录，采纳甲来源。' });
  fireEvent.click(screen.getByRole('button', { name: '确认保存字段裁决' }));
  await screen.findByText(/已恢复有效，将移出待核对列表/);
  expect(writes[1].body).toEqual({ ...writes[0].body, expected_preview_fingerprint: 'preview1' });
  expect(callbacks.onDecided).toHaveBeenCalledWith(entity); expect(callbacks.onDirty).toHaveBeenLastCalledWith(false); expect(submitForm).not.toHaveBeenCalled();
});

it('invalidates previews after edits and sends hold/clear without accidentally selecting a source', async () => {
  const writes: Record<string, unknown>[] = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, options) => {
    if (options?.method === 'POST') { const body = JSON.parse(String(options.body)); writes.push(body); return json({ ...preview, operation: body.operation, after: { present: false }, data_status_after: 'incomplete' }); }
    return json({ ...context, fields: [{ ...context.fields[0], decision }] });
  });
  render(<ContributionDecisions {...props()} />); await open();
  fireEvent.change(screen.getByLabelText('字段处理方式'), { target: { value: 'hold' } });
  fireEvent.change(screen.getByLabelText(/^裁决理由/), { target: { value: '暂缓采用，等待来源核实' } });
  fireEvent.click(screen.getByRole('button', { name: '预览裁决影响' })); await screen.findByLabelText('裁决影响预览');
  expect(writes[0]).toMatchObject({ operation: 'hold', selected_contribution_id: null });
  fireEvent.change(screen.getByLabelText(/^裁决理由/), { target: { value: '补充核对理由' } });
  expect(screen.queryByLabelText('裁决影响预览')).toBeNull();
  fireEvent.change(screen.getByLabelText('字段处理方式'), { target: { value: 'clear' } });
  fireEvent.click(screen.getByRole('button', { name: '预览裁决影响' })); await screen.findByLabelText('裁决影响预览');
  expect(writes[1]).toMatchObject({ operation: 'clear', selected_contribution_id: null, reason: '补充核对理由' });
});

it.each(['preview', 'commit'])('keeps the %s conflict draft and requires explicit refresh, rebinding and another preview', async stage => {
  const writes: Record<string, unknown>[] = []; let refreshed = false; let failOnce = true;
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, options) => {
    if (options?.method === 'POST') {
      writes.push(JSON.parse(String(options.body)));
      if (failOnce && (String(url).endsWith('/preview') ? stage === 'preview' : stage === 'commit')) { failOnce = false; return json({ error: { message: '来源贡献已更新，请核对' } }, 409); }
      return json(preview);
    }
    return json(refreshed ? { ...context, expected_revision: 9, expected_contribution_fingerprint: 'fingerprint2', fields: [{ ...context.fields[0], candidates: [{ ...candidate, revision: 4 }] }] } : context);
  });
  render(<ContributionDecisions {...props()} />); await open(); choose();
  fireEvent.click(screen.getByRole('button', { name: '预览裁决影响' }));
  if (stage === 'commit') { await screen.findByLabelText('裁决影响预览'); fireEvent.click(screen.getByRole('button', { name: '确认保存字段裁决' })); }
  await screen.findByText('来源贡献已更新，请核对');
  expect((screen.getByLabelText(/^裁决理由/) as HTMLTextAreaElement).value).toBe('已核对入库记录，采纳甲来源。');
  expect((screen.getByRole('radio', { name: '选择库存来源甲修订3' }) as HTMLInputElement).checked).toBe(true);
  expect((screen.getByRole('button', { name: '预览裁决影响' }) as HTMLButtonElement).disabled).toBe(true);
  const failedWrites = writes.length; refreshed = true;
  fireEvent.click(screen.getByRole('button', { name: '读取最新裁决基线' }));
  await screen.findByRole('button', { name: '使用最新基线重新绑定草稿' });
  expect(writes).toHaveLength(failedWrites); expect((screen.getByRole('radio', { name: '选择库存来源甲修订3' }) as HTMLInputElement).checked).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: '使用最新基线重新绑定草稿' }));
  expect((screen.getByRole('radio', { name: '选择库存来源甲修订4' }) as HTMLInputElement).checked).toBe(true);
  expect(screen.queryByRole('button', { name: '确认保存字段裁决' })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '预览裁决影响' })); await screen.findByLabelText('裁决影响预览');
  expect(writes.at(-1)).toMatchObject({ expected_revision: 9, expected_contribution_fingerprint: 'fingerprint2', reason: '已核对入库记录，采纳甲来源。' });
});

it('requires both local and field permissions and refuses a model different from the frozen editor', async () => {
  let data = context;
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => json(data));
  const callbacks = props();
  const view = render(<ContributionDecisions {...callbacks} canDecide={false} />); await open();
  expect(screen.queryByRole('button', { name: '预览裁决影响' })).toBeNull();
  expect((screen.getByRole('radio', { name: '选择库存来源甲修订3' }) as HTMLInputElement).disabled).toBe(true);
  view.rerender(<ContributionDecisions {...callbacks} canDecide />);
  data = { ...context, fields: [{ ...context.fields[0], can_decide: false }] };
  fireEvent.click(screen.getByRole('button', { name: '读取最新裁决基线' }));
  await waitFor(() => expect((screen.getByRole('radio', { name: '选择库存来源甲修订3' }) as HTMLInputElement).disabled).toBe(true));
  expect((screen.getByRole('button', { name: '预览裁决影响' }) as HTMLButtonElement).disabled).toBe(true);
  data = { ...context, expected_model_release_id: 'r2' };
  fireEvent.click(screen.getByRole('button', { name: '读取最新裁决基线' })); await screen.findByText(/裁决模型与此对象编辑器的冻结模型不同/);
  expect((screen.getByRole('button', { name: '预览裁决影响' }) as HTMLButtonElement).disabled).toBe(true);
  expect(fetcher.mock.calls.every(([, options]) => !options?.method)).toBe(true);
});

it('pages decision history lazily and clears all contribution material when history authorization is revoked', async () => {
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async url => {
    const path = String(url);
    if (path.includes('/history')) return path.includes('before=2') ? json({ error: { message: '裁决查看权限已撤销' } }, 403) : json({ items: [{ ...decision, revision: 2 }], next_cursor: 2 });
    if (path.endsWith('/contribution-decisions')) return json(context);
    return json({ items: [], next_cursor: null });
  });
  render(<ObjectContributions objectId="o" objectRevision={7} modelReleaseId="r1" canRead canDecide />);
  expect(fetcher).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '来源贡献（展开）' })); await open();
  expect(fetcher.mock.calls.some(([url]) => String(url).includes('/history'))).toBe(false);
  choose(); fireEvent.click(screen.getAllByRole('button', { name: '查看授权候选值' })[0]);
  fireEvent.click(screen.getByRole('button', { name: '查看裁决历史' }));
  const history = await screen.findByLabelText('字段裁决历史'); await within(history).findByText('核对来源');
  fireEvent.click(within(history).getByRole('button', { name: '更早裁决' }));
  await screen.findByText('裁决查看权限已撤销');
  await waitFor(() => expect(screen.queryByText(/9223372036854775807/)).toBeNull());
  expect(screen.queryByLabelText(/^裁决理由/)).toBeNull(); expect(screen.queryByLabelText('字段裁决历史')).toBeNull();
});

it('drops a late preview across objects and clears draft/preview on a local permission loss', async () => {
  let resolvePreview: (value: Response) => void = () => {};
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, options) => {
    if (options?.method === 'POST') return new Promise<Response>(resolve => { resolvePreview = resolve; });
    if (String(url).endsWith('/contribution-decisions')) return json({ ...context, object_id: String(url).includes('/new/') ? 'new' : 'o' });
    return json({ items: [], next_cursor: null });
  });
  const callbacks = { onDirty: vi.fn(), onDecided: vi.fn() };
  const view = render(<ObjectContributions {...callbacks} objectId="o" objectRevision={7} modelReleaseId="r1" canRead canDecide />);
  fireEvent.click(screen.getByRole('button', { name: '来源贡献（展开）' })); await open(); choose();
  fireEvent.click(screen.getByRole('button', { name: '预览裁决影响' }));
  view.rerender(<ObjectContributions {...callbacks} objectId="new" objectRevision={7} modelReleaseId="r1" canRead canDecide />);
  await act(async () => resolvePreview(json(preview)));
  expect(screen.queryByLabelText('裁决影响预览')).toBeNull(); expect(screen.queryByLabelText(/^裁决理由/)).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '来源贡献（展开）' })); await open(); choose();
  view.rerender(<ObjectContributions {...callbacks} objectId="new" objectRevision={7} modelReleaseId="r1" canRead={false} canDecide={false} />);
  expect(screen.queryByRole('button')).toBeNull(); expect(callbacks.onDirty).toHaveBeenLastCalledWith(false);
});

it('merges decision dirty state with ObjectEditor and refreshes the detail and background after confirmation', async () => {
  let committed = false;
  const onDirty = vi.fn(), onSaved = vi.fn(), onClose = vi.fn();
  vi.spyOn(window, 'confirm').mockReturnValue(false);
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, options) => {
    const path = String(url);
    if (options?.method === 'POST') { if (path.endsWith('/preview')) return json(preview); committed = true; return json({ decision, object: entity }); }
    if (path.endsWith('/contribution-decisions')) return json(committed ? { ...context, expected_revision: 8, data_status: 'active' } : context);
    return json({ items: [], next_cursor: null, total: 0 });
  });
  render(<ObjectEditor object={{ ...entity, revision: 7, data_status: 'conflict', properties: { note: '人工说明' } }} types={[{ key: 'item', label: '物料', properties: [{ key: 'stock', label: '库存', data_type: 'integer', source_owned: true }, { key: 'note', label: '人工备注', data_type: 'string' }], relations: [] }]} modelReleaseId="r1" projectId="p" canWrite canReadContributions onDirty={onDirty} onSaved={onSaved} onClose={onClose} />);
  fireEvent.click(screen.getByRole('button', { name: '来源贡献（展开）' })); await open(); choose();
  expect(onDirty).toHaveBeenLastCalledWith(true);
  fireEvent.click(screen.getByRole('button', { name: '关闭侧栏' })); expect(onClose).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '预览裁决影响' })); await screen.findByLabelText('裁决影响预览');
  fireEvent.click(screen.getByRole('button', { name: '确认保存字段裁决' }));
  await screen.findByText(/字段裁决已保存，对象修订 8/);
  expect((screen.getByRole('textbox', { name: /^库存/ }) as HTMLInputElement).value).toBe('9223372036854775807');
  expect((screen.getByLabelText('人工备注') as HTMLInputElement).value).toBe('人工说明');
  await waitFor(() => expect((screen.getByLabelText('人工备注') as HTMLInputElement).disabled).toBe(false));
  expect(onSaved).toHaveBeenCalledTimes(1); expect(onDirty).toHaveBeenLastCalledWith(false);
  expect(fetcher.mock.calls.some(([, options]) => options?.method === 'PATCH')).toBe(false);
});

it('keeps ordinary object changes intact and prevents starting a concurrent decision', async () => {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async url => json(String(url).endsWith('/contribution-decisions') ? context : { items: [], next_cursor: null }));
  render(<ObjectEditor object={entity} types={[{ key: 'item', label: '物料', properties: [{ key: 'note', label: '人工备注', data_type: 'string' }], relations: [] }]} modelReleaseId="r1" projectId="p" canWrite canReadContributions onDirty={vi.fn()} onSaved={vi.fn()} onClose={vi.fn()} />);
  fireEvent.change(screen.getByLabelText('人工备注'), { target: { value: '未保存的人工说明' } });
  fireEvent.click(screen.getByRole('button', { name: '来源贡献（展开）' })); await open();
  expect(screen.queryByRole('button', { name: '预览裁决影响' })).toBeNull();
  expect((screen.getByRole('radio', { name: '选择库存来源甲修订3' }) as HTMLInputElement).disabled).toBe(true);
  expect((screen.getByLabelText('人工备注') as HTMLInputElement).value).toBe('未保存的人工说明');
});

it('removes newly hidden field values on a successful refresh without silently discarding the reason', async () => {
  let hidden = false;
  vi.spyOn(globalThis, 'fetch').mockImplementation(async () => json(hidden ? { ...context, expected_revision: 9, fields: [context.fields[1]] } : context));
  render(<ContributionDecisions {...props()} />); await open(); choose();
  fireEvent.click(screen.getAllByRole('button', { name: '查看授权候选值' })[0]);
  expect(screen.getByText(/9223372036854775807/)).not.toBeNull(); hidden = true;
  fireEvent.click(screen.getByRole('button', { name: '读取最新裁决基线' }));
  await screen.findByText(/原字段已不可用/);
  expect(screen.queryByText(/9223372036854775807/)).toBeNull();
  expect(screen.getByText(/尚未提交的理由：已核对入库记录/)).not.toBeNull();
  expect(screen.queryByRole('button', { name: '确认保存字段裁决' })).toBeNull();
});

it('keeps the contribution panel and object drawer open while a confirmation request is in flight', async () => {
  let resolveCommit: (value: Response) => void = () => {}; let committed = false;
  const onSaved = vi.fn(), onClose = vi.fn();
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, options) => {
    if (options?.method === 'POST') return String(url).endsWith('/preview') ? json(preview) : new Promise<Response>(resolve => { resolveCommit = resolve; });
    if (String(url).endsWith('/contribution-decisions')) return json({ ...context, expected_revision: committed ? 8 : 7 });
    return json({ items: [], next_cursor: null, total: 0 });
  });
  render(<ObjectEditor object={{ ...entity, revision: 7, data_status: 'conflict' }} types={[{ key: 'item', label: '物料', properties: [], relations: [] }]} modelReleaseId="r1" projectId="p" canWrite canReadContributions onDirty={vi.fn()} onSaved={onSaved} onClose={onClose} />);
  fireEvent.click(screen.getByRole('button', { name: '来源贡献（展开）' })); await open(); choose();
  fireEvent.click(screen.getByRole('button', { name: '预览裁决影响' })); await screen.findByLabelText('裁决影响预览');
  fireEvent.click(screen.getByRole('button', { name: '确认保存字段裁决' }));
  await waitFor(() => expect((screen.getByRole('button', { name: '关闭侧栏' }) as HTMLButtonElement).disabled).toBe(true));
  expect((screen.getByRole('button', { name: '来源贡献（收起）' }) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByRole('button', { name: '收起字段裁决' }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: '关闭侧栏' })); expect(onClose).not.toHaveBeenCalled();
  committed = true; await act(async () => resolveCommit(json({ decision, object: entity })));
  await screen.findByText(/字段裁决已保存，对象修订 8/);
  await waitFor(() => expect((screen.getByRole('button', { name: '关闭侧栏' }) as HTMLButtonElement).disabled).toBe(false));
  expect(onSaved).toHaveBeenCalledTimes(1);
});
