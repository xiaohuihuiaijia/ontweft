import { useEffect, useRef, useState } from 'react';
import { ApiError, request, protectedReadUnavailable } from '../../shared/api/api';
import { Drawer, ErrorNotice, formatTime, Loading, Status } from '../../shared/ui/components';
import { DocumentPreview } from './DocumentPreview';
import { useResource } from '../../shared/hooks/hooks';
import type { ListResult } from '../../shared/model/types';

type Engine = 'auto' | 'basic' | 'docling';
interface ParseSummary { id: string; source_revision: number; parse_revision: number; content_hash: string; ir_fingerprint: string; parser: string; parser_version: string; engine: string; origin: 'worker' | 'legacy'; configuration?: unknown; created_at: string; is_current: boolean }
interface ParseSnapshot extends ParseSummary { ir: unknown; ir_json?: string; filename: string; download_url: string }
interface CurrentDocument { id: string; filename: string; revision: number; parse_revision: number; content_hash: string; ir_fingerprint: string | null; current_parse_id: string | null; active_parse_job: { id: string; status: string; engine: string } | null; state: string; error?: string | null; ir: unknown }
export interface DocumentReference { document_id: string; revision: number; parse_revision: number; ir_fingerprint: string; content_hash: string; parse_id?: string; element_path?: string }
interface InspectorProps { documentId: string; fixedParseId?: string; canReparse?: boolean; referenceDisabled?: boolean; onReference?: (reference: DocumentReference) => void; onChanged?: () => void }
const denied = protectedReadUnavailable;
const hasIR = (ir: unknown) => !!ir && typeof ir === 'object' && Array.isArray((ir as { elements?: unknown }).elements);

export function DocumentDetails({ onClose, ...props }: InspectorProps & { onClose: () => void }) {
  return <Drawer title={props.fixedParseId ? '固定历史解析版本' : '文档解析与原件'} onClose={onClose}><div className="drawer-body"><DocumentInspector {...props} /></div></Drawer>;
}

export function DocumentInspector(props: InspectorProps) { return <InspectorSession key={`${props.documentId}:${props.fixedParseId ?? 'current'}`} {...props} />; }

