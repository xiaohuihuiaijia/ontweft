import { useEffect, useRef, useState } from 'react';
import { ApiError, request, scoped } from '../../shared/api/api';
import { Empty, ErrorNotice, Loading } from '../../shared/ui/components';
import { useResource } from '../../shared/hooks/hooks';
import { ObjectTable } from './ObjectTable';
import type { BusinessObject, TypeList } from '../../shared/model/types';

export interface FulltextIndex { kind: 'object_fulltext'; generation: string | null; tokenizer_version: string; status: 'not_ready' | 'updating' | 'ready' | 'failed' }
interface SearchInput { count_total: boolean; project_id: string; q: string; type_key?: string; limit: number; offset: number; expected_release_id: string | null; expected_model_version: number }
interface SearchResult { items: BusinessObject[]; total: number | null; has_more: boolean; limit: number; offset: number; query: { text: string; terms: string[]; mode: 'all_terms' }; release_id: string | null; model_version: number; index: FulltextIndex; matches: Record<string, { fields: string[] }> }
interface ModelBaseline { releaseId: string | null; version: number }
interface Props { projectId: string; types?: TypeList; modelError?: unknown; loadingTypes: boolean; refreshTypes: () => void; opening: boolean; onOpen: (id: string) => void; refreshKey: number; canManage?: boolean }
const indexLabels: Record<FulltextIndex['status'], string> = { not_ready: '全文索引尚未就绪', updating: '全文索引更新中，结果暂不完整', ready: '对象全文索引已就绪', failed: '全文索引处理失败，不能确认结果完整性' };
const denied = (error: unknown) => error instanceof ApiError && [401, 403, 404].includes(error.status);

export function ObjectFulltext(props: Props) { return <FulltextSession key={props.projectId} {...props} />; }

