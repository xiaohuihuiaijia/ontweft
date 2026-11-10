import { useEffect, useState } from 'react';
import { ApiError } from '../../../shared/api/api';
import { ErrorNotice, formatTime, JsonView, Loading } from '../../../shared/ui/components';
import { useResource } from '../../../shared/hooks/hooks';
import { ContributionDecisions } from './ContributionDecisions';
import type { BusinessObject, RecordData } from '../../../shared/model/types';

interface Contribution {
  id: string; identity_id: string; revision: number; record_id: string | null; record_codec: string;
  release_id: string; mapping_key: string; active: boolean; withdraw_reason: string | null;
  stream_id: string | null; sequence: number | string | null; created_at: string;
  mapped_values: RecordData; source_id?: string; resource_id?: string;
}
interface ContributionPage { data_status?: string; items: Contribution[]; next_cursor: string | null }
interface HistoryPage { items: Contribution[]; next_cursor: number | null }
const denied = (error: unknown) => error instanceof ApiError && [401, 403, 404].includes(error.status);

/** Permission comes from the current project. Remounting clears all cursor/value/history state. */
interface Props { objectId: string; objectRevision?: number; modelReleaseId?: string | null; canRead: boolean; canDecide?: boolean; onDirty?: (dirty: boolean) => void; onDecided?: (object: BusinessObject) => void; onSubmitting?: (submitting: boolean) => void }
export function ObjectContributions(props: Props) {
  return props.canRead ? <ContributionPanel key={props.objectId} {...props} /> : null;
}

function ContributionPanel({ objectId, objectRevision, modelReleaseId, canDecide = false, onDirty, onDecided, onSubmitting }: Props) {
  const [open, setOpen] = useState(false);
  const [epoch, setEpoch] = useState(0);
  const [decisionDirty, setDecisionDirty] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [listEpoch, setListEpoch] = useState(0);
  const [accessError, setAccessError] = useState<unknown>();
  useEffect(() => { onDirty?.(decisionDirty); return () => onDirty?.(false); }, [decisionDirty, onDirty]);
  useEffect(() => { onSubmitting?.(submitting); return () => onSubmitting?.(false); }, [submitting, onSubmitting]);
  return <section className="object-contributions" aria-label="来源贡献">
    <button type="button" className="text-button" disabled={submitting} aria-expanded={open} onClick={() => { if (open && decisionDirty && !window.confirm('放弃尚未提交的字段裁决选择和理由？')) return; setOpen(value => !value); setAccessError(undefined); }}>来源贡献{open ? '（收起）' : '（展开）'}</button>
    {open && <><p className="muted">此面板需要来源写入权限。来源贡献与对象当前生效属性不同；多源冲突或已撤回值仅用于诊断，不代表已采纳的事实。人工字段不会因来源撤回而自动删除。</p>
      {accessError ? <ErrorNotice error={accessError} onRetry={() => { setAccessError(undefined); setEpoch(value => value + 1); }} /> : <div key={epoch}><ContributionDecisions objectId={objectId} objectRevision={objectRevision} modelReleaseId={modelReleaseId} canDecide={canDecide} onDirty={setDecisionDirty} onSubmitting={setSubmitting} onDenied={setAccessError} onDecided={object => { setListEpoch(value => value + 1); onDecided?.(object); }} /><ContributionPages key={listEpoch} objectId={objectId} onDenied={setAccessError} /></div>}
    </>}
  </section>;
}

function ContributionValues({ row }: { row: Contribution }) {
  const [show, setShow] = useState(false);
  return <><p className="muted">授权字段：{Object.keys(row.mapped_values).join('、') || '当前无可读字段'}</p><button type="button" className="text-button" aria-expanded={show} onClick={() => setShow(value => !value)}>{show ? '收起此来源授权值' : '查看此来源授权值'}</button>{show && <div>{!row.active && <p className="notice warning">该贡献已撤回，以下值不作为当前对象属性生效。</p>}<JsonView value={row.mapped_values} /></div>}</>;
}

function ContributionCard({ row, history = false }: { row: Contribution; history?: boolean }) {
  return <><div className="section-heading"><strong>{row.mapping_key} · 修订 {row.revision}</strong><span className={`badge ${row.active ? '' : 'status-paused'}`}>{row.active ? '来源贡献未撤回' : '来源贡献已撤回'}</span></div>
    <dl className="cdc-metadata">{row.source_id && <><dt>来源 / 资源</dt><dd><code>{row.source_id}</code><br /><code>{row.resource_id}</code></dd></>}<dt>{history ? '历史记录' : '贡献标识'}</dt><dd><code>{row.id}</code></dd><dt>映射模型发布</dt><dd><code>{row.release_id}</code></dd><dt>证据记录</dt><dd><code>{row.record_id ?? '—'}</code> · {row.record_codec}</dd>{row.stream_id && <><dt>CDC 流 / 序号</dt><dd><code>{row.stream_id}</code> · {row.sequence ?? '—'}</dd></>}{row.withdraw_reason && <><dt>撤回原因</dt><dd>{row.withdraw_reason}</dd></>}<dt>记录时间</dt><dd>{formatTime(row.created_at)}</dd></dl><ContributionValues key={`${row.id}:${row.revision}`} row={row} />
  </>;
}

