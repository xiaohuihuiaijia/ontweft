import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { AIProposalPanel } from './AIProposalPanel';
import type { Proposal } from './types';

vi.mock('../graph/GraphCanvas', () => ({ GraphCanvas: () => <div>候选模型图</div> }));
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
const definition = { types: [{ key: 'product', label: '产品', properties: [{ key: 'name', label: '名称', data_type: 'string', source_owned: true }, { key: 'code', label: '型号', data_type: 'string', source_owned: true }], relations: [] }], mappings: [] };
const evidence = [{ document_id: 'd1', page: 2, quote: '合成产品 A', parse_id: 'page-2' }];
const ready = { id: 'p1', revision: 4, status: 'ready', definition, baseline_release_id: 'r1', candidates: [{ id: 'c1', type_key: 'product', properties: { name: '合成产品 A', code: 'A' }, business_key: { code: 'A' }, evidence, selected: true }], relations: [], inputs: { instructions: '产品规格' } } as Proposal;
const connection = { id: 'm1', enabled: true, name: '本地合成服务', roles: { extraction: 'synthetic', synthesis: 'synthetic' }, models: [] };

it('shows grounded modeling reasons, stale notices and persisted partial-import issues after reopening', async () => {
  mocks(path => path === '/api/ai/proposals/p1' ? json({ ...ready, status: 'imported',
    definition: { ...definition, types: [{ ...definition.types[0], relations: [{ key: 'fits', label: '适用于', target_type: 'product', cardinality: 'many', properties: [{ key: 'condition', label: '安装条件', data_type: 'string' }] }] }] },
    relations: [{ id: 'r1', source_candidate_id: 'c1', target_candidate_id: 'c1', key: 'fits', properties: { condition: '仅干燥环境' }, evidence }],
    model_notes: [{ target_kind: 'type', type_key: 'product', key: '', question: '哪些产品符合要求？', reason: '用已有产品类型承载规格', change: 'reused', stale: true, evidence }],
    imported: { imported: 1, counts: { created: 1, updated: 0, unchanged: 0, blocked: 0 }, relations: 0, relations_blocked: 1, issues: [{ candidate_id: 'r1', message: '保留已有关系条件' }] },
  }) : undefined);
  render(<AIProposalPanel projectId="project" can={() => true} onDirty={() => {}} />);
  await screen.findByRole('option', { name: '产品规格 · ready' });
  fireEvent.change(screen.getByLabelText('生成记录'), { target: { value: 'p1' } });
  fireEvent.click(await screen.findByText('为什么这样建模 · 1 条说明'));
  expect(screen.getByText('哪些产品符合要求？')).toBeTruthy();
  expect(screen.getByText(/当前定义已修改或移除/)).toBeTruthy();
  expect(screen.getByText('部分导入完成')).toBeTruthy();
  expect(screen.getByText(/保留已有关系条件/)).toBeTruthy();
  expect((screen.getByRole('button', { name: '导入所选实例' }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: '关系 1' }));
  expect(screen.getByText('安装条件')).toBeTruthy();
  expect(screen.getByText('仅干燥环境')).toBeTruthy();
  expect((screen.getByRole('button', { name: '编辑关系候选' }) as HTMLButtonElement).disabled).toBe(true);
});

it('opens the shared relation detail from a persisted conflict for read-only review', async () => {
  const fetch = mocks(path => path === '/api/ai/proposals/p1' ? json({ ...ready, status: 'imported', imported: {
    imported: 1, relations_blocked: 1, issues: [{ relation_id: 'saved-edge', candidate_id: 'r1', message: '候选条件不同，原值保留' }],
  } }) : path === '/api/relations/saved-edge' ? json({ id: 'saved-edge', revision: 1, source_id: 'a', target_id: 'b', label: '适用于', properties: { condition: '干燥环境' }, evidence: [], model_release_id: 'r1', definition: { key: 'fits', label: '适用于', target_type: 'product', cardinality: 'many', properties: [{ key: 'condition', label: '适用条件', data_type: 'string' }] } }) : undefined);
  render(<AIProposalPanel projectId="project" role="viewer" can={permission => permission !== 'objects:write'} onDirty={() => {}} />);
  await screen.findByRole('option', { name: '产品规格 · ready' });
  fireEvent.change(screen.getByLabelText('生成记录'), { target: { value: 'p1' } });
  fireEvent.click(await screen.findByRole('button', { name: '查看关系' }));
  const drawer = await screen.findByRole('complementary', { name: '关系详情' });
  await within(drawer).findByText('干燥环境');
  expect(within(drawer).queryByRole('button', { name: '编辑条件与证据' })).toBeNull();
  expect(fetch.mock.calls.filter(([, options]) => options?.method === 'POST' || options?.method === 'PATCH')).toHaveLength(0);
});

