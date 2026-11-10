import { useEffect, useRef, useState } from 'react';
import { ApiError, request } from '../../../shared/api/api';
import { ErrorNotice, Field, formatTime, JsonView, Loading } from '../../../shared/ui/components';
import { useResource } from '../../../shared/hooks/hooks';
import type { BusinessObject } from '../../../shared/model/types';

type Operation = 'select' | 'hold' | 'clear';
type FieldState = 'unopposed' | 'conflict' | 'selected' | 'stale' | 'held' | 'withdrawn';
interface Decision {
  id: string; revision: number; operation: Operation; reason: string; actor_id: string; created_at: string;
  selected_contribution_id: string | null; selected_contribution_revision: number | null;
}
interface Candidate {
  contribution_id: string; revision: number; identity_id: string; source_id: string; source_name?: string; resource_id: string;
  record_id: string; release_id: string; mapping_key: string; active: boolean; value: unknown;
}
interface DecisionField {
  field_key: string; label: string; data_type: string; unit?: string | null; can_decide: boolean;
  state: FieldState; current_present: boolean; current_value?: unknown; decision: Decision | null; candidates: Candidate[];
}
interface Context {
  object_id: string; expected_revision: number; expected_model_release_id: string | null;
  expected_contribution_fingerprint: string; data_status: string; fields: DecisionField[];
}
interface Preview {
  preview_fingerprint: string; field_key: string; operation: Operation;
  before: { present: boolean; value?: unknown }; after: { present: boolean; value?: unknown };
  data_status_before: string; data_status_after: string; remaining_visible_conflicts: string[]; scope: 'platform_only';
}
interface Props {
  objectId: string; objectRevision?: number; modelReleaseId?: string | null; canDecide: boolean;
  onDirty: (dirty: boolean) => void; onDecided: (object: BusinessObject) => void; onDenied: (error: unknown) => void;
  onSubmitting?: (submitting: boolean) => void;
}
const stateLabels: Record<FieldState, string> = { unopposed: '无冲突', conflict: '来源冲突', selected: '已选择来源', stale: '决定已过期', held: '已暂停字段', withdrawn: '来源已撤回' };
const statusLabel = (state: string) => ({ active: '有效', conflict: '来源冲突', incomplete: '数据不完整', withdrawn: '来源已撤回' }[state] ?? state);
const operationLabels: Record<Operation, string> = { select: '选择来源', hold: '保留双方并暂停字段', clear: '撤销人为决定' };
const needsAttention = (field: DecisionField) => ['conflict', 'stale', 'held'].includes(field.state);
const denied = (error: unknown) => error instanceof ApiError && [401, 403, 404].includes(error.status);

function DecisionMetadata({ decision }: { decision: Decision }) {
  return <dl className="cdc-metadata"><dt>处理方式 / 修订</dt><dd>{operationLabels[decision.operation]} · {decision.revision}</dd><dt>理由</dt><dd className="decision-reason">{decision.reason}</dd><dt>处理人 / 时间</dt><dd><code>{decision.actor_id}</code> · {formatTime(decision.created_at)}</dd>{decision.selected_contribution_id && <><dt>选中贡献</dt><dd><code>{decision.selected_contribution_id}</code> · 修订 {decision.selected_contribution_revision}</dd></>}</dl>;
}

function CandidateValue({ candidate }: { candidate: Candidate }) {
  const [open, setOpen] = useState(false);
  return <><button type="button" className="text-button" aria-expanded={open} onClick={() => setOpen(value => !value)}>{open ? '收起候选值' : '查看授权候选值'}</button>{open && <><JsonView value={candidate.value} /><dl className="cdc-metadata"><dt>身份 / 证据记录</dt><dd><code>{candidate.identity_id}</code><br /><code>{candidate.record_id}</code></dd><dt>贡献模型发布</dt><dd><code>{candidate.release_id}</code></dd></dl></>}</>;
}

