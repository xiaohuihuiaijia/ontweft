import { useEffect, useRef, useState } from 'react';
import { ApiError, request, scoped, protectedReadUnavailable } from '../../shared/api/api';
import { Drawer, Empty, ErrorNotice, Loading, PageTitle, Status } from '../../shared/ui/components';
import { DocumentInspector } from '../documents/DocumentDetails';

type Kind = 'all' | 'document' | 'knowledge';
type EvidenceMode = 'original_evidence' | 'comprehensive_knowledge';
interface IndexState { status: 'not_ready' | 'updating' | 'ready' | 'failed'; generation: string | null; pending: number; errors: number }
export interface ContentIndex { document: IndexState; knowledge: IndexState }
interface Coverage { complete: boolean; quality: string; warnings: string[]; pages?: Record<string, unknown>[] }
export interface ContentHit extends Coverage {
  id: string; kind: 'document' | 'knowledge'; asset_id: string; title: string;
  source_revision: number; parse_revision: number; source_fingerprint: string; generation: string;
  excerpt: string; anchor: unknown; status: 'ready' | 'partial';
}
interface SearchInput { project_id: string; q: string; kind: Kind; evidence_mode: EvidenceMode; limit: number; offset: number }
interface SearchResult { items: ContentHit[]; total: number; limit: number; offset: number; query: { text: string; terms: string[]; mode: 'all_terms_in_fragment' }; index: ContentIndex }
interface EvidenceResult {
  item: ContentHit;
  unit: { text: string; chunk_text: string; evidence?: unknown; provenance?: unknown[]; metadata?: unknown; unit_text_start?: number; unit_text_end?: number };
  references?: Record<string, unknown>[]; coverage: Coverage; download_url?: string;
}
const kindLabel = { document: '文档', knowledge: '已发布知识' };
const indexLabel = { not_ready: '尚未就绪', updating: '更新中', ready: '已就绪', failed: '处理失败' };
const denied = protectedReadUnavailable;
const sameHit = (left: ContentHit, right: ContentHit) => ['id', 'kind', 'asset_id', 'source_revision', 'parse_revision', 'source_fingerprint', 'generation'].every(key => left[key as keyof ContentHit] === right[key as keyof ContentHit]);
const knownAnchorLabels: Record<string, string> = { element_path: '结构路径', page_number: '页码', page: '页码', sheet: '工作表', cell_address: '单元格', line_start: '开始行', line_end: '结束行', char_start: '开始字符', char_end: '结束字符', bbox: '原始坐标', coordinate_system: '坐标体系', source_id: '来源标识', source_revision: '来源修订', content_hash: '原件哈希' };
const safeMetadataValue = (_key: string, value: unknown) => typeof value === 'number' && Number.isInteger(value) && !Number.isSafeInteger(value) ? '整数超出浏览器精确范围，请以原文核对' : value;

function AnchorView({ value }: { value: unknown }) {
  if (!value) return <p className="muted">未提供额外定位信息；不会推测页码或坐标。</p>;
  if (typeof value !== 'object' || Array.isArray(value)) return <pre className="content-text">{typeof value === 'string' ? value : JSON.stringify(value, safeMetadataValue)}</pre>;
  const entries = Object.entries(value).filter(([, entry]) => entry !== null && entry !== undefined);
  return <dl className="content-anchor">{entries.map(([key, entry]) => <div key={key}><dt>{knownAnchorLabels[key] ?? key}</dt><dd>{typeof entry === 'object' ? JSON.stringify(entry, safeMetadataValue) : String(safeMetadataValue(key, entry))}</dd></div>)}</dl>;
}

function BoundedText({ value, label, limit = 10000 }: { value: string; label: string; limit?: number }) {
  const clipped = value.length > limit;
  let text = value.slice(0, limit);
  if (clipped && /[\uD800-\uDBFF]$/.test(text)) text = text.slice(0, -1);
  return <>{clipped && <p className="notice warning">{label}较长，此处仅预览前 {limit} 个字符位置，未展示全部内容。</p>}<pre className="content-text" aria-label={label}>{text}</pre></>;
}

