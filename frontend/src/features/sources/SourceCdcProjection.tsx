import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, request, write } from '../../shared/api/api';
import { ErrorNotice, formatTime, JsonView, Loading, Status } from '../../shared/ui/components';

interface ProjectionBinding { id: string; release_id: string; source_revision: number; generation: string; resource_ids: string[]; created_at: string }
interface ProjectionJob { id: string; status: string; error?: unknown; result?: unknown }
export interface CdcProjection {
  binding: ProjectionBinding | null;
  current: { release_id: string | null; version: number; source_revision: number };
  resources: { resource_id: string; label: string; mapping_keys: string[]; covered: boolean }[];
  status: 'not_configured' | 'waiting' | 'running' | 'caught_up' | 'blocked' | 'budget_exceeded' | 'failed';
  reason_code: string; message: string;
  received_sequence: number; projected_sequence: number; indexed_sequence: number; indexing_status: 'not_configured';
  fulltext?: { status: 'not_ready' | 'pending' | 'ready' | 'failed'; sequence: number; generation: string | null; message: string };
  baseline: null | { window_id: string; end_sequence: number; proof_kind: string };
  last_job: ProjectionJob | null; can_project: boolean; can_configure: boolean;
}
interface ProjectionDraft { releaseId: string | null; sourceRevision: number; resourceIds: string[] }
const statuses: Record<CdcProjection['status'], string> = { not_configured: '尚未配置', waiting: '等待完整窗口', running: '投影处理中', caught_up: '已追平（本次读取）', blocked: '投影被阻止', budget_exceeded: '窗口超出处理预算', failed: '上次投影失败' };
const failedIdentity = (data: CdcProjection) => JSON.stringify([data.binding?.id, data.last_job?.id, data.last_job?.status, data.reason_code, data.projected_sequence]);

