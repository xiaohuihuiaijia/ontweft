import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { ObjectRelations } from './ObjectRelations';

const props = { object: { id: 'o', project_id: 'p', type_key: 'item', revision: 1, properties: {} }, type: { key: 'item', label: '物料', properties: [], relations: [] }, modelReleaseId: 'r1', canWrite: false, onChanged: () => {}, initialRelationKey: 'related' };
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });

it('can browse past 200 relations without submitting its enclosing object form', async () => {
  const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
    const url = new URL(String(input), 'http://test');
    if (url.pathname !== '/api/relations') return json({ items: [], total: 0 });
    const offset = Number(url.searchParams.get('offset'));
    return json({ items: Array.from({ length: Math.min(25, 205 - offset) }, (_, i) => ({ id: `r${offset + i + 1}`, source_id: 'o', target_id: `目标-${offset + i + 1}`, type_key: 'related' })), total: 205 });
  });
  const submit = vi.fn(event => event.preventDefault());
  render(<form onSubmit={submit}><ObjectRelations {...props} /></form>);
  await screen.findByText('→ related · 目标-1');
  for (let page = 1; page <= 8; page++) {
    fireEvent.click(within(screen.getByRole('group', { name: '对象关系分页' })).getByRole('button', { name: '下一页' }));
    await screen.findByText(`→ related · 目标-${page * 25 + 1}`);
  }
  expect(screen.getByText('→ related · 目标-205')).toBeTruthy();
  expect((within(screen.getByRole('group', { name: '对象关系分页' })).getByRole('button', { name: '下一页' }) as HTMLButtonElement).disabled).toBe(true);
  expect(submit).not.toHaveBeenCalled();
  expect(fetch.mock.calls.some(([path]) => String(path).includes('offset=200'))).toBe(true);
});

it('returns to the preceding relation page when the last item on a page is deleted', async () => {
  let total = 26;
  vi.spyOn(window, 'confirm').mockReturnValue(true);
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, options) => {
    if (options?.method === 'DELETE') { total = 25; return json({ ok: true }); }
    const url = new URL(String(input), 'http://test');
    if (url.pathname !== '/api/relations') return json({ items: [], total: 0 });
    const offset = Number(url.searchParams.get('offset'));
    return json({ items: Array.from({ length: Math.max(0, Math.min(25, total - offset)) }, (_, i) => ({ id: `r${offset + i + 1}`, revision: 1, source_id: 'o', target_id: `目标-${offset + i + 1}`, type_key: 'related' })), total });
  });
  render(<ObjectRelations {...props} canWrite />);
  await screen.findByText('→ related · 目标-1');
  fireEvent.click(within(screen.getByRole('group', { name: '对象关系分页' })).getByRole('button', { name: '下一页' }));
  await screen.findByText('→ related · 目标-26');
  fireEvent.click(screen.getByRole('button', { name: '删除此关系' }));
  await screen.findByText('→ related · 目标-1');
  expect(screen.queryByText('→ related · 目标-26')).toBeNull();
});

it('does not retain relation evidence from the preceding page after access is denied', async () => {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
    const path = String(input);
    if (!path.includes('/relations?')) return json({ items: [], total: 0 });
    return path.includes('offset=25') ? json({ error: { message: '关系权限已撤销' } }, 403) : json({ items: [{ id: 'r1', source_id: 'o', target_id: '先前授权目标', type_key: 'related' }], total: 26 });
  });
  render(<ObjectRelations {...props} />);
  await screen.findByText('→ related · 先前授权目标');
  fireEvent.click(within(screen.getByRole('group', { name: '对象关系分页' })).getByRole('button', { name: '下一页' }));
  await screen.findByText('关系权限已撤销');
  await waitFor(() => expect(screen.queryByText(/先前授权目标/)).toBeNull());
});
