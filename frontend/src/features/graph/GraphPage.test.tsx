import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { GraphPage } from './GraphPage';
import type { ChangeSet, Definition, GraphResult } from '../../shared/model/types';

vi.mock('sigma', () => ({ default: class {} }));
vi.mock('@sigma/edge-curve', () => ({ EdgeCurvedArrowProgram: class {} }));

const model: GraphResult = {
  nodes: [{ id: 'asset', label: '设备', type_key: 'asset', kind: 'type', properties: {} }],
  edges: [{ id: 'asset.depends', source: 'asset', target: 'asset', label: '依赖', type_key: 'depends', kind: 'definition' }], truncated: false,
};
const knowledge: GraphResult = {
  nodes: [{ id: 'k1', label: '已发布知识', type_key: 'knowledge', kind: 'knowledge', properties: {} }, { id: 'd1', label: '证据文档', type_key: 'document', kind: 'document', properties: {} }],
  edges: [{ id: 'k1:d1:0', source: 'k1', target: 'd1', label: '引用', type_key: 'citation', kind: 'citation' }], truncated: false,
};
const definition: Definition = {
  types: [
    { key: 'asset', label: '设备', properties: [{ key: 'secret', label: '受限源字段', data_type: 'string', read_roles: ['owner'], source_owned: true }], relations: [{ key: 'depends', label: '依赖', target_type: 'asset', cardinality: 'many' }] },
    { key: 'location', label: '位置', properties: [], relations: [] },
  ],
  mappings: [{ key: 'mapped', source_id: 's', resource_id: 'assets', type_key: 'asset', schema_fingerprint: 'a'.repeat(64), fields: { secret: { source_field: 'raw_secret', transform: 'identity' } } }],
};
beforeEach(() => {
  // Keyboard selection uses the exact API graph while WebGL/layout is verified in a real browser.
  vi.stubGlobal('Worker', class { postMessage() {} terminate() {} });
});

it('requests the selected bounded node budget and explains truncated results', async () => {
  const reads: string[] = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async url => {
    const path = String(url); reads.push(path);
    return new Response(JSON.stringify(path.includes('/graph?') ? { ...model, truncated: true } : { items: [], total: 0 }));
  });
  render(<GraphPage projectId="p" can={() => true} onDirty={() => {}} />);
  const budget = await screen.findByLabelText('图谱节点预算');
  expect(reads.some(url => url.includes('/graph?') && new URL(url, 'http://test').searchParams.get('limit') === '200')).toBe(true);
  fireEvent.change(budget, { target: { value: '500' } });
  await screen.findByText('当前显示有限子图，不代表全部关系。本次节点预算 500，最多载入 1000 条关系。');
  expect(reads.some(url => url.includes('/graph?') && new URL(url, 'http://test').searchParams.get('limit') === '500')).toBe(true);
});

