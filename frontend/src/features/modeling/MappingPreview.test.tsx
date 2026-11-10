import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { MappingPreview } from './MappingPreview';
import { MappingEditor } from './MappingEditor';
import type { ChangeSet, MappingDef } from '../../shared/model/types';

const mapping: MappingDef = { key: 'precision', source_id: 'source', resource_id: 'rows', type_key: 'item', schema_fingerprint: 'fingerprint', fields: { code: { source_field: 'id', transform: 'integer' } } };
const draft: ChangeSet = { id: 'change', project_id: 'p', title: '预览草稿', revision: 3, status: 'draft', baseline_release_id: 'release', definition: { types: [], mappings: [mapping] } };
const snapshot = { id: 'snap', source_id: 'source', source_revision: 2, resource_id: 'rows', schema_fingerprint: 'fingerprint', attempt: 3, status: 'complete', record_count: 2, generation: 1, release_id: 'older-release' };
const exact = '9223372036854775807';
const records = Array.from({ length: 25 }, (_, i) => ({ id: `r${i + 1}`, source_key_json: `{"id":${exact}}`, values_json: `{"id":${exact}}` }));
const result = {
  binding: { changeset_id: draft.id, changeset_revision: 3, mapping_key: mapping.key, snapshot_id: snapshot.id, snapshot_attempt: 3, snapshot_status: 'complete', source_id: 'source', source_revision: 2, resource_id: 'rows', schema_fingerprint: 'fingerprint', mapping_fingerprint: 'mapping-hash', snapshot_release_id: 'older-release' },
  rows: [{ record_id: 'r1', valid: true, fields: [{ target_field: 'code', source_field: 'id', transform: 'integer', source_state: 'value', raw_value_json: exact, transformed_value_json: exact, validated_value_json: exact, errors: [] }], properties_json: `{"code":${exact}}`, errors: [] }],
  summary: { scope: 'selected_records_only', selected_count: 1, valid_count: 1, invalid_count: 0, writes: 0, capture_complete: true },
};
const props = { mapping, context: { draft, dirty: false, busy: false }, sourceRevision: 2, sourceLoading: false, onRefreshSource: () => {} };
function mockApi(onPreview: (body: Record<string, unknown>) => Promise<Response> | Response, state = snapshot, count = 2) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, options) => {
    if (String(url) === '/api/mappings/preview') return onPreview(JSON.parse(String(options?.body)));
    return new Response(JSON.stringify(String(url).includes('/records') ? { snapshot: state, items: records.slice(0, count), total: count } : { items: [state, { ...state, id: 'other-resource', resource_id: 'elsewhere' }, { ...state, id: 'pending', status: 'capturing' }], total: 3 }));
  });
}
async function choose() {
  await waitFor(() => expect((screen.getByLabelText('用于预览的持久快照') as HTMLSelectElement).disabled).toBe(false));
  fireEvent.change(screen.getByLabelText('用于预览的持久快照'), { target: { value: 'snap' } });
  fireEvent.click(await screen.findByRole('checkbox', { name: '选择记录 r1' }));
}

it('requires explicit record selection and binds the saved change revision/current attempt while preserving exact numeric text', async () => {
  const writes: unknown[] = [];
  mockApi(body => { writes.push(body); return new Response(JSON.stringify(result)); });
  const view = render(<MappingPreview {...props} />);
  expect((screen.getByRole('button', { name: '预览选中记录' }) as HTMLButtonElement).disabled).toBe(true);
  await choose(); fireEvent.click(screen.getByRole('button', { name: '预览选中记录' }));
  expect((await screen.findByLabelText('目标值')).textContent).toBe(exact);
  expect(screen.getByLabelText('源值').textContent).toBe(exact);
  expect(writes).toEqual([{ changeset_id: 'change', expected_revision: 3, mapping_key: 'precision', snapshot_id: 'snap', expected_snapshot_attempt: 3, record_ids: ['r1'] }]);
  expect(screen.queryByText(/9223372036854776000/)).toBeNull();
  view.rerender(<MappingPreview {...props} context={{ ...props.context, dirty: true }} />);
  expect(screen.queryByLabelText('当前映射预览结果')).toBeNull();
  await screen.findByText(/旧预览已失效/);
  expect((screen.getByRole('button', { name: '预览选中记录' }) as HTMLButtonElement).disabled).toBe(true);
  view.rerender(<MappingPreview {...props} context={{ ...props.context, draft: { ...draft, revision: 4 } }} />);
  expect(screen.queryByLabelText('当前映射预览结果')).toBeNull();
});

