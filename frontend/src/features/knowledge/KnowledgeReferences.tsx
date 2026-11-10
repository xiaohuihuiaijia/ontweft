import { useEffect, useState } from 'react';
import { ApiError, scoped } from '../../shared/api/api';
import { ErrorNotice, Field, JsonView, Loading } from '../../shared/ui/components';
import { DocumentInspector, type DocumentReference } from '../documents/DocumentDetails';
import { useResource } from '../../shared/hooks/hooks';
import type { ListResult, RecordData } from '../../shared/model/types';

interface Document { id: string; filename: string; revision: number; state: string }

export function KnowledgeReferences({ projectId, value, disabled, onChange }: { projectId: string; value: string; disabled: boolean; onChange: (value: string) => void }) {
  const [offset, setOffset] = useState(0);
  const documents = useResource<ListResult<Document>>(scoped('/documents', projectId, { limit: 25, offset }));
  const [documentId, setDocumentId] = useState(''); const [elementPath, setElementPath] = useState(''); const [error, setError] = useState<unknown>();
  const [historyReference, setHistoryReference] = useState<{ documentId: string; parseId: string }>();
  const [pendingCapture, setPendingCapture] = useState<string[]>([]);
  const sourceDenied = documents.error instanceof ApiError && [401, 403, 404].includes(documents.error.status);
  useEffect(() => { if (sourceDenied) { setDocumentId(''); setHistoryReference(undefined); } }, [sourceDenied]);
  let refs: RecordData[] = [];
  try { const parsed: unknown = JSON.parse(value); if (Array.isArray(parsed)) refs = parsed.filter((r): r is RecordData => !!r && typeof r === 'object' && !Array.isArray(r)); } catch { /* The editable JSON retains invalid input for correction. */ }
  const add = (binding: DocumentReference) => {
    try {
      const parsed: unknown = JSON.parse(value); if (!Array.isArray(parsed)) throw new Error('请先修正下方引用 JSON，再添加引用。');
      const next = { ...binding, ...(elementPath.trim() ? { element_path: elementPath.trim() } : {}) };
      if (!parsed.some(r => JSON.stringify(r) === JSON.stringify(next))) onChange(JSON.stringify([...parsed, next], null, 2));
      if (!next.parse_id) setPendingCapture(values => [...values, JSON.stringify(next)]);
      setDocumentId(''); setElementPath(''); setError(undefined);
    } catch (failure) { setError(failure); }
  };
  return <section className="knowledge-references"><h3>来源引用</h3><p className="muted">先预览来源，再固定原件修订、解析修订与指纹。保存和发布时服务端再次核对权限及版本。</p><ErrorNotice error={error ?? documents.error} />
    {refs.length ? <ul className="relationship-list">{refs.map((ref, index) => <li key={`${ref.document_id}:${index}`}><span>{documents.data?.items.find(d => d.id === ref.document_id)?.filename ?? String(ref.document_id ?? '缺少文档 ID')} · 原件修订 {String(ref.revision ?? '未填写')} · 解析修订 {String(ref.parse_revision ?? '未绑定')}</span><code>{String(ref.element_path ?? '整个文档')}</code>{ref.parse_id ? <button type="button" className="text-button" onClick={() => setHistoryReference({ documentId: String(ref.document_id), parseId: String(ref.parse_id) })}>查看引用的固定解析版本</button> : pendingCapture.includes(JSON.stringify(ref)) ? <p className="muted">此引用来自本次实际预览，保存时由服务端归档并返回固定解析标识。</p> : <p className="notice warning">旧引用未绑定解析版本，需重新选择来源保存新修订。</p>}{!disabled && <button type="button" className="danger-link" onClick={() => onChange(JSON.stringify(refs.filter((_, i) => i !== index), null, 2))}>移除引用</button>}</li>)}</ul> : <p className="muted">没有登记来源引用。</p>}
    {historyReference && !sourceDenied && <div className="reference-preview"><button type="button" className="text-button" onClick={() => setHistoryReference(undefined)}>关闭固定引用预览</button><DocumentInspector documentId={historyReference.documentId} fixedParseId={historyReference.parseId} /></div>}
    {!disabled && !sourceDenied && <div className="property-editor"><Field label="选择可引用文档"><select value={documentId} disabled={documents.loading} onChange={event => { setDocumentId(event.target.value); setElementPath(''); }}><option value="">选择来源并预览解析</option>{documents.data?.items.map(d => <option key={d.id} value={d.id}>{d.filename} · 原件修订 {d.revision} · {d.state}</option>)}</select></Field>{documents.data && documents.data.total > 25 && <div className="pager"><span>可选来源共 {documents.data.total} 项</span><button type="button" className="secondary" disabled={!offset} onClick={() => { setOffset(Math.max(0, offset - 25)); setDocumentId(''); }}>上一页引用来源</button><button type="button" className="secondary" disabled={offset + 25 >= documents.data.total} onClick={() => { setOffset(offset + 25); setDocumentId(''); }}>下一页引用来源</button></div>}
      <Field label="结构锚点（可选）" hint="从下方实际解析的证据定位复制 element_path；留空引用整个解析版本。切换历史解析后请重新核对锚点，服务端会验证其是否存在。"><input value={elementPath} onChange={event => setElementPath(event.target.value)} /></Field>
      {documentId && <DocumentInspector documentId={documentId} referenceDisabled={disabled} onReference={add} />}
    </div>}
    <details><summary>引用结构（JSON）</summary><Field label="来源引用结构" hint="新引用必须包含已预览来源的revision、parse_revision、ir_fingerprint、content_hash，已归档时同时包含parse_id；可选element_path。不会自动推测旧引用版本。"><textarea className="code-input" rows={5} disabled={disabled} value={value} onChange={e => onChange(e.target.value)} /></Field></details>
  </section>;
}

export function KnowledgeHistory({ id }: { id: string }) {
  const history = useResource<ListResult<RecordData>>(`/knowledge/${encodeURIComponent(id)}/history`);
  return <details><summary>页面修订历史</summary>{history.loading ? <Loading /> : history.error ? <ErrorNotice error={history.error} onRetry={history.reload} /> : <JsonView value={history.data?.items ?? []} />}</details>;
}

