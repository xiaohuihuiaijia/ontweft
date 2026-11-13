import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { OntologyEditor, OntologyPage } from './OntologyPage';
import type { ChangeSet, Definition } from '../../shared/model/types';

it('resumes an existing unpublished model instead of suggesting another empty start', async () => {
  const draft = { id: 'waiting', project_id: 'p', title: '待发布产品模型', revision: 2, status: 'draft', definition: { types: [], mappings: [] }, baseline_release_id: null, model_diff: { items: [], counts: { added: 0, modified: 0, removed: 0 }, affected_type_keys: [], affected_mapping_keys: [] } };
  vi.spyOn(globalThis, 'fetch').mockImplementation(async url => new Response(JSON.stringify(String(url).includes('/changesets') ? { items: [draft], total: 1 } : String(url).includes('/ontology/definition') ? { definition: draft.definition, release_id: null, version: 0 } : { items: [], total: 0, release_id: null, version: 0 })));
  render(<OntologyPage projectId="p" can={() => true} onDirty={() => {}} onNavigate={vi.fn()} />);
  fireEvent.click(await screen.findByRole('button', { name: '继续查看模型草稿' }));
  expect((screen.getByLabelText('变更名称') as HTMLInputElement).value).toBe('待发布产品模型');
  expect(screen.queryByRole('button', { name: '接入表格并生成模型建议' })).toBeNull();
});

it('does not describe permission-filtered types as a missing model', async () => {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ items: [], total: 0, release_id: 'r1', version: 1 })));
  render(<OntologyPage projectId="p" can={() => false} onDirty={() => {}} onNavigate={vi.fn()} />);
  await screen.findByText('当前没有可见的已发布类型');
  expect(screen.queryByRole('button', { name: '接入表格并生成模型建议' })).toBeNull();
  expect(screen.queryByRole('button', { name: '新建变更集' })).toBeNull();
});

it('lets an editor amend an approved unpublished revision and requires approval again after saving', async () => {
  const definition = { types: [], mappings: [] };
  let current: ChangeSet = { id: 'change', project_id: 'p', title: '已批准草稿', revision: 3, status: 'approved', definition, author_id: 'previous-author', approved_by: 'reviewer', validation: { valid: true, errors: [] } };
  const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, options) => {
    if (String(url) === '/api/changesets/change' && options?.method === 'PATCH') {
      const body = JSON.parse(String(options.body));
      expect(body.expected_revision).toBe(3);
      current = { ...current, ...body, revision: 4, status: 'draft', author_id: 'editor', approved_by: undefined, validation: undefined };
      return new Response(JSON.stringify(current));
    }
    if (String(url) === '/api/changesets/change/validate') {
      current = { ...current, revision: 5, status: 'validated', validation: { valid: true, errors: [] } };
      return new Response(JSON.stringify(current));
    }
    return new Response(JSON.stringify({ items: [], total: 0 }));
  });
  render(<OntologyEditor projectId="p" can={() => true} modelApproval={{ mode: 'separate', userId: 'editor' }} onDirty={() => {}} onClose={() => {}} onUpdated={() => {}} initialDraft={current} initialDefinition={definition} initialBaselineReleaseId={null} />);
  const title = screen.getByLabelText('变更名称') as HTMLInputElement;
  expect(title.disabled).toBe(false);
  fireEvent.change(title, { target: { value: '纠正审批后发现的问题' } });
  expect(screen.queryByRole('button', { name: '发布已批准修订' })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '保存草稿' }));
  fireEvent.click(await screen.findByRole('button', { name: '验证' }));
  const approve = await screen.findByRole('button', { name: '批准当前修订' }) as HTMLButtonElement;
  expect(approve.disabled).toBe(true);
  expect(screen.getByText('当前修订由你编辑，请由另一位有审批权限的成员批准。')).toBeTruthy();
  expect(fetch.mock.calls.some(([url]) => String(url).endsWith('/approve'))).toBe(false);
});