function DecisionHistory({ objectId, fieldKey, onDenied }: { objectId: string; fieldKey: string; onDenied: (error: unknown) => void }) {
  const [cursors, setCursors] = useState<(number | null)[]>([null]);
  const before = cursors.at(-1);
  const result = useResource<{ items: Decision[]; next_cursor: number | null }>(`/objects/${encodeURIComponent(objectId)}/contribution-decisions/${encodeURIComponent(fieldKey)}/history?limit=20${before !== null ? `&before=${before}` : ''}`);
  useEffect(() => { if (denied(result.error)) onDenied(result.error); }, [result.error, onDenied]);
  return <section className="contribution-history" aria-label="字段裁决历史"><h4>字段裁决历史 · 第 {cursors.length} 页</h4><p className="muted">历史处理记录不代表当前仍然有效；读取时重新核对当前权限。</p>{result.loading ? <Loading /> : result.error ? <ErrorNotice error={result.error} onRetry={result.reload} /> : <>{result.data?.items.map(item => <article className="contribution-card" key={item.id}><DecisionMetadata decision={item} /></article>)}{!result.data?.items.length && <p className="muted">暂无裁决记录。</p>}<div className="pager"><button type="button" className="secondary" disabled={cursors.length === 1} onClick={() => setCursors(values => values.slice(0, -1))}>较新裁决</button><button type="button" className="secondary" disabled={result.data?.next_cursor == null} onClick={() => { const next = result.data?.next_cursor; if (next != null) setCursors(values => [...values, next]); }}>更早裁决</button></div></>}</section>;
}

