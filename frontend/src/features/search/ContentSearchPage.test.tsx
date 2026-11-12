import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { ContentSearchPage, type ContentHit, type ContentIndex } from './ContentSearchPage';
import App from '../../app/App';

// Navigation tests exercise the content route, not WebGL (unavailable in jsdom).
vi.mock('../graph/GraphCanvas', () => ({ GraphCanvas: () => null }));

const index: ContentIndex = { document: { status: 'ready', generation: 'dg1', pending: 0, errors: 0 }, knowledge: { status: 'ready', generation: 'kg1', pending: 0, errors: 0 } };
const hash = 'a'.repeat(64);
const documentHit: ContentHit = { id: 'chunk-d', kind: 'document', asset_id: 'd1', title: '库存依据.docx', source_revision: 3, parse_revision: 5, source_fingerprint: 'fingerprint-d', generation: 'dg1', excerpt: '库存不足，请补充原料。', anchor: { element_path: 'docx/paragraph/301' }, status: 'partial', complete: false, quality: 'needs_review', warnings: ['第2页需要OCR'] };
const knowledgeHit: ContentHit = { ...documentHit, id: 'chunk-k', kind: 'knowledge', asset_id: 'k1', title: '已发布库存政策', source_revision: 2, parse_revision: 0, source_fingerprint: 'fingerprint-k', generation: 'kg1', excerpt: '已发布的安全库存规则', anchor: { element_path: 'body' }, status: 'ready', complete: true, quality: 'structured', warnings: [] };
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
const result = (items: ContentHit[] = [documentHit], extra: Record<string, unknown> = {}) => ({ items, total: items.length, limit: 20, offset: 0, query: { text: '库存 原料', terms: ['库存', '原料'], mode: 'all_terms_in_fragment' }, index, ...extra });
const evidence = (hit = documentHit) => ({ item: hit, unit: { text: '原始单元上下文，包括未命中的段落。', chunk_text: '<img src=x onerror=alert(1)>命中分块原文', unit_text_start: 20, unit_text_end: 60, evidence: { element_path: hit.anchor && (hit.anchor as { element_path: string }).element_path, sheet: '库存', cell_address: 'C7', content_hash: hash } }, coverage: { complete: hit.complete, quality: hit.quality, warnings: hit.warnings }, ...(hit.kind === 'document' ? { download_url: `/api/documents/d1/download?expected_revision=3&expected_hash=${hash}` } : { references: [{ document_id: 'd1', revision: 3, element_path: 'docx/paragraph/301' }] }) });
const search = async () => { fireEvent.change(screen.getByLabelText('内容关键词'), { target: { value: '库存 原料' } }); fireEvent.click(screen.getByRole('button', { name: '检索内容' })); await screen.findByText(documentHit.excerpt); };