it('keeps a published revision read-only for an editor', () => {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ items: [], total: 0 })));
  const draft = { id: 'published', project_id: 'p', title: '已发布', revision: 6, status: 'published', definition: { types: [], mappings: [] } };
  render(<OntologyEditor projectId="p" can={() => true} onDirty={() => {}} onClose={() => {}} onUpdated={() => {}} initialDraft={draft} initialDefinition={draft.definition} initialBaselineReleaseId={null} />);
  expect((screen.getByLabelText('变更名称') as HTMLInputElement).disabled).toBe(true);
  expect(screen.queryByRole('button', { name: '保存草稿' })).toBeNull();
});

it('keeps only one validation poll in flight and stops polling before approval', async () => {
  vi.useFakeTimers();
  try {
    const pending: ((value: Response) => void)[] = [];
    const definition = { types: [], mappings: [] };
    const initial = { id: 'c1', project_id: 'p', title: '合成校验', revision: 1, status: 'validating', definition, validation: { valid: false, errors: [] } };
    vi.spyOn(globalThis, 'fetch').mockImplementation((url, options) => {
      if (String(url) === '/api/changesets/c1' && !options?.method) return new Promise(resolve => pending.push(resolve));
      if (String(url) === '/api/changesets/c1/approve') return Promise.resolve(new Response(JSON.stringify({ ...initial, revision: 3, status: 'approved', validation: { valid: true } })));
      return Promise.resolve(new Response(JSON.stringify({ items: [], total: 0 })));
    });
    render(<OntologyEditor projectId="p" can={() => true} onDirty={() => {}} onClose={() => {}} onUpdated={() => {}} initialDraft={initial} initialDefinition={definition} initialBaselineReleaseId={null} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(4000); });
    expect(pending).toHaveLength(1);
    await act(async () => { pending[0](new Response(JSON.stringify({ ...initial, revision: 2, status: 'validated', validation: { valid: true } }))); });
    await act(async () => fireEvent.click(screen.getByRole('button', { name: '批准当前修订' })));
    await act(async () => { await vi.advanceTimersByTimeAsync(4000); });
    expect(pending).toHaveLength(1);
    expect(screen.getByRole('button', { name: '发布已批准修订' })).toBeTruthy();
  } finally { vi.useRealTimers(); }
});