function ContributionPages({ objectId, onDenied }: { objectId: string; onDenied: (error: unknown) => void }) {
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  return <ContributionList key={cursors.at(-1) ?? 'first'} objectId={objectId} cursor={cursors.at(-1) ?? null} page={cursors.length} onBack={() => setCursors(value => value.slice(0, -1))} onNext={cursor => setCursors(value => [...value, cursor])} onDenied={onDenied} />;
}

function ContributionList({ objectId, cursor, page, onBack, onNext, onDenied }: { objectId: string; cursor: string | null; page: number; onBack: () => void; onNext: (cursor: string) => void; onDenied: (error: unknown) => void }) {
  const [history, setHistory] = useState<string>();
  const path = `/objects/${encodeURIComponent(objectId)}/contributions?limit=20${cursor ? `&after=${encodeURIComponent(cursor)}` : ''}`;
  const result = useResource<ContributionPage>(path);
  useEffect(() => { if (denied(result.error)) onDenied(result.error); }, [result.error, onDenied]);
  if (result.loading) return <Loading />;
  if (result.error) return <ErrorNotice error={result.error} onRetry={result.reload} />;
  return <><div className="section-heading"><span>当前贡献 · 第 {page} 页{result.data?.data_status ? ` · 对象状态 ${result.data.data_status}` : ''}</span><button type="button" className="text-button" onClick={() => { setHistory(undefined); result.reload(); }}>刷新来源贡献</button></div>
    {!result.data?.items.length ? <p className="muted">当前页没有来源贡献；人工录入对象可能没有来源贡献。</p> : result.data.items.map(row => <article className="contribution-card" key={row.id}><ContributionCard row={row} /><button type="button" className="secondary" aria-expanded={history === row.id} onClick={() => setHistory(value => value === row.id ? undefined : row.id)}>{history === row.id ? '收起贡献历史' : '查看贡献历史'}</button>{history === row.id && <ContributionHistory key={row.id} objectId={objectId} contributionId={row.id} onDenied={onDenied} />}</article>)}
    <div className="pager"><span>按授权贡献分页，不展示未授权总数</span><button type="button" className="secondary" disabled={page === 1} onClick={onBack}>上一页贡献</button><button type="button" className="secondary" disabled={!result.data?.next_cursor} onClick={() => result.data?.next_cursor && onNext(result.data.next_cursor)}>下一页贡献</button></div>
  </>;
}

function ContributionHistory({ objectId, contributionId, onDenied }: { objectId: string; contributionId: string; onDenied: (error: unknown) => void }) {
  const [cursors, setCursors] = useState<(number | null)[]>([null]);
  return <ContributionHistoryPage key={cursors.at(-1) ?? 'first'} objectId={objectId} contributionId={contributionId} before={cursors.at(-1) ?? null} page={cursors.length} onBack={() => setCursors(value => value.slice(0, -1))} onNext={cursor => setCursors(value => [...value, cursor])} onDenied={onDenied} />;
}

function ContributionHistoryPage({ objectId, contributionId, before, page, onBack, onNext, onDenied }: { objectId: string; contributionId: string; before: number | null; page: number; onBack: () => void; onNext: (cursor: number) => void; onDenied: (error: unknown) => void }) {
  const path = `/objects/${encodeURIComponent(objectId)}/contributions/${encodeURIComponent(contributionId)}/history?limit=20${before !== null ? `&before=${before}` : ''}`;
  const result = useResource<HistoryPage>(path);
  useEffect(() => { if (denied(result.error)) onDenied(result.error); }, [result.error, onDenied]);
  if (result.loading) return <Loading />;
  if (result.error) return <ErrorNotice error={result.error} onRetry={result.reload} />;
  return <section className="contribution-history" aria-label="贡献历史"><h4>历史贡献 · 第 {page} 页</h4><p className="muted">历史快照按当前字段权限裁剪；历史值不替换当前对象属性。</p>{result.data?.items.map(row => <article className="contribution-card" key={row.id}><ContributionCard row={row} history /></article>)}{!result.data?.items.length && <p className="muted">当前页没有历史修订。</p>}<div className="pager"><button type="button" className="secondary" disabled={page === 1} onClick={onBack}>较新修订</button><button type="button" className="secondary" disabled={!result.data?.next_cursor} onClick={() => result.data?.next_cursor && onNext(result.data.next_cursor)}>更早修订</button></div></section>;
}
