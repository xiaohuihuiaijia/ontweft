import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { SourceCdc, type CdcStream } from './SourceCdc';

const initial: CdcStream = { id: 'stream-1', source_id: 'source-1', source_revision: 7, generation: 'generation-1', connector: 'postgresql', state: 'active', received_sequence: 26, projected_sequence: 0, indexed_sequence: 0, projection_status: 'not_configured', source_partition: { server: 'test' }, lease_holder: 'collector-1', lease_until: 1800000000, fencing_token: 3 };
const event = (sequence: number) => ({ id: `e-${sequence}`, sequence, event_id: `event-${sequence}`, record_hash: `hash-${sequence}`, record_json: JSON.stringify({ value: { after: { product: `测试记录 ${sequence}` } } }) });
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
const projection = (stream = initial) => ({ binding: null, current: { release_id: 'r1', version: 1, source_revision: stream.source_revision }, resources: [], status: 'not_configured', reason_code: 'no_binding', message: '对象投影尚未配置', received_sequence: stream.received_sequence, projected_sequence: stream.projected_sequence, indexed_sequence: stream.indexed_sequence, indexing_status: 'not_configured', baseline: null, last_job: null, can_project: false, can_configure: false });

it('keeps receipt separate from projection, pages by sequence, and reflects actual pause/resume responses', async () => {
  let current = { ...initial }; const calls: { path: string; body?: unknown }[] = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, options) => {
    const path = String(url); calls.push({ path, body: options?.body ? JSON.parse(String(options.body)) : undefined });
    if (path.endsWith('/projection')) return json(projection(current));
    if (path.endsWith('/pause') || path.endsWith('/resume')) { current = { ...current, state: path.endsWith('/pause') ? 'paused' : 'active', lease_holder: null, lease_until: 0, fencing_token: current.fencing_token + 1 }; return json({ stream: current }); }
    if (path.includes('/events?')) return json({ stream: current, items: path.includes('after=25&') ? [event(26)] : Array.from({ length: 25 }, (_, index) => event(index + 1)) });
    return json({ items: [current] });
  });
  render(<SourceCdc projectId="p" sourceId="source-1" sourceRevision={7} />);
  await screen.findByText('event-1');
  await screen.findByText('对象投影尚未配置');
  expect(screen.getByLabelText('CDC 水位').textContent).toContain('已接收 received26');
  expect(screen.getByLabelText('CDC 水位').textContent).toContain('对象投影 projected0');
  expect(screen.getByLabelText('CDC 水位').textContent).toContain('综合索引 indexed0');
  await waitFor(() => expect((screen.getByRole('button', { name: '下一页事件' }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button', { name: '下一页事件' }));
  await screen.findByText('event-26');
  expect(calls.some(call => call.path === '/api/cdc/streams/stream-1/events?after=25&limit=25')).toBe(true);
  expect((screen.getByRole('button', { name: '下一页事件' }) as HTMLButtonElement).disabled).toBe(true);
  await waitFor(() => expect((screen.getByRole('button', { name: '上一页事件' }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button', { name: '上一页事件' }));
  await screen.findByText('event-1');
  await waitFor(() => expect((screen.getByRole('button', { name: '暂停接收' }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button', { name: '暂停接收' }));
  await screen.findByRole('button', { name: '恢复接收' });
  expect(screen.getByText('无当前租约')).toBeTruthy();
  expect(calls.find(call => call.path.endsWith('/pause'))?.body).toEqual({});
  await waitFor(() => expect((screen.getByRole('button', { name: '恢复接收' }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button', { name: '恢复接收' }));
  await screen.findByRole('button', { name: '暂停接收' });
  expect(calls.some(call => /\/(lease|append)$/.test(call.path))).toBe(false);
});

it('uses the loaded source revision and exposes synthetic tokens only on explicit reveal, without persistent storage', async () => {
  let current: CdcStream | undefined; const writes: { path: string; body: unknown }[] = [];
  const storage = vi.spyOn(Storage.prototype, 'setItem');
  vi.spyOn(window, 'confirm').mockReturnValue(true);
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, options) => {
    const path = String(url);
    if (path.endsWith('/projection')) return json(projection(current));
    if (options?.method === 'POST') {
      writes.push({ path, body: JSON.parse(String(options.body)) }); current = { ...initial, received_sequence: 0, lease_holder: null, lease_until: 0 };
      return json({ stream: current, token: path.endsWith('rotate-token') ? 'synthetic-rotated-token' : 'synthetic-once-token' });
    }
    return json(path.includes('/events?') ? { stream: current, items: [] } : { items: current ? [current] : [] });
  });
  const view = render(<SourceCdc key="source-1" projectId="p" sourceId="source-1" sourceRevision={7} />);
  fireEvent.click(await screen.findByRole('button', { name: '登记 PostgreSQL CDC 通道' }));
  await screen.findByRole('button', { name: '显示本次令牌' });
  expect(writes).toEqual([{ path: '/api/cdc/streams', body: { source_id: 'source-1', expected_source_revision: 7 } }]);
  expect(screen.queryByDisplayValue('synthetic-once-token')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '显示本次令牌' }));
  expect(screen.getByDisplayValue('synthetic-once-token')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: '已处理，关闭令牌' }));
  expect(screen.queryByDisplayValue('synthetic-once-token')).toBeNull();
  expect(screen.queryByRole('button', { name: '显示本次令牌' })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '刷新 CDC 状态' }));
  await waitFor(() => expect((screen.getByRole('button', { name: '轮换采集令牌' }) as HTMLButtonElement).disabled).toBe(false));
  expect(screen.queryByRole('button', { name: '显示本次令牌' })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '轮换采集令牌' }));
  await screen.findByRole('button', { name: '显示本次令牌' });
  expect(screen.queryByDisplayValue('synthetic-rotated-token')).toBeNull();
  view.rerender(<SourceCdc key="source-2" projectId="p" sourceId="source-2" sourceRevision={1} />);
  await screen.findByRole('button', { name: '登记 PostgreSQL CDC 通道' });
  expect(screen.queryByLabelText('本次采集令牌')).toBeNull();
  expect(storage).not.toHaveBeenCalled();
});

it('does not retry a registration conflict and requires an explicit state refresh before another write', async () => {
  let posts = 0;
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, options) => {
    if (options?.method === 'POST') { posts++; return json({ error: { code: 'revision_conflict', message: '来源配置版本已变化' } }, 409); }
    return json({ items: [] });
  });
  render(<SourceCdc projectId="p" sourceId="source-1" sourceRevision={7} />);
  fireEvent.click(await screen.findByRole('button', { name: '登记 PostgreSQL CDC 通道' }));
  await screen.findByText('来源配置版本已变化');
  expect(posts).toBe(1);
  expect(screen.queryByRole('button', { name: '登记 PostgreSQL CDC 通道' })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '刷新 CDC 状态' }));
  await screen.findByRole('button', { name: '登记 PostgreSQL CDC 通道' });
  expect(posts).toBe(1);
});