/** This panel observes a durable inbox. Enqueue acknowledgements never advance displayed watermarks. */
export function SourceCdcProjection({ streamId, refreshKey, canConfigure, disabled, onBusy }: { streamId: string; refreshKey: number; canConfigure: boolean; disabled: boolean; onBusy: (busy: boolean) => void }) {
  const [data, setData] = useState<CdcProjection>(); const [draft, setDraft] = useState<ProjectionDraft>();
  const [loading, setLoading] = useState(true); const [busy, setBusy] = useState(false); const [error, setError] = useState<unknown>();
  const [message, setMessage] = useState(''); const [needsRefresh, setNeedsRefresh] = useState(false);
  const [confirmedFailure, setConfirmedFailure] = useState(''); const [queuedJob, setQueuedJob] = useState<ProjectionJob>();
  const mounted = useRef(false); const controller = useRef<AbortController | undefined>(undefined);
  const locked = disabled || loading || busy;
  useEffect(() => { onBusy(loading || busy); return () => onBusy(false); }, [loading, busy, onBusy]);
  const read = useCallback(async (manual = false) => {
    controller.current?.abort(); const pending = new AbortController(); controller.current = pending;
    setLoading(true); setError(undefined);
    try {
      const current = await request<CdcProjection>(`/cdc/streams/${encodeURIComponent(streamId)}/projection`, { signal: pending.signal });
      if (pending.signal.aborted || !mounted.current) return;
      setData(current); setQueuedJob(undefined); setNeedsRefresh(false);
      setDraft(value => value ?? { releaseId: current.current.release_id, sourceRevision: current.current.source_revision, resourceIds: [] });
      setConfirmedFailure(manual && current.status === 'failed' ? failedIdentity(current) : '');
    } catch (failure) {
      if (pending.signal.aborted || !mounted.current) return;
      setError(failure); setNeedsRefresh(true); setConfirmedFailure('');
      if (failure instanceof ApiError && [401, 403, 404].includes(failure.status)) { setData(undefined); setQueuedJob(undefined); }
    } finally { if (!pending.signal.aborted && mounted.current) setLoading(false); }
  }, [streamId]);
  useEffect(() => { mounted.current = true; void read(); return () => { mounted.current = false; controller.current?.abort(); }; }, [read, refreshKey]);
  const activeJob = queuedJob ?? data?.last_job;
  useEffect(() => {
    if ((!activeJob || !['pending', 'running'].includes(activeJob.status)) && data?.fulltext?.status !== 'pending') return;
    if (loading || busy || error || needsRefresh) return;
    const timer = window.setInterval(() => { if (document.visibilityState === 'visible') void read(); }, 5000);
    return () => window.clearInterval(timer);
  }, [activeJob, data?.fulltext?.status, loading, busy, error, needsRefresh, read]);
  const staleDraft = !!data && !!draft && (draft.releaseId !== data.current.release_id || draft.sourceRevision !== data.current.source_revision);
  const coveredCount = data?.resources.filter(resource => resource.covered).length ?? 0;
  const selectedCovered = !!draft?.resourceIds.length && draft.resourceIds.length === coveredCount && draft.resourceIds.every(id => data?.resources.some(resource => resource.resource_id === id && resource.covered));
  const configureAllowed = !!data && !data.binding && canConfigure && data.can_configure && !!draft?.releaseId && !staleDraft && selectedCovered && !needsRefresh;
  const failureReviewed = data?.status !== 'failed' || confirmedFailure === failedIdentity(data);
  const projectAllowed = !!data?.binding && data.can_project && !['blocked', 'budget_exceeded', 'not_configured'].includes(data.status) && failureReviewed && !needsRefresh && !['pending', 'running'].includes(activeJob?.status ?? '');
  const configure = async () => {
    if (locked || !configureAllowed || !draft) return;
    setBusy(true); setError(undefined); setMessage('');
    try {
      const current = await write<CdcProjection>(`/cdc/streams/${encodeURIComponent(streamId)}/projection`, { expected_release_id: draft.releaseId, expected_source_revision: draft.sourceRevision, resource_ids: draft.resourceIds });
      if (!mounted.current) return;
      setData(current); setNeedsRefresh(false); setMessage('投影绑定已保存并冻结。配置不代表对象已更新，实际进度请核对投影水位。');
    } catch (failure) { if (mounted.current) { setError(failure); setNeedsRefresh(true); if (failure instanceof ApiError && [401, 403, 404].includes(failure.status)) setData(undefined); } }
    finally { if (mounted.current) setBusy(false); }
  };
  const project = async () => {
    if (locked || !projectAllowed || !data?.binding) return;
    setBusy(true); setError(undefined); setMessage(''); setConfirmedFailure('');
    try {
      const result = await write<{ job: ProjectionJob }>(`/cdc/streams/${encodeURIComponent(streamId)}/project`, { expected_binding_id: data.binding.id });
      if (!mounted.current) return;
      setQueuedJob(result.job); setMessage(`已请求投影任务 ${result.job.id}。排队或复用任务不代表对象已提交；以下水位以重新读取的服务端状态为准。`);
      await read();
    } catch (failure) { if (mounted.current) { setError(failure); setNeedsRefresh(true); if (failure instanceof ApiError && [401, 403, 404].includes(failure.status)) { setData(undefined); setQueuedJob(undefined); } } }
    finally { if (mounted.current) setBusy(false); }
  };
  return <section className="cdc-projection" aria-label="CDC 业务对象投影">
    <div className="section-heading"><h4>业务对象投影</h4><button type="button" className="secondary" disabled={locked} onClick={() => { setMessage(''); void read(true); }}>刷新投影状态</button></div>
    <ErrorNotice error={error} />{needsRefresh && <p className="notice warning">投影状态未能确认。请先刷新核对，不会自动重试配置、换绑模型或重复提交任务。</p>}
    {message && <p className="notice" role="status">{message}</p>}{loading && <Loading />}
    {data && <>
      <div className={`notice ${['failed', 'blocked', 'budget_exceeded'].includes(data.status) ? 'warning' : ''}`}><strong>{statuses[data.status]}</strong><p>{data.message}</p>{data.reason_code && <code>{data.reason_code}</code>}{needsRefresh && <p>下列数据是上次成功读取的状态。</p>}</div>
      <div className="cdc-watermarks" aria-label="CDC 水位"><div><span>已接收 received</span><strong>{data.received_sequence}</strong><small>已持久记录的事件序号，不代表对象更新</small></div><div><span>对象投影 projected</span><strong>{data.projected_sequence}</strong><small>已提交对象投影的事件边界</small></div><div><span>对象全文 fulltext</span><strong>{data.fulltext?.sequence ?? '—'}</strong><small>{data.fulltext ? { not_ready: '尚未建立对象全文索引', pending: '对象全文索引处理中', ready: '对象全文索引已消费', failed: '对象全文索引处理失败' }[data.fulltext.status] : '服务未提供独立全文索引进度'}</small></div><div><span>综合索引 indexed</span><strong>{data.indexed_sequence}</strong><small>向量索引未配置；全文进度不代表综合索引完成</small></div></div>
      {data.fulltext && <div className={`notice ${data.fulltext.status === 'failed' ? 'warning' : ''}`} aria-label="对象全文进度"><p>{data.fulltext.message}</p><small>全文索引代次：<code>{data.fulltext.generation ?? '尚未建立'}</code>；该序号来自实际索引消费，不由投影结果或任务排队推算。</small></div>}
      <p className="muted">后台按完整窗口处理新收到的事件。这里是读取时的水位；已追平不保证此后没有新事件，任务成功也不代表索引已更新。</p>
      {data.binding ? <><dl className="cdc-metadata"><dt>固定模型发布</dt><dd><code>{data.binding.release_id}</code></dd><dt>固定来源修订</dt><dd>{data.binding.source_revision}</dd><dt>固定采集代次</dt><dd><code>{data.binding.generation}</code></dd><dt>完整资源范围</dt><dd>{data.binding.resource_ids.map(id => <div key={id}><code>{id}</code></div>)}</dd><dt>绑定时间</dt><dd>{formatTime(data.binding.created_at)}</dd><dt>当前模型</dt><dd>v{data.current.version} · <code>{data.current.release_id ?? '尚未发布'}</code></dd><dt>当前来源修订</dt><dd>{data.current.source_revision}</dd></dl><p className="muted">已有绑定只读；修改范围、模型或来源基线需要受控迁移，当前页面不提供覆盖配置。</p>{(data.binding.release_id !== data.current.release_id || data.binding.source_revision !== data.current.source_revision) && <p className="notice warning">当前模型或来源修订与固定绑定不同。页面不会自动改绑，请按服务端阻塞原因处理。</p>}</> : <>
        <h4>首次配置投影范围</h4><p className="muted">必须显式选中此来源的全部已发布映射资源，并与采集端完整范围一致。漏选资源不能作为跳过事件的方式；配置后范围和基线保持只读。</p>
        {!canConfigure || !data.can_configure ? <p className="muted">配置需要当前项目同时具备来源写入与本体写入权限；已有绑定可按服务端授权查看和触发。</p> : <><p className="muted">表单固定模型：<code>{draft?.releaseId ?? '尚未发布'}</code> · 来源修订 {draft?.sourceRevision}</p>{!data.current.release_id && <p className="notice warning">请先发布本体及该来源的字段映射，再配置投影。</p>}{staleDraft && <div className="notice warning"><p>表单基线已过期，选择保留，不能提交旧模型或来源修订。</p><button type="button" className="secondary" disabled={locked} onClick={() => setDraft({ releaseId: data.current.release_id, sourceRevision: data.current.source_revision, resourceIds: [] })}>按当前基线重新选择范围</button></div>}
          <fieldset className="cdc-projection-resources" disabled={locked || needsRefresh || staleDraft}><legend>完整采集资源范围</legend>{data.resources.map(resource => <label key={resource.resource_id} className="checkbox"><input type="checkbox" disabled={!resource.covered} checked={!!draft?.resourceIds.includes(resource.resource_id)} onChange={event => setDraft(value => value && ({ ...value, resourceIds: event.target.checked ? [...value.resourceIds, resource.resource_id] : value.resourceIds.filter(id => id !== resource.resource_id) }))} /><span>{resource.label || resource.resource_id} <code>{resource.resource_id}</code><small>{resource.covered ? `已发布映射：${resource.mapping_keys.join('、')}` : '缺少完整发布映射，暂不可配置'}</small></span></label>)}{!data.resources.length && <p className="muted">没有可用资源。请先发现来源结构并发布映射，再刷新。</p>}</fieldset>
          <button type="button" disabled={locked || !configureAllowed} onClick={() => void configure()}>保存首次投影绑定</button>
        </>}
      </>}
      {data.baseline && <dl className="cdc-metadata"><dt>初始基线窗口</dt><dd><code>{data.baseline.window_id}</code></dd><dt>基线结束水位</dt><dd>{data.baseline.end_sequence}</dd><dt>完整性证明</dt><dd><code>{data.baseline.proof_kind}</code></dd></dl>}
      {data.last_job && <div className="cdc-job"><h4>最近投影任务 <Status value={data.last_job.status} /></h4><code>{data.last_job.id}</code>{data.last_job.error != null && <><p className="muted">服务端返回的失败信息</p><JsonView value={data.last_job.error} /></>}{data.last_job.result != null && <details><summary>任务结果与窗口信息</summary><JsonView value={data.last_job.result} /></details>}</div>}
      {data.status === 'failed' && !failureReviewed && <p className="notice warning">重试前请先点击“刷新投影状态”核对当前绑定、失败原因与任务状态。</p>}
      {data.binding && <button type="button" disabled={locked || !projectAllowed} onClick={() => void project()}>{data.status === 'failed' ? '重新请求一次完整窗口投影' : '请求一次完整窗口投影'}</button>}
      {['blocked', 'budget_exceeded'].includes(data.status) && <p className="muted">当前原因未解除前不能盲目重试。页面不跳过事件、不拆开事务，也不自动重建基线。</p>}
    </>}
  </section>;
}