it('edits a graph relation through a complete draft, preserving hidden schema/mappings and CAS approval stages', async () => {
  const writes: { url: string; body: Record<string, unknown> }[] = [];
  let modelReads = 0;
  let draft: ChangeSet;
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, options) => {
    const path = String(url);
    let response: unknown = { items: [], total: 0 };
    if (path.includes('/graph?')) { response = model; if (path.includes('kind=model')) modelReads++; }
    else if (path.includes('/ontology/definition')) response = { definition, version: 2, release_id: 'r2' };
    else if (path.includes('/ontology/types')) response = { items: [{ ...definition.types[0], properties: [] }], total: 1 };
    else if (options?.method && options.method !== 'GET') {
      const body = JSON.parse(String(options.body)); writes.push({ url: path, body });
      if (path === '/api/changesets') draft = { ...body, id: 'c1', revision: 1, status: 'draft' };
      else {
        expect(body.expected_revision).toBe(draft.revision);
        const action = path.split('/').pop();
        draft = { ...draft, ...(action === 'c1' ? body : {}), revision: draft.revision + 1, status: action === 'validate' ? 'validated' : action === 'approve' ? 'approved' : action === 'publish' ? 'published' : 'draft', validation: { valid: true, errors: [] } };
      }
      response = draft;
    }
    return new Response(JSON.stringify(response));
  });
  render(<GraphPage projectId="p" can={() => true} onDirty={() => {}} />);
  fireEvent.click(screen.getByRole('tab', { name: '本体模型' }));
  await screen.findByRole('button', { name: '设备' });
  fireEvent.click(screen.getByText('关系列表（键盘可操作）'));
  fireEvent.click(screen.getByRole('button', { name: '设备 — 依赖 → 设备' }));
  fireEvent.click(screen.getByRole('button', { name: '编辑关系定义（变更集）' }));
  const role = await screen.findByLabelText('业务角色');
  await waitFor(() => expect(document.activeElement).toBe(screen.getByLabelText('关系标识')));
  fireEvent.change(role, { target: { value: '运行依赖' } });
  fireEvent.click(screen.getByRole('button', { name: '保存草稿' }));
  await screen.findByText('草稿已保存。尚未改变已发布模型。');
  expect(writes[0].body.definition).toEqual({ ...definition, types: [{ ...definition.types[0], relations: [{ ...definition.types[0].relations[0], label: '运行依赖' }] }, definition.types[1]] });
  expect(writes[0].body.expected_baseline_release_id).toBe('r2');
  expect(modelReads).toBe(1);
  fireEvent.change(role, { target: { value: '设备依赖' } });
  fireEvent.click(screen.getByRole('button', { name: '保存草稿' }));
  await waitFor(() => expect(writes.length).toBe(2));
  await waitFor(() => expect((screen.getByRole('button', { name: '验证' }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button', { name: '验证' }));
  fireEvent.click(await screen.findByRole('button', { name: '批准当前修订' }));
  fireEvent.click(await screen.findByRole('button', { name: '发布已批准修订' }));
  await screen.findByText('发布成功，已发布模型已刷新。');
  expect(writes.map(w => w.url)).toEqual(['/api/changesets', '/api/changesets/c1', '/api/changesets/c1/validate', '/api/changesets/c1/approve', '/api/changesets/c1/publish']);
  await waitFor(() => expect(modelReads).toBe(2));
});

it('opens a citation in its knowledge editor and retains the draft and revision on a CAS conflict', async () => {
  const patches: unknown[] = [];
  const refs = [{ document_id: 'd1', revision: 2, element_path: 'text/line/1' }];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, options) => {
    const path = String(url);
    let response: unknown = { items: [], total: 0 };
    if (path.includes('/graph?')) response = knowledge;
    else if (path === '/api/knowledge/k1' && options?.method === 'PATCH') {
      patches.push(JSON.parse(String(options.body)));
      return new Response(JSON.stringify({ error: { code: 'revision_conflict', message: '其他人已更新页面' } }), { status: 409 });
    } else if (path === '/api/knowledge/k1') response = { id: 'k1', title: '最新草稿', body: '未发布内容', revision: 3, published_revision: 1, references: refs };
    return new Response(JSON.stringify(response));
  });
  render(<GraphPage projectId="p" can={() => true} onDirty={() => {}} />);
  fireEvent.click(screen.getByRole('tab', { name: '文档与知识' }));
  await screen.findByRole('button', { name: '已发布知识' });
  fireEvent.click(screen.getByText('关系列表（键盘可操作）'));
  fireEvent.click(screen.getByRole('button', { name: '已发布知识 — 引用 → 证据文档' }));
  fireEvent.click(screen.getByRole('button', { name: '编辑所属知识页与引用' }));
  const body = await screen.findByLabelText(/正文（Markdown）/);
  expect((screen.getByLabelText('标题') as HTMLInputElement).value).toBe('最新草稿');
  fireEvent.change(body, { target: { value: '保留我的修改' } });
  fireEvent.click(screen.getByRole('button', { name: '保存草稿' }));
  await screen.findByRole('alert');
  expect(patches).toEqual([{ title: '最新草稿', body: '保留我的修改', references: refs, expected_revision: 3 }]);
  expect((body as HTMLTextAreaElement).value).toBe('保留我的修改');
  expect(screen.queryByRole('button', { name: '发布当前修订' })).toBeNull();
  expect((screen.getByRole('tab', { name: '本体模型' }) as HTMLButtonElement).disabled).toBe(true);
});

