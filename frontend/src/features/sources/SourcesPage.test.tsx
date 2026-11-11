import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { SourcesPage } from './SourcesPage';

let finishVersion: () => void;
vi.mock('./FileVersions', () => ({ FileVersions: ({ source, onUpdated }: { source: { id: string; name: string; kind: string }; onUpdated: (value: unknown) => void }) => <button onClick={() => { void new Promise<void>(resolve => { finishVersion = resolve; }).then(() => onUpdated({ ...source, revision: 2, resources: [] })); }}>测试：提交文件版本</button> }));

it('retains historical resources and snapshots without offering disabled connector operations', async () => {
  const calls: string[] = [];
  const source = { id: 'legacy', name: '历史 MongoDB', kind: 'mongo', revision: 1, config: {}, resources: [{ resource_id: 'items', name: 'items', fields: [], primary_key: ['id'], foreign_keys: [] }] };
  vi.spyOn(globalThis, 'fetch').mockImplementation(async url => {
    calls.push(String(url));
    return new Response(JSON.stringify(String(url).includes('/snapshots') ? { items: [{ id: 'retained-snapshot', resource_id: 'items', status: 'complete', source_revision: 1 }], total: 1 } : { items: [source], total: 1 }));
  });
  render(<SourcesPage projectId="p" canWrite canConfigureProjection integrationsEnabled={false} onDirty={() => {}} />);
  fireEvent.click(await screen.findByRole('button', { name: '查看资源 →' }));
  await screen.findByText('retained-snapshot');
  fireEvent.click(screen.getByRole('button', { name: 'items' }));
  expect(screen.getByText(/此连接器扩展已停用/)).toBeTruthy();
  for (const name of ['测试连接', '发现资源', '读取最多 20 条样本', '采集所选资源快照']) expect(screen.queryByRole('button', { name })).toBeNull();
  expect(screen.getByRole('button', { name: '查看记录与投影结果' })).toBeTruthy();
  expect(calls.every(url => url.includes('/sources'))).toBe(true);
});