it('pages relationships by 20 and applies selection to the visible relationship id', async () => {
  let submitted: Record<string, unknown> | undefined;
  const relations = Array.from({ length: 21 }, (_, i) => ({ id: `r${i}`, key: 'fits', source_candidate_id: 'c1', target_candidate_id: 'c2', selected: true, evidence }));
  mocks((path, options) => {
    if (path !== '/api/ai/proposals/p1') return;
    if (options?.method === 'PATCH') { submitted = JSON.parse(String(options.body)); return json({ ...ready, ...submitted, revision: 5 }); }
    return json({ ...ready, relations });
  });
  render(<AIProposalPanel projectId="project" can={() => true} onDirty={() => {}} />);
  await screen.findByRole('option', { name: '产品规格 · ready' });
  fireEvent.change(screen.getByLabelText('生成记录'), { target: { value: 'p1' } });
  fireEvent.click(await screen.findByRole('button', { name: '关系 21' }));
  expect(screen.getAllByRole('button', { name: '编辑关系候选' })).toHaveLength(20);
  fireEvent.click(screen.getByRole('button', { name: '下一批关系' }));
  expect(screen.getAllByRole('button', { name: '编辑关系候选' })).toHaveLength(1);
  fireEvent.click(screen.getByLabelText('采纳关系 r20'));
  await waitFor(() => expect(submitted).toBeTruthy());
  const updated = submitted!.relations as Array<{ id: string; selected: boolean }>;
  expect(updated[0].selected).toBe(true);
  expect(updated[20].selected).toBe(false);
});

it('resets the candidate batch when starting another proposal after viewing a later batch', async () => {
  const old = { ...ready, candidates: Array.from({ length: 21 }, (_, i) => ({ ...ready.candidates[0], id: `old-${i}`, properties: { name: `合成旧记录${i}` } })) };
  const fresh = { ...ready, id: 'fresh', candidates: [{ ...ready.candidates[0], id: 'fresh-record' }], job: { id: 'job', status: 'succeeded' } };
  mocks((path, options) => {
    if (path === '/api/ai/proposals/p1') return json(old);
    if (path === '/api/ai/proposals' && options?.method === 'POST') return json({ ...fresh, status: 'pending', job: { id: 'job', status: 'pending' } });
    if (path === '/api/ai/proposals/fresh') return json(fresh);
  });
  render(<AIProposalPanel projectId="project" can={() => true} onDirty={() => {}} preferredDocumentId="d1" />);
  await screen.findByRole('option', { name: '产品规格 · ready' });
  fireEvent.change(screen.getByLabelText('生成记录'), { target: { value: 'p1' } });
  fireEvent.click(await screen.findByRole('button', { name: '实例 21' }));
  fireEvent.click(screen.getByRole('button', { name: '下一批' }));
  expect(screen.getByLabelText('采纳候选 old-20')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: '起草新模型' }));
  vi.useFakeTimers();
  try {
    await act(async () => fireEvent.click(screen.getByRole('button', { name: '自动理解并起草' })));
    await act(async () => { await vi.advanceTimersByTimeAsync(2500); });
    expect(screen.getByRole('button', { name: '实例 1' })).toBeTruthy();
    expect(screen.getByLabelText('采纳候选 fresh-record')).toBeTruthy();
    expect(screen.queryByRole('button', { name: '上一批' })).toBeNull();
  } finally { vi.useRealTimers(); }
});

