import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { OntologyEditor, OntologyPage } from './OntologyPage';
import type { ChangeSet } from '../../shared/model/types';

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
