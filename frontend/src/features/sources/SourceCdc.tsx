import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, request, scoped, write } from '../../shared/api/api';
import { Empty, ErrorNotice, Field, formatTime, JsonView, Loading, Status } from '../../shared/ui/components';
import { SourceCdcProjection } from './SourceCdcProjection';

export interface CdcStream {
  id: string; source_id: string; source_revision: number; generation: string;
  connector: string; state: string; received_sequence: number; projected_sequence: number;
  indexed_sequence: number; projection_status: string; source_partition: unknown;
  lease_holder: string | null; lease_until: number; fencing_token: number;
}
interface CdcEvent { id: string; sequence: number; event_id: string; record_hash: string; record_json?: string }
export interface StreamResult { stream: CdcStream; token?: string }
const pageSize = 25;

/** Mount per source. The collector credential is deliberately never persisted or included in JSON views. */
export function SourceCdc({ projectId, sourceId, sourceRevision, canConfigureProjection = false, disabled = false, onBusy }: {
  projectId: string; sourceId: string; sourceRevision: number; canConfigureProjection?: boolean; disabled?: boolean; onBusy?: (busy: boolean) => void;
}) {
  const [stream, setStream] = useState<CdcStream>();
  const [generations, setGenerations] = useState<CdcStream[]>([]);
  const [selectedId, setSelectedId] = useState<string>();
  const [events, setEvents] = useState<CdcEvent[]>([]);
  const [cursors, setCursors] = useState([0]);
  const [loaded, setLoaded] = useState(false); const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false); const [error, setError] = useState<unknown>();
  const [needsRefresh, setNeedsRefresh] = useState(false); const [message, setMessage] = useState('');
  const [token, setToken] = useState<string>(); const [revealed, setRevealed] = useState(false);
  const [projectionBusy, setProjectionBusy] = useState(false); const [projectionEpoch, setProjectionEpoch] = useState(0);
  const mounted = useRef(false); const pendingRead = useRef<AbortController | null>(null);
  const after = cursors[cursors.length - 1];
  const locked = disabled || loading || busy || projectionBusy || !!token;

  useEffect(() => { onBusy?.(loading || busy || projectionBusy || !!token); return () => onBusy?.(false); }, [loading, busy, projectionBusy, token, onBusy]);
  const read = useCallback(async (cursor: number) => {
    pendingRead.current?.abort();
    const controller = new AbortController(); pendingRead.current = controller;
    setLoading(true); setError(undefined);
    try {
      const result = await request<{ items: CdcStream[] }>(scoped('/cdc/streams', projectId), { signal: controller.signal });
      const history = result.items.filter(item => item.source_id === sourceId);
      const found = history.find(item => item.id === selectedId) ?? history.find(item => item.state !== 'retired') ?? history[0];
      const page = found ? await request<{ stream: CdcStream; items: CdcEvent[] }>(`/cdc/streams/${encodeURIComponent(found.id)}/events?after=${cursor}&limit=${pageSize}`, { signal: controller.signal }) : undefined;
      if (!controller.signal.aborted && mounted.current) {
        setGenerations(history); setStream(page?.stream ?? found); setEvents(page?.items ?? []); setLoaded(true); setNeedsRefresh(false); setProjectionEpoch(value => value + 1);
      }
    } catch (e) { if (!controller.signal.aborted && mounted.current) { setError(e); setNeedsRefresh(true); setStream(undefined); setEvents([]); setLoaded(false); } }
    finally { if (!controller.signal.aborted && mounted.current) setLoading(false); }
  }, [projectId, sourceId, selectedId]);
  useEffect(() => { mounted.current = true; void read(after); return () => { mounted.current = false; pendingRead.current?.abort(); }; }, [read, after]);

  const operate = async (operation: 'create' | 'pause' | 'resume' | 'rotate-token') => {
    if (locked || needsRefresh) return;
    if (operation === 'rotate-token' && !window.confirm('轮换后旧采集令牌立即失效，当前租约将被清除。采集端需要更新令牌后续接，是否继续？')) return;
    setBusy(true); setError(undefined); setMessage('');
    try {
      const response = operation === 'create'
        ? await write<StreamResult>('/cdc/streams', { source_id: sourceId, expected_source_revision: sourceRevision })
        : await write<StreamResult>(`/cdc/streams/${encodeURIComponent(stream!.id)}/${operation}`, {});
      if (!mounted.current) return;
      // Retain only the credential-free stream in the ordinary display state.
      setStream(response.stream); setToken(response.token); setRevealed(false); setProjectionEpoch(value => value + 1);
      setMessage(operation === 'create' ? '采集通道已登记。配置独立采集端后才会接收事件；登记本身不会启动采集。' : operation === 'pause' ? '通道已暂停，当前租约已清除。已接收事件仍保留。' : operation === 'resume' ? '通道已恢复接收，采集端需要重新取得租约后续接。' : '采集令牌已轮换，旧令牌与旧租约已失效。');
    } catch (e) {
      if (mounted.current) {
        setError(e); setNeedsRefresh(true);
        if (e instanceof ApiError && [401, 403, 404].includes(e.status)) { setStream(undefined); setEvents([]); setLoaded(false); }
      }
    }
    finally { if (mounted.current) setBusy(false); }
  };

  return <section className="snapshot-section cdc-section" aria-label="CDC 持久事件收件箱">
    <div className="section-heading"><h3>CDC 持久事件收件箱</h3><button type="button" className="secondary" disabled={locked} onClick={() => { setMessage(''); void read(after); }}>刷新 CDC 状态</button></div>
    <div className="notice"><strong>接收、业务投影与索引分别核对</strong><p>持久接收 PostgreSQL 事件不代表业务对象已经更新；投影绑定、完整窗口与已提交水位见下方独立状态，索引仍单独管理。</p></div>
    <ErrorNotice error={error} />
    {needsRefresh && <p className="muted">请刷新 CDC 状态核对服务端结果后再操作。若生成令牌的响应丢失，请核对已有通道，再主动轮换令牌；不会自动重试登记或轮换。</p>}
    {message && <div className="notice success" role="status">{message}</div>}
    {token && <div className="notice warning" aria-label="本次采集令牌">
      <strong>令牌仅本次提供</strong><p>仅保存在当前页面内存。关闭此提示、切换页面或刷新后无法再次查看；遗失时需要轮换。请交给获授权的采集端并安全保存，勿放入截图、日志或文档。</p>
      {revealed ? <Field label="本次采集令牌值"><input readOnly autoComplete="off" spellCheck={false} value={token} /></Field> : <button type="button" className="secondary" onClick={() => setRevealed(true)}>显示本次令牌</button>}
      <button type="button" className="text-button" onClick={() => { setToken(undefined); setRevealed(false); }}>已处理，关闭令牌</button>
    </div>}
    {loading ? <Loading /> : !stream && loaded && !error ? <Empty title="尚未登记 CDC 通道"><span>每个来源只保留一个当前采集代；重建后旧代及事件仍可查阅。</span><br /><button type="button" disabled={locked || needsRefresh} onClick={() => void operate('create')}>登记 PostgreSQL CDC 通道</button></Empty> : null}
    {stream && <>
      {generations.length > 1 && <Field label="采集历史"><select value={stream.id} disabled={locked} onChange={event => { setSelectedId(event.target.value); setCursors([0]); }} >{generations.map(item => <option key={item.id} value={item.id}>{item.state === 'retired' ? '历史代（只读）' : '当前代'} · {item.generation.slice(0, 8)}</option>)}</select></Field>}
      <div className="inline-info"><span>接收状态 <Status value={stream.state} /></span><span>来源修订 {stream.source_revision} · 租约代次 {stream.fencing_token}</span></div>
      {stream.source_revision !== sourceRevision && <div className="notice warning">当前来源修订为 {sourceRevision}，与通道固定的修订 {stream.source_revision} 不同。请核对配置兼容性；当前通道不会自动改绑新来源修订。</div>}
      <dl className="cdc-metadata"><dt>通道 ID</dt><dd><code>{stream.id}</code></dd><dt>采集代次</dt><dd><code>{stream.generation}</code></dd><dt>租约持有者</dt><dd><code>{stream.lease_holder ?? '无当前租约'}</code></dd><dt>租约到期时间</dt><dd>{stream.lease_until ? formatTime(new Date(stream.lease_until * 1000).toISOString()) : '—'}<small>（读取时状态，刷新可核对续租）</small></dd></dl>
      <div className="toolbar">
        {stream.state === 'active' && <button type="button" className="secondary" disabled={locked || needsRefresh} onClick={() => void operate('pause')}>暂停接收</button>}
        {stream.state === 'paused' && <button type="button" className="secondary" disabled={locked || needsRefresh} onClick={() => void operate('resume')}>恢复接收</button>}
        {stream.state !== 'retired' && <button type="button" className="secondary" disabled={locked || needsRefresh} onClick={() => void operate('rotate-token')}>轮换采集令牌</button>}
      </div>
      <p className="muted">暂停、恢复或轮换均会清除现有租约。此页面不取得采集租约、不代发原始事件；采集端独立运行。</p>
      <SourceCdcProjection key={stream.id} streamId={stream.id} refreshKey={projectionEpoch} canConfigure={canConfigureProjection} onRebuilt={result => { setStream(result.stream); setGenerations(value => [result.stream, ...value.map(item => item.id === stream.id ? { ...item, state: 'retired' } : item)]); setSelectedId(result.stream.id); setEvents([]); setCursors([0]); setToken(result.token); setRevealed(false); setMessage('新采集代已建立。请使用新令牌、独立的采集状态目录和复制槽启动完整快照；旧代及已投影对象保留。'); }} disabled={disabled || loading || busy || !!token} onBusy={setProjectionBusy} />
      <details><summary>来源分区信息</summary><JsonView value={stream.source_partition} /></details>
      <h4>原始事件</h4>
      {loading ? null : error ? <p className="muted">事件列表未能刷新，不能据此判断接收进度。</p> : events.length ? <div className="table-wrap"><table><thead><tr><th>序号</th><th>事件标识</th><th>原始内容</th></tr></thead><tbody>{events.map(event => <tr key={event.id}><td>{event.sequence}</td><td><code>{event.event_id}</code><small className="row-id">记录哈希：{event.record_hash}</small></td><td><details><summary>查看事件 {event.sequence}</summary>{typeof event.record_json === 'string' ? <pre className="json-view">{event.record_json}</pre> : <p className="muted">服务未返回精确事件原文，暂不展示可能丢失整数精度的 JSON 预览。</p>}</details></td></tr>)}</tbody></table></div> : <p className="muted">{after ? `本次读取在序号 ${after} 后没有更多事件，可刷新核对后续接收。` : '尚无已接收事件。通道启用不代表采集端已经连接。'}</p>}
      <div className="pager"><span>按事件序号读取，每页最多 {pageSize} 条{!loading && !error && events.length ? ` · 本页 ${events[0].sequence}–${events[events.length - 1].sequence}` : ''}</span><button type="button" className="secondary" disabled={locked || needsRefresh || cursors.length < 2} onClick={() => setCursors(value => value.slice(0, -1))}>上一页事件</button><button type="button" className="secondary" disabled={locked || needsRefresh || events.length < pageSize} onClick={() => setCursors(value => [...value, events[events.length - 1].sequence])}>下一页事件</button></div>
    </>}
  </section>;
}
