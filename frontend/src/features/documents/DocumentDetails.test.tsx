import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { DocumentDetails, DocumentInspector } from './DocumentDetails';
import { KnowledgeEditor } from '../knowledge/KnowledgePages';
import { DocumentsPage } from './DocumentsPage';

const hash = 'a'.repeat(64); const fp1 = 'b'.repeat(64); const fp2 = 'c'.repeat(64);
const ir = (text = '保留的旧解析原文', complete = true) => ({ complete, quality: 'structured', parser: 'test-parser', parser_version: '1', warnings: [], pages: [], elements: [{ element_id: 'e1', kind: 'paragraph', text, evidence: { element_path: 'docx/paragraph/1' } }] });
const doc = { id: 'd1', filename: '证据.docx', revision: 2, parse_revision: 1, content_hash: hash, ir_fingerprint: fp1, current_parse_id: 'parse-1', active_parse_job: null, state: 'ready', ir: ir() };
const parse = { id: 'parse-1', document_id: 'd1', filename: doc.filename, source_revision: 2, parse_revision: 1, content_hash: hash, ir_fingerprint: fp1, parser: 'test-parser', parser_version: '1', engine: 'basic', origin: 'legacy', configuration: {}, created_at: '2026-10-29T00:00:00Z', is_current: false, ir: ir('已归档原文'), ir_json: '{"value":9223372036854775807}', download_url: `/api/documents/d1/download?expected_revision=2&expected_hash=${hash}` };
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
const initialPage = { id: 'k1', title: '知识', body: '原正文', references: [], revision: 1, published_revision: 1 };

it('identifies native snapshots and offers the native parser without a Word or Docling path', async () => {
  const nativeDoc = { ...doc, filename: '知识.feishu-docx.json', ir: { ...ir('原生块正文'), format: 'feishu_docx' } };
  const writes: { engine: string }[] = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (_input, options) => {
    if (options?.method === 'POST') { writes.push(JSON.parse(String(options.body))); return json({ document: nativeDoc, job: { id: 'native-job', status: 'pending' } }, 202); }
    return json(nativeDoc);
  });
  render(<DocumentInspector documentId="d1" canReparse />);
  await screen.findByText('原生块正文');
  expect(screen.getByRole('link', { name: '下载原生块快照（校验修订与哈希）' })).not.toBeNull();
  expect(screen.queryByRole('option', { name: /Docling/ })).toBeNull();
  fireEvent.change(screen.getByLabelText('重新解析引擎'), { target: { value: 'basic' } });
  fireEvent.click(screen.getByRole('button', { name: '提交重新解析' }));
  await screen.findByText(/重新解析任务 native-job 已提交/);
  expect(writes[0].engine).toBe('basic');
});

it('opens document history for a read-only user, pages summaries and preserves exact archived JSON without offering reparse or restore', async () => {
  const paths: string[] = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
    const path = String(input); paths.push(path);
    if (path.includes('/parses?')) return json({ items: [parse], total: 26 });
    if (path.endsWith('/parses/parse-1')) return json(parse);
    if (path === '/api/documents/d1') return json(doc);
    return json({ items: [doc], total: 1 });
  });
  render(<DocumentsPage projectId="p" canWrite={false} />);
  fireEvent.click(await screen.findByRole('button', { name: '查看结构与原件' })); await screen.findByText('保留的旧解析原文');
  expect(screen.queryByRole('button', { name: '提交重新解析' })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '查看解析历史' }));
  fireEvent.click(await screen.findByRole('button', { name: '下一页解析历史' }));
  await waitFor(() => expect(paths.some(path => path.endsWith('offset=25'))).toBe(true));
  fireEvent.click(await screen.findByRole('button', { name: '查看解析版本 1' })); await screen.findByText('已归档原文');
  expect(screen.queryByText('保留的旧解析原文')).toBeNull(); expect(screen.getByText(/没有恢复为当前版本的写操作/)).not.toBeNull();
  expect(screen.getByText(/旧解析配置可能未知/)).not.toBeNull();
  const summary = screen.getByText('归档解析精确JSON原文'); fireEvent.click(summary);
  const detail = summary.closest('details')!; detail.open = true; fireEvent(detail, new Event('toggle'));
  expect((await screen.findByLabelText('归档解析精确JSON原文')).textContent).toBe('{"value":9223372036854775807}');
  expect(screen.getByRole('link', { name: '下载对应原件（校验修订与哈希）' }).getAttribute('href')).toBe(parse.download_url);
});