function CoverageView({ value, kind }: { value: Coverage; kind: ContentHit['kind'] }) {
  return <div className={`content-coverage ${value.complete ? '' : 'notice warning'}`}>
    <p>{kind === 'document' ? (value.complete ? '来源解析标记完整' : '来源仅部分解析，未命中不能证明原件没有相关内容') : '内容来自指定的已发布知识修订'} · 质量：{value.quality || '未提供'}</p>
    <p className="muted">完整性描述来源处理范围，不代表一个命中片段包含整个来源。</p>
    {value.warnings?.map((warning, i) => <p className="notice warning" key={i}>{warning}</p>)}
    {!!value.pages?.length && <details><summary>实际逐页处理记录（{value.pages.length} 项）</summary>{value.pages.slice(0, 100).map((page, i) => <AnchorView key={i} value={page} />)}{value.pages.length > 100 && <p className="muted">仅预览前100项处理记录。</p>}</details>}
  </div>;
}

function downloadHref(value: EvidenceResult): string | undefined {
  if (value.item.kind !== 'document' || !value.download_url) return undefined;
  const prefix = `/api/documents/${encodeURIComponent(value.item.asset_id)}/download?`;
  if (!value.download_url.startsWith(prefix)) return undefined;
  const query = new URLSearchParams(value.download_url.slice(prefix.length));
  return query.get('expected_revision') === String(value.item.source_revision) && /^[a-f0-9]{64}$/i.test(query.get('expected_hash') ?? '') ? value.download_url : undefined;
}

function EvidenceDrawer({ hit, data, error, loading, onClose }: { hit: ContentHit; data?: EvidenceResult; error: unknown; loading: boolean; onClose: () => void }) {
  const [context, setContext] = useState(false);
  const [reference, setReference] = useState<{ documentId: string; parseId: string }>();
  const href = data && downloadHref(data);
  return <Drawer title="搜索命中证据（固定版本）" onClose={onClose}><div className="drawer-body content-evidence">
    <ErrorNotice error={error} />{loading && <Loading />}
    {data && <><div className="inline-info"><span>{kindLabel[data.item.kind]}</span><span>{data.item.kind === 'knowledge' ? '已发布修订' : '文档修订'} {data.item.source_revision}</span></div>
      <h3>{data.item.title}</h3>{data.item.kind === 'knowledge' && <p className="notice">只读展示搜索命中的已发布修订；即使你有编辑权限，也不会替换成当前草稿。</p>}
      <CoverageView value={data.coverage} kind={data.item.kind} />
      <h4>命中分块原文</h4>{typeof data.unit.chunk_text === 'string' ? <BoundedText value={data.unit.chunk_text} label="命中分块原文" /> : <p className="notice warning">服务未返回命中分块原文，不能用其他版本正文替代。</p>}
      {data.unit.unit_text_start !== undefined && data.unit.unit_text_end !== undefined && <p className="muted">原始单元内字符范围：{data.unit.unit_text_start}–{data.unit.unit_text_end}（使用服务端返回的位置）。</p>}
      <button type="button" className="secondary" aria-expanded={context} onClick={() => setContext(value => !value)}>{context ? '收起原始单元上下文' : '展开原始单元上下文'}</button>
      {context && <section><h4>原始单元上下文（有限预览）</h4><BoundedText value={data.unit.text} label="原始单元上下文" /></section>}
      <h4>命中锚点</h4><AnchorView value={data.item.anchor} />
      {data.unit.evidence != null && <><h4>原始证据定位</h4><AnchorView value={data.unit.evidence} /></>}
      {!!data.unit.provenance?.length && <details><summary>全部来源定位（{data.unit.provenance.length} 项）</summary>{data.unit.provenance.map((entry, i) => <AnchorView value={entry} key={i} />)}</details>}
      {data.unit.metadata != null && <details><summary>单元元信息</summary><AnchorView value={data.unit.metadata} /></details>}
      {!!data.references?.length && <section><h4>该已发布修订的来源引用</h4><p className="muted">已绑定的引用只打开固定归档解析，重新核权；不会用当前文档替代旧版本。</p>{data.references.map((ref, i) => <div key={i}>{ref.status === 'unavailable' ? <p className="notice warning">该引用当前不可用，未提供原文或跳转。</p> : ref.status === 'legacy_unbound' ? <p className="notice warning">旧引用未绑定解析版本，需重新选择来源保存新修订。</p> : ref.status === 'bound' ? <p className="muted">引用已绑定固定解析版本</p> : <p className="muted">引用绑定状态未确认</p>}<AnchorView value={ref} />{ref.status === 'bound' && typeof ref.document_id === 'string' && typeof ref.parse_id === 'string' && <button type="button" className="secondary" onClick={() => setReference({ documentId: ref.document_id as string, parseId: ref.parse_id as string })}>打开引用的固定历史解析</button>}</div>)}</section>}
      {reference && <section className="reference-preview"><button type="button" className="text-button" onClick={() => setReference(undefined)}>关闭引用历史解析</button><DocumentInspector documentId={reference.documentId} fixedParseId={reference.parseId} /></section>}
      {href && <a className="download-link" href={href} target="_blank" rel="noreferrer">下载此版本原件（再次核权和校验）</a>}
      {data.item.kind === 'document' && !href && <p className="muted">服务未提供可校验版本的原件入口。</p>}
      <details><summary>证据版本绑定</summary><AnchorView value={{ asset_id: hit.asset_id, source_revision: hit.source_revision, parse_revision: hit.parse_revision, source_fingerprint: hit.source_fingerprint, generation: hit.generation }} /></details>
    </>}
  </div></Drawer>;
}