it.each([undefined, 'snapshot-old', 'native'])('opens ontology candidate evidence at its original page and native parse binding (%s)', async nativeParseId => {
  const pathSuffix = nativeParseId ? `&native_parse_id=${nativeParseId}` : '';
  const fetch = mocks(path => path === '/api/ai/proposals/p1' ? json({ ...ready, candidates: [{ ...ready.candidates[0], evidence: [{ ...evidence[0], native_parse_id: nativeParseId }] }] }) : path === `/api/documents/d1/pages?limit=1&offset=1${pathSuffix}` ? json({ items: [{ page_number: 2, label: '第二页', text: '第二页合成原文', elements: [], status: 'parsed', has_image: false }], total: 3, revision: 1, content_hash: 'hash' }) : undefined);
  render(<AIProposalPanel projectId="project" can={() => true} onDirty={() => {}} />);
  await screen.findByRole('option', { name: '产品规格 · ready' });
  fireEvent.change(screen.getByLabelText('生成记录'), { target: { value: 'p1' } });
  fireEvent.click(await screen.findByRole('button', { name: '实例 1' }));
  fireEvent.click(screen.getByRole('button', { name: /^来源 · 第 2 页/ }));
  await screen.findByText('第二页合成原文');
  expect(fetch.mock.calls.some(([path]) => String(path).includes('/parses/'))).toBe(false);
});

function mocks(handler?: (path: string, options?: RequestInit) => Response | Promise<Response> | undefined) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, options) => {
    const path = String(input), handled = handler?.(path, options);
    if (handled) return handled;
    if (path.includes('/model-connections')) return json({ items: [connection], total: 1 });
    if (path.includes('/documents?')) return json({ items: [{ id: 'd1', filename: '合成资料.pdf', state: 'ready' }], total: 1 });
    if (path.includes('/ontology/definition')) return json({ definition, release_id: 'r1' });
    if (path.includes('/ai/proposals?')) return json({ items: [ready] });
    if (path === '/api/ai/proposals/p1') return json(ready);
    return json({ items: [], total: 0 });
  });
}

