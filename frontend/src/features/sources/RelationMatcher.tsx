import { useEffect, useState } from 'react';
import { scoped, write } from '../../shared/api/api';
import { useResource } from '../../shared/hooks/hooks';
import type { TypeList } from '../../shared/model/types';
import { ErrorNotice, Field, Loading } from '../../shared/ui/components';

const labels = { create: '可建立', existing: '已有关系', missing_value: '关联值缺失', unmatched: '没有匹配目标', ambiguous: '多个目标同值', self_excluded: '自引用未启用', cardinality_conflict: '已有单目标关系冲突' };
type MatchStatus = keyof typeof labels;
interface MatchRow { source_id: string; source_revision: number; value: string | number | boolean | null; target_id: string | null; status: MatchStatus; candidate_count: number }
interface MatchReport { plan_hash: string; counts: Record<MatchStatus, number>; source_count: number; target_count: number; unresolved_count: number; samples: MatchRow[]; issues: MatchRow[]; semantics: string }
interface MatchRule { project_id: string; expected_model_release_id: string; source_type: string; relation_key: string; source_field: string; target_field: string; allow_self: boolean }
interface SavedRule { id: string; name: string; revision: number; enabled: boolean; config: MatchRule; error?: string; job?: { status: string; result: { pending?: number; blocked?: number } }; issues: { id: string; entity_id: string; error: string }[] }
interface IssuePage { plan_hash: string; rows: MatchRow[]; offset: number; limit: number; total: number }
const family = (kind: string) => ['string', 'enum'].includes(kind) ? 'text' : kind;

