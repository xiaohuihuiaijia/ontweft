import { fireEvent, render, screen, waitFor } from '@testing-library/react';
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
it('clears the frozen action target and preview when searching for another object', async () => {
  const a = { id:'a', project_id:'p', type_key:'asset', revision:1, properties:{name:'Alpha'} };
  const b = { ...a, id:'b', properties:{name:'Beta'} };
  vi.spyOn(globalThis,'fetch').mockImplementation(async url => {
    const path = String(url);
    const value = path.includes('/actions/definitions') ? {items:[{key:'feishu.create_task',configured:false}],total:1}
      : path.includes('/objects?') ? {items:path.includes('q=Beta')?[b]:[a],total:1}
      : path.endsWith('/actions/preview') ? {snapshot:{target_id:'a',target_revision:1,parameters:{title:'Check',body:'Synthetic'}},snapshot_hash:'hash',remote_send_count:0,execution_configured:false}
      : {items:[],total:0};
    return new Response(JSON.stringify(value));
  });
  render(<ActionsPage projectId="p" userId="u" can={() => true} onDirty={() => {}} />);
  await waitFor(() => expect((screen.getByRole('button',{name:'申请创建飞书任务'}) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button',{name:'申请创建飞书任务'}));
  await screen.findByRole('option',{name:'Alpha · 修订 1'});
  fireEvent.change(screen.getByLabelText('业务目标'),{target:{value:'a'}});
  fireEvent.change(screen.getByLabelText('任务标题'),{target:{value:'Check'}});
  fireEvent.click(screen.getByRole('button',{name:'预览发送与审批范围'}));
  await screen.findByRole('heading',{name:'当前预览'});
  fireEvent.change(screen.getByLabelText('搜索动作目标'),{target:{value:'Beta'}});
  fireEvent.click(screen.getByRole('button',{name:'查询目标'}));
  await screen.findByRole('option',{name:'Beta · 修订 1'});
  expect((screen.getByLabelText('业务目标') as HTMLSelectElement).value).toBe('');
  expect((screen.getByRole('button',{name:'提交审批申请'}) as HTMLButtonElement).disabled).toBe(true);
});