it('does not treat an unavailable service or revoked permission as an empty successful inbox', async () => {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(json({ error: { message: 'CDC 服务尚未部署' } }, 404));
  const view = render(<SourceCdc projectId="p" sourceId="source-1" sourceRevision={7} />);
  await screen.findByText('CDC 服务尚未部署');
  expect(screen.queryByRole('button', { name: '登记 PostgreSQL CDC 通道' })).toBeNull();
  view.unmount();
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, options) => options?.method === 'POST'
    ? json({ error: { message: '项目权限已撤销' } }, 403)
    : json(String(url).endsWith('/projection') ? projection() : String(url).includes('/events?') ? { stream: initial, items: [event(1)] } : { items: [initial] }));
  render(<SourceCdc projectId="p" sourceId="source-1" sourceRevision={7} />);
  await screen.findByText('event-1');
  await screen.findByText('对象投影尚未配置');
  await waitFor(() => expect((screen.getByRole('button', { name: '暂停接收' }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button', { name: '暂停接收' }));
  await screen.findByText('项目权限已撤销');
  expect(screen.queryByText('event-1')).toBeNull();
  expect(screen.queryByText('collector-1')).toBeNull();
  expect(screen.queryByText('尚无已接收事件。通道启用不代表采集端已经连接。')).toBeNull();
});

it('renders exact server JSON text for large integers and refuses a lossy legacy preview', async () => {
  const precise = '{"key":{"id":9223372036854775807},"offset":{"lsn":9007199254740993}}';
  let includeText = true;
  vi.spyOn(globalThis, 'fetch').mockImplementation(async url => json(String(url).endsWith('/projection') ? projection() : String(url).includes('/events?') ? {
    stream: initial,
    items: [{ ...event(1), record_json: includeText ? precise : undefined, record: JSON.parse(precise) }],
  } : { items: [initial] }));
  render(<SourceCdc projectId="p" sourceId="source-1" sourceRevision={7} />);
  fireEvent.click(await screen.findByText('查看事件 1'));
  expect(screen.getByText(precise, { exact: true })).toBeTruthy();
  expect(screen.queryByText(/9223372036854776000/)).toBeNull();
  includeText = false;
  await screen.findByText('对象投影尚未配置');
  await waitFor(() => expect((screen.getByRole('button', { name: '刷新 CDC 状态' }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button', { name: '刷新 CDC 状态' }));
  await screen.findByText('服务未返回精确事件原文，暂不展示可能丢失整数精度的 JSON 预览。');
  expect(screen.queryByText(/9223372036854776000/)).toBeNull();
});
