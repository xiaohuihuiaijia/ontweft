import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { FileVersions } from './FileVersions';

it('reuses the source CSV encoding and delimiter when previewing a new version', async () => {
  let upload: FormData | undefined;
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, options) => {
    if (options?.method === 'POST') { upload = options.body as FormData; return new Response(JSON.stringify({ id: 'v2', number: 2, preview: {}, jobs: [] })); }
    return new Response(JSON.stringify({ items: [] }));
  });
  render(<FileVersions source={{ id: 's', name: 'Inventory', kind: 'tabular', revision: 2, config: { business_keys: { sheet_1: ['c1'] }, encoding: 'gb18030', delimiter: ';' } }} onUpdated={vi.fn()} />);
  fireEvent.change(screen.getByLabelText('更新文件'), { target: { files: [new File(['id;name\n1;test'], 'next.csv')] } });
  fireEvent.click(screen.getByRole('button', { name: '预览更新' }));
  await waitFor(() => expect(upload).toBeDefined());
  expect(upload!.get('encoding')).toBe('gb18030');
  expect(upload!.get('delimiter')).toBe(';');
});
