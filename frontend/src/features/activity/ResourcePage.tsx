import { useEffect, useState } from 'react';
import { scoped, write } from '../../shared/api/api';
import { Drawer, Empty, ErrorNotice, formatTime, JsonView, Loading, Pager, PageTitle, Status } from '../../shared/ui/components';
import { useResource } from '../../shared/hooks/hooks';
import type { ListResult, RecordData } from '../../shared/model/types';

const configs: Record<string, { title: string; description: string; empty: string }> = {
  jobs: { title: '任务中心', description: '观察持久任务的运行、部分完成与失败状态。', empty: '当前项目没有任务' },
  audit: { title: '审计记录', description: '查询服务端记录的操作者、目标、版本与结果。', empty: '当前没有可见审计记录' },
};

export function ResourcePage({ resource, projectId, userId, canManageJobs }: { resource: 'jobs' | 'audit'; projectId: string; userId?: string; canManageJobs?: boolean }) {
  const [offset, setOffset] = useState(0); const [selected, setSelected] = useState<RecordData>();
  const data = useResource<ListResult<RecordData>>(scoped(`/${resource}`, projectId, { offset, limit: 25 }));
  const config = configs[resource];
  const [operationError, setOperationError] = useState<unknown>(); const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (resource !== 'jobs' || data.error || !data.data?.items.some(item => ['pending', 'running', 'retry_wait'].includes(String(item.status)))) return;
    const timer = window.setInterval(() => { if (document.visibilityState === 'visible') data.reload(); }, 5000);
    return () => window.clearInterval(timer);
  }, [resource, data.data, data.error, data.reload]);
  const operate = async (item: RecordData, operation: string) => {
    setBusy(true); setOperationError(undefined);
    try { await write(`/jobs/${encodeURIComponent(String(item.id))}/${operation}`, {}); setSelected(undefined); data.reload(); } catch (e) { setOperationError(e); } finally { setBusy(false); }
  };
  return <><PageTitle title={config.title} description={config.description}><button className="secondary" onClick={data.reload}>刷新</button></PageTitle>{data.loading ? <Loading /> : data.error ? <ErrorNotice error={data.error} onRetry={data.reload} /> : !data.data?.items.length ? <Empty title={config.empty}>这里会显示实际 API 返回的数据。</Empty> : <><div className="table-wrap"><table><thead><tr><th>名称 / 标识</th><th>类型 / 操作</th><th>状态</th><th>更新时间</th><th /></tr></thead><tbody>{data.data.items.map((item, i) => <tr key={String(item.id ?? i)}><td><strong>{String(item.title ?? item.name ?? item.id ?? '记录')}</strong><small className="row-id">{String(item.id ?? '')}</small></td><td>{String(item.kind ?? item.type ?? item.action ?? item.event ?? item.connector_type ?? '—')}</td><td><Status value={item.status} /></td><td>{formatTime(item.updated_at ?? item.created_at)}</td><td><button className="text-button" onClick={() => setSelected(item)}>查看详情</button></td></tr>)}</tbody></table></div><Pager offset={offset} total={data.data.total} onChange={setOffset} /></>}{selected && <Drawer title={String(selected.title ?? selected.name ?? '记录详情')} onClose={() => setSelected(undefined)}><div className="drawer-body"><ErrorNotice error={operationError} /><JsonView value={selected} />{resource === 'jobs' && (canManageJobs || selected.owner_id === userId) && <div className="toolbar">{['pending', 'running', 'retry_wait'].includes(String(selected.status)) && <button className="secondary" disabled={busy} onClick={() => operate(selected, 'cancel')}>取消任务</button>}{['failed', 'cancelled'].includes(String(selected.status)) && <button disabled={busy} onClick={() => operate(selected, 'retry')}>重新提交任务</button>}</div>}</div></Drawer>}</>;
}