it('searches without an ontology baseline and opens the exact document chunk beyond the ordinary preview with bounded plain-text context', async () => {
  const calls: { path: string; body: unknown }[] = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, options) => {
    const path = String(input); calls.push({ path, body: options?.body ? JSON.parse(String(options.body)) : undefined });
    if (path.includes('/status')) return json(index);
    if (path.endsWith('/evidence')) return json({ ...evidence(), unit: { ...evidence().unit, metadata: { table_cell: { value: '9223372036854775807', formula: '=A1' }, unexpected_numeric_value: 9223372036854775807 }, text: '完整上下文起点' + '正文'.repeat(7000) + '末尾未展示内容' } });
    return json(result([documentHit], { index: { ...index, document: { ...index.document, status: 'updating', pending: 1 } } }));
  });
  const view = render(<ContentSearchPage projectId="p" canManage={false} />); await search();
  expect(calls.find(call => call.path === '/api/search/content')?.body).toEqual({ project_id: 'p', q: '库存 原料', kind: 'all', evidence_mode: 'comprehensive_knowledge', limit: 20, offset: 0 });
  expect(calls.some(call => call.path.includes('/ontology'))).toBe(false);
  expect(screen.getByText(/全部查询词必须在同一个索引分块中匹配/)).not.toBeNull();
  expect(screen.getByText(/零条结果不能证明没有相关内容/)).not.toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '打开固定版本证据' }));
  await screen.findByLabelText('命中分块原文');
  expect(calls.find(call => call.path.endsWith('/evidence'))?.body).toEqual({ project_id: 'p', evidence_mode: 'comprehensive_knowledge', chunk_id: 'chunk-d', expected_generation: 'dg1', expected_fingerprint: 'fingerprint-d', expected_revision: 3, expected_parse_revision: 5 });
  expect(screen.getByText('C7')).not.toBeNull(); expect(screen.getByText('第2页需要OCR')).not.toBeNull();
  expect(screen.queryByLabelText('原始单元上下文')).toBeNull(); expect(view.container.querySelector('img,script')).toBeNull();
  expect(screen.getByRole('link', { name: '下载此版本原件（再次核权和校验）' }).getAttribute('href')).toBe(`/api/documents/d1/download?expected_revision=3&expected_hash=${hash}`);
  fireEvent.click(screen.getByRole('button', { name: '展开原始单元上下文' }));
  expect(screen.getByLabelText('原始单元上下文').textContent?.length).toBe(10000);
  expect(screen.getByText(/仅预览前 10000 个字符位置，未展示全部内容/)).not.toBeNull();
  expect(screen.queryByText(/末尾未展示内容/)).toBeNull();
  expect(screen.getByText(/整数超出浏览器精确范围，请以原文核对/)).not.toBeNull(); expect(screen.queryByText(/9223372036854776000/)).toBeNull();
  expect(screen.getByText(/"value":"9223372036854775807"/)).not.toBeNull();
});

it('opens only the published knowledge revision even for a maintainer and never invokes the current draft editor', async () => {
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async input => String(input).includes('/status') ? json(index) : String(input).endsWith('/evidence') ? json({ ...evidence(knowledgeHit), unit: { text: '已发布完整正文', chunk_text: '已发布命中正文' }, references: [{ document_id: 'd1', revision: 3, element_path: 'docx/paragraph/301', status: 'unavailable' }] }) : json(result([knowledgeHit])));
  render(<ContentSearchPage projectId="p" canManage />);
  fireEvent.change(screen.getByLabelText('内容关键词'), { target: { value: '库存' } }); fireEvent.click(screen.getByRole('button', { name: '检索内容' }));
  fireEvent.click(await screen.findByRole('button', { name: '打开固定版本证据' })); await screen.findByText('已发布命中正文');
  expect(screen.getByText(/即使你有编辑权限，也不会替换成当前草稿/)).not.toBeNull();
  expect(screen.queryByRole('button', { name: '保存草稿' })).toBeNull(); expect(screen.queryByRole('link', { name: /下载/ })).toBeNull();
  expect(fetcher.mock.calls.some(([url]) => String(url).includes('/knowledge/') || String(url).includes('/documents/'))).toBe(false);
  expect(screen.getByText('该已发布修订的来源引用')).not.toBeNull();
  expect(screen.getByText('该引用当前不可用，未提供原文或跳转。')).not.toBeNull();
});

