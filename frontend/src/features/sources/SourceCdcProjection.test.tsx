import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { SourceCdcProjection, type CdcProjection } from './SourceCdcProjection';

const unbound: CdcProjection = { binding: null, current: { release_id: 'release-1', version: 1, source_revision: 3 }, resources: [{ resource_id: 'public.items', label: '库存', mapping_keys: ['inventory'], covered: true }, { resource_id: 'public.orders', label: '订单', mapping_keys: ['orders'], covered: true }, { resource_id: 'public.private', label: '未映射资源', mapping_keys: [], covered: false }], status: 'not_configured', reason_code: 'no_binding', message: '请先配置投影绑定', received_sequence: 20, projected_sequence: 0, indexed_sequence: 0, indexing_status: 'not_configured', baseline: null, last_job: null, can_project: false, can_configure: true };
const bound: CdcProjection = { ...unbound, binding: { id: 'binding-1', release_id: 'release-1', source_revision: 3, generation: 'g1', resource_ids: ['public.items', 'public.orders'], created_at: '2026-10-28T01:00:00Z' }, can_configure: false, can_project: true, status: 'waiting', reason_code: 'snapshot_incomplete', message: '等待完整快照证明' };
const props = { streamId: 'stream-1', refreshKey: 0, canConfigure: true, disabled: false, onBusy: vi.fn() };
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
afterEach(() => vi.useRealTimers());

it('requires explicit resource selection, freezes creation inputs and makes a successful binding read-only', async () => {
  const writes: unknown[] = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, options) => { if (options?.method === 'POST') { writes.push(JSON.parse(String(options.body))); return json(bound); } return json(unbound); });
  render(<SourceCdcProjection {...props} />);
  const save = await screen.findByRole('button', { name: '保存首次投影绑定' });
  expect((save as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByRole('checkbox', { name: /未映射资源/ }) as HTMLInputElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole('checkbox', { name: /库存/ }));
  expect((save as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole('checkbox', { name: /订单/ }));
  fireEvent.click(save);
  await screen.findByText(/投影绑定已保存并冻结/);
  expect(writes).toEqual([{ expected_release_id: 'release-1', expected_source_revision: 3, resource_ids: ['public.items', 'public.orders'] }]);
  expect(screen.queryByRole('button', { name: '保存首次投影绑定' })).toBeNull(); expect(screen.queryByRole('checkbox')).toBeNull();
  expect(screen.getByLabelText('CDC 水位').textContent).toContain('对象投影 projected0');
});

it('does not expose configuration without both permissions but permits server-authorized trigger of an existing binding', async () => {
  let current = unbound; const writes: unknown[] = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, options) => { if (options?.method === 'POST') { writes.push(JSON.parse(String(options.body))); return json({ job: { id: 'job-1', status: 'pending' } }, 202); } return json(current); });
  render(<SourceCdcProjection {...props} canConfigure={false} />);
  await screen.findByText(/配置需要当前项目同时具备/);
  expect(screen.queryByRole('button', { name: '保存首次投影绑定' })).toBeNull();
  current = bound; fireEvent.click(screen.getByRole('button', { name: '刷新投影状态' }));
  fireEvent.click(await screen.findByRole('button', { name: '请求一次完整窗口投影' }));
  await screen.findByText(/已请求投影任务 job-1/);
  expect(writes).toEqual([{ expected_binding_id: 'binding-1' }]);
  expect(screen.getByLabelText('CDC 水位').textContent).toContain('已接收 received20');
  expect(screen.getByLabelText('CDC 水位').textContent).toContain('对象投影 projected0');
  expect(screen.getByLabelText('CDC 水位').textContent).toContain('综合索引 indexed0');
});

