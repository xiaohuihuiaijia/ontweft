import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { SourceSnapshots } from './SourceSnapshots';

it.each([true, false])('preserves exact source identity, values and evidence text without rounded fallback (text=%s)', async withText => {
  const identity = '{"id":9223372036854775807}';
  const values = '{"id":9223372036854775807,"name":"UI联调"}';
  const record = '{"source_key":{"id":9223372036854775807},"values":{"id":9223372036854775807},"evidence":[]}';
  const snapshot = { id: 'snapshot', resource_id: 'items', source_revision: 1, status: 'complete', record_count: 1 };
  vi.spyOn(globalThis, 'fetch').mockImplementation(async url => new Response(JSON.stringify(String(url).includes('/source-records/')
    ? { values: { id: 9223372036854776000 }, ...(withText ? { record_json: record } : {}) }
    : String(url).includes('/records')
      ? { snapshot, total: 1, items: [{ id: 'record', source_key: { id: 9223372036854776000 }, values: { id: 9223372036854776000 }, ...(withText ? { source_key_json: identity, values_json: values } : {}) }] }
      : { items: [snapshot], total: 1 })));
  render(<SourceSnapshots sourceId="source" sourceRevision={1} />);
  fireEvent.click(await screen.findByRole('button', { name: '查看记录与投影结果' }));
  if (withText) {
    expect((await screen.findByLabelText('原始身份')).textContent).toBe(identity);
    expect(screen.getByLabelText('记录内容').textContent).toBe(values);
  } else {
    await screen.findByText(/未返回原始身份的精确文本/);
    screen.getByText(/未返回记录内容的精确文本/);
  }
  fireEvent.click(await screen.findByRole('button', { name: '查看证据记录' }));
  if (withText) expect((await screen.findByLabelText('原始证据记录')).textContent).toBe(record);
  else await screen.findByText(/未返回原始证据记录的精确文本/);
  expect(screen.queryByText(/9223372036854776000/)).toBeNull();
});


it('discards a late evidence response after switching snapshots', async () => {
  let finish!: (response: Response) => void;
  const snapshots = ['one', 'two'].map(id => ({ id, resource_id: id, status: 'complete', source_revision: 1 }));
  vi.spyOn(globalThis, 'fetch').mockImplementation(async url => {
    const path = String(url);
    if (path.includes('/source-records/')) return new Promise(resolve => { finish = resolve; });
    if (path.includes('/snapshots/one/records')) return new Response(JSON.stringify({ snapshot: snapshots[0], total: 1, items: [{ id: 'r1', source_key_json: '{}', values_json: '{}' }] }));
    if (path.includes('/snapshots/two/records')) return new Response(JSON.stringify({ snapshot: snapshots[1], total: 0, items: [] }));
    return new Response(JSON.stringify({ items: snapshots, total: 2 }));
  });
  render(<SourceSnapshots sourceId="source" sourceRevision={1} />);
  fireEvent.click((await screen.findAllByRole('button', { name: '查看记录与投影结果' }))[0]);
  fireEvent.click(await screen.findByRole('button', { name: '查看证据记录' }));
  fireEvent.click(screen.getAllByRole('button', { name: '查看记录与投影结果' })[1]);
  await act(async () => { finish(new Response(JSON.stringify({ record_json: '{"secret":"old snapshot"}' }))); });
  expect(screen.queryByLabelText('原始证据记录')).toBeNull();
});

it('clears current evidence and snapshot metadata after read access is revoked', async () => {
  let denied = false;
  const snapshot = { id: 's', resource_id: 'items', source_revision: 1, status: 'complete' };
  vi.spyOn(globalThis, 'fetch').mockImplementation(async url => {
    if (denied) return new Response(JSON.stringify({ error: { message: '来源已撤权' } }), { status: 403 });
    return new Response(JSON.stringify(String(url).includes('/source-records/') ? { record_json: '{"private":"authorized evidence"}' } : String(url).includes('/records') ? { snapshot, total: 1, items: [{ id: 'r1', source_key_json: '{}', values_json: '{}' }] } : { items: [snapshot], total: 1 }));
  });
  render(<SourceSnapshots sourceId="source" sourceRevision={1} />);
  fireEvent.click(await screen.findByRole('button', { name: '查看记录与投影结果' }));
  fireEvent.click(await screen.findByRole('button', { name: '查看证据记录' }));
  await screen.findByLabelText('原始证据记录');
  denied = true;
  fireEvent.click(screen.getByRole('button', { name: '刷新采集状态' }));
  await waitFor(() => expect(screen.queryByText('当前快照')).toBeNull());
  expect(screen.queryByLabelText('原始证据记录')).toBeNull();
});