it('clears evidence on scope changes, forces documents for original evidence, and rejects late project or scope responses', async () => {
  let resolveOld: (response: Response) => void = () => {}; const queries: Record<string, unknown>[] = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation((input, options) => {
    if (String(input).includes('/status')) return Promise.resolve(json(index));
    if (String(input).endsWith('/evidence')) return Promise.resolve(json(evidence()));
    const body = JSON.parse(String(options?.body)); queries.push(body);
    if (queries.length === 2 || body.project_id === 'old') return new Promise<Response>(resolve => { resolveOld = resolve; });
    return Promise.resolve(json(result()));
  });
  const view = render(<ContentSearchPage projectId="p" canManage={false} />); await search();
  fireEvent.click(screen.getByRole('button', { name: '打开固定版本证据' })); await screen.findByLabelText('命中分块原文');
  fireEvent.change(screen.getByLabelText('内容来源种类'), { target: { value: 'knowledge' } });
  expect(screen.queryByRole('complementary')).toBeNull(); expect(screen.queryByText(documentHit.excerpt)).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '检索内容' })); await waitFor(() => expect(queries).toHaveLength(2));
  fireEvent.change(screen.getByLabelText('内容证据模式'), { target: { value: 'original_evidence' } });
  await act(async () => resolveOld(json(result([knowledgeHit]))));
  expect(screen.queryByText(knowledgeHit.excerpt)).toBeNull(); expect((screen.getByLabelText('内容来源种类') as HTMLSelectElement).value).toBe('document');
  fireEvent.click(screen.getByRole('button', { name: '检索内容' })); await screen.findByText(documentHit.excerpt);
  expect(queries[2]).toMatchObject({ kind: 'document', evidence_mode: 'original_evidence' });
  view.rerender(<ContentSearchPage projectId="old" canManage={false} />);
  fireEvent.change(screen.getByLabelText('内容关键词'), { target: { value: '旧项目' } }); fireEvent.click(screen.getByRole('button', { name: '检索内容' }));
  await waitFor(() => expect(queries).toHaveLength(4)); view.rerender(<ContentSearchPage projectId="new" canManage={false} />);
  await act(async () => resolveOld(json(result())));
  expect(screen.queryByText(documentHit.excerpt)).toBeNull(); expect((screen.getByLabelText('内容关键词') as HTMLInputElement).value).toBe('');
});

it.each([403, 404, 503])('clears hits and evidence after the fixed-version resource becomes unavailable (%s)', async status => {
  let evidenceReads = 0;
  vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
    if (String(input).includes('/status')) return json(index);
    if (String(input).endsWith('/evidence')) { evidenceReads++; return evidenceReads === 1 ? json(evidence()) : json({ error: { message: '该证据当前不可读取', code: status === 503 ? 'source_authorization_unknown' : 'forbidden' } }, status); }
    return json(result());
  });
  render(<ContentSearchPage projectId="p" canManage={false} />); await search();
  fireEvent.click(screen.getByRole('button', { name: '打开固定版本证据' })); await screen.findByLabelText('命中分块原文');
  fireEvent.click(screen.getByRole('button', { name: '关闭侧栏' })); fireEvent.click(screen.getByRole('button', { name: '打开固定版本证据' }));
  await screen.findByText('该证据当前不可读取'); expect(screen.queryByRole('complementary')).toBeNull(); expect(screen.queryByText(documentHit.excerpt)).toBeNull();
  expect(screen.queryByText('dg1')).toBeNull(); expect(evidenceReads).toBe(2);
});

it('does not replace stale evidence on 409 or accept an evidence response from another generation', async () => {
  let evidenceReads = 0;
  vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
    if (String(input).includes('/status')) return json(index);
    if (String(input).endsWith('/evidence')) { evidenceReads++; return evidenceReads === 1 ? json({ error: { message: '原文解析修订已改变，请重新检索' } }, 409) : json(evidence({ ...documentHit, generation: 'other-generation' })); }
    return json(result());
  });
  render(<ContentSearchPage projectId="p" canManage={false} />); await search();
  fireEvent.click(screen.getByRole('button', { name: '打开固定版本证据' })); await screen.findByText('原文解析修订已改变，请重新检索');
  expect(screen.queryByLabelText('命中分块原文')).toBeNull(); expect((screen.getByLabelText('内容关键词') as HTMLInputElement).value).toBe('库存 原料'); expect(evidenceReads).toBe(1);
  fireEvent.click(screen.getByRole('button', { name: '关闭侧栏' })); fireEvent.click(screen.getByRole('button', { name: '打开固定版本证据' }));
  await screen.findByText('证据响应与搜索命中的版本绑定不一致，请重新检索后打开。'); expect(screen.queryByLabelText('命中分块原文')).toBeNull();
});

