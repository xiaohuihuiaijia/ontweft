import { fireEvent, render, screen } from '@testing-library/react';
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
