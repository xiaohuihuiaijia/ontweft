import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { ResourcePage } from './ResourcePage';

it('updates an open task detail after refreshing its status', async () => {
  let status = 'running';
  vi.spyOn(globalThis, 'fetch').mockImplementation(async () => new Response(JSON.stringify({ items: [{ id: 'job', title: '合成任务', status, owner_id: 'u' }], total: 1 })));
  render(<ResourcePage resource="jobs" projectId="p" userId="u" />);
  fireEvent.click(await screen.findByRole('button', { name: '查看详情' }));
  expect(screen.getByRole('button', { name: '取消任务' })).toBeTruthy();
  status = 'succeeded';
  fireEvent.click(screen.getByRole('button', { name: '刷新' }));
  await waitFor(() => expect(screen.queryByRole('button', { name: '取消任务' })).toBeNull());
  expect(screen.getByRole('button', { name: '关闭侧栏' })).toBeTruthy();
  expect(screen.getByText(/"status": "succeeded"/)).toBeTruthy();
});

it('removes protected task detail when a list refresh is denied', async () => {
  let denied = false;
  vi.spyOn(globalThis, 'fetch').mockImplementation(async () => denied ? new Response(JSON.stringify({ error: { message: '任务权限已撤销' } }), { status: 403 }) : new Response(JSON.stringify({ items: [{ id: 'job', title: '合成任务', status: 'failed', private_detail: '先前授权内容' }], total: 1 })));
  render(<ResourcePage resource="jobs" projectId="p" userId="u" />);
  fireEvent.click(await screen.findByRole('button', { name: '查看详情' }));
  denied = true;
  fireEvent.click(screen.getByRole('button', { name: '刷新' }));
  await screen.findByText('任务权限已撤销');
  expect(screen.queryByText(/先前授权内容/)).toBeNull();
  expect(screen.queryByRole('button', { name: '关闭侧栏' })).toBeNull();
});