function InspectorSession({ documentId, fixedParseId, canReparse = false, referenceDisabled = false, onReference, onChanged }: InspectorProps) {
  const [document, setDocument] = useState<CurrentDocument>(); const [snapshot, setSnapshot] = useState<ParseSnapshot>();
  const [parseId, setParseId] = useState<string | undefined>(fixedParseId); const [engine, setEngine] = useState<Engine>('auto');
  const [error, setError] = useState<unknown>(); const [loading, setLoading] = useState(true); const [busy, setBusy] = useState(false); const [needsRefresh, setNeedsRefresh] = useState(false); const [blocked, setBlocked] = useState(false); const [message, setMessage] = useState('');
  const [historyOpen, setHistoryOpen] = useState(false); const [offset, setOffset] = useState(0);
  const [showExactIR, setShowExactIR] = useState(false);
  const history = useResource<ListResult<ParseSummary>>(historyOpen && !blocked ? `/documents/${encodeURIComponent(documentId)}/parses?limit=25&offset=${offset}` : null);
  const mounted = useRef(false); const reading = useRef<AbortController | undefined>(undefined); const writing = useRef<AbortController | undefined>(undefined);
  const clearProtected = () => { setDocument(undefined); setSnapshot(undefined); setBlocked(true); };
  const read = async (requestedParseId?: string) => {
    reading.current?.abort(); const pending = new AbortController(); reading.current = pending;
    setLoading(true); setError(undefined); setParseId(requestedParseId); setSnapshot(undefined); setMessage(''); setShowExactIR(false);
    try {
      if (requestedParseId) {
        const value = await request<ParseSnapshot>(`/documents/${encodeURIComponent(documentId)}/parses/${encodeURIComponent(requestedParseId)}`, { signal: pending.signal });
        if (pending.signal.aborted || !mounted.current) return;
        if (value.id !== requestedParseId) throw new ApiError(409, '返回的解析版本与所选历史版本不一致。');
        setSnapshot(value);
      } else {
        const value = await request<CurrentDocument>(`/documents/${encodeURIComponent(documentId)}`, { signal: pending.signal });
        if (pending.signal.aborted || !mounted.current) return;
        if (value.id !== documentId) throw new ApiError(409, '返回的文档身份与所选文档不一致。');
        setDocument(value); history.reload();
      }
      setNeedsRefresh(false); setBlocked(false);
    } catch (failure) { if (!pending.signal.aborted && mounted.current) { setError(failure); setNeedsRefresh(true); if (denied(failure)) clearProtected(); } }
    finally { if (!pending.signal.aborted && mounted.current) setLoading(false); }
  };
  useEffect(() => { mounted.current = true; void read(fixedParseId); return () => { mounted.current = false; reading.current?.abort(); writing.current?.abort(); }; }, []);
  const historyDenied = denied(history.error);
  useEffect(() => { if (historyDenied) { reading.current?.abort(); clearProtected(); setError(history.error); setLoading(false); } }, [historyDenied, history.error]);
  const reparse = async () => {
    if (!document || !canReparse || !currentBaselineAvailable || parseId || busy || loading || needsRefresh || blocked || document.active_parse_job) return;
    const pending = new AbortController(); writing.current = pending; setBusy(true); setError(undefined); setMessage('');
    try {
      const result = await request<{ document: CurrentDocument; job: { id: string; status: string } }>(`/documents/${encodeURIComponent(documentId)}/reparse`, { method: 'POST', body: JSON.stringify({ expected_revision: document.revision, expected_parse_revision: document.parse_revision, expected_ir_fingerprint: document.ir_fingerprint, engine }), signal: pending.signal });
      if (pending.signal.aborted || !mounted.current) return;
      setDocument(result.document); setMessage(`重新解析任务 ${result.job.id} 已提交。排队不代表完成；新结果成功前保留已有解析，失败不会替换旧版本。请主动刷新元信息查看进度。`); onChanged?.();
    } catch (failure) { if (!pending.signal.aborted && mounted.current) { setError(failure); setNeedsRefresh(true); if (denied(failure)) clearProtected(); } }
    finally { if (!pending.signal.aborted && mounted.current) setBusy(false); }
  };
  const visibleDocument = !blocked && !historyDenied ? document : undefined;
  const visibleSnapshot = !blocked && !historyDenied ? snapshot : undefined;
  const value = parseId ? visibleSnapshot : visibleDocument;
  const ir = value?.ir;
  const nativeSnapshot = value?.filename.toLowerCase().endsWith('.feishu-docx.json') || (!!ir && typeof ir === 'object' && (ir as { format?: string }).format === 'feishu_docx');
  const revision = parseId ? visibleSnapshot?.source_revision : visibleDocument?.revision;
  const currentParseId = parseId ? visibleSnapshot?.id : visibleDocument?.current_parse_id;
  const currentBaselineAvailable = !!visibleDocument && Number.isInteger(visibleDocument.parse_revision) && Object.hasOwn(visibleDocument, 'ir_fingerprint') && Object.hasOwn(visibleDocument, 'current_parse_id') && Object.hasOwn(visibleDocument, 'active_parse_job');
  const completeIR = hasIR(ir) && (ir as { complete?: boolean }).complete === true;
  const binding = value && revision !== undefined && Number.isInteger(value.parse_revision) && (parseId || currentBaselineAvailable) && completeIR && (currentParseId || visibleDocument?.state === 'ready') && value.ir_fingerprint ? { document_id: documentId, revision, parse_revision: value.parse_revision, ir_fingerprint: value.ir_fingerprint, content_hash: value.content_hash, ...(currentParseId ? { parse_id: currentParseId } : {}) } : undefined;
  const rawDownload = parseId ? visibleSnapshot?.download_url : visibleDocument ? `/api/documents/${encodeURIComponent(documentId)}/download?${new URLSearchParams({ expected_revision: String(visibleDocument.revision), expected_hash: visibleDocument.content_hash })}` : undefined;
  const downloadPrefix = `/api/documents/${encodeURIComponent(documentId)}/download?`;
  const downloadParams = rawDownload?.startsWith(downloadPrefix) ? new URLSearchParams(rawDownload.slice(downloadPrefix.length)) : undefined;
  const download = downloadParams?.get('expected_revision') === String(revision) && downloadParams.get('expected_hash') === value?.content_hash ? rawDownload : undefined;
  return <section className="document-inspector" aria-label="文档解析版本">
    <div className="inline-info"><strong>{parseId ? '历史解析只读预览' : '当前解析与原件'}</strong><button type="button" className="secondary" disabled={loading || busy} onClick={() => void read(parseId)}>{parseId ? '重新核对历史版本权限' : '刷新当前文档元信息'}</button></div>
    <ErrorNotice error={error} />{message && <p className="notice" role="status">{message}</p>}{needsRefresh && <p className="notice warning">当前版本或操作结果需重新核对。解析引擎选择保留；必须主动刷新元信息后再决定是否提交，不会自动重绑或重试。</p>}{loading && <Loading />}
    {value && <><h3>{value.filename}</h3><p className="muted">原件修订 {revision} · 解析修订 {value.parse_revision}{parseId ? ` · ${visibleSnapshot?.is_current ? '当前生效解析' : '历史解析'}` : ''}</p>
      {parseId && <p className="notice">仅查看此固定解析版本；没有恢复为当前版本的写操作。</p>}
      {visibleSnapshot?.origin === 'legacy' && <p className="notice warning">此版本由既有当前IR归档，旧解析配置可能未知；归档本身不证明旧知识引用已绑定到此版本。</p>}
      {!parseId && visibleDocument && <><Status value={visibleDocument.state} />{visibleDocument.active_parse_job && <p className="notice">已有解析任务 {visibleDocument.active_parse_job.id} · {visibleDocument.active_parse_job.status} · {visibleDocument.active_parse_job.engine}。不可重复提交。</p>}{visibleDocument.error && <p className="notice warning">{visibleDocument.error}。已有解析如仍显示，代表保留的旧结果。</p>}</>}
      {download && <a className="download-link" href={download} target="_blank" rel="noreferrer">{nativeSnapshot ? '下载原生块快照（校验修订与哈希）' : '下载对应原件（校验修订与哈希）'}</a>}
      <details><summary>解析版本与配置</summary><dl className="content-anchor"><div><dt>归档解析标识</dt><dd>{currentParseId ?? '尚未归档，保存引用时由服务端固定'}</dd></div><div><dt>解析指纹</dt><dd>{value.ir_fingerprint ?? '尚无有效解析'}</dd></div><div><dt>原件哈希</dt><dd>{value.content_hash}</dd></div>{visibleSnapshot && <><div><dt>解析器</dt><dd>{visibleSnapshot.parser} · {visibleSnapshot.parser_version}</dd></div><div><dt>引擎</dt><dd>{visibleSnapshot.engine}</dd></div><div><dt>归档来源</dt><dd>{visibleSnapshot.origin === 'legacy' ? '既有解析归档' : '解析任务'}</dd></div><div><dt>配置</dt><dd><pre className="json-view">{JSON.stringify(visibleSnapshot.configuration, null, 2)}</pre></dd></div></>}</dl></details>
      {loading && <p className="muted">以下是上次读取的解析，刷新完成前不可提交或添加引用。</p>}
      <DocumentPreview key={`${documentId}:${currentParseId ?? 'current'}:${value.parse_revision}:${value.ir_fingerprint}`} value={ir} />
      {typeof visibleSnapshot?.ir_json === 'string' && <details onToggle={event => setShowExactIR(event.currentTarget.open)}><summary>归档解析精确JSON原文</summary>{showExactIR && <pre className="json-view" aria-label="归档解析精确JSON原文">{visibleSnapshot.ir_json}</pre>}</details>}
      {onReference && <div className="document-reference-choice"><p className="muted">先核对上方解析，再将当前预览的原件与解析版本固定到知识引用。</p><button type="button" disabled={!binding || loading || busy || needsRefresh || referenceDisabled} onClick={() => binding && onReference(binding)}>使用此解析版本作为引用</button>{!binding && <p className="notice warning">没有完整、有效且可核对的解析，不能添加知识来源引用。</p>}</div>}
      {!parseId && canReparse && <div className="property-editor reparse-panel"><h4>重新解析当前原件</h4><p className="muted">请求固定当前读取的原件修订、解析修订和指纹；成功后新解析成为当前版本，原归档版本仍可查看。</p>{!currentBaselineAvailable && <p className="notice warning">服务未提供完整的解析基线，不能安全提交。请更新服务后刷新。</p>}<label className="field"><span>重新解析引擎</span><select aria-label="重新解析引擎" disabled={busy} value={engine} onChange={event => setEngine(event.target.value as Engine)}><option value="auto">自动选择</option><option value="basic">{nativeSnapshot ? '原生块结构解析' : '基础解析（明确降级）'}</option>{!nativeSnapshot && <option value="docling">Docling（需服务端配置）</option>}</select></label><button type="button" disabled={busy || loading || needsRefresh || !currentBaselineAvailable || !!visibleDocument?.active_parse_job} onClick={() => void reparse()}>提交重新解析</button></div>}
    </>}
    {!fixedParseId && !blocked && !historyDenied && <section className="parse-history"><div className="inline-info"><button type="button" className="secondary" aria-expanded={historyOpen} disabled={busy} onClick={() => setHistoryOpen(open => !open)}>{historyOpen ? '收起解析历史' : '查看解析历史'}</button>{parseId && <button type="button" className="text-button" disabled={loading || busy} onClick={() => void read()}>返回当前解析并刷新</button>}</div>{historyOpen && <><ErrorNotice error={history.error} onRetry={history.reload} />{history.loading ? <Loading /> : history.data && <>{!history.data.items.length && <p className="muted">尚无归档解析版本；当前有效解析可在保存引用时由服务端归档。</p>}<ul className="relationship-list">{history.data.items.map(item => <li key={item.id}><span>解析修订 {item.parse_revision} · 原件修订 {item.source_revision} · {item.parser} {item.parser_version} · {item.is_current ? '当前' : '历史'}</span><small>{formatTime(item.created_at)} · {item.origin === 'legacy' ? '既有解析归档' : '任务归档'}</small><button type="button" className="text-button" disabled={busy || loading} onClick={() => void read(item.id)}>查看解析版本 {item.parse_revision}</button></li>)}</ul><div className="pager"><span>归档版本共 {history.data.total} 个</span><button type="button" className="secondary" disabled={!offset} onClick={() => setOffset(Math.max(0, offset - 25))}>上一页解析历史</button><button type="button" className="secondary" disabled={offset + 25 >= history.data.total} onClick={() => setOffset(offset + 25)}>下一页解析历史</button></div></>}</>}</section>}
  </section>;
}