it('prevents source/resource switching while an old sample is in flight and clears samples before the next resource', async () => {
  let resolveSample: (response: Response) => void = () => {};
  const source = { id: 's', name: '测试来源', kind: 'sqlite', revision: 1, config: {}, resources: ['A', 'B'].map(name => ({ resource_id: name, name, fields: [], primary_key: ['id'], foreign_keys: [] })) };
  vi.spyOn(globalThis, 'fetch').mockImplementation(async url => {
    if (String(url).includes('/sample')) return new Promise<Response>(resolve => { resolveSample = resolve; });
    return new Response(JSON.stringify(String(url).includes('/snapshots') ? { items: [], total: 0 } : { items: [source], total: 1 }), { status: 200 });
  });
  render(<SourcesPage projectId="p" canWrite onDirty={() => {}} />);
  fireEvent.click(await screen.findByRole('button', { name: '查看资源 →' }));
  fireEvent.click(screen.getByRole('button', { name: 'A' }));
  fireEvent.click(screen.getByRole('button', { name: '读取最多 20 条样本' }));
  expect((screen.getByRole('button', { name: 'B' }) as HTMLButtonElement).disabled).toBe(true);
  resolveSample(new Response(JSON.stringify({ sample_json: JSON.stringify({ records: [{ values: { name: 'A的独立样本' } }] }) }), { status: 200 }));
  await screen.findByText(/A的独立样本/);
  await waitFor(() => expect((screen.getByRole('button', { name: 'B' }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button', { name: 'B' }));
  expect(screen.queryByText(/A的独立样本/)).toBeNull();
});

it('does not show or fetch CDC controls without source write permission', async () => {
  const paths: string[] = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async url => {
    paths.push(String(url));
    return new Response(JSON.stringify({ items: [{ id: 'pg', name: '只读 PostgreSQL', kind: 'postgres', revision: 1, config: {}, resources: [] }], total: 1 }));
  });
  render(<SourcesPage projectId="p" canWrite={false} onDirty={() => {}} />);
  fireEvent.click(await screen.findByRole('button', { name: '查看资源 →' }));
  expect(screen.queryByLabelText('CDC 持久事件收件箱')).toBeNull();
  expect(paths.some(path => path.includes('/cdc/'))).toBe(false);
});

it.each([true, false])('shows exact sample text without parsing, or explicitly withholds a legacy rounded sample (text=%s)', async withText => {
  const sampleText = '{"records":[{"values":{"id":9223372036854775807}}]}';
  const source = { id: 's', name: '精度来源', kind: 'sqlite', revision: 1, config: {}, resources: [{ resource_id: 'items', name: 'items', fields: [], primary_key: ['id'], foreign_keys: [] }] };
  vi.spyOn(globalThis, 'fetch').mockImplementation(async url => new Response(JSON.stringify(String(url).includes('/sample')
    ? { records: [{ values: { id: 9223372036854776000 } }], ...(withText ? { sample_json: sampleText } : {}) }
    : String(url).includes('/snapshots') ? { items: [], total: 0 } : { items: [source], total: 1 })));
  render(<SourcesPage projectId="p" canWrite onDirty={() => {}} />);
  fireEvent.click(await screen.findByRole('button', { name: '查看资源 →' }));
  fireEvent.click(screen.getByRole('button', { name: 'items' }));
  fireEvent.click(screen.getByRole('button', { name: '读取最多 20 条样本' }));
  if (withText) expect((await screen.findByLabelText('来源样本')).textContent).toBe(sampleText);
  else await screen.findByText(/未返回来源样本的精确文本/);
  expect(screen.queryByText(/9223372036854776000/)).toBeNull();
});



it('clears previously displayed source samples after current access is denied', async () => {
  let revoked = false;
  const source = { id: 's', name: '测试来源', kind: 'sqlite', revision: 1, config: {}, resources: [{ resource_id: 'A', name: 'A', fields: [], primary_key: ['id'], foreign_keys: [] }] };
  vi.spyOn(globalThis, 'fetch').mockImplementation(async url => {
    if (String(url).includes('/sample')) return revoked ? new Response(JSON.stringify({ error: { message: '来源访问已撤销' } }), { status: 403 }) : new Response(JSON.stringify({ sample_json: '{"secret":"先前授权样本"}' }));
    return new Response(JSON.stringify(String(url).includes('/snapshots') ? { items: [], total: 0 } : { items: [source], total: 1 }));
  });
  render(<SourcesPage projectId="p" canWrite onDirty={() => {}} />);
  fireEvent.click(await screen.findByRole('button', { name: '查看资源 →' }));
  fireEvent.click(screen.getByRole('button', { name: 'A' }));
  fireEvent.click(screen.getByRole('button', { name: '读取最多 20 条样本' }));
  await screen.findByText(/先前授权样本/);
  revoked = true;
  fireEvent.click(screen.getByRole('button', { name: '读取最多 20 条样本' }));
  await screen.findByText('来源访问已撤销');
  expect(screen.queryByText(/先前授权样本/)).toBeNull();
  expect(screen.queryByRole('button', { name: '收起' })).toBeNull();
});

it('does not reopen the old source when its file version finishes after a source switch', async () => {
  const sources = ['first', 'second'].map(id => ({ id, name: `来源 ${id}`, kind: 'tabular', revision: 1, config: {}, resources: [] }));
  vi.spyOn(globalThis, 'fetch').mockImplementation(async url => new Response(JSON.stringify(String(url).includes('/snapshots') ? { items: [], total: 0 } : { items: sources, total: 2 })));
  render(<SourcesPage projectId="p" canWrite onDirty={() => {}} />);
  fireEvent.click((await screen.findAllByRole('button', { name: '查看资源 →' }))[0]);
  fireEvent.click(screen.getByRole('button', { name: '测试：提交文件版本' }));
  fireEvent.click(screen.getAllByRole('button', { name: '查看资源 →' })[1]);
  await act(async () => { finishVersion(); });
  expect(screen.getByRole('heading', { name: '来源 second', level: 2 })).toBeTruthy();
  expect(screen.queryByRole('heading', { name: '来源 first', level: 2 })).toBeNull();
});
