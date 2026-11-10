import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { RelationMatcher } from './RelationMatcher';

const types = { release_id: 'release-1', version: 1, items: [
  { key: 'Item', label: '条目', properties: [{ key: 'group', label: '分组编号', data_type: 'string' }], relations: [{ key: 'belongs', label: '所属', target_type: 'Group', cardinality: 'one' }] },
  { key: 'Group', label: '分组', properties: [{ key: 'code', label: '编号', data_type: 'string' }, { key: 'count', label: '数量', data_type: 'integer' }], relations: [] },
], total: 2 };
const report = { plan_hash: 'a'.repeat(64), source_count: 2, target_count: 1, unresolved_count: 1, samples: [], issues: [], semantics: '精确匹配', counts: { create: 1, existing: 0, missing_value: 0, unmatched: 1, ambiguous: 0, self_excluded: 0, cardinality_conflict: 0 } };
const noop = () => {};
async function selectRule() {
  await screen.findByRole('option', { name: '条目' });
  fireEvent.change(screen.getByLabelText('起点类型'), { target: { value: 'Item' } });
  fireEvent.change(screen.getByLabelText('要建立的关系'), { target: { value: 'belongs' } });
  fireEvent.change(screen.getByLabelText('起点关联字段'), { target: { value: 'group' } });
  fireEvent.change(screen.getByLabelText('目标匹配字段'), { target: { value: 'code' } });
}

it('requires an explicit partial choice and submits exactly the previewed rule and version', async () => {
  let applied: Record<string, unknown> | undefined;
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, options) => {
    if (String(url).endsWith('match-apply')) {
      applied = JSON.parse(options?.body as string);
      return new Response(JSON.stringify({ created: 1, existing: 0, unresolved: 1, message: '已记录规则与修订' }));
    }
    return new Response(JSON.stringify(String(url).endsWith('match-preview') ? report : types));
  });
  render(<RelationMatcher projectId="p" disabled={false} onBusy={noop} onDirty={noop} />);
  await selectRule();
  expect(screen.queryByRole('option', { name: '数量 · integer' })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '预览关系匹配' }));
  const apply = await screen.findByRole('button', { name: '建立 1 条关系' });
  expect((apply as HTMLButtonElement).disabled).toBe(true);
  expect(applied).toBeUndefined();
  fireEvent.click(screen.getByRole('checkbox', { name: /我已知有 1 条未解决/ }));
  fireEvent.click(apply);
  await screen.findByText(/已建立 1 条关系/);
  expect(applied).toEqual({ rule: { project_id: 'p', expected_model_release_id: 'release-1', source_type: 'Item', relation_key: 'belongs', source_field: 'group', target_field: 'code', allow_self: false }, expected_plan_hash: report.plan_hash, allow_partial: true });
});

it('retains a stale preview error and clears preview when its rule is edited', async () => {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async url => new Response(JSON.stringify(String(url).endsWith('match-apply') ? { error: { message: '对象已改变，请重新预览' } } : String(url).endsWith('match-preview') ? { ...report, unresolved_count: 0 } : types), { status: String(url).endsWith('match-apply') ? 409 : 200 }));
  render(<RelationMatcher projectId="p" disabled={false} onBusy={noop} onDirty={noop} />);
  await selectRule();
  fireEvent.click(screen.getByRole('button', { name: '预览关系匹配' }));
  fireEvent.click(await screen.findByRole('button', { name: '建立 1 条关系' }));
  await screen.findByText('对象已改变，请重新预览');
  expect(screen.getByLabelText('关系匹配结果')).toBeTruthy();
  fireEvent.change(screen.getByLabelText('目标匹配字段'), { target: { value: '' } });
  await waitFor(() => expect(screen.queryByLabelText('关系匹配结果')).toBeNull());
});