/** A server-computed field context, separate from the paginated provenance list. */
export function ContributionDecisions(props: Props) {
  const { objectId, objectRevision, modelReleaseId, canDecide, onDirty, onDecided, onDenied, onSubmitting } = props;
  const [open, setOpen] = useState(false);
  const [context, setContext] = useState<Context>();
  const [latest, setLatest] = useState<Context>();
  const [fieldKey, setFieldKey] = useState('');
  const [operation, setOperation] = useState<Operation>('select');
  const [selected, setSelected] = useState('');
  const [reason, setReason] = useState('');
  const [dirty, setDirty] = useState(false);
  const [attentionOnly, setAttentionOnly] = useState(true);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [preview, setPreview] = useState<Preview>();
  const [error, setError] = useState<unknown>();
  const [notice, setNotice] = useState('');
  const [mustRefresh, setMustRefresh] = useState(false);
  const [busy, setBusy] = useState<'read' | 'preview' | 'commit' | null>(null);
  const controller = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  const previousRevision = useRef(objectRevision);
  const path = `/objects/${encodeURIComponent(objectId)}/contribution-decisions`;
  const field = context?.fields.find(item => item.field_key === fieldKey);
  const selectedCandidate = field?.candidates.find(item => item.contribution_id === selected);
  const schemaMatches = context?.expected_model_release_id === modelReleaseId;
  const canMutate = canDecide && !!field?.can_decide && schemaMatches;
  const body = context && field ? {
    expected_revision: context.expected_revision, expected_model_release_id: context.expected_model_release_id,
    expected_contribution_fingerprint: context.expected_contribution_fingerprint, field_key: fieldKey, operation,
    selected_contribution_id: operation === 'select' ? selected || null : null, reason: reason.trim(),
  } : null;
  const validDraft = !!body && !!body.reason && (operation !== 'select' || !!selectedCandidate?.active);

  useEffect(() => { onDirty(dirty); return () => onDirty(false); }, [dirty, onDirty]);
  useEffect(() => { onSubmitting?.(busy === 'commit'); return () => onSubmitting?.(false); }, [busy, onSubmitting]);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; controller.current?.abort(); }; }, []);
  useEffect(() => {
    if (!canDecide) { controller.current?.abort(); setBusy(null); setPreview(undefined); setSelected(''); setReason(''); setDirty(false); }
  }, [canDecide]);
  useEffect(() => {
    if (previousRevision.current !== objectRevision) {
      previousRevision.current = objectRevision;
      if (context && context.expected_revision !== objectRevision) { setPreview(undefined); setLatest(undefined); setMustRefresh(true); }
    }
  }, [objectRevision, context]);

  const invalidate = () => { setPreview(undefined); setDirty(true); setNotice(''); };
  const fail = (e: unknown) => {
    setError(e); setPreview(undefined);
    if (denied(e)) { setContext(undefined); setLatest(undefined); setSelected(''); setReason(''); setDirty(false); onDenied(e); }
    else if (e instanceof ApiError && [0, 409].includes(e.status)) { setMustRefresh(true); setLatest(undefined); }
  };
  const read = async (preserve = dirty) => {
    controller.current?.abort(); const abort = new AbortController(); controller.current = abort;
    setBusy('read'); setError(undefined); setPreview(undefined); setLatest(undefined);
    try {
      const result = await request<Context>(path, { signal: abort.signal });
      if (abort.signal.aborted || !mounted.current) return;
      if (preserve) {
        // A successful refresh may reduce field visibility without returning 403.
        // Keep the draft/baseline, but do not keep values of fields no longer returned.
        const visible = new Set(result.fields.map(item => item.field_key));
        setContext(value => value ? { ...value, fields: value.fields.filter(item => visible.has(item.field_key)) } : value);
        setLatest(result); setMustRefresh(true);
      }
      else { setContext(result); setMustRefresh(false); setFieldKey(key => result.fields.some(item => item.field_key === key) ? key : result.fields.find(needsAttention)?.field_key ?? ''); }
    } catch (e) { if (!abort.signal.aborted && mounted.current) fail(e); }
    finally { if (!abort.signal.aborted && mounted.current) setBusy(null); }
  };
  const resetDraft = () => { setSelected(''); setReason(''); setOperation('select'); setDirty(false); setPreview(undefined); setHistoryOpen(false); };
  const toggle = () => {
    if (open) {
      if (dirty && !window.confirm('放弃尚未提交的字段裁决选择和理由？')) return;
      controller.current?.abort(); setBusy(null); resetDraft(); setContext(undefined); setLatest(undefined); setError(undefined); setOpen(false);
    } else { setOpen(true); void read(false); }
  };
  const rebind = () => {
    if (!latest) return;
    const nextField = latest.fields.find(item => item.field_key === fieldKey);
    if (!nextField?.candidates.some(item => item.contribution_id === selected && item.active)) setSelected('');
    setContext(latest); setLatest(undefined); setMustRefresh(false); setPreview(undefined); setHistoryOpen(false); setError(undefined);
    setNotice('草稿已显式绑定最新对象、模型与贡献集合。请重新核对来源修订和值，再预览。');
  };
  const submit = async (commit: boolean) => {
    if (!body || !canMutate || !validDraft || busy || mustRefresh || latest || (commit && !preview)) return;
    const frozen = { ...body, ...(commit ? { expected_preview_fingerprint: preview!.preview_fingerprint } : {}) };
    const abort = new AbortController(); controller.current = abort; setBusy(commit ? 'commit' : 'preview'); setError(undefined); setNotice('');
    try {
      if (commit) {
        const result = await request<{ decision: Decision; object: BusinessObject }>(path, { method: 'POST', body: JSON.stringify(frozen), signal: abort.signal });
        if (abort.signal.aborted || !mounted.current) return;
        resetDraft(); setContext(undefined); setLatest(undefined); setMustRefresh(false);
        setNotice(`字段裁决已保存，对象修订 ${result.object.revision}，数据状态：${statusLabel(result.object.data_status ?? 'active')}。${result.object.data_status === 'active' ? '已恢复有效，将移出待核对列表。' : '仍须核对其余问题，未标记全部冲突已解决。'}仅更新平台，未向来源系统写回。`);
        onDecided(result.object);
        // Re-read the server's context after success; this is not a retry of the write.
        await read(false);
      } else {
        const result = await request<Preview>(`${path}/preview`, { method: 'POST', body: JSON.stringify(frozen), signal: abort.signal });
        if (abort.signal.aborted || !mounted.current) return;
        if (result.scope !== 'platform_only' || result.field_key !== body.field_key || result.operation !== body.operation) throw new Error('预览响应与当前操作不一致，不能确认。');
        setPreview(result);
      }
    } catch (e) { if (!abort.signal.aborted && mounted.current) fail(e); }
    finally { if (!abort.signal.aborted && mounted.current) setBusy(null); }
  };

  return <section className="contribution-decisions" aria-label="字段裁决">
    <button type="button" className="secondary" disabled={busy === 'commit'} aria-expanded={open} onClick={toggle}>{open ? '收起字段裁决' : '查看字段冲突与裁决'}</button>
    {open && <><p className="muted">每次处理一个授权字段。选择来源固定到具体贡献修订；该字段任一来源更新或模型发布后可能过期。暂停字段会保留来源记录，后续来源更新不会自动解除暂停。撤销决定恢复默认聚合，不保证消除冲突。</p>
      <ErrorNotice error={error} />{notice && <p className="notice success" role="status">{notice}</p>}
      <button type="button" className="text-button" disabled={!!busy} onClick={() => void read()}>读取最新裁决基线</button>
      {busy === 'read' && <Loading />}
      {latest && <div className="notice warning"><p>已读取最新对象修订 {latest.expected_revision}。原选择与理由仍保留，尚未替换旧基线。</p><button type="button" className="secondary" disabled={!!busy} onClick={rebind}>使用最新基线重新绑定草稿</button></div>}
      {mustRefresh && !latest && <p className="notice warning">当前裁决基线不可继续提交。请主动读取最新情况、重新绑定草稿并再次预览；不会自动重试。</p>}
      {context && <><p className="resource-meta">裁决对象修订 {context.expected_revision} · 数据状态 {statusLabel(context.data_status)} · 模型 <code>{context.expected_model_release_id ?? '未发布'}</code></p>
        {!schemaMatches && <p className="notice warning">裁决模型与此对象编辑器的冻结模型不同。可查看当前诊断，但请保留理由后关闭并重新打开对象，不能使用旧字段表单提交。</p>}
        <label className="checkbox"><input type="checkbox" checked={attentionOnly} onChange={e => setAttentionOnly(e.target.checked)} />只看冲突、过期或暂停字段</label>
        <div className="decision-field-list" aria-label="授权贡献字段">{context.fields.filter(item => !attentionOnly || needsAttention(item)).map(item => <button type="button" className={`secondary ${fieldKey === item.field_key ? 'selected' : ''}`} key={item.field_key} aria-pressed={fieldKey === item.field_key} disabled={!!busy} onClick={() => {
          if (item.field_key === fieldKey || (dirty && !window.confirm('切换字段会放弃尚未提交的裁决选择和理由，是否继续？'))) return;
          resetDraft(); setFieldKey(item.field_key); setNotice(''); setError(undefined);
        }}>{item.label} · {stateLabels[item.state]}</button>)}</div>
        {!context.fields.some(item => !attentionOnly || needsAttention(item)) && <p className="muted">此范围没有可见字段；取消上方筛选可查看其他授权贡献字段。这里不展示隐藏字段数量。</p>}
        {fieldKey && !field && <div className="notice warning"><p>原字段已不可用。原理由仍保留，请核对当前授权字段后重新选择。</p>{dirty && <><p className="decision-reason">尚未提交的理由：{reason}</p><button type="button" className="text-button" onClick={() => { if (window.confirm('放弃尚未提交的裁决选择和理由？')) resetDraft(); }}>放弃裁决草稿</button></>}</div>}
        {field && <div className="decision-editor" key={field.field_key}>
          <h4>{field.label}{field.unit ? `（${field.unit}）` : ''} · {stateLabels[field.state]}</h4><p className="muted"><code>{field.field_key}</code> · {field.data_type}</p>
          {field.state === 'stale' && <p className="notice warning">先前选择已过期；旧决定保留为处理记录，不能当作当前有效采纳。</p>}
          <p>当前生效值</p>{field.current_present ? <JsonView value={field.current_value} /> : <p className="muted">当前字段不生效或未提供值；这与空值 null 不同。</p>}
          {field.decision && <details><summary>当前人为决定记录</summary><DecisionMetadata decision={field.decision} /></details>}
          <div className="decision-candidates" aria-label="授权候选来源">{field.candidates.map(candidate => <article className="contribution-card" key={`${candidate.contribution_id}:${candidate.revision}`}>
            <label className="decision-candidate"><input type="radio" name={`contribution-choice-${objectId}-${fieldKey}`} aria-label={`选择${candidate.mapping_key}修订${candidate.revision}`} checked={selected === candidate.contribution_id} disabled={!!busy || !canMutate || !candidate.active || operation !== 'select'} onChange={() => { setSelected(candidate.contribution_id); invalidate(); }} /><span><strong>{candidate.source_name || candidate.source_id}</strong> · 贡献修订 {candidate.revision}<small>映射 {candidate.mapping_key} · 资源 {candidate.resource_id}</small><code>{candidate.contribution_id}</code>{!candidate.active && <span className="notice warning">已撤回，不可选择；保留用于追溯。</span>}</span></label>
            <CandidateValue candidate={candidate} />
          </article>)}</div>
          {!canMutate && <p className="muted">当前仅可查看。处理需要来源写入和对象写入权限、字段可裁决权限及同版本模型；普通对象有未保存修改时也需先处理。</p>}
          {canDecide && <><Field label="字段处理方式"><select value={operation} disabled={!!busy || !canMutate} onChange={e => { setOperation(e.target.value as Operation); invalidate(); }}><option value="select">选择来源</option><option value="hold">保留双方并暂停字段</option><option value="clear" disabled={!field.decision || field.decision.operation === 'clear'}>撤销人为决定</option></select></Field>
            <Field label="裁决理由" hint="理由与处理人、贡献修订、对象修订一并留存；不要填写无关敏感信息。"><textarea rows={3} value={reason} disabled={!!busy || !canMutate} onChange={e => { setReason(e.target.value); invalidate(); }} /></Field>
            <div className="toolbar"><button type="button" className="secondary" disabled={!!busy || !canMutate || !validDraft || mustRefresh || !!latest} onClick={() => void submit(false)}>{busy === 'preview' ? '预览中…' : '预览裁决影响'}</button>{dirty && <button type="button" className="text-button" disabled={!!busy} onClick={() => { if (window.confirm('放弃尚未提交的裁决选择和理由？')) resetDraft(); }}>放弃裁决草稿</button>}</div>
          </>}
          {preview && <section className="decision-preview" aria-label="裁决影响预览"><h4>确认前核对</h4><p>{operationLabels[preview.operation]} · 仅平台生效，未向来源写回</p><div className="form-grid"><div><strong>处理前</strong>{preview.before.present ? <JsonView value={preview.before.value} /> : <p>字段不生效或未提供</p>}</div><div><strong>处理后</strong>{preview.after.present ? <JsonView value={preview.after.value} /> : <p>字段不生效或未提供</p>}</div></div><p>预计对象状态：{statusLabel(preview.data_status_before)} → {statusLabel(preview.data_status_after)}</p><p>仍需核对的可见冲突字段：{preview.remaining_visible_conflicts.map(key => context.fields.find(item => item.field_key === key)?.label ?? key).join('、') || '无可见项；对象是否有效以服务端状态为准'}</p><p className="muted">本次仅预览该对象字段和数据状态，不代表关系、知识或外部动作的完整影响分析，也未执行外部业务。</p><button type="button" disabled={!!busy || !canMutate || mustRefresh || !!latest} onClick={() => void submit(true)}>{busy === 'commit' ? '确认中…' : '确认保存字段裁决'}</button></section>}
          <button type="button" className="text-button" aria-expanded={historyOpen} onClick={() => setHistoryOpen(value => !value)}>{historyOpen ? '收起裁决历史' : '查看裁决历史'}</button>{historyOpen && <DecisionHistory key={field.field_key} objectId={objectId} fieldKey={field.field_key} onDenied={onDenied} />}
        </div>}
      </>}
    </>}
  </section>;
}