export function RelationMatcher({ projectId, disabled, onDirty, onBusy }: {
  projectId: string; disabled: boolean; onDirty: (value: boolean) => void; onBusy: (value: boolean) => void;
}) {
  const types = useResource<TypeList>(scoped('/ontology/types', projectId));
  const rules = useResource<{ items: SavedRule[] }>(scoped('/relation-rules', projectId));
  const [editingRule, setEditingRule] = useState<SavedRule>();
  const pending = rules.data?.items.some(r => r.job && ['pending', 'running'].includes(r.job.status));
  useEffect(() => { if (!pending) return; const timer = window.setInterval(rules.reload, 2000); return () => window.clearInterval(timer); }, [pending, rules.reload]);
  const [sourceKey, setSourceKey] = useState(''); const [relationKey, setRelationKey] = useState('');
  const [sourceField, setSourceField] = useState(''); const [targetField, setTargetField] = useState('');
  const [allowSelf, setAllowSelf] = useState(false); const [partial, setPartial] = useState(false);
  const [plan, setPlan] = useState<{ rule: MatchRule; report: MatchReport; issueOffset: number }>();
  const [busy, setBusy] = useState(false); const [dirty, setDirty] = useState(false);
  const [error, setError] = useState<unknown>(); const [message, setMessage] = useState('');
  useEffect(() => { onDirty(dirty); return () => onDirty(false); }, [dirty, onDirty]);
  useEffect(() => { onBusy(busy || !!plan); return () => onBusy(false); }, [busy, plan, onBusy]);
  const source = types.data?.items.find(item => item.key === sourceKey);
  const relation = source?.relations.find(item => item.key === relationKey);
  const target = types.data?.items.find(item => item.key === relation?.target_type);
  const sourceProp = source?.properties.find(field => field.key === sourceField);
  const reset = () => { setPlan(undefined); setPartial(false); setError(undefined); setMessage(''); setDirty(true); };
  const preview = async () => {
    if (!types.data?.release_id) return;
    const rule = { project_id: projectId, expected_model_release_id: types.data.release_id, source_type: sourceKey, relation_key: relationKey, source_field: sourceField, target_field: targetField, allow_self: allowSelf };
    setBusy(true); setError(undefined); setMessage(''); setPartial(false);
    try { setPlan({ rule, report: await write<MatchReport>('/relations/match-preview', rule), issueOffset: 0 }); }
    catch (e) { setError(e); } finally { setBusy(false); }
  };
  const apply = async () => {
    if (!plan) return;
    setBusy(true); setError(undefined);
    try {
      const result = await write<{ created: number; existing: number; unresolved: number; message: string }>('/relations/match-apply', { rule: plan.rule, expected_plan_hash: plan.report.plan_hash, allow_partial: partial });
      setMessage(`已建立 ${result.created} 条关系，保留 ${result.existing} 条已有关系，${result.unresolved} 条未解决。${result.message}`);
      setPlan(undefined); setDirty(false);
    } catch (e) { setError(e); } finally { setBusy(false); }
  };
  const saveRule = async () => {
    if (!types.data?.release_id) return;
    const rule = { project_id: projectId, expected_model_release_id: types.data.release_id, source_type: sourceKey, relation_key: relationKey, source_field: sourceField, target_field: targetField, allow_self: allowSelf };
    setBusy(true); setError(undefined);
    try {
      if (editingRule) await write(`/relation-rules/${editingRule.id}`, { expected_revision: editingRule.revision, enabled: true, rule }, 'PATCH');
      else await write('/relation-rules', { name: `${source?.label ?? sourceKey} → ${relation?.label ?? relationKey}`, rule });
      setMessage('持续规则已保存，后台开始匹配。人工关系保留，冲突记录单独暂停。'); setDirty(false); setPlan(undefined); setEditingRule(undefined); rules.reload();
    } catch (e) { setError(e); } finally { setBusy(false); }
  };
  const loadIssues = async (offset: number) => {
    if (!plan) return;
    setBusy(true); setError(undefined);
    try {
      const page = await write<IssuePage>('/relations/match-issues', { rule: plan.rule, expected_plan_hash: plan.report.plan_hash, offset, limit: 25 });
      setPlan(previous => previous?.report.plan_hash === page.plan_hash ? { ...previous, issueOffset: page.offset, report: { ...previous.report, issues: page.rows } } : previous);
    } catch (e) { setError(e); } finally { setBusy(false); }
  };
  return <section className="relation-matcher" aria-labelledby="relation-matcher-title">
    <div className="section-heading"><div><span className="intake-kicker">连接业务对象</span><h3 id="relation-matcher-title">确认规则，再建立关系</h3></div><button className="text-button" disabled={busy || disabled} onClick={() => { reset(); types.reload(); }}>刷新已发布模型</button></div>
    <p className="muted">对当前项目所选类型的全部有效对象进行匹配。先在模型中定义并发布关系，再选择两侧字段；可选择一次性确认匹配，或保存持续规则，在后续字段变化时自动维护规则贡献。</p>
    <ErrorNotice error={error ?? types.error ?? rules.error} />{message && <div className="notice success" role="status">{message}</div>}
    {types.loading ? <Loading /> : <fieldset disabled={disabled || busy}><legend>关系与匹配字段</legend>
      <div className="form-grid"><Field label="起点类型"><select value={sourceKey} onChange={e => { reset(); setSourceKey(e.target.value); setRelationKey(''); setSourceField(''); setTargetField(''); }}><option value="">选择已定义关系的类型</option>{types.data?.items.filter(type => type.relations.length).map(type => <option key={type.key} value={type.key}>{type.label}</option>)}</select></Field>
        <Field label="要建立的关系"><select value={relationKey} onChange={e => { reset(); setRelationKey(e.target.value); setTargetField(''); }}><option value="">选择已发布关系</option>{source?.relations.map(item => <option key={item.key} value={item.key}>{item.label} → {types.data?.items.find(type => type.key === item.target_type)?.label ?? item.target_type}</option>)}</select></Field>
        <Field label="起点关联字段"><select value={sourceField} onChange={e => { reset(); setSourceField(e.target.value); setTargetField(''); }}><option value="">选择用于匹配的字段</option>{source?.properties.filter(field => field.data_type !== 'json').map(field => <option key={field.key} value={field.key}>{field.label} · {field.data_type}</option>)}</select></Field>
        <Field label="目标匹配字段"><select value={targetField} onChange={e => { reset(); setTargetField(e.target.value); }}><option value="">选择类型兼容的目标字段</option>{target?.properties.filter(field => sourceProp && family(field.data_type) === family(sourceProp.data_type)).map(field => <option key={field.key} value={field.key}>{field.label} · {field.data_type}</option>)}</select></Field></div>
      {relation?.target_type === sourceKey && <label className="checkbox"><input type="checkbox" checked={allowSelf} onChange={e => { reset(); setAllowSelf(e.target.checked); }} />允许对象连接到自身</label>}
      <button className="secondary" disabled={!sourceKey || !relationKey || !sourceField || !targetField || !types.data?.release_id || busy} onClick={preview}>{busy ? '处理中…' : '预览关系匹配'}</button><button className="secondary" disabled={!sourceKey || !relationKey || !sourceField || !targetField || !types.data?.release_id || busy} onClick={saveRule}>{editingRule ? '更新持续规则' : '保存为持续规则'}</button>
    </fieldset>}
    {!!rules.data?.items.length && <section><div className="section-heading"><h4>持续规则</h4><button className="text-button" onClick={rules.reload}>刷新规则状态</button></div>{rules.data.items.map(rule => <article key={rule.id} className="type-editor"><strong>{rule.name}</strong><p>{rule.enabled ? '已启用' : '已暂停'} · {rule.job?.status === 'pending' || rule.job?.status === 'running' ? '正在后台处理' : '本轮处理结束'}{rule.job?.result.blocked ? ` · ${rule.job.result.blocked} 条记录暂停` : ''}</p>{rule.error && <p className="notice warning">{rule.error}</p>}<div className="toolbar"><button className="secondary" disabled={busy || disabled} onClick={() => { setEditingRule(rule); setSourceKey(rule.config.source_type); setRelationKey(rule.config.relation_key); setSourceField(rule.config.source_field); setTargetField(rule.config.target_field); setAllowSelf(rule.config.allow_self); reset(); }}>修改规则</button><button className="secondary" disabled={busy || disabled} onClick={async () => { setBusy(true); setError(undefined); try { await write(`/relation-rules/${rule.id}`, { expected_revision: rule.revision, enabled: !rule.enabled }, 'PATCH'); rules.reload(); } catch (e) { setError(e); } finally { setBusy(false); } }}>{rule.enabled ? '暂停规则' : '启用并重算'}</button></div>{rule.issues.length > 0 && <details><summary>查看暂停记录（最多 25 条）</summary>{rule.issues.map(issue => <p key={issue.id}><code>{issue.entity_id}</code> · {issue.error}</p>)}</details>}</article>)}</section>}
    {plan && <div className="relation-match-report" aria-label="关系匹配结果">
      <p>起点 {plan.report.source_count} 个 · 目标 {plan.report.target_count} 个。{plan.report.semantics}</p>
      <dl className="match-counts">{Object.entries(labels).map(([key, label]) => <div key={key}><dt>{label}</dt><dd>{plan.report.counts[key as MatchStatus]}</dd></div>)}</dl>
      <h4>匹配样本（最多 25 条）</h4><MatchTable rows={plan.report.samples} />
      {plan.report.unresolved_count > 0 && <details><summary>查看未解决记录（共 {plan.report.unresolved_count} 条）</summary><MatchTable rows={plan.report.issues} />
        <nav className="toolbar" aria-label="未解决记录分页"><button className="secondary" disabled={busy || plan.issueOffset === 0} onClick={() => loadIssues(Math.max(0, plan.issueOffset - 25))}>上一页</button><span role="status">第 {plan.issueOffset + 1}–{plan.issueOffset + plan.report.issues.length} 条，共 {plan.report.unresolved_count} 条</span><button className="secondary" disabled={busy || plan.issueOffset + plan.report.issues.length >= plan.report.unresolved_count} onClick={() => loadIssues(plan.issueOffset + 25)}>下一页</button></nav>
      </details>}
      {plan.report.unresolved_count > 0 && <label className="checkbox"><input type="checkbox" checked={partial} disabled={busy} onChange={e => setPartial(e.target.checked)} />我已知有 {plan.report.unresolved_count} 条未解决，仅建立 {plan.report.counts.create} 条唯一匹配关系</label>}
      <div className="toolbar"><button disabled={busy || !plan.report.counts.create || (!!plan.report.unresolved_count && !partial)} onClick={apply}>建立 {plan.report.counts.create} 条关系</button><button className="secondary" disabled={busy} onClick={() => { setPlan(undefined); setDirty(false); }}>取消预览</button></div>
    </div>}
  </section>;
}

function MatchTable({ rows }: { rows: MatchRow[] }) {
  return <div className="table-wrap"><table><thead><tr><th>起点 / 关联值</th><th>结果</th><th>目标</th></tr></thead><tbody>{rows.map(row => <tr key={row.source_id}><td><span className="match-value">{row.value == null ? '—' : String(row.value)}</span><code className="row-id">{row.source_id}</code></td><td>{labels[row.status]}</td><td>{row.target_id ? <code className="row-id">{row.target_id}</code> : row.candidate_count ? `${row.candidate_count} 个候选` : '—'}</td></tr>)}</tbody></table></div>;
}
