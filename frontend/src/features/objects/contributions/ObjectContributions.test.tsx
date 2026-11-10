import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { ObjectContributions } from './ObjectContributions';

const row = { id: 'c1', identity_id: 'identity-1', revision: 3, record_id: 'record-1', record_codec: 'source_record_revision', release_id: 'release-1', mapping_key: '库存映射', active: true, withdraw_reason: null, stream_id: null, sequence: null, created_at: '2026-10-28T12:00:00Z', mapped_values: { code: '9223372036854775807' }, source_id: 'source-1', resource_id: 'public.inventory' };
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });

it('loads only on authorized expansion, pages actual contributions and requests history/values explicitly', async () => {
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async url => {
    const path = String(url);
    if (path.includes('/history')) return json({ items: [{ ...row, id: path.includes('before=2') ? 'h1' : 'h2', revision: path.includes('before=2') ? 1 : 2, active: false, withdraw_reason: 'source_deleted' }], next_cursor: path.includes('before=2') ? null : 2 });
    return json({ data_status: 'active', items: [{ ...row, id: path.includes('after=c1') ? 'c2' : 'c1', mapping_key: path.includes('after=c1') ? '第二来源映射' : row.mapping_key }], next_cursor: path.includes('after=c1') ? null : 'c1' });
  });
  const view = render(<ObjectContributions objectId="o" canRead={false} />);
  expect(screen.queryByText('来源贡献（展开）')).toBeNull(); expect(fetcher).not.toHaveBeenCalled();
  view.rerender(<ObjectContributions objectId="o" canRead />);
  expect(fetcher).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '来源贡献（展开）' }));
  await screen.findByText('库存映射 · 修订 3');
  expect(fetcher).toHaveBeenCalledTimes(1); expect(screen.queryByText(/9223372036854775807/)).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '查看此来源授权值' }));
  expect(screen.getByText(/9223372036854775807/).textContent).toContain('9223372036854775807');
  fireEvent.click(screen.getByRole('button', { name: '查看贡献历史' }));
  const history = await screen.findByLabelText('贡献历史');
  await within(history).findByText('库存映射 · 修订 2');
  fireEvent.click(within(history).getByRole('button', { name: '查看此来源授权值' }));
  expect(within(history).getByText('该贡献已撤回，以下值不作为当前对象属性生效。')).not.toBeNull();
  fireEvent.click(within(history).getByRole('button', { name: '更早修订' }));
  await screen.findByText('库存映射 · 修订 1');
  expect(fetcher.mock.calls.some(([url]) => String(url).endsWith('/c1/history?limit=20&before=2'))).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: '下一页贡献' }));
  await screen.findByText('第二来源映射 · 修订 3');
  expect(screen.queryByLabelText('贡献历史')).toBeNull();
  expect(fetcher.mock.calls.some(([url]) => String(url).endsWith('/contributions?limit=20&after=c1'))).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: '来源贡献（收起）' }));
  expect(screen.queryByText('source-1')).toBeNull();
});

it('clears current and historical material on denied history and never retries it automatically', async () => {
  let revoked = false;
  const submit = vi.fn();
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async url => {
    if (revoked || String(url).includes('/history')) { revoked = true; return json({ error: { message: '来源查看权限已撤销' } }, 403); }
    return json({ data_status: 'conflict', items: [row], next_cursor: null });
  });
  render(<form onSubmit={event => { event.preventDefault(); submit(); }}><ObjectContributions objectId="o" canRead /></form>);
  fireEvent.click(screen.getByRole('button', { name: '来源贡献（展开）' }));
  await screen.findByText('source-1');
  fireEvent.click(screen.getByRole('button', { name: '查看此来源授权值' }));
  fireEvent.click(screen.getByRole('button', { name: '查看贡献历史' }));
  await screen.findByText('来源查看权限已撤销');
  await waitFor(() => expect(screen.queryByText('source-1')).toBeNull());
  expect(screen.queryByText(/9223372036854775807/)).toBeNull();
  expect(screen.queryByLabelText('贡献历史')).toBeNull();
  expect(fetcher).toHaveBeenCalledTimes(2);
  fireEvent.click(screen.getByRole('button', { name: '重新加载' }));
  await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(3));
  expect(submit).not.toHaveBeenCalled();
});

it('drops a late previous-object response and clears state immediately on permission loss', async () => {
  let resolveOld: (response: Response) => void = () => {};
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(url => String(url).includes('/old/') ? new Promise<Response>(resolve => { resolveOld = resolve; }) : Promise.resolve(json({ items: [{ ...row, mapping_key: '新对象映射' }], next_cursor: null })));
  const view = render(<ObjectContributions objectId="old" canRead />);
  fireEvent.click(screen.getByRole('button', { name: '来源贡献（展开）' }));
  await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));
  view.rerender(<ObjectContributions objectId="new" canRead />);
  expect(screen.getByRole('button', { name: '来源贡献（展开）' }).getAttribute('aria-expanded')).toBe('false');
  await act(async () => resolveOld(json({ items: [row], next_cursor: null })));
  expect(screen.queryByText('库存映射 · 修订 3')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '来源贡献（展开）' }));
  await screen.findByText('新对象映射 · 修订 3');
  view.rerender(<ObjectContributions objectId="new" canRead={false} />);
  expect(screen.queryByText('source-1')).toBeNull(); expect(screen.queryByRole('button')).toBeNull();
  expect(fetcher).toHaveBeenCalledTimes(2);
});