function FulltextSession({ projectId, types, modelError, loadingTypes, refreshTypes, opening, onOpen, refreshKey, canManage = false }: Props) {
  const [q, setQ] = useState(''); const [typeKey, setTypeKey] = useState('');
  const [baseline, setBaseline] = useState<ModelBaseline>(); const [applied, setApplied] = useState<SearchInput>();
  const [storedResult, setResult] = useState<SearchResult>(); const [error, setError] = useState<unknown>();
  const [loading, setLoading] = useState(false); const [conflict, setConflict] = useState(false); const [message, setMessage] = useState('');
  const [rebuildDenied, setRebuildDenied] = useState(false);
  const controller = useRef<AbortController | undefined>(undefined); const mounted = useRef(false);
  const epoch = useRef(0); const priorRefresh = useRef(refreshKey);
  const status = useResource<FulltextIndex>(scoped('/search/status', projectId));
  const currentBaseline = types && types.release_id !== undefined && types.version !== undefined ? { releaseId: types.release_id, version: types.version } : undefined;
  const modelChanged = !!baseline && !!currentBaseline && (baseline.releaseId !== currentBaseline.releaseId || baseline.version !== currentBaseline.version);
  const protectedError = denied(status.error) || denied(modelError);
  const result = protectedError || modelChanged ? undefined : storedResult;
  const typeUnavailable = !!typeKey && !!types && !types.items.some(type => type.key === typeKey);
  const queryChanged = !!applied && (q.trim() !== applied.q || typeKey !== (applied.type_key ?? ''));
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; controller.current?.abort(); epoch.current++; }; }, []);
  useEffect(() => { if (!baseline && currentBaseline) setBaseline(currentBaseline); }, [baseline, types]);
  useEffect(() => {
    if (protectedError) { controller.current?.abort(); epoch.current++; setResult(undefined); setLoading(false); }
  }, [protectedError]);
  useEffect(() => {
    if (priorRefresh.current !== refreshKey) { priorRefresh.current = refreshKey; controller.current?.abort(); epoch.current++; setResult(undefined); setLoading(false); setMessage('对象已修改。请刷新索引状态后重新执行查询，索引更新可能稍有延迟。'); status.reload(); }
  }, [refreshKey, status.reload]);
  useEffect(() => { if (modelChanged) { controller.current?.abort(); epoch.current++; setResult(undefined); setLoading(false); } }, [modelChanged]);
  const execute = async (input: SearchInput) => {
    if (conflict || modelChanged || protectedError) return;
    controller.current?.abort(); const pending = new AbortController(); controller.current = pending; const sequence = ++epoch.current;
    setLoading(true); setResult(undefined); setError(undefined); setMessage(''); setApplied(input);
    try {
      const value = await request<SearchResult>('/search/objects', { method: 'POST', body: JSON.stringify(input), signal: pending.signal });
      if (pending.signal.aborted || !mounted.current || sequence !== epoch.current) return;
      if (value.release_id !== input.expected_release_id || value.model_version !== input.expected_model_version) throw new ApiError(409, '搜索结果的模型基线与请求不一致，请刷新模型后重新核对。');
      setResult(value);
    } catch (failure) {
      if (pending.signal.aborted || !mounted.current || sequence !== epoch.current) return;
      setError(failure instanceof ApiError && failure.status === 0 ? new ApiError(0, '全文查询未完成，请检查网络后主动重试。') : failure);
      if (failure instanceof ApiError && failure.status === 409) setConflict(true);
    } finally { if (!pending.signal.aborted && mounted.current && sequence === epoch.current) setLoading(false); }
  };
  const submit = (event: React.FormEvent) => {
    event.preventDefault(); if (!baseline || !q.trim() || q.trim().length > 200 || loading || loadingTypes || typeUnavailable) return;
    void execute({ count_total: false, project_id: projectId, q: q.trim(), ...(typeKey ? { type_key: typeKey } : {}), limit: 20, offset: 0, expected_release_id: baseline.releaseId, expected_model_version: baseline.version });
  };
  const rebuild = async () => {
    if (!canManage || rebuildDenied || loading || loadingTypes || !baseline?.releaseId || conflict || modelChanged || protectedError) return;
    controller.current?.abort(); const pending = new AbortController(); controller.current = pending; const sequence = ++epoch.current;
    setLoading(true); setResult(undefined); setError(undefined); setMessage('');
    try {
      const value = await request<FulltextIndex>('/search/rebuild', { method: 'POST', body: JSON.stringify({ project_id: projectId, expected_release_id: baseline.releaseId, expected_model_version: baseline.version }), signal: pending.signal });
      if (pending.signal.aborted || !mounted.current || sequence !== epoch.current) return;
      setApplied(undefined); setMessage(`对象全文索引重建已在后台排队（${indexLabels[value.status]}）。关键词和类型已保留；旧结果已清除，请主动重新查询，排队不代表重建完成。`); status.reload();
    } catch (failure) {
      if (pending.signal.aborted || !mounted.current || sequence !== epoch.current) return;
      setError(failure);
      if (failure instanceof ApiError && failure.status === 409) setConflict(true);
      if (denied(failure)) { setResult(undefined); setApplied(undefined); setRebuildDenied(true); }
    } finally { if (!pending.signal.aborted && mounted.current && sequence === epoch.current) setLoading(false); }
  };
  const displayTypes = result && types?.release_id === result.release_id && types.version === result.model_version ? types.items : [];
  return <section aria-label="对象全文检索" className="object-fulltext">
    <p className="muted">检索当前可读的有效对象，按服务端分词后的全部词匹配。不会查询文档正文、调用模型或生成 SQL。</p>
    <form className="filter-bar" onSubmit={submit}><select aria-label="全文对象类型" value={typeKey} onChange={event => setTypeKey(event.target.value)}><option value="">全部可读类型</option>{types?.items.map(type => <option key={type.key} value={type.key}>{type.label}</option>)}</select><input aria-label="全文关键词" value={q} maxLength={200} onChange={event => setQ(event.target.value)} placeholder="输入中文业务词…" /><button disabled={loading || loadingTypes || !baseline || !q.trim() || conflict || modelChanged || typeUnavailable || protectedError}>执行全文检索</button></form>
    <div className="inline-info"><span>查询模型基线：v{baseline?.version ?? '—'} · <code>{baseline?.releaseId ?? '尚未发布'}</code></span><button type="button" className="text-button" disabled={loadingTypes} onClick={refreshTypes}>读取最新模型，保留关键词</button><button type="button" className="text-button" disabled={status.loading} onClick={status.reload}>刷新全文索引状态</button></div>
    {(conflict || modelChanged) && <div className="notice warning"><p>全文查询模型基线需要重新核对。关键词保留；刷新模型后显式重新绑定，不会自动重试。</p><button type="button" className="secondary" disabled={loadingTypes || !currentBaseline || !modelChanged} onClick={() => { if (currentBaseline) { setBaseline(currentBaseline); setConflict(false); setError(undefined); setResult(undefined); setApplied(undefined); setMessage('已重新绑定当前模型，尚未执行全文检索。'); } }}>按当前模型重新绑定全文查询</button></div>}
    {typeUnavailable && <p className="notice warning">所选类型当前不可读，请重新选择。</p>}
    {status.loading ? <Loading /> : status.error ? <ErrorNotice error={status.error} onRetry={status.reload} /> : status.data && <div className={`notice ${status.data.status === 'ready' ? '' : 'warning'}`} role="status"><strong>当前状态：{indexLabels[status.data.status]}</strong><p>对象全文索引代次 <code>{status.data.generation ?? '尚未建立'}</code> · 分词版本 <code>{status.data.tokenizer_version}</code>。不代表向量或综合索引已完成。</p></div>}
    {canManage && !rebuildDenied && <div className="inline-info"><button type="button" className="secondary" disabled={loading || loadingTypes || !baseline?.releaseId || conflict || modelChanged || protectedError} onClick={() => void rebuild()}>重建对象全文索引</button><span className="muted">仅重建可重建的全文索引，不修改对象、事件或模型；期间结果可能暂不完整。</span></div>}
    <ErrorNotice error={error} />{message && <p className="notice" role="status">{message}</p>}
    {queryChanged && <p className="notice warning">关键词或类型已修改。下面结果及分页仍对应上次执行条件，请重新执行后查看新条件。</p>}
    {loading ? <Loading /> : result ? <>
      <div className="inline-info"><span>本次关键词：{result.query.text} · 全部词匹配：{result.query.terms.join('、')}</span><button type="button" className="text-button" disabled={!applied || conflict || modelChanged} onClick={() => applied && void execute({ ...applied, offset: 0 })}>重新执行上次全文查询</button></div>
      <p className={`notice ${result.index.status === 'ready' ? '' : 'warning'}`}>本次读取：{indexLabels[result.index.status]}。{result.index.status !== 'ready' ? '已返回的安全结果仅是当前可检索部分；零条结果不能证明没有匹配对象。' : '分页按每次查询时的索引读取，更新期间结果可能变化。'} 索引代次：<code>{result.index.generation ?? '尚未建立'}</code></p>
      {result.items.length ? <ObjectTable items={result.items} types={displayTypes} disabled={opening} onOpen={onOpen} matches={result.matches} /> : <Empty title={result.index.status === 'ready' ? '没有符合全文条件的对象' : '当前暂无可返回的全文结果'} />}
      <div className="pager"><span>本页显示 {result.items.length} 条{result.has_more ? '，还有更多结果' : ''}{result.index.status !== 'ready' ? '（索引结果暂不完整）' : ''} · 当前偏移 {result.offset}</span><button type="button" className="secondary" disabled={!result.offset || !applied || conflict || modelChanged} onClick={() => applied && void execute({ ...applied, offset: Math.max(0, result.offset - result.limit) })}>上一页全文结果</button><button type="button" className="secondary" disabled={!applied || !result.has_more || result.offset + result.limit > 1000 || conflict || modelChanged} onClick={() => applied && void execute({ ...applied, offset: result.offset + result.limit })}>下一页全文结果</button></div>
      {result.has_more && result.offset + result.limit > 1000 && <p className="muted">本入口最多查看到偏移 1000，请缩小关键词或类型范围。</p>}
    </> : !error && <Empty title="输入关键词并执行全文检索">索引状态刷新不会自动执行查询。</Empty>}
  </section>;
}