it('retains a stale configuration selection on 409 and requires explicit baseline reset after refresh', async () => {
  let current = unbound; let posts = 0;
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, options) => { if (options?.method === 'POST') { posts++; current = { ...unbound, current: { ...unbound.current, release_id: 'release-2', version: 2 } }; return json({ error: { message: '模型发布已变化' } }, 409); } return json(current); });
  render(<SourceCdcProjection {...props} />);
  fireEvent.click(await screen.findByRole('checkbox', { name: /库存/ }));
  fireEvent.click(screen.getByRole('checkbox', { name: /订单/ }));
  fireEvent.click(screen.getByRole('button', { name: '保存首次投影绑定' })); await screen.findByText('模型发布已变化');
  expect(posts).toBe(1); expect((screen.getByRole('checkbox', { name: /库存/ }) as HTMLInputElement).checked).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: '刷新投影状态' }));
  await screen.findByText(/表单基线已过期/);
  expect((screen.getByRole('button', { name: '保存首次投影绑定' }) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByRole('checkbox', { name: /库存/ }) as HTMLInputElement).checked).toBe(true); expect(posts).toBe(1);
  fireEvent.click(screen.getByRole('button', { name: '按当前基线重新选择范围' }));
  expect((screen.getByRole('checkbox', { name: /库存/ }) as HTMLInputElement).checked).toBe(false);
});

it('requires explicit refresh before failed retry and forbids blind retry for blocked/budget states', async () => {
  let current: CdcProjection = { ...bound, status: 'failed', reason_code: 'conversion_failed', message: '字段转换失败', last_job: { id: 'failed-job', status: 'failed', error: 'quantity invalid' } }; let posts = 0;
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, options) => { if (options?.method === 'POST') { posts++; return json({ job: { id: 'retry-job', status: 'pending' } }, 202); } return json(current); });
  render(<SourceCdcProjection {...props} />);
  const retry = await screen.findByRole('button', { name: '重新请求一次完整窗口投影' }); expect((retry as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: '刷新投影状态' }));
  await waitFor(() => expect((retry as HTMLButtonElement).disabled).toBe(false)); fireEvent.click(retry);
  await screen.findByText(/已请求投影任务 retry-job/); expect(posts).toBe(1);
  current = { ...bound, status: 'blocked', reason_code: 'schema_changed', message: '结构变化', can_project: true };
  fireEvent.click(screen.getByRole('button', { name: '刷新投影状态' })); await screen.findByText('结构变化');
  expect((screen.getByRole('button', { name: '请求一次完整窗口投影' }) as HTMLButtonElement).disabled).toBe(true);
  current = { ...current, status: 'budget_exceeded', message: '事务超过预算' };
  fireEvent.click(screen.getByRole('button', { name: '刷新投影状态' })); await screen.findByText('事务超过预算');
  expect((screen.getByRole('button', { name: '请求一次完整窗口投影' }) as HTMLButtonElement).disabled).toBe(true); expect(posts).toBe(1);
});

it('polls only active jobs, reads committed watermarks and stops at waiting without inferring indexed progress', async () => {
  let reads = 0; let current: CdcProjection = { ...bound, status: 'running', last_job: { id: 'j', status: 'pending' } };
  vi.spyOn(globalThis, 'fetch').mockImplementation(async () => { reads++; return json(current); });
  render(<SourceCdcProjection {...props} />); await screen.findByText('j');
  vi.useFakeTimers();
  // Start a fresh active-job read after fake timers so the polling interval is controlled.
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: '刷新投影状态' })); });
  const before = reads;
  current = { ...bound, status: 'waiting', projected_sequence: 12, baseline: { window_id: 'window-1', end_sequence: 12, proof_kind: 'completed_notification' }, last_job: { id: 'j', status: 'succeeded', result: { end_sequence: 12 } } };
  await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
  expect(reads).toBe(before + 1); expect(screen.getByLabelText('CDC 水位').textContent).toContain('对象投影 projected12');
  expect(screen.getByLabelText('CDC 水位').textContent).toContain('综合索引 indexed0');
  await act(async () => { await vi.advanceTimersByTimeAsync(15000); }); expect(reads).toBe(before + 1);
});

it.each([403, 404])('clears protected projection data on denied/unavailable reads rather than displaying unconfigured success (status=%s)', async status => {
  let denied = false;
  vi.spyOn(globalThis, 'fetch').mockImplementation(async () => denied ? json({ error: { message: status === 403 ? '投影权限已撤销' : '投影 API 尚未部署' } }, status) : json(bound));
  render(<SourceCdcProjection {...props} />); await screen.findByText('g1');
  denied = true; fireEvent.click(screen.getByRole('button', { name: '刷新投影状态' })); await screen.findByText(status === 403 ? '投影权限已撤销' : '投影 API 尚未部署');
  expect(screen.queryByText('g1')).toBeNull(); expect(screen.queryByLabelText('CDC 水位')).toBeNull(); expect(screen.queryByRole('button', { name: '保存首次投影绑定' })).toBeNull();
});