it('keeps pagination on applied queries and distinguishes an incomplete empty index from a negative search result', async () => {
  const bodies: Record<string, unknown>[] = []; let atLimit = false;
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, options) => {
    if (String(input).includes('/status')) return json(index);
    const body = JSON.parse(String(options?.body)); bodies.push(body);
    return json(result(body.offset ? [] : [documentHit], { total: atLimit ? 2000 : 21, offset: atLimit ? 1000 : body.offset, index: { ...index, document: { ...index.document, status: 'failed', errors: 1 } } }));
  });
  render(<ContentSearchPage projectId="p" canManage={false} />); await search();
  fireEvent.change(screen.getByLabelText('内容关键词'), { target: { value: '尚未执行的新词' } }); fireEvent.click(screen.getByRole('button', { name: '下一页内容' }));
  await screen.findByText('当前暂无可返回的内容命中'); expect(bodies[1]).toMatchObject({ q: '库存 原料', offset: 20 });
  expect(screen.queryByText('没有符合条件的内容分块')).toBeNull();
  atLimit = true; fireEvent.click(screen.getByRole('button', { name: '检索内容' }));
  await screen.findByText(/偏移 1000/); expect((screen.getByRole('button', { name: '下一页内容' }) as HTMLButtonElement).disabled).toBe(true);
});

it('restricts rebuild to maintainers and requires an explicit status refresh after generation conflict before queueing a new rebuild', async () => {
  let current = index; const writes: Record<string, unknown>[] = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, options) => {
    if (String(input).includes('/status')) return json(current);
    if (String(input).endsWith('/rebuild')) { writes.push(JSON.parse(String(options?.body))); if (writes.length === 1) { current = { ...index, document: { ...index.document, generation: 'dg2' } }; return json({ error: { message: '内容索引代次已改变' } }, 409); } return json({ ...current, document: { ...current.document, status: 'updating', pending: 1 } }, 202); }
    return json(result());
  });
  const view = render(<ContentSearchPage projectId="p" canManage={false} />); await search();
  expect(screen.queryByRole('button', { name: '重建当前范围内容索引' })).toBeNull();
  view.rerender(<ContentSearchPage projectId="p" canManage />);
  fireEvent.click(screen.getByRole('button', { name: '重建当前范围内容索引' })); await screen.findByText('内容索引代次已改变');
  expect(writes[0]).toEqual({ project_id: 'p', kind: 'all', expected_generations: { document: 'dg1', knowledge: 'kg1' } });
  expect((screen.getByRole('button', { name: '重建当前范围内容索引' }) as HTMLButtonElement).disabled).toBe(true); expect(screen.queryByText(documentHit.excerpt)).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '刷新内容索引状态' }));
  await waitFor(() => expect((screen.getByRole('button', { name: '重建当前范围内容索引' }) as HTMLButtonElement).disabled).toBe(false)); expect(writes).toHaveLength(1);
  fireEvent.click(screen.getByRole('button', { name: '重建当前范围内容索引' })); await screen.findByText(/内容索引重建已排队/);
  expect(writes[1]).toMatchObject({ expected_generations: { document: 'dg2', knowledge: 'kg1' } });
  expect((screen.getByLabelText('内容关键词') as HTMLInputElement).value).toBe('库存 原料'); expect(screen.queryByText(documentHit.excerpt)).toBeNull();
  fireEvent.change(screen.getByLabelText('内容来源种类'), { target: { value: 'document' } });
  fireEvent.click(screen.getByRole('button', { name: '重建当前范围内容索引' })); await waitFor(() => expect(writes).toHaveLength(3));
  expect(writes[2]).toEqual({ project_id: 'p', kind: 'document', expected_generations: { document: 'dg2' } });
});

