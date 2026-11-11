import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { ModelConnections } from './ModelConnections';

it('saves compatible model roles and keeps the existing credential when editing without a new key', async () => {
  const connection = { id: 'm1', name: '合成模型', base_url: 'http://localhost:9100/v1', revision: 3, enabled: true, has_credentials: true, models: [{ id: 'synthetic', label: '合成模型', text: true, vision: true, json_schema: true, context_window: 8192, max_output_tokens: 4096, parameters: { temperature: 0 }, input_price_per_million: 0, output_price_per_million: 0 }], roles: { extraction: 'synthetic', synthesis: 'synthetic' } };
  let submitted: Record<string, unknown> | undefined;
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (_path, options) => {
    if (options?.method === 'PUT') { submitted = JSON.parse(String(options.body)); return new Response(JSON.stringify({ ...connection, ...submitted, revision: 4 })); }
    return new Response(JSON.stringify({ items: [connection], total: 1 }));
  });
  render(<ModelConnections onDirty={() => {}} />);
  fireEvent.click(await screen.findByRole('button', { name: '编辑连接' }));
  expect((screen.getByLabelText('替换密钥（留空保留原密钥）') as HTMLInputElement).value).toBe('');
  fireEvent.change(screen.getByLabelText('模型 ID'), { target: { value: 'synthetic-v2' } });
  fireEvent.click(screen.getByRole('button', { name: '保存模型连接' }));
  await waitFor(() => expect(submitted?.expected_revision).toBe(3));
  expect(submitted?.api_key).toBeUndefined();
  expect(submitted?.roles).toEqual({ extraction: 'synthetic-v2', synthesis: 'synthetic-v2' });
});
