import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { FunctionsPage, type FunctionVersion } from './FunctionsPage';

const type = { key: 'stock', label: '库存指标', properties: ['demand', 'inventory', 'reserved'].map(key => ({ key, label: key, data_type: 'number', unit: '件' })), relations: [] };
const version: FunctionVersion = { id: 'f1', project_id: 'p', key: 'shortage', label: '库存缺口', version: 1, revision: 3, status: 'published', kernel_key: 'inventory_gap', kernel_version: 1, unit: '件', baseline_release_id: 'r1', author_id: 'author', reviewed_by: 'reviewer', snapshot_hash: 'approved-hash', reviewed_hash: 'approved-hash', artifact: { key: 'inventory_gap', version: 1 }, bindings: { demand: { type_key: 'stock', field: 'demand' }, inventory: { type_key: 'stock', field: 'inventory' }, reserved: { type_key: 'stock', field: 'reserved' } } };
const catalog = { items: [{ key: 'inventory_gap', version: 1, self_tests: { passed: 3 } }], user_code_execution: false };

it('registers only typed field bindings against the loaded release and hides self review', async () => {
  let body: Record<string, unknown> | undefined;
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, options) => {
    const path = String(url); let result: unknown = { items: [], next_offset: null };
    if (path.includes('/functions/catalog')) result = catalog;
    else if (path.includes('/ontology/types')) result = { items: [type], total: 1, release_id: 'r1', version: 1 };
    else if (path === '/api/functions' && options?.method === 'POST') { body = JSON.parse(String(options.body)); result = { ...version, ...body, status: 'draft', revision: 1, author_id: 'me', reviewed_by: null }; }
    return new Response(JSON.stringify(result));
  });
  render(<FunctionsPage projectId="p" userId="me" can={() => true} onDirty={() => {}} />);
  await waitFor(() => expect((screen.getByRole('button', { name: '登记库存缺口函数' }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button', { name: '登记库存缺口函数' }));
  fireEvent.change(await screen.findByLabelText('函数稳定标识'), { target: { value: 'shortage' } });
  fireEvent.change(screen.getByLabelText('函数名称'), { target: { value: '库存缺口' } });
  fireEvent.change(screen.getByLabelText('共同单位'), { target: { value: '件' } });
  for (const [label, key] of [['需求量', 'demand'], ['库存量', 'inventory'], ['预留量', 'reserved']]) {
    fireEvent.change(screen.getByLabelText(label + '类型'), { target: { value: 'stock' } });
    fireEvent.change(screen.getByLabelText(label + '字段'), { target: { value: key } });
  }
  fireEvent.click(screen.getByRole('button', { name: '保存函数草稿' }));
  await screen.findByText('函数配置已保存为草稿，需要另一位审核人员审阅。修改会撤销旧审阅。');
  expect(body).toEqual({ project_id: 'p', key: 'shortage', label: '库存缺口', kernel_key: 'inventory_gap', kernel_version: 1, unit: '件', baseline_release_id: 'r1', bindings: version.bindings });
  expect(screen.queryByRole('button', { name: '审阅当前配置' })).toBeNull();
  expect(screen.queryByRole('button', { name: '发布已审阅版本' })).toBeNull();
});

it('runs using selected object revisions without retrying conflicts and displays exact server results and evidence', async () => {
  const runs: unknown[] = [];
  const object = { id: 'o1', type_key: 'stock', revision: 7, properties: { name: '库存甲', demand: '0.3', inventory: '0.3', reserved: '0.2' } };
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, options) => {
    const path = String(url); let result: unknown = { items: [], next_offset: null };
    if (path.includes('/functions/catalog')) result = catalog;
    else if (path.includes('/ontology/types')) result = { items: [type], total: 1, release_id: 'r1', version: 1 };
    else if (path.includes('/objects?')) result = { items: [object], total: 1 };
    else if (path === '/api/functions/f1/runs' && options?.method === 'POST') {
      runs.push(JSON.parse(String(options.body)));
      if (runs.length === 1) return new Response(JSON.stringify({ error: { code: 'revision_conflict', message: '对象版本已变化' } }), { status: 409 });
      result = { id: 'run1', function_version_id: 'f1', model_release_id: 'r1', request_hash: 'request-hash', artifact_hash: 'kernel-hash', inputs: Object.fromEntries(['demand', 'inventory', 'reserved'].map(name => [name, { object_id: 'o1', revision: 7, type_key: 'stock', field: name, value: object.properties[name as 'demand'], evidence: [{ source_id: 's1', fields: [name] }] }])), result: { kind: 'computed', values: { available: '0.1', gap: '0.2' }, unit: '件', formula: {}, null_policy: 'reject_missing_or_null' } };
    } else if (path === '/api/functions/f1') result = version;
    else if (path.includes('/functions?')) result = { items: [version], next_offset: null };
    return new Response(JSON.stringify(result));
  });
  render(<FunctionsPage projectId="p" userId="reader" can={permission => permission === 'objects:read'} onDirty={() => {}} />);
  fireEvent.click(await screen.findByRole('button', { name: '查看配置与运行' }));
  fireEvent.click(await screen.findByRole('tab', { name: '按对象运行' }));
  await waitFor(() => expect((screen.getByLabelText('需求量输入对象') as HTMLSelectElement).disabled).toBe(false));
  for (const name of ['需求量', '库存量', '预留量']) fireEvent.change(screen.getByLabelText(name + '输入对象'), { target: { value: 'o1' } });
  fireEvent.click(screen.getByRole('button', { name: '按所选修订运行' }));
  await screen.findByRole('alert');
  expect(runs).toEqual([{ expected_function_revision: 3, objects: { demand: { object_id: 'o1', expected_revision: 7 }, inventory: { object_id: 'o1', expected_revision: 7 }, reserved: { object_id: 'o1', expected_revision: 7 } } }]);
  expect((screen.getByLabelText('需求量输入对象') as HTMLSelectElement).value).toBe('o1');
  // A second execution requires a deliberate click; no automatic retry or refresh of the pinned revisions.
  fireEvent.click(screen.getByRole('button', { name: '按所选修订运行' }));
  await screen.findByRole('heading', { name: '计算结果' });
  expect(runs.length).toBe(2);
  expect(screen.getByText('0.1')).toBeTruthy();
  expect(screen.getByText('0.2')).toBeTruthy();
  expect(screen.getByText('输入修订与证据')).toBeTruthy();
});