it('submits a frozen reparse baseline, prevents another active job, and preserves the old IR after the job fails', async () => {
  let current: Record<string, unknown> = doc; const bodies: unknown[] = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (_input, options) => {
    if (options?.method === 'POST') { bodies.push(JSON.parse(String(options.body))); current = { ...doc, active_parse_job: { id: 'job-1', status: 'pending', engine: 'docling' } }; return json({ document: current, job: { id: 'job-1', status: 'pending' } }, 202); }
    return json(current);
  });
  render(<DocumentDetails documentId="d1" canReparse onClose={vi.fn()} />); await screen.findByText('保留的旧解析原文');
  fireEvent.change(screen.getByLabelText('重新解析引擎'), { target: { value: 'docling' } }); fireEvent.click(screen.getByRole('button', { name: '提交重新解析' }));
  await screen.findByText(/重新解析任务 job-1 已提交/);
  expect(bodies).toEqual([{ expected_revision: 2, expected_parse_revision: 1, expected_ir_fingerprint: fp1, engine: 'docling' }]);
  expect(screen.getByText('保留的旧解析原文')).not.toBeNull(); expect((screen.getByRole('button', { name: '提交重新解析' }) as HTMLButtonElement).disabled).toBe(true);
  current = { ...doc, error: '新解析任务失败，保留旧版本' };
  fireEvent.click(screen.getByRole('button', { name: '刷新当前文档元信息' })); await screen.findByText(/新解析任务失败，保留旧版本/);
  expect(screen.getByText('保留的旧解析原文')).not.toBeNull(); expect((screen.getByRole('button', { name: '提交重新解析' }) as HTMLButtonElement).disabled).toBe(false); expect(bodies).toHaveLength(1);
});

it('keeps the selected engine on stale-baseline conflict and only uses a newly read baseline after explicit refresh', async () => {
  let current = doc; const bodies: unknown[] = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (_input, options) => {
    if (options?.method === 'POST') { bodies.push(JSON.parse(String(options.body))); current = { ...doc, parse_revision: 2, ir_fingerprint: fp2, current_parse_id: 'parse-2', ir: ir('新当前解析') }; return json({ error: { message: '解析基线已改变' } }, 409); }
    return json(current);
  });
  render(<DocumentInspector documentId="d1" canReparse />); await screen.findByText('保留的旧解析原文');
  fireEvent.change(screen.getByLabelText('重新解析引擎'), { target: { value: 'basic' } }); fireEvent.click(screen.getByRole('button', { name: '提交重新解析' })); await screen.findByText('解析基线已改变');
  expect((screen.getByLabelText('重新解析引擎') as HTMLSelectElement).value).toBe('basic'); expect((screen.getByRole('button', { name: '提交重新解析' }) as HTMLButtonElement).disabled).toBe(true); expect(bodies).toHaveLength(1);
  fireEvent.click(screen.getByRole('button', { name: '刷新当前文档元信息' })); await screen.findByText('新当前解析');
  expect(bodies).toHaveLength(1); fireEvent.click(screen.getByRole('button', { name: '提交重新解析' })); await screen.findByText('解析基线已改变');
  expect(bodies[1]).toEqual({ expected_revision: 2, expected_parse_revision: 2, expected_ir_fingerprint: fp2, engine: 'basic' });
});

it.each([403, 404, 503])('clears a fixed historical preview when access fails and never falls back to the current document (%s)', async status => {
  let failure = false; const paths: string[] = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async input => { paths.push(String(input)); return failure ? json({ error: { message: '归档版本不可读取', code: status === 503 ? 'source_authorization_unknown' : 'forbidden' } }, status) : json(parse); });
  render(<DocumentInspector documentId="d1" fixedParseId="parse-1" />); await screen.findByText('已归档原文');
  failure = true; fireEvent.click(screen.getByRole('button', { name: '重新核对历史版本权限' })); await screen.findByText('归档版本不可读取');
  expect(screen.queryByText('已归档原文')).toBeNull(); expect(screen.queryByRole('link')).toBeNull();
  expect(paths).toEqual(['/api/documents/d1/parses/parse-1', '/api/documents/d1/parses/parse-1']);
});

it('discards late previews when the selected document changes and refuses to bind incomplete IR', async () => {
  let resolveOld: (response: Response) => void = () => {}; const onReference = vi.fn();
  vi.spyOn(globalThis, 'fetch').mockImplementation(input => String(input).endsWith('/d1') ? new Promise<Response>(resolve => { resolveOld = resolve; }) : Promise.resolve(json({ ...doc, id: 'd2', ir: ir('部分解析内容', false), state: 'partial' })));
  const view = render(<DocumentInspector documentId="d1" onReference={onReference} />);
  view.rerender(<DocumentInspector documentId="d2" onReference={onReference} />); await screen.findByText('部分解析内容');
  await act(async () => resolveOld(json(doc))); expect(screen.queryByText('保留的旧解析原文')).toBeNull();
  expect((screen.getByRole('button', { name: '使用此解析版本作为引用' }) as HTMLButtonElement).disabled).toBe(true); expect(onReference).not.toHaveBeenCalled();
});