export function ContentSearchPage({ projectId, canManage }: { projectId: string; canManage: boolean }) { return <ContentSearchSession key={projectId} projectId={projectId} canManage={canManage} />; }

function ContentSearchSession({ projectId, canManage }: { projectId: string; canManage: boolean }) {
  const [q, setQ] = useState(''); const [kind, setKind] = useState<Kind>('all'); const [mode, setMode] = useState<EvidenceMode>('comprehensive_knowledge');
  const [index, setIndex] = useState<ContentIndex>(); const [indexError, setIndexError] = useState<unknown>(); const [indexLoading, setIndexLoading] = useState(false);
  const [result, setResult] = useState<SearchResult>(); const [applied, setApplied] = useState<SearchInput>(); const [error, setError] = useState<unknown>(); const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState<ContentHit>(); const [evidence, setEvidence] = useState<EvidenceResult>(); const [evidenceError, setEvidenceError] = useState<unknown>(); const [evidenceLoading, setEvidenceLoading] = useState(false);
  const [rebuilding, setRebuilding] = useState(false); const [rebuildBlocked, setRebuildBlocked] = useState(false); const [manageDenied, setManageDenied] = useState(false); const [message, setMessage] = useState('');
  const mounted = useRef(false);
  const lastIndex = useRef<ContentIndex | undefined>(undefined);
  const searchRequest = useRef<AbortController | undefined>(undefined); const evidenceRequest = useRef<AbortController | undefined>(undefined); const statusRequest = useRef<AbortController | undefined>(undefined); const rebuildRequest = useRef<AbortController | undefined>(undefined);
  const clearEvidence = () => { evidenceRequest.current?.abort(); setSelected(undefined); setEvidence(undefined); setEvidenceError(undefined); setEvidenceLoading(false); };
  const clearResults = () => { searchRequest.current?.abort(); setResult(undefined); setApplied(undefined); setLoading(false); clearEvidence(); };
  const changeScope = (nextKind: Kind, nextMode: EvidenceMode) => { clearResults(); setKind(nextKind); setMode(nextMode); setError(undefined); setMessage('检索范围已更改，请主动执行新查询。'); };
  const readStatus = async (manual = false) => {
    statusRequest.current?.abort(); const pending = new AbortController(); statusRequest.current = pending;
    setIndexLoading(true); setIndexError(undefined); setIndex(undefined);
    try {
      const value = await request<ContentIndex>(scoped('/search/content/status', projectId), { signal: pending.signal });
      if (pending.signal.aborted || !mounted.current) return;
      if (lastIndex.current && (['document', 'knowledge'] as const).some(key => lastIndex.current![key].generation !== value[key].generation)) {
        clearResults(); setMessage('内容索引代次已变化，已清除旧结果和证据。关键词保留，请主动重新检索。');
      }
      lastIndex.current = value; setIndex(value); if (manual) setRebuildBlocked(false);
    } catch (failure) { if (!pending.signal.aborted && mounted.current) { setIndexError(failure); if (denied(failure)) clearResults(); } }
    finally { if (!pending.signal.aborted && mounted.current) setIndexLoading(false); }
  };
  useEffect(() => { mounted.current = true; void readStatus(); return () => { mounted.current = false; [searchRequest, evidenceRequest, statusRequest, rebuildRequest].forEach(ref => ref.current?.abort()); }; }, []);
  const run = async (input: SearchInput) => {
    if (rebuilding) return;
    clearResults(); const pending = new AbortController(); searchRequest.current = pending;
    setLoading(true); setError(undefined); setMessage(''); setApplied(input);
    try {
      const value = await request<SearchResult>('/search/content', { method: 'POST', body: JSON.stringify(input), signal: pending.signal });
      if (!pending.signal.aborted && mounted.current) setResult(value);
    } catch (failure) {
      if (!pending.signal.aborted && mounted.current) { setError(failure instanceof ApiError && failure.status === 0 ? new ApiError(0, '内容检索未完成，请检查网络后主动重试。') : failure); if (denied(failure)) { clearResults(); setIndex(undefined); } }
    } finally { if (!pending.signal.aborted && mounted.current) setLoading(false); }
  };
  const open = async (hit: ContentHit) => {
    clearEvidence(); const pending = new AbortController(); evidenceRequest.current = pending;
    setSelected(hit); setEvidenceLoading(true);
    try {
      const value = await request<EvidenceResult>('/search/content/evidence', { method: 'POST', body: JSON.stringify({ project_id: projectId, evidence_mode: mode, chunk_id: hit.id, expected_generation: hit.generation, expected_fingerprint: hit.source_fingerprint, expected_revision: hit.source_revision, expected_parse_revision: hit.parse_revision }), signal: pending.signal });
      if (pending.signal.aborted || !mounted.current) return;
      if (!sameHit(value.item, hit)) throw new ApiError(409, '证据响应与搜索命中的版本绑定不一致，请重新检索后打开。');
      setEvidence(value);
    } catch (failure) {
      if (!pending.signal.aborted && mounted.current) {
        if (denied(failure)) { clearResults(); setIndex(undefined); setError(failure); }
        else setEvidenceError(failure instanceof ApiError && failure.status === 0 ? new ApiError(0, '证据读取未完成，请关闭后主动重新打开。') : failure);
      }
    } finally { if (!pending.signal.aborted && mounted.current) setEvidenceLoading(false); }
  };
  const rebuild = async () => {
    if (!index || indexLoading || rebuilding || rebuildBlocked || !canManage || manageDenied) return;
    clearResults(); const pending = new AbortController(); rebuildRequest.current = pending;
    setRebuilding(true); setError(undefined); setMessage('');
    try {
      const expectedGenerations = Object.fromEntries((kind === 'all' ? ['document', 'knowledge'] as const : [kind]).map(key => [key, index[key].generation]));
      const value = await request<ContentIndex>('/search/content/rebuild', { method: 'POST', body: JSON.stringify({ project_id: projectId, kind, expected_generations: expectedGenerations }), signal: pending.signal });
      if (pending.signal.aborted || !mounted.current) return;
      lastIndex.current = value; setIndex(value); setMessage('内容索引重建已排队。关键词和范围保留；请主动重新检索，重建期间结果可能不完整。此操作不修改原件或知识正文。');
    } catch (failure) {
      if (!pending.signal.aborted && mounted.current) { setError(failure); setRebuildBlocked(true); if (denied(failure)) { setIndex(undefined); setManageDenied(true); } }
    } finally { if (!pending.signal.aborted && mounted.current) setRebuilding(false); }
  };
  const activeKinds = kind === 'all' ? (['document', 'knowledge'] as const) : [kind];
  const relevantKinds = mode === 'original_evidence' ? (['document'] as const) : activeKinds;
  const partialIndex = !!result && relevantKinds.some(key => result.index[key].status !== 'ready');
  const queryChanged = !!applied && q.trim() !== applied.q;
  return <>
    <PageTitle title="内容检索" description="检索文档与已发布知识，按固定修订重新核对原始证据。" />
    <form className="content-search-form" onSubmit={event => { event.preventDefault(); if (!q.trim() || loading || rebuilding) return; void run({ project_id: projectId, q: q.trim(), kind, evidence_mode: mode, limit: 20, offset: 0 }); }}>
      <label>证据模式<select aria-label="内容证据模式" disabled={rebuilding} value={mode} onChange={event => { const next = event.target.value as EvidenceMode; changeScope(next === 'original_evidence' ? 'document' : kind, next); }}><option value="comprehensive_knowledge">综合知识（文档与已发布知识）</option><option value="original_evidence">原始证据（仅文档）</option></select></label>
      <label>来源种类<select aria-label="内容来源种类" disabled={rebuilding || mode === 'original_evidence'} value={kind} onChange={event => changeScope(event.target.value as Kind, mode)}><option value="all">全部内容</option><option value="document">文档</option><option value="knowledge">已发布知识</option></select></label>
      <label className="content-search-keyword">关键词<input aria-label="内容关键词" value={q} maxLength={200} onChange={event => setQ(event.target.value)} placeholder="输入要检索的中文业务词…" /></label><button disabled={!q.trim() || loading || rebuilding}>检索内容</button>
    </form>
    <p className="muted">全部查询词必须在同一个索引分块中匹配；不会把跨分块命中拼成同一证据。综合知识包含已发布知识，不等同全部原始事实；不搜索作者草稿，也不生成答案。</p>
    <div className="content-index-panel"><div className="inline-info"><strong>内容索引状态</strong><button type="button" className="text-button" disabled={indexLoading || rebuilding} onClick={() => void readStatus(true)}>刷新内容索引状态</button></div><ErrorNotice error={indexError} />{indexLoading && <Loading />}
      {index && <div className="content-index-grid">{(['document', 'knowledge'] as const).map(key => <div key={key}><strong>{kindLabel[key]}：{indexLabel[index[key].status] ?? index[key].status}</strong><p>待处理 {index[key].pending} · 失败 {index[key].errors}</p><code>{index[key].generation ?? '尚未建立索引代次'}</code></div>)}</div>}
      {canManage && !manageDenied && <div className="inline-info"><button type="button" className="secondary" disabled={!index || indexLoading || rebuilding || rebuildBlocked} onClick={() => void rebuild()}>重建当前范围内容索引</button><span>仅重建索引，结果可能暂时不完整。</span></div>}
      {rebuildBlocked && <p className="notice warning">重建状态或索引代次需重新核对。请主动刷新状态后再决定是否请求，不会自动重试。</p>}
    </div>
    <ErrorNotice error={error} />{message && <p className="notice" role="status">{message}</p>}{rebuilding && <Loading />}
    {queryChanged && <p className="notice warning">关键词已修改，当前结果和分页仍对应上次执行条件。</p>}
    {loading ? <Loading /> : result ? <>
      <div className="inline-info"><span>本次：{result.query.text} · 同分块全部词：{result.query.terms.join('、')}</span><span>可读命中分块 {result.total} 个（不是来源数量）</span></div>
      {partialIndex && <p className="notice warning">本次查询的相关索引尚未全部就绪，结果暂不完整；零条结果不能证明没有相关内容。</p>}
      {result.items.length ? <div className="content-search-results">{result.items.map(hit => <article className="content-hit" key={hit.id}>
        <div className="inline-info"><span className="content-kind">{kindLabel[hit.kind]}</span><span>{hit.kind === 'knowledge' ? '已发布修订' : '文档修订'} {hit.source_revision}{hit.kind === 'document' ? ` · 解析修订 ${hit.parse_revision}` : ''}</span><Status value={hit.status} /></div>
        <h3>{hit.title}</h3><BoundedText value={hit.excerpt} label="命中片段" limit={1500} />
        {(!hit.complete || hit.status === 'partial') && <p className="notice warning">来源仅部分解析；此命中不代表全文已覆盖。</p>}
        <p className="muted">质量：{hit.quality || '未提供'}{hit.warnings?.length ? ` · ${hit.warnings.length} 条来源提示，打开证据查看` : ''}</p>
        <details><summary>搜索命中锚点</summary><AnchorView value={hit.anchor} /></details><button type="button" className="secondary" onClick={() => void open(hit)}>打开固定版本证据</button>
      </article>)}</div> : <Empty title={partialIndex ? '当前暂无可返回的内容命中' : '没有符合条件的内容分块'} />}
      <div className="pager"><span>偏移 {result.offset} · 每页 {result.limit} 个分块；每页重新核权，不保证跨页快照一致。</span><button type="button" className="secondary" disabled={!applied || !result.offset} onClick={() => applied && void run({ ...applied, offset: Math.max(0, result.offset - result.limit) })}>上一页内容</button><button type="button" className="secondary" disabled={!applied || result.offset + result.limit >= result.total || result.offset + result.limit > 1000} onClick={() => applied && void run({ ...applied, offset: result.offset + result.limit })}>下一页内容</button></div>
      {result.total > 1020 && <p className="muted">本入口最多查看到偏移1000，请缩小关键词或来源范围。</p>}
    </> : !error && <Empty title="输入关键词后检索内容">无需发布本体模型；结果只覆盖当前授权的可检索内容。</Empty>}
    {selected && <EvidenceDrawer key={`${selected.id}:${selected.generation}`} hit={selected} data={evidence} error={evidenceError} loading={evidenceLoading} onClose={clearEvidence} />}
  </>;
}