it('discards a late preview when mapping inputs change and does not automatically send a replacement', async () => {
  let resolve: (value: Response) => void = () => {};
  const api = mockApi(() => new Promise<Response>(done => { resolve = done; }));
  const view = render(<MappingPreview {...props} />); await choose();
  fireEvent.click(screen.getByRole('button', { name: '预览选中记录' }));
  view.rerender(<MappingPreview {...props} mapping={{ ...mapping, fields: { code: { source_field: 'other', transform: 'integer' } } }} context={{ ...props.context, dirty: true }} />);
  resolve(new Response(JSON.stringify(result)));
  await screen.findByText(/旧预览已失效/);
  expect(screen.queryByLabelText('当前映射预览结果')).toBeNull();
  expect(api.mock.calls.filter(([url]) => String(url).includes('/mappings/preview'))).toHaveLength(1);
});

it('keeps a conflict explicit and requires renewed dependency selection rather than retrying the stale revision', async () => {
  const api = mockApi(() => new Response(JSON.stringify({ error: { code: 'revision_conflict', message: '草稿修订已改变' } }), { status: 409 }));
  render(<MappingPreview {...props} />); await choose();
  fireEvent.click(screen.getByRole('button', { name: '预览选中记录' }));
  await screen.findByRole('alert'); screen.getByText(/预览绑定已过期/);
  expect((screen.getByRole('checkbox', { name: '选择记录 r1' }) as HTMLInputElement).checked).toBe(true);
  expect((screen.getByRole('button', { name: '预览选中记录' }) as HTMLButtonElement).disabled).toBe(true);
  expect(api.mock.calls.filter(([url]) => String(url).includes('/mappings/preview'))).toHaveLength(1);
});

it('labels failed-attempt samples as diagnostics and shows per-field conversion errors without target values', async () => {
  mockApi(() => new Response(JSON.stringify({ ...result, binding: { ...result.binding, snapshot_status: 'failed' }, summary: { ...result.summary, valid_count: 0, invalid_count: 1, capture_complete: false }, rows: [{ ...result.rows[0], valid: false, properties_json: null, fields: [{ ...result.rows[0].fields[0], raw_value_json: '"not an integer"', transformed_value_json: null, validated_value_json: null, errors: [{ stage: 'transform', code: 'invalid_integer', message: '无法转换为整数' }] }] }] })), { ...snapshot, status: 'failed' });
  render(<MappingPreview {...props} />); await choose();
  screen.getByText(/失败采集的已保存样本，仅诊断/);
  fireEvent.click(screen.getByRole('button', { name: '预览选中记录' }));
  await screen.findByText(/无法转换为整数/); screen.getByText(/仅失败\/部分采集的选中样本/);
  expect(screen.queryByLabelText('目标值')).toBeNull();
});

it('limits explicit selection to twenty records and rejects source dependency changes', async () => {
  mockApi(() => new Response(JSON.stringify(result)), snapshot, 25);
  const view = render(<MappingPreview {...props} />); await choose();
  for (let i = 2; i <= 20; i++) fireEvent.click(screen.getByLabelText(`选择记录 r${i}`, { exact: true }));
  expect((screen.getByLabelText('选择记录 r21', { exact: true }) as HTMLInputElement).disabled).toBe(true);
  view.rerender(<MappingPreview {...props} sourceRevision={3} />);
  await screen.findByText(/快照尝试、来源修订或结构与当前映射不一致/);
  expect((screen.getByRole('button', { name: '预览选中记录' }) as HTMLButtonElement).disabled).toBe(true);
});

it('does not fetch preview dependencies without both authoring and source permission', async () => {
  const api = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ items: [], total: 0 })));
  render(<MappingEditor projectId="p" types={[]} value={[mapping]} disabled onChange={() => {}} canReadSources={false} preview={props.context} />);
  expect(screen.queryByLabelText(/的记录预览/)).toBeNull();
  expect(api).not.toHaveBeenCalled();
});