it('binds original evidence mode and clears existing results and evidence when a refreshed index generation changes', async () => {
  let current = index; const requests: Record<string, unknown>[] = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, options) => {
    if (String(input).includes('/status')) return json(current);
    if (String(input).endsWith('/evidence')) { requests.push(JSON.parse(String(options?.body))); return json(evidence()); }
    return json(result());
  });
  render(<ContentSearchPage projectId="p" canManage={false} />);
  fireEvent.change(screen.getByLabelText('内容证据模式'), { target: { value: 'original_evidence' } }); await search();
  fireEvent.click(screen.getByRole('button', { name: '打开固定版本证据' })); await screen.findByLabelText('命中分块原文');
  expect(requests[0]).toMatchObject({ evidence_mode: 'original_evidence', expected_parse_revision: 5 });
  current = { ...index, document: { ...index.document, generation: 'dg2' } };
  fireEvent.click(screen.getByRole('button', { name: '刷新内容索引状态' }));
  await screen.findByText(/内容索引代次已变化，已清除旧结果和证据/);
  expect(screen.queryByRole('complementary')).toBeNull(); expect(screen.queryByText(documentHit.excerpt)).toBeNull();
  expect((screen.getByLabelText('内容关键词') as HTMLInputElement).value).toBe('库存 原料');
});

it('adds the content search navigation with current-project management permission rather than the account permission union', async () => {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
    if (String(input) === '/api/auth/me') return json({ features: ['knowledge'], user: { id: 'u', email: 'reader@example.test', display_name: '读者' }, tenant: { id: 't', name: '测试企业' }, projects: [{ id: 'reader', name: '只读项目', permissions: ['objects:read'] }, { id: 'owner', name: '维护项目', permissions: ['objects:read', 'sources:write'] }], permissions: ['objects:read', 'sources:write'] });
    if (String(input).includes('/search/content/status')) return json(index);
    return json({ items: [], total: 0, release_id: null, version: 0 });
  });
  render(<App />);
  fireEvent.click(await screen.findByRole('button', { name: '知识页面' }));
  fireEvent.click(await screen.findByRole('button', { name: '内容检索' }));
  await screen.findByRole('heading', { name: '内容检索' }); await screen.findByText('文档：已就绪');
  expect(screen.queryByRole('button', { name: '重建当前范围内容索引' })).toBeNull();
  fireEvent.change(screen.getByLabelText('内容关键词'), { target: { value: '旧项目条件' } });
  fireEvent.change(screen.getByLabelText('当前项目'), { target: { value: 'owner' } });
  await screen.findByRole('button', { name: '重建当前范围内容索引' });
  expect((screen.getByLabelText('内容关键词') as HTMLInputElement).value).toBe('');
});

it('opens bound knowledge references only through their immutable parse endpoint and labels legacy references without guessing a current parse', async () => {
  const paths: string[] = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
    const path = String(input); paths.push(path);
    if (path.includes('/status')) return json(index);
    if (path.endsWith('/parses/parse-old')) return json({ id: 'parse-old', source_revision: 3, parse_revision: 1, filename: '历史证据.docx', content_hash: hash, ir_fingerprint: 'old-fp', origin: 'worker', is_current: false, engine: 'basic', parser: 'test', parser_version: '1', ir: { complete: true, parser: 'test', parser_version: '1', elements: [{ element_id: 'e1', kind: 'paragraph', text: '固定历史解析原文', evidence: {} }] }, download_url: `/api/documents/d1/download?expected_revision=3&expected_hash=${hash}` });
    if (path.endsWith('/evidence')) return json({ ...evidence(knowledgeHit), references: [{ document_id: 'd1', revision: 3, parse_id: 'parse-old', status: 'bound' }, { document_id: 'd2', revision: 1, status: 'legacy_unbound' }] });
    return json(result([knowledgeHit]));
  });
  render(<ContentSearchPage projectId="p" canManage />);
  fireEvent.change(screen.getByLabelText('内容关键词'), { target: { value: '库存' } }); fireEvent.click(screen.getByRole('button', { name: '检索内容' }));
  fireEvent.click(await screen.findByRole('button', { name: '打开固定版本证据' })); await screen.findByText('旧引用未绑定解析版本，需重新选择来源保存新修订。');
  expect(screen.getAllByRole('button', { name: '打开引用的固定历史解析' })).toHaveLength(1);
  fireEvent.click(screen.getByRole('button', { name: '打开引用的固定历史解析' })); await screen.findByText('固定历史解析原文');
  expect(paths.filter(path => path.includes('/documents/'))).toEqual(['/api/documents/d1/parses/parse-old']);
  expect(screen.queryByRole('button', { name: '提交重新解析' })).toBeNull();
});