it('polls pending fulltext independently and never advances the composite index from projection or fulltext success', async () => {
  let reads = 0;
  let current: CdcProjection = { ...bound, status: 'caught_up', received_sequence: 20, projected_sequence: 20, last_job: { id: 'projected-job', status: 'succeeded' }, fulltext: { status: 'pending', sequence: 12, generation: 'fulltext-g1', message: '等待对象全文消费' } };
  vi.spyOn(globalThis, 'fetch').mockImplementation(async () => { reads++; return json(current); });
  render(<SourceCdcProjection {...props} />); await screen.findByText('等待对象全文消费');
  expect(screen.getByLabelText('CDC 水位').textContent).toContain('对象全文 fulltext12');
  expect(screen.getByLabelText('CDC 水位').textContent).toContain('综合索引 indexed0');
  vi.useFakeTimers(); await act(async () => { fireEvent.click(screen.getByRole('button', { name: '刷新投影状态' })); }); const before = reads;
  const visible = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
  await act(async () => { await vi.advanceTimersByTimeAsync(5000); }); expect(reads).toBe(before);
  visible.mockReturnValue('visible'); current = { ...current, fulltext: { ...current.fulltext!, status: 'ready', sequence: 20, message: '对象全文已消费到20' } };
  await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
  expect(screen.getByLabelText('CDC 水位').textContent).toContain('对象全文 fulltext20');
  expect(screen.getByLabelText('CDC 水位').textContent).toContain('综合索引 indexed0');
  const completed = reads; await act(async () => { await vi.advanceTimersByTimeAsync(15000); }); expect(reads).toBe(completed);
  expect(screen.getByText('向量索引未配置；全文进度不代表综合索引完成')).not.toBeNull();
});

it('keeps committed projection visible when fulltext fails and does not infer a missing fulltext watermark', async () => {
  let current: CdcProjection = { ...bound, projected_sequence: 20, fulltext: { status: 'failed', sequence: 12, generation: 'g', message: '全文消费失败，请检查后台任务' } };
  vi.spyOn(globalThis, 'fetch').mockImplementation(async () => json(current));
  render(<SourceCdcProjection {...props} />); await screen.findByText('全文消费失败，请检查后台任务');
  expect(screen.getByLabelText('CDC 水位').textContent).toContain('对象投影 projected20');
  expect(screen.getByLabelText('CDC 水位').textContent).toContain('对象全文 fulltext12');
  current = { ...current, fulltext: undefined }; fireEvent.click(screen.getByRole('button', { name: '刷新投影状态' }));
  await screen.findByText('服务未提供独立全文索引进度');
  expect(screen.getByLabelText('CDC 水位').textContent).toContain('对象全文 fulltext—');
});


it('rebuilds a new generation against the current revision and hands the one-time credential to the parent', async () => {
  const onRebuilt = vi.fn(); const result = { stream: { id: 'stream-2', generation: 'g2' }, token: 'synthetic-once' };
  const writes: unknown[] = [];
  vi.spyOn(window, 'confirm').mockReturnValue(true);
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, options) => {
    if (options?.method === 'POST') { expect(String(url)).toContain('/stream-1/rebuild'); writes.push(JSON.parse(String(options.body))); return json(result, 201); }
    return json({ ...bound, can_rebuild: true, current: { release_id: 'release-2', version: 2, source_revision: 4 } });
  });
  render(<SourceCdcProjection {...props} onRebuilt={onRebuilt} />);
  fireEvent.click(await screen.findByRole('button', { name: '从完整快照重建基线' }));
  await waitFor(() => expect(onRebuilt).toHaveBeenCalledWith(result));
  expect(writes).toEqual([{ expected_binding_id: 'binding-1', expected_release_id: 'release-2', expected_source_revision: 4, resource_ids: ['public.items', 'public.orders'] }]);
  expect(screen.queryByText('synthetic-once')).toBeNull();
});