it('creates a reference only from the preview and uses canonical archived references in later knowledge saves', async () => {
  const writes: Record<string, unknown>[] = []; const canonical = { document_id: 'd1', revision: 2, parse_revision: 1, ir_fingerprint: fp1, content_hash: hash, parse_id: 'parse-1', element_path: 'docx/paragraph/1' };
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, options) => {
    const path = String(input);
    if (options?.method === 'PATCH') { writes.push(JSON.parse(String(options.body))); return json({ ...initialPage, body: '已保存正文', revision: writes.length + 1, references: [canonical] }); }
    if (path === '/api/documents/d1') return json({ ...doc, current_parse_id: null });
    if (path.includes('/documents?')) return json({ items: [doc], total: 1 });
    return json({ items: [], total: 0 });
  });
  render(<KnowledgeEditor projectId="p" initialPage={initialPage} canWrite canPublish onDirty={vi.fn()} onClose={vi.fn()} onUpdated={vi.fn()} />);
  await screen.findByRole('option', { name: /证据.docx/ });
  fireEvent.change(screen.getByLabelText('选择可引用文档'), { target: { value: 'd1' } }); await screen.findByText('保留的旧解析原文');
  fireEvent.change(screen.getByLabelText(/结构锚点（可选）/), { target: { value: 'docx/paragraph/1' } });
  fireEvent.click(screen.getByRole('button', { name: '来源定位' })); expect(writes).toHaveLength(0);
  fireEvent.click(screen.getByRole('button', { name: '使用此解析版本作为引用' })); expect(writes).toHaveLength(0);
  expect(screen.getByText(/保存时由服务端归档并返回固定解析标识/)).not.toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '保存草稿' })); await screen.findByText(/草稿已保存/);
  expect(writes[0].references).toEqual([{ document_id: 'd1', revision: 2, parse_revision: 1, ir_fingerprint: fp1, content_hash: hash, element_path: 'docx/paragraph/1' }]);
  expect(JSON.parse((screen.getByLabelText(/来源引用结构/) as HTMLTextAreaElement).value)).toEqual([canonical]);
  fireEvent.change(screen.getByLabelText(/正文（Markdown）/), { target: { value: '再修改正文' } }); fireEvent.click(screen.getByRole('button', { name: '保存草稿' }));
  await waitFor(() => expect(writes).toHaveLength(2)); expect(writes[1].references).toEqual([canonical]);
});

it('binds a deliberately selected historical parse and does not guess legacy reference versions', async () => {
  const writes: Record<string, unknown>[] = []; const oldRef = { document_id: 'd1', revision: 2 };
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, options) => {
    const path = String(input);
    if (options?.method === 'PATCH') { const body = JSON.parse(String(options.body)); writes.push(body); return json({ ...initialPage, revision: 2, references: body.references }); }
    if (path === '/api/documents/d1') return json({ ...doc, parse_revision: 2, ir_fingerprint: fp2, current_parse_id: 'parse-2' });
    if (path.endsWith('/parses/parse-1')) return json(parse);
    if (path.includes('/parses?')) return json({ items: [parse], total: 1 });
    return json({ items: path.includes('/documents?') ? [doc] : [], total: 1 });
  });
  render(<KnowledgeEditor projectId="p" initialPage={{ ...initialPage, references: [oldRef] }} canWrite canPublish onDirty={vi.fn()} onClose={vi.fn()} onUpdated={vi.fn()} />);
  expect(screen.getByText('旧引用未绑定解析版本，需重新选择来源保存新修订。')).not.toBeNull(); expect(writes).toHaveLength(0);
  fireEvent.click(screen.getByRole('button', { name: '移除引用' })); await screen.findByRole('option', { name: /证据.docx/ });
  fireEvent.change(screen.getByLabelText('选择可引用文档'), { target: { value: 'd1' } }); await screen.findByText('保留的旧解析原文');
  fireEvent.click(screen.getByRole('button', { name: '查看解析历史' })); fireEvent.click(await screen.findByRole('button', { name: '查看解析版本 1' })); await screen.findByText('已归档原文');
  fireEvent.click(screen.getByRole('button', { name: '使用此解析版本作为引用' })); fireEvent.click(screen.getByRole('button', { name: '保存草稿' }));
  await waitFor(() => expect(writes).toHaveLength(1)); expect(writes[0].references).toEqual([{ document_id: 'd1', revision: 2, parse_revision: 1, ir_fingerprint: fp1, content_hash: hash, parse_id: 'parse-1' }]);
});