it('sends the reviewed revision for an independent reviewer and does not offer arbitrary code fields', async () => {
  const requests: { path: string; body: unknown }[] = [];
  const draft = { ...version, status: 'draft', revision: 1, reviewed_by: undefined, reviewed_hash: undefined };
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, options) => {
    const path = String(url); let result: unknown = { items: [], next_offset: null };
    if (path.includes('/functions/catalog')) result = catalog;
    else if (path.includes('/ontology/types')) result = { items: [type], total: 1, release_id: 'r1', version: 1 };
    else if (path === '/api/functions/f1') result = draft;
    else if (path.includes('/functions?')) result = { items: [draft], next_offset: null };
    else if (options?.method === 'POST') { requests.push({ path, body: JSON.parse(String(options.body)) }); result = { ...draft, status: path.endsWith('/review') ? 'reviewed' : 'published', revision: path.endsWith('/review') ? 2 : 3, reviewed_by: 'reviewer', reviewed_hash: draft.snapshot_hash }; }
    return new Response(JSON.stringify(result));
  });
  render(<FunctionsPage projectId="p" userId="reviewer" can={permission => ['ontology:approve', 'ontology:publish', 'objects:read'].includes(permission)} onDirty={() => {}} />);
  fireEvent.click(await screen.findByRole('button', { name: '查看配置与运行' }));
  fireEvent.click(await screen.findByRole('button', { name: '审阅当前配置' }));
  fireEvent.click(await screen.findByRole('button', { name: '发布已审阅版本' }));
  await screen.findByText('函数版本已发布，可以按对象修订运行。');
  expect(requests).toEqual([{ path: '/api/functions/f1/review', body: { expected_revision: 1 } }, { path: '/api/functions/f1/publish', body: { expected_revision: 2 } }]);
  expect(screen.queryByRole('button', { name: '保存函数草稿' })).toBeNull();
  expect(screen.queryByRole('textbox', { name: /代码/ })).toBeNull();
});

it('reauthorizes history details and does not render a hidden total or rejected evidence', async () => {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async url => {
    const path = String(url); let result: unknown = { items: [], next_offset: null };
    if (path.includes('/functions/catalog')) result = catalog;
    else if (path.includes('/ontology/types')) result = { items: [type], total: 1, release_id: 'r1', version: 1 };
    else if (path === '/api/functions/f1') result = version;
    else if (path.includes('/functions?')) result = { items: [version], next_offset: null };
    else if (path.includes('/functions/f1/runs?')) result = { items: [{ id: 'run1', result: { values: { available: '60', gap: '40' }, unit: '件' } }], next_offset: null, total: 9999 };
    else if (path === '/api/function-runs/run1') return new Response(JSON.stringify({ error: { code: 'forbidden', message: '输入字段当前不可读' } }), { status: 403 });
    return new Response(JSON.stringify(result));
  });
  render(<FunctionsPage projectId="p" userId="reader" can={permission => permission === 'objects:read'} onDirty={() => {}} />);
  fireEvent.click(await screen.findByRole('button', { name: '查看配置与运行' }));
  fireEvent.click(await screen.findByRole('tab', { name: '结果历史' }));
  fireEvent.click(await screen.findByRole('button', { name: '查看结果与证据' }));
  await screen.findByRole('alert');
  expect(screen.queryByRole('heading', { name: '计算结果' })).toBeNull();
  expect(screen.queryByText(/9999/)).toBeNull();
});