it('ignores a late validation result after saving a corrected stopped-validation draft', async () => {
  vi.useFakeTimers();
  try {
    let resolvePoll: (value: Response) => void = () => {};
    let pollSignal: AbortSignal | null | undefined;
    const definition = { types: [], mappings: [] };
    const initial = { id: 'c1', project_id: 'p', title: '待修正校验', revision: 1, status: 'validating', definition, validation: { valid: false, errors: [] }, validation_job: { id: 'job', status: 'failed', error: '合成失败', objects_checked: 0 } };
    vi.spyOn(globalThis, 'fetch').mockImplementation((url, options) => {
      if (String(url) === '/api/changesets/c1' && !options?.method) { pollSignal = options?.signal; return new Promise(resolve => { resolvePoll = resolve; }); }
      if (String(url) === '/api/changesets/c1' && options?.method === 'PATCH') return Promise.resolve(new Response(JSON.stringify({ ...initial, title: '修正后的草稿', revision: 2, status: 'draft', validation_job: undefined })));
      return Promise.resolve(new Response(JSON.stringify({ items: [], total: 0 })));
    });
    render(<OntologyEditor projectId="p" can={() => true} onDirty={() => {}} onClose={() => {}} onUpdated={() => {}} initialDraft={initial} initialDefinition={definition} initialBaselineReleaseId={null} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    fireEvent.change(screen.getByLabelText('变更名称'), { target: { value: '修正后的草稿' } });
    await act(async () => fireEvent.click(screen.getByRole('button', { name: '保存草稿' })));
    expect(pollSignal?.aborted).toBe(true);
    await act(async () => { resolvePoll(new Response(JSON.stringify(initial))); });
    expect(screen.getByText('修订 2')).toBeTruthy();
    expect(screen.getByRole('button', { name: '验证' })).toBeTruthy();
    expect((screen.getByLabelText('变更名称') as HTMLInputElement).value).toBe('修正后的草稿');
  } finally { vi.useRealTimers(); }
});

it('starts a new complete draft from the management definition, preserving mappings and hidden field definitions', async () => {
  const type = { key: 'asset', label: '设备', properties: [{ key: 'secret', label: '受限字段', data_type: 'string', read_roles: ['owner'], source_owned: true }], relations: [] };
  const mapping = { key: 'source_asset', source_id: 's', resource_id: 'assets', type_key: 'asset', schema_fingerprint: 'a'.repeat(64), fields: { secret: { source_field: 'secret_value', transform: 'identity' } } };
  const definition = { types: [type], mappings: [mapping] };
  let submitted: Record<string, unknown> | undefined;
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, options) => {
    const path = String(url);
    let response: unknown = { items: [], total: 0 };
    if (path.includes('/ontology/definition')) response = { definition, version: 1, release_id: 'r1' };
    else if (path.includes('/ontology/types')) response = { items: [{ ...type, properties: [] }], total: 1 };
    else if (path === '/api/changesets' && options?.method === 'POST') {
      submitted = JSON.parse(String(options.body)); response = { ...submitted, id: 'c2', revision: 1, status: 'draft', validation: {} };
    }
    return new Response(JSON.stringify(response), { status: 200 });
  });
  render(<OntologyPage projectId="p" can={() => true} onDirty={() => {}} />);
  await waitFor(() => expect((screen.getByRole('button', { name: '新建变更集' }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button', { name: '新建变更集' }));
  fireEvent.change(screen.getByLabelText('变更名称'), { target: { value: '保留现有映射' } });
  fireEvent.click(screen.getByRole('button', { name: '保存草稿' }));
  await waitFor(() => expect(submitted?.definition).toEqual(definition));
  expect(submitted?.expected_baseline_release_id).toBe('r1');
  expect(screen.queryByRole('heading', { name: '验证结果' })).toBeNull();
});

it('keeps a new draft bound to the loaded release and preserves input when that baseline is stale', async () => {
  let submitted: Record<string, unknown> | undefined;
  const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, options) => {
    if (String(url).includes('/ontology/templates')) return new Response(JSON.stringify({ items: [], total: 0 }));
    submitted = JSON.parse(String(options?.body));
    return new Response(JSON.stringify({ error: { code: 'baseline_conflict', message: '已发布模型已变化，请重新核对定义' } }), { status: 409 });
  });
  const props = { projectId: 'p', can: (permission: string) => permission === 'ontology:write', onDirty: () => {}, onClose: () => {}, onUpdated: () => {}, initialDefinition: { types: [], mappings: [] } };
  const view = render(<OntologyEditor {...props} initialBaselineReleaseId="r1" />);
  fireEvent.change(screen.getByLabelText('变更名称'), { target: { value: '保留我读到的基线' } });
  view.rerender(<OntologyEditor {...props} initialBaselineReleaseId="r2" />);
  fireEvent.click(screen.getByRole('button', { name: '保存草稿' }));
  await screen.findByRole('alert');
  expect(submitted?.expected_baseline_release_id).toBe('r1');
  expect((screen.getByLabelText('变更名称') as HTMLInputElement).value).toBe('保留我读到的基线');
  expect(fetch.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(1);
});

it('keeps the published editor mounted while the management definition reloads', async () => {
  let draft: ChangeSet;
  let managementReads = 0;
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, options) => {
    const path = String(url); let response: unknown = { items: [], total: 0 };
    if (path.includes('/ontology/definition')) { managementReads++; response = { definition: { types: [], mappings: [] }, release_id: 'r1', version: 1 }; }
    else if (options?.method === 'POST') {
      const body = JSON.parse(String(options.body));
      if (path === '/api/changesets') draft = { ...body, id: 'c1', revision: 1, status: 'draft' };
      else draft = { ...draft, revision: draft.revision + 1, status: path.endsWith('/publish') ? 'published' : path.endsWith('/approve') ? 'approved' : 'validated', validation: { valid: true, errors: [] } };
      response = draft;
    }
    return new Response(JSON.stringify(response));
  });
  render(<OntologyPage projectId="p" can={() => true} onDirty={() => {}} />);
  await waitFor(() => expect((screen.getByRole('button', { name: '新建变更集' }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button', { name: '新建变更集' }));
  fireEvent.change(screen.getByLabelText('变更名称'), { target: { value: '发布后保留结果' } });
  fireEvent.click(screen.getByRole('button', { name: '保存草稿' }));
  fireEvent.click(await screen.findByRole('button', { name: '验证' }));
  fireEvent.click(await screen.findByRole('button', { name: '批准当前修订' }));
  fireEvent.click(await screen.findByRole('button', { name: '发布已批准修订' }));
  await waitFor(() => expect(managementReads).toBe(2));
  await screen.findByText('发布成功，已发布模型已刷新。');
  expect((screen.getByLabelText('变更名称') as HTMLInputElement).value).toBe('发布后保留结果');
  expect(screen.queryByRole('heading', { name: '新建模型变更' })).toBeNull();
});


it('explains separate approval for the current revision author while preserving single-person and other-reviewer approval', async () => {
  const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ items: [], total: 0 })));
  const draft = { id: 'change', project_id: 'p', title: '待审批', author_id: 'author', revision: 4, status: 'validated', definition: { types: [], mappings: [] }, validation: { valid: true, errors: [] } };
  const props = { projectId: 'p', can: () => true, onDirty: () => {}, onClose: () => {}, onUpdated: () => {}, initialDraft: draft, initialDefinition: draft.definition, initialBaselineReleaseId: null };
  const view = render(<OntologyEditor {...props} {...{ modelApproval: { userId: 'author', mode: 'separate' as const } }} />);
  const button = screen.getByRole('button', { name: '批准当前修订' }) as HTMLButtonElement;
  expect(button.disabled).toBe(true);
  expect(screen.getByText('当前修订由你编辑，请由另一位有审批权限的成员批准。')).toBeTruthy();
  fireEvent.click(button);
  expect(fetch.mock.calls.some(([, options]) => options?.method === 'POST')).toBe(false);
  view.rerender(<OntologyEditor {...props} {...{ modelApproval: { userId: 'author', mode: 'single' as const } }} />);
  expect(button.disabled).toBe(false);
  view.rerender(<OntologyEditor {...props} {...{ modelApproval: { userId: 'reviewer', mode: 'separate' as const } }} />);
  expect(button.disabled).toBe(false);
});

