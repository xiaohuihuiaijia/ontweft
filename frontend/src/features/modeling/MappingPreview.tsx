import { useEffect, useRef, useState } from 'react';
import { ApiError, request } from '../../shared/api/api';
import { ErrorNotice, ExactJson, Field, Loading, Pager, Status } from '../../shared/ui/components';
import { useResource } from '../../shared/hooks/hooks';
import type { ChangeSet, ListResult, MappingDef } from '../../shared/model/types';

interface Snapshot { id: string; source_id: string; source_revision: number; resource_id: string; schema_fingerprint: string; attempt: number; status: string; record_count: number; generation: number; release_id?: string | null }
interface SourceRecord { id: string; source_key_json?: string; values_json?: string }
interface PreviewError { field?: string; stage: string; code: string; message: string }
interface PreviewResult {
  binding: { changeset_id: string; changeset_revision: number; mapping_key: string; snapshot_id: string; snapshot_attempt: number; snapshot_status: string; source_id: string; source_revision: number; resource_id: string; schema_fingerprint: string; mapping_fingerprint: string; snapshot_release_id?: string | null };
  rows: { record_id: string; valid: boolean; fields: { target_field: string; source_field: string; transform: string; source_state: 'missing' | 'null' | 'value'; raw_value_json: string | null; transformed_value_json: string | null; validated_value_json: string | null; errors: PreviewError[] }[]; properties_json: string | null; errors: PreviewError[] }[];
  summary: { scope: 'selected_records_only'; selected_count: number; valid_count: number; invalid_count: number; writes: number; capture_complete: boolean };
}
export interface PreviewContext { draft: ChangeSet | null; dirty: boolean; busy: boolean }
const usableStatus = (status: string) => status === 'complete' || status === 'failed';
const snapshotBinding = (value?: Snapshot) => value ? [value.id, value.source_id, value.source_revision, value.resource_id, value.schema_fingerprint, value.attempt, value.status] : null;

