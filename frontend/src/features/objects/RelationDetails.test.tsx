import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { RelationDetails } from './RelationDetails';

const relation = {
  id: 'r1', revision: 4, source_id: 'a', target_id: 'b', label: '适用于', model_release_id: 'release-2',
  properties: { condition: '室内', count: '9007199254740993', retained: 'unchanged' }, evidence: [],
  definition: { key: 'fits', label: '适用于', target_type: 'item', cardinality: 'many', properties: [
    { key: 'condition', label: '适用条件', data_type: 'string' },
    { key: 'count', label: '数量', data_type: 'integer' },
    { key: 'retained', label: '原值', data_type: 'string' },
  ] },
};

it('patches only explicitly edited fields with the fresh relation and model revisions', async () => {
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, options) => new Response(JSON.stringify(
    options?.method === 'PATCH' ? relation : String(url).includes('/objects/') ? { evidence: [] } : relation,
  )));
  const changed = vi.fn();
  render(<RelationDetails relationId="r1" canWrite role="owner" onChanged={changed} />);
  fireEvent.click(await screen.findByRole('button', { name: '编辑条件与证据' }));
  await waitFor(() => expect((screen.getByLabelText('数量') as HTMLInputElement).disabled).toBe(false));
  expect((screen.getByLabelText('数量') as HTMLInputElement).value).toBe('9007199254740993');
  fireEvent.change(screen.getByLabelText('适用条件'), { target: { value: '' } });
  fireEvent.change(screen.getByLabelText('数量'), { target: { value: '9007199254740994' } });
  fireEvent.click(screen.getByRole('button', { name: '保存关系条件' }));
  await waitFor(() => expect(changed).toHaveBeenCalledOnce());
  const calls = fetcher.mock.calls.filter(([, options]) => options?.method === 'PATCH');
  expect(JSON.parse(String(calls[0][1]?.body))).toEqual({
    expected_revision: 4, expected_model_release_id: 'release-2', properties: { count: '9007199254740994' },
    remove_properties: ['condition'], evidence: [], reason: '',
  });
});

it('retains user edits on conflict and never adds manual support', async () => {
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, options) => options?.method === 'PATCH'
    ? new Response(JSON.stringify({ error: { code: 'conflict', message: '版本冲突' } }), { status: 409 })
    : new Response(JSON.stringify(String(url).includes('/objects/') ? { evidence: [] } : relation)));
  render(<RelationDetails relationId="r1" canWrite role="owner" onChanged={vi.fn()} />);
  fireEvent.click(await screen.findByRole('button', { name: '编辑条件与证据' }));
  await waitFor(() => expect((screen.getByLabelText('适用条件') as HTMLInputElement).disabled).toBe(false));
  fireEvent.change(screen.getByLabelText('适用条件'), { target: { value: '室外' } });
  fireEvent.click(screen.getByRole('button', { name: '保存关系条件' }));
  expect((await screen.findByRole('alert')).textContent).toContain('版本冲突');
  expect((screen.getByLabelText('适用条件') as HTMLInputElement).value).toBe('室外');
  const payload = JSON.parse(String(fetcher.mock.calls.find(([, options]) => options?.method === 'PATCH')?.[1]?.body));
  expect(payload).not.toHaveProperty('manual');
  expect(payload).not.toHaveProperty('source_id');
});