it('submits the selected document, explicit pages and user goal without writing a model directly', async () => {
  let submitted: Record<string, unknown> | undefined;
  const fetch = mocks((path, options) => {
    if (path === '/api/ai/proposals' && options?.method === 'POST') { submitted = JSON.parse(String(options.body)); return json({ ...ready, status: 'pending' }); }
  });
  render(<AIProposalPanel projectId="project" can={() => true} onDirty={() => {}} preferredDocumentId="d1" />);
  await screen.findByRole('option', { name: '本地合成服务' });
  fireEvent.change(screen.getByLabelText('你希望理解什么？（可选）'), { target: { value: '只提取有证据的产品规格' } });
  fireEvent.change(screen.getByLabelText('文档 d1 的页码范围'), { target: { value: '2-3,8' } });
  fireEvent.click(screen.getByRole('button', { name: '按需求生成候选' }));
  await waitFor(() => expect(submitted).toMatchObject({ project_id: 'project', connection_id: 'm1', document_ids: ['d1'], instructions: '只提取有证据的产品规格', pages: { d1: [2, 3, 8] }, budget_cny: 20 }));
  expect(fetch.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(1);
});

it('edits candidate values and business keys using its current revision, preserving original evidence', async () => {
  let submitted: Record<string, unknown> | undefined;
  mocks((path, options) => {
    if (path.endsWith('/ai/proposals/p1') && options?.method === 'PATCH') { submitted = JSON.parse(String(options.body)); return json({ ...ready, ...submitted, revision: 5 }); }
  });
  render(<AIProposalPanel projectId="project" can={() => true} onDirty={() => {}} />);
  await screen.findByRole('option', { name: '产品规格 · ready' });
  fireEvent.change(screen.getByLabelText('生成记录'), { target: { value: 'p1' } });
  fireEvent.click(await screen.findByRole('button', { name: '实例 1' }));
  fireEvent.click(screen.getByRole('button', { name: '编辑此候选' }));
  fireEvent.change(screen.getByRole('textbox', { name: '名称' }), { target: { value: '修正的产品名称' } });
  fireEvent.change(screen.getByRole('textbox', { name: '型号' }), { target: { value: 'A-01' } });
  fireEvent.click(screen.getByRole('button', { name: '保存候选' }));
  await waitFor(() => expect(submitted?.expected_revision).toBe(4));
  expect(submitted?.candidates).toEqual([{ ...ready.candidates[0], properties: { name: '修正的产品名称', code: 'A-01' }, business_key: { code: 'A-01' }, evidence }]);
});

it('does not request candidate data for a read-only user and clears it after access is revoked', async () => {
  let denied = false;
  const fetch = mocks((path) => path.endsWith('/ai/proposals/p1') && denied ? json({ error: { message: '权限已撤销' } }, 403) : undefined);
  const props = { projectId: 'project', can: () => false, onDirty: () => {} };
  const view = render(<AIProposalPanel {...props} />);
  expect(fetch).not.toHaveBeenCalled();
  view.rerender(<AIProposalPanel {...props} can={() => true} />);
  await screen.findByRole('option', { name: '产品规格 · ready' });
  fireEvent.change(screen.getByLabelText('生成记录'), { target: { value: 'p1' } });
  await screen.findByText('候选模型图');
  denied = true;
  fireEvent.click(screen.getByRole('button', { name: '刷新' }));
  await screen.findByText('权限已撤销');
  expect(screen.queryByText('候选模型图')).toBeNull();
});

it('keeps the selected material scope when the original preview switches to another document', async () => {
  let submitted: Record<string, unknown> | undefined;
  mocks((path, options) => {
    if (path.includes('/documents?')) return json({ items: [{ id: 'd1', filename: '资料一.pdf' }, { id: 'd2', filename: '资料二.pdf' }], total: 2 });
    if (path === '/api/ai/proposals' && options?.method === 'POST') { submitted = JSON.parse(String(options.body)); return json({ ...ready, status: 'pending' }); }
  });
  const props = { projectId: 'project', can: () => true, onDirty: () => {} };
  const view = render(<AIProposalPanel {...props} preferredDocumentId="d1" />);
  fireEvent.click(await screen.findByLabelText(/资料二.pdf/));
  view.rerender(<AIProposalPanel {...props} preferredDocumentId="d2" />);
  fireEvent.click(screen.getByRole('button', { name: '自动理解并起草' }));
  await waitFor(() => expect(submitted?.document_ids).toEqual(['d1', 'd2']));
});

it('atomically excludes incident relations when deselecting an instance and does not silently reselect them', async () => {
  const writes: Record<string, unknown>[] = [];
  let current = { ...ready, status: 'adopted', changeset_id: 'change', candidates: [...ready.candidates, { ...ready.candidates[0], id: 'c2' }], relations: [{ id: 'edge', source_candidate_id: 'c1', target_candidate_id: 'c2', key: 'related', selected: true, evidence }] };
  mocks((path, options) => {
    if (path.endsWith('/ai/proposals/p1')) {
      if (options?.method === 'PATCH') { const body = JSON.parse(String(options.body)); writes.push(body); current = { ...current, ...body, revision: current.revision + 1 }; }
      return json(current);
    }
  });
  render(<AIProposalPanel projectId="project" can={() => true} onDirty={() => {}} />);
  await screen.findByRole('option', { name: '产品规格 · ready' });
  fireEvent.change(screen.getByLabelText('生成记录'), { target: { value: 'p1' } });
  fireEvent.click(await screen.findByRole('button', { name: '实例 2' }));
  fireEvent.click(screen.getByLabelText('采纳候选 c1'));
  await waitFor(() => expect(writes).toHaveLength(1));
  expect(writes[0].relations).toEqual([{ ...current.relations[0], selected: false }]);
  await waitFor(() => expect((screen.getByRole('button', { name: '全部排除' }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button', { name: '全部排除' }));
  await waitFor(() => expect(writes).toHaveLength(2));
  expect((writes[1].candidates as { selected: boolean }[]).every(item => !item.selected)).toBe(true);
  await waitFor(() => expect((screen.getByRole('button', { name: '全选' }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button', { name: '全选' }));
  await waitFor(() => expect(writes).toHaveLength(3));
  expect(writes[2].relations).toEqual([{ ...current.relations[0], selected: false }]);
});

it.each(['ready', 'adopted'])('binds edits to the opened %s revision even after a refresh', async status => {
  let current = { ...ready, status }, submitted: Record<string, unknown> | undefined;
  mocks((path, options) => {
    if (!path.endsWith('/ai/proposals/p1')) return;
    if (options?.method === 'PATCH') { submitted = JSON.parse(String(options.body)); return json({ error: { message: '候选版本已改变' } }, 409); }
    return json(current);
  });
  render(<AIProposalPanel projectId="project" can={() => true} onDirty={() => {}} />);
  await screen.findByRole('option', { name: '产品规格 · ready' });
  fireEvent.change(screen.getByLabelText('生成记录'), { target: { value: 'p1' } });
  fireEvent.click(await screen.findByRole('button', { name: '实例 1' }));
  fireEvent.click(screen.getByRole('button', { name: '编辑此候选' }));
  fireEvent.change(screen.getByRole('textbox', { name: '名称' }), { target: { value: '未覆盖的本地编辑' } });
  current = { ...current, revision: 5 };
  fireEvent.click(screen.getByRole('button', { name: '刷新' }));
  await screen.findByText('修订 5');
  fireEvent.click(screen.getByRole('button', { name: '保存候选' }));
  await waitFor(() => expect(submitted?.expected_revision).toBe(4));
  expect((screen.getByRole('textbox', { name: '名称' }) as HTMLTextAreaElement).value).toContain('未覆盖的本地编辑');
});

it('uses the adopted review model and freezes its separate changeset revision', async () => {
  let current = { ...ready, status: 'adopted', changeset_id: 'change', review_changeset_revision: 3, review_definition: { ...definition, types: [{ ...definition.types[0], label: '审核中的产品' }] } };
  let submitted: Record<string, unknown> | undefined;
  mocks((path, options) => {
    if (!path.endsWith('/ai/proposals/p1')) return;
    if (options?.method === 'PATCH') { submitted = JSON.parse(String(options.body)); return json({ error: { message: '模型变更集修订已变化' } }, 409); }
    return json(current);
  });
  render(<AIProposalPanel projectId="project" can={() => true} onDirty={() => {}} />);
  await screen.findByRole('option', { name: '产品规格 · ready' });
  fireEvent.change(screen.getByLabelText('生成记录'), { target: { value: 'p1' } });
  fireEvent.click(await screen.findByRole('button', { name: '实例 1' }));
  fireEvent.click(screen.getByRole('button', { name: '编辑此候选' }));
  expect(screen.getByRole('option', { name: '审核中的产品' })).toBeTruthy();
  current = { ...current, review_changeset_revision: 4 };
  fireEvent.click(screen.getByRole('button', { name: '刷新' }));
  await waitFor(() => expect((screen.getByRole('button', { name: '保存候选' }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button', { name: '保存候选' }));
  await waitFor(() => expect(submitted).toMatchObject({ expected_revision: 4, expected_changeset_revision: 3 }));
});


it('clears an open candidate and evidence when a protected refresh is denied', async () => {
  let denied = false;
  mocks(path => denied && path.endsWith('/ai/proposals/p1') ? json({ error: { message: '提案访问已撤销' } }, 403) : undefined);
  render(<AIProposalPanel projectId="project" can={() => true} onDirty={() => {}} />);
  await screen.findByRole('option', { name: '产品规格 · ready' });
  fireEvent.change(screen.getByLabelText('生成记录'), { target: { value: 'p1' } });
  fireEvent.click(await screen.findByRole('button', { name: '实例 1' }));
  fireEvent.click(screen.getByRole('button', { name: '编辑此候选' }));
  expect(screen.getByRole('textbox', { name: '名称' })).toBeTruthy();
  denied = true;
  fireEvent.click(screen.getByRole('button', { name: '刷新' }));
  await screen.findByText('提案访问已撤销');
  expect(screen.queryByRole('textbox', { name: '名称' })).toBeNull();
  expect(screen.queryByText('合成产品 A')).toBeNull();
});

it('clears cached candidate editors when permissions are removed and does not resurrect them', async () => {
  mocks();
  const props = { projectId: 'project', onDirty: vi.fn() };
  const view = render(<AIProposalPanel {...props} can={() => true} />);
  await screen.findByRole('option', { name: '产品规格 · ready' });
  fireEvent.change(screen.getByLabelText('生成记录'), { target: { value: 'p1' } });
  fireEvent.click(await screen.findByRole('button', { name: '实例 1' }));
  fireEvent.click(screen.getByRole('button', { name: '编辑此候选' }));
  view.rerender(<AIProposalPanel {...props} can={() => false} />);
  view.rerender(<AIProposalPanel {...props} can={() => true} />);
  expect(screen.queryByRole('textbox', { name: '名称' })).toBeNull();
  expect(screen.queryByText('合成产品 A')).toBeNull();
});

it('prevents candidate changes or dismissal while the captured revision is saving', async () => {
  let finish!: (response: Response) => void;
  mocks((path, options) => path.endsWith('/ai/proposals/p1') && options?.method === 'PATCH' ? new Promise(resolve => { finish = resolve; }) : undefined);
  render(<AIProposalPanel projectId="project" can={() => true} onDirty={() => {}} />);
  await screen.findByRole('option', { name: '产品规格 · ready' });
  fireEvent.change(screen.getByLabelText('生成记录'), { target: { value: 'p1' } });
  fireEvent.click(await screen.findByRole('button', { name: '实例 1' }));
  fireEvent.click(screen.getByRole('button', { name: '编辑此候选' }));
  fireEvent.click(screen.getByRole('button', { name: '保存候选' }));
  expect((screen.getByRole('textbox', { name: '名称' }) as HTMLTextAreaElement).disabled).toBe(true);
  expect((screen.getByRole('textbox', { name: '型号' }) as HTMLTextAreaElement).disabled).toBe(true);
  expect((screen.getByRole('button', { name: '取消编辑' }) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByRole('button', { name: '关闭侧栏' }) as HTMLButtonElement).disabled).toBe(true);
  finish(json({ ...ready, revision: 5 }));
  await waitFor(() => expect(screen.queryByRole('textbox', { name: '名称' })).toBeNull());
});


it('keeps selected documents and their page ranges across pages without submitting the draft form', async () => {
  let submitted: Record<string, unknown> | undefined;
  const fetch = mocks((path, options) => {
    if (path.includes('/documents?')) {
      const offset = Number(new URL(path, 'http://test').searchParams.get('offset'));
      return json({ items: Array.from({ length: Math.min(25, 51 - offset) }, (_, i) => ({ id: `d${offset + i + 1}`, filename: `资料 ${offset + i + 1}.pdf`, state: 'ready' })), total: 51 });
    }
    if (path === '/api/ai/proposals' && options?.method === 'POST') { submitted = JSON.parse(String(options.body)); return json(ready); }
  });
  render(<AIProposalPanel projectId="project" can={() => true} onDirty={() => {}} />);
  fireEvent.click(await screen.findByRole('checkbox', { name: /资料 1.pdf/ }));
  fireEvent.change(screen.getByLabelText('文档 d1 的页码范围'), { target: { value: '2-3' } });
  fireEvent.click(within(screen.getByRole('group', { name: '文档列表分页' })).getByRole('button', { name: '下一页' }));
  fireEvent.click(await screen.findByRole('checkbox', { name: /资料 26.pdf/ }));
  fireEvent.click(within(screen.getByRole('group', { name: '文档列表分页' })).getByRole('button', { name: '下一页' }));
  fireEvent.click(await screen.findByRole('checkbox', { name: /资料 51.pdf/ }));
  fireEvent.click(within(screen.getByRole('group', { name: '文档列表分页' })).getByRole('button', { name: '上一页' }));
  expect((await screen.findByRole('checkbox', { name: /资料 26.pdf/ })) as HTMLInputElement).toHaveProperty('checked', true);
  fireEvent.click(within(screen.getByRole('group', { name: '文档列表分页' })).getByRole('button', { name: '上一页' }));
  expect((await screen.findByRole('checkbox', { name: /资料 1.pdf/ })) as HTMLInputElement).toHaveProperty('checked', true);
  expect((screen.getByLabelText('文档 d1 的页码范围') as HTMLInputElement).value).toBe('2-3');
  expect(screen.getByText('选择可用材料 · 已选 3 份文档')).toBeTruthy();
  expect(fetch.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(0);
  fireEvent.click(screen.getByRole('button', { name: '自动理解并起草' }));
  await waitFor(() => expect(submitted).toMatchObject({ document_ids: ['d1', 'd26', 'd51'], pages: { d1: [2, 3] } }));
});

it('opens proposals on later pages while keeping the current proposal selected during browsing', async () => {
  const fetch = mocks(path => {
    if (path.includes('/ai/proposals?')) {
      const offset = Number(new URL(path, 'http://test').searchParams.get('offset'));
      return json({ items: Array.from({ length: Math.min(25, 26 - offset) }, (_, i) => ({ ...ready, id: `p${offset + i + 1}`, title: `记录 ${offset + i + 1}` })), total: 26 });
    }
    if (path === '/api/ai/proposals/p26') return json({ ...ready, id: 'p26', title: '记录 26', revision: 26 });
    if (path === '/api/ai/proposals/p1') return json({ ...ready, title: '记录 1' });
  });
  render(<AIProposalPanel projectId="project" can={() => true} onDirty={() => {}} />);
  await screen.findByRole('option', { name: '记录 1 · ready' });
  fireEvent.change(screen.getByLabelText('生成记录'), { target: { value: 'p1' } });
  await screen.findByText('修订 4');
  fireEvent.click(within(screen.getByRole('group', { name: '生成记录分页' })).getByRole('button', { name: '下一页' }));
  await screen.findByRole('option', { name: '记录 26 · ready' });
  expect((screen.getByLabelText('生成记录') as HTMLSelectElement).value).toBe('p1');
  fireEvent.change(screen.getByLabelText('生成记录'), { target: { value: 'p26' } });
  await screen.findByText('修订 26');
  expect(fetch.mock.calls.some(([path]) => path === '/api/ai/proposals/p26')).toBe(true);
});

it('clears cross-page document selections when a page read loses access', async () => {
  mocks(path => {
    if (path.includes('/documents?')) return path.includes('offset=25') ? json({ error: { message: '资料权限已撤销' } }, 403) : json({ items: [{ id: 'd1', filename: '先前授权资料.pdf' }], total: 26 });
  });
  render(<AIProposalPanel projectId="project" can={() => true} onDirty={() => {}} />);
  fireEvent.click(await screen.findByRole('checkbox', { name: /先前授权资料.pdf/ }));
  fireEvent.click(within(screen.getByRole('group', { name: '文档列表分页' })).getByRole('button', { name: '下一页' }));
  await screen.findByText('资料权限已撤销');
  await waitFor(() => expect(screen.queryByLabelText('文档 d1 的页码范围')).toBeNull());
  expect(screen.queryByText('先前授权资料.pdf')).toBeNull();
  expect((screen.getByRole('button', { name: '自动理解并起草' }) as HTMLButtonElement).disabled).toBe(true);
});


it('blocks import until the server confirms the published prerequisites and refreshes after publishing', async () => {
  let available = false;
  let draft = { id: 'change', project_id: 'project', title: '已采纳草稿', revision: 2, status: 'approved', definition, validation: { valid: true, errors: [] } };
  const fetch = mocks((path, options) => {
    if (path === '/api/ai/proposals/p1') return json({ ...ready, status: 'adopted', changeset_id: 'change', import_ready: available, import_block_reason: available ? null : '请先发布当前绑定草稿' });
    if (path === '/api/changesets/change') return json(draft);
    if (path === '/api/changesets/change/publish') { available = true; draft = { ...draft, revision: 3, status: 'published' }; return json(draft); }
  });
  render(<AIProposalPanel projectId="project" can={() => true} onDirty={() => {}} />);
  await screen.findByRole('option', { name: '产品规格 · ready' });
  fireEvent.change(screen.getByLabelText('生成记录'), { target: { value: 'p1' } });
  const button = await screen.findByRole('button', { name: '导入所选实例' }) as HTMLButtonElement;
  expect(button.disabled).toBe(true);
  expect(screen.getByText('请先发布当前绑定草稿')).toBeTruthy();
  fireEvent.click(button);
  expect(fetch.mock.calls.some(([path]) => String(path).endsWith('/import'))).toBe(false);
  fireEvent.click(screen.getByRole('button', { name: '打开模型变更集' }));
  fireEvent.click(await screen.findByRole('button', { name: '发布已批准修订' }));
  await waitFor(() => expect(button.disabled).toBe(false));
  expect(screen.queryByText('请先发布当前绑定草稿')).toBeNull();
});