export function MappingPreview({ mapping, context, sourceRevision, sourceLoading, onRefreshSource }: { mapping: MappingDef; context: PreviewContext; sourceRevision?: number; sourceLoading: boolean; onRefreshSource: () => void }) {
  const [offset, setOffset] = useState(0); const [recordOffset, setRecordOffset] = useState(0); const [snapshotId, setSnapshotId] = useState(''); const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [running, setRunning] = useState(false); const [requested, setRequested] = useState(false); const [result, setResult] = useState<{ key: string; value: PreviewResult }>(); const [error, setError] = useState<unknown>();
  const controller = useRef<AbortController | undefined>(undefined);
  const snapshots = useResource<ListResult<Snapshot>>(mapping.source_id ? `/sources/${encodeURIComponent(mapping.source_id)}/snapshots?limit=25&offset=${offset}` : null);
  const candidates = snapshots.data?.items.filter(s => s.resource_id === mapping.resource_id && usableStatus(s.status) && s.attempt > 0) ?? [];
  const snapshot = candidates.find(s => s.id === snapshotId);
  const records = useResource<ListResult<SourceRecord> & { snapshot: Snapshot }>(snapshot ? `/snapshots/${encodeURIComponent(snapshot.id)}/records?limit=25&offset=${recordOffset}` : null);
  const currentSnapshot = records.data?.snapshot.id === snapshotId ? records.data.snapshot : undefined;
  const currentRecords = currentSnapshot ? records.data?.items ?? [] : [];
  const saved = !!context.draft && ['draft', 'validated', 'approved'].includes(context.draft.status) && !context.dirty;
  const matching = !!snapshot && !!currentSnapshot && JSON.stringify(snapshotBinding(snapshot)) === JSON.stringify(snapshotBinding(currentSnapshot)) && currentSnapshot.source_id === mapping.source_id && currentSnapshot.resource_id === mapping.resource_id && currentSnapshot.schema_fingerprint === mapping.schema_fingerprint && currentSnapshot.source_revision === sourceRevision;
  const key = JSON.stringify([context.draft?.id, context.draft?.revision, context.draft?.status, context.draft?.baseline_release_id, context.dirty, context.busy, mapping, sourceRevision, sourceLoading, snapshotBinding(snapshot), snapshotBinding(currentSnapshot), snapshots.loading, records.loading, recordOffset, selectedIds]);
  const currentKey = useRef(key); currentKey.current = key;
  useEffect(() => {
    controller.current?.abort(); setRunning(false); setResult(undefined); setError(undefined);
    return () => controller.current?.abort();
  }, [key]);
  const ready = saved && !context.busy && !sourceLoading && !snapshots.loading && !records.loading && matching && selectedIds.length > 0 && selectedIds.length <= 20 && selectedIds.every(id => currentRecords.some(r => r.id === id && typeof r.source_key_json === 'string' && typeof r.values_json === 'string'));
  const visible = result?.key === key ? result.value : undefined;
  const preview = async () => {
    if (!ready || !context.draft || !snapshot) return;
    controller.current?.abort(); const active = new AbortController(); controller.current = active;
    const requestedKey = key; setRunning(true); setRequested(true); setError(undefined); setResult(undefined);
    try {
      const value = await request<PreviewResult>('/mappings/preview', { method: 'POST', signal: active.signal, body: JSON.stringify({ changeset_id: context.draft.id, expected_revision: context.draft.revision, mapping_key: mapping.key, snapshot_id: snapshot.id, expected_snapshot_attempt: snapshot.attempt, record_ids: selectedIds }) });
      if (active.signal.aborted || currentKey.current !== requestedKey) return;
      const b = value.binding;
      if (b.changeset_id !== context.draft.id || b.changeset_revision !== context.draft.revision || b.mapping_key !== mapping.key || b.snapshot_id !== snapshot.id || b.snapshot_attempt !== snapshot.attempt || b.source_id !== mapping.source_id || b.source_revision !== sourceRevision || b.resource_id !== mapping.resource_id || b.schema_fingerprint !== mapping.schema_fingerprint) throw new Error('预览返回的依赖版本与当前选择不一致，请重新读取并绑定。');
      setResult({ key: requestedKey, value });
    } catch (failure) {
      if (!active.signal.aborted && currentKey.current === requestedKey) setError(failure instanceof ApiError && failure.status === 0 ? new Error('预览请求未完成，原草稿保持不变。请核对网络后主动重试。') : failure);
    } finally { if (!active.signal.aborted && currentKey.current === requestedKey) setRunning(false); }
  };
  return <section className="mapping-preview" aria-label={`映射 ${mapping.key || '未命名'} 的记录预览`}>
    <h4>选中记录预览</h4><p className="muted">仅预览已保存变更集的修订；不会采集数据、创建对象或发布模型。最多明确选择20条记录，结果不代表全量验证。</p>
    {!saved && <p className="notice warning">请先保存模型与映射草稿。已发布变更集须新建草稿后预览；未保存修改时旧结果不可用。</p>}
    {context.draft && <p className="muted">变更集 <code>{context.draft.id}</code> · 已保存修订 {context.draft.revision}</p>}
    <ErrorNotice error={snapshots.error ?? records.error} />
    <Field label="用于预览的持久快照"><select value={snapshotId} disabled={context.busy || snapshots.loading || !mapping.resource_id} onChange={e => { setSnapshotId(e.target.value); setRecordOffset(0); setSelectedIds([]); }}><option value="">选择快照与当前尝试</option>{candidates.map(s => <option key={s.id} value={s.id}>{s.id} · 尝试{s.attempt} · {s.status === 'failed' ? '失败采集，仅诊断' : '已完成'} · {s.record_count}条</option>)}</select></Field>
    <div className="toolbar"><button type="button" className="secondary" disabled={context.busy || running} onClick={() => { setSelectedIds([]); snapshots.reload(); records.reload(); onRefreshSource(); }}>刷新预览依赖</button></div>
    <p className="muted">快照按来源分页，本页仅显示同资源的已完成或失败采集记录。切换快照或记录页将清除记录选择。</p>
    {snapshots.data && snapshots.data.total > 25 && <Pager offset={offset} total={snapshots.data.total} onChange={value => { setOffset(value); setSnapshotId(''); setRecordOffset(0); setSelectedIds([]); }} />}
    {snapshot && !matching && !records.loading && <p className="notice warning">快照尝试、来源修订或结构与当前映射不一致。请刷新依赖并核对来源，必要时修改并保存映射后重新选择。</p>}
    {currentSnapshot?.status === 'failed' && <p className="notice warning">失败采集的已保存样本，仅诊断；不代表完整快照或全量校验。</p>}
    {records.loading && snapshot ? <Loading /> : matching && <><div className="table-wrap"><table><thead><tr><th>选择</th><th>原始身份</th><th>源记录</th></tr></thead><tbody>{currentRecords.map(record => <tr key={record.id}><td><input type="checkbox" aria-label={`选择记录 ${record.id}`} checked={selectedIds.includes(record.id)} disabled={!saved || context.busy || typeof record.source_key_json !== 'string' || typeof record.values_json !== 'string' || (selectedIds.length >= 20 && !selectedIds.includes(record.id))} onChange={event => setSelectedIds(ids => event.target.checked ? [...ids, record.id] : ids.filter(id => id !== record.id))} /><code className="row-id">{record.id}</code></td><td><ExactJson text={record.source_key_json} label="预览记录身份" /></td><td><ExactJson text={record.values_json} label="预览源记录" /></td></tr>)}</tbody></table></div>{records.data && <Pager offset={recordOffset} total={records.data.total} onChange={value => { setRecordOffset(value); setSelectedIds([]); }} />}</>}
    <div className="toolbar"><span>已选择 {selectedIds.length} / 20 条</span><button type="button" disabled={!ready || running || (error instanceof ApiError && error.status === 409)} onClick={preview}>{running ? '正在预览…' : '预览选中记录'}</button></div>
    <ErrorNotice error={error} />{error instanceof ApiError && error.status === 409 && <p className="notice warning">预览绑定已过期。保留当前草稿，先核对最新变更集、来源和快照；需要修改的草稿重新保存，再明确选择快照与记录后预览，不会自动重新绑定。</p>}
    {requested && !visible && !running && !error && <p className="notice warning">旧预览已失效，当前依赖或输入已变化，请重新核对选择并运行预览。</p>}
    {visible && <section aria-label="当前映射预览结果"><p className="notice">仅选中 {visible.summary.selected_count} 条：通过 {visible.summary.valid_count}，失败 {visible.summary.invalid_count}；对象写入 {visible.summary.writes}。不代表整个来源或本体通过验证。</p>{!visible.summary.capture_complete && <p className="notice warning">仅失败/部分采集的选中样本，不证明完整采集。</p>}<p className="muted">快照 {visible.binding.snapshot_id} · 尝试 {visible.binding.snapshot_attempt} · 映射指纹 <code>{visible.binding.mapping_fingerprint}</code></p>{visible.rows.map(row => <article key={row.record_id}><h4>记录 {row.record_id} <Status value={row.valid ? 'succeeded' : 'failed'} /></h4><div className="table-wrap"><table><thead><tr><th>字段与转换</th><th>源值</th><th>转换后</th><th>目标值</th><th>逐字段错误</th></tr></thead><tbody>{row.fields.map(field => <tr key={field.target_field}><td><strong>{field.source_field} → {field.target_field}</strong><p><code>{field.transform}</code></p></td><td>{field.source_state === 'missing' ? <span>源字段缺失</span> : <ExactJson text={field.raw_value_json} label="源值" />}</td><td>{field.transformed_value_json === null ? '未生成' : <ExactJson text={field.transformed_value_json} label="转换值" />}</td><td>{field.validated_value_json === null ? '未通过目标校验' : <ExactJson text={field.validated_value_json} label="目标值" />}</td><td>{field.errors.length ? <ul>{field.errors.map((e, i) => <li key={i}>{e.field ? `${e.field} · ` : ''}{e.stage} / {e.code}：{e.message}</li>)}</ul> : '无错误'}</td></tr>)}</tbody></table></div>{row.errors.length > 0 && <ul className="notice error">{row.errors.map((e, i) => <li key={i}>{e.field ? `${e.field} · ` : ''}{e.stage} / {e.code}：{e.message}</li>)}</ul>}{row.properties_json !== null && <details><summary>目标属性精确文本</summary><ExactJson text={row.properties_json} label="目标属性" /></details>}</article>)}</section>}
  </section>;
}