it('does not expose model editing without permission or treat document nodes as editable knowledge pages', async () => {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async url => new Response(JSON.stringify(String(url).includes('/graph?') ? (String(url).includes('kind=knowledge') ? knowledge : model) : { items: [], total: 0 })));
  render(<GraphPage projectId="p" can={() => false} onDirty={() => {}} />);
  fireEvent.click(screen.getByRole('tab', { name: '本体模型' }));
  fireEvent.click(await screen.findByRole('button', { name: '设备' }));
  expect(screen.queryByRole('button', { name: '编辑类型（变更集）' })).toBeNull();
  fireEvent.click(screen.getByRole('tab', { name: '文档与知识' }));
  fireEvent.click(await screen.findByRole('button', { name: '证据文档' }));
  expect(screen.queryByRole('button', { name: '编辑知识页与引用' })).toBeNull();
  expect(screen.queryByRole('button', { name: '查看知识页' })).toBeNull();
});

it('keeps graph-owned authorized counts while refusing metadata from a different release', async () => {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async url => new Response(JSON.stringify(String(url).includes('/graph?') ? { ...model, release_id: 'r1', version: 1, nodes: [{ ...model.nodes[0], type_label: '设备', visible_property_count: 2 }] } : { items: [{ ...definition.types[0], label: '新模型标签', description: '新模型解释不可混入', properties: [{ key: 'private', label: '新模型私有字段', data_type: 'string' }] }], release_id: 'r2', total: 1 })));
  render(<GraphPage projectId="p" can={() => false} onDirty={() => {}} />);
  fireEvent.click(screen.getByRole('tab', { name: '本体模型' }));
  fireEvent.click(await screen.findByRole('button', { name: '设备' }));
  const inspector = document.querySelector('.graph-inspector')! as HTMLElement;
  expect(within(inspector).getByText('2')).toBeTruthy();
  expect(within(inspector).queryByText('等待同版本模型')).toBeNull();
  expect(screen.queryByText('新模型解释不可混入')).toBeNull();
  expect(screen.queryByText('新模型私有字段')).toBeNull();
  expect(screen.queryByText('新模型标签')).toBeNull();
  expect(screen.getByText(/图与字段模型版本尚未对齐/)).toBeTruthy();
});

it('applies type and search filters to the visible counts and keyboard lists with a reset', async () => {
  const instances = { nodes: [{ id: 'a', label: '星图项目', type_key: 'project', type_label: '项目', kind: 'object', properties: {} }, { id: 'b', label: '林悦', type_key: 'person', type_label: '人员', kind: 'object', properties: {} }], edges: [{ id: 'e', source: 'b', target: 'a', label: '负责', type_key: 'owns', kind: 'relation' }], truncated: false, release_id: 'r1' };
  vi.spyOn(globalThis, 'fetch').mockImplementation(async url => new Response(JSON.stringify(String(url).includes('/graph?') ? instances : { items: [], total: 0, release_id: 'r1' })));
  render(<GraphPage projectId="p" can={() => false} onDirty={() => {}} />);
  await screen.findByRole('button', { name: '林悦' });
  fireEvent.click(screen.getByRole('button', { name: '人员1' }));
  expect(screen.queryByRole('button', { name: '林悦' })).toBeNull();
  expect(screen.getByRole('status').textContent).toContain('当前显示 1 个节点 · 0 条关系');
  fireEvent.change(screen.getByLabelText('在当前子图查找'), { target: { value: '无匹配' } });
  expect(screen.getByRole('status').textContent).toContain('当前显示 0 个节点 · 0 条关系');
  fireEvent.click(screen.getByRole('button', { name: '重置筛选' }));
  expect(screen.getByRole('button', { name: '林悦' })).toBeTruthy();
  expect(screen.getByRole('status').textContent).toContain('当前显示 2 个节点 · 1 条关系');
});