it('saves type aliases and optional relationship fields without changing existing object fields', async () => {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ items: [], total: 0 })));
  const existingProperty = { key: 'code', label: '产品编号', data_type: 'string' as const, required: true, source_owned: true, read_roles: ['owner'] };
  const definition: Definition = { types: [{ key: 'product', label: '连接器', properties: [existingProperty], relations: [{ key: 'fits', label: '适用于', target_type: 'product', cardinality: 'many' }] }], mappings: [] };
  const save = vi.fn().mockResolvedValue(undefined);
  render(<OntologyEditor projectId="p" can={() => true} onDirty={() => {}} onClose={() => {}} onUpdated={() => {}} initialDefinition={definition} initialBaselineReleaseId={null} initialTitle="补充适用条件" onSaveDefinition={save} />);
  fireEvent.change(screen.getByLabelText(/类型别名（每行一个）/), { target: { value: '插接件\nConnector' } });
  fireEvent.click(screen.getByRole('button', { name: '添加关系属性' }));
  fireEvent.change(screen.getByLabelText('关系字段标识'), { target: { value: 'condition' } });
  fireEvent.change(screen.getByLabelText('关系字段名称'), { target: { value: '适用条件' } });
  // Only the original object field offers required/source-owned controls.
  expect(screen.getAllByLabelText('必填')).toHaveLength(1);
  expect(screen.getAllByLabelText('字段由来源系统管理（平台对象表单只读）')).toHaveLength(1);
  fireEvent.click(screen.getByRole('button', { name: '保存草稿' }));
  await waitFor(() => expect(save).toHaveBeenCalledOnce());
  const saved = save.mock.calls[0][0] as Definition;
  expect(saved.types[0].aliases).toEqual(['插接件', 'Connector']);
  expect(saved.types[0].properties).toEqual([existingProperty]);
  expect(saved.types[0].relations[0].properties).toEqual([{ key: 'condition', label: '适用条件', data_type: 'string' }]);
});
