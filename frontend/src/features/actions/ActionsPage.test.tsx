import { fireEvent, render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { ActionsPage } from './ActionsPage';

it('never offers self approval or execution for an unconfigured frozen destination', async () => {
  const run = { id: 'a1', revision: 1, status: 'requested', action_key: 'feishu.create_task', requester_id: 'u1', target_id: 'o1', target_revision: 2, parameters: { title: '维护任务' }, snapshot: { destination_id: 'feishu:unconfigured', target_id: 'o1', target_revision: 2, parameters: { title: '维护任务', body: '仅审批，无外部发送' }, recipients: [] } };
  vi.spyOn(globalThis, 'fetch').mockImplementation(async url => {
    const path = String(url);
    const response = path.includes('/definitions') ? { items: [{ key: 'feishu.create_task', configured: false }], total: 1 } : path.endsWith('/actions/a1') ? run : { items: [run], total: 1 };
    return new Response(JSON.stringify(response), { status: 200 });
  });
  render(<ActionsPage projectId="p" userId="u1" can={() => true} onDirty={() => {}} />);
  fireEvent.click(await screen.findByRole('button', { name: '查看审批快照' }));
  await screen.findByRole('heading', { name: '冻结审批内容' });
  expect(screen.queryByRole('button', { name: '批准此冻结快照' })).toBeNull();
  expect(screen.queryByRole('button', { name: '提交执行队列' })).toBeNull();
  expect(screen.getByText('仅审批，无外部发送')).toBeTruthy();
});
