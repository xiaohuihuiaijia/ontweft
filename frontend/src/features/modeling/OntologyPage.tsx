import { useEffect, useRef, useState } from 'react';
import { request, scoped, write } from '../../shared/api/api';
import { Drawer, Empty, ErrorNotice, Field, JsonView, Loading, PageTitle, Status } from '../../shared/ui/components';
import { useResource } from '../../shared/hooks/hooks';
import { MappingEditor } from './MappingEditor';
import { ModelStarter, ModelWorkflow } from './ModelStarter';
import type { ChangeSet, DataType, Definition, DefinitionResult, ListResult, MappingDef, TypeDef, TypeList } from '../../shared/model/types';

const typeOptions: DataType[] = ['string', 'integer', 'number', 'boolean', 'date', 'datetime', 'enum', 'json'];
interface Props { projectId: string; can: (permission: string) => boolean; onDirty: (dirty: boolean) => void }

export function OntologyPage({ projectId, can, onDirty }: Props) {
  const types = useResource<TypeList>(scoped('/ontology/types', projectId));
  const management = useResource<DefinitionResult>(can('ontology:write') || can('ontology:approve') || can('ontology:publish') ? scoped('/ontology/definition', projectId) : null);
  const changes = useResource<ListResult<ChangeSet>>(scoped('/changesets', projectId));
  const [editor, setEditor] = useState<{ draft?: ChangeSet; definition: Definition; baseline: string | null }>();
  return <><PageTitle title="本体模型" description="定义业务类型、字段与关系，通过变更集验证、审核和发布。">{can('ontology:write') && <button onClick={() => { if (management.data) setEditor({ definition: management.data.definition, baseline: management.data.release_id ?? null }); }} disabled={!management.data || editor !== undefined}>新建变更集</button>}</PageTitle>
    <ErrorNotice error={management.error} onRetry={management.reload} /><div className="section-heading"><h2>已发布类型</h2><span className="muted">{types.data?.version ? `版本 ${types.data.version}` : '尚无发布版本'}</span></div>
    {types.loading ? <Loading /> : types.error ? <ErrorNotice error={types.error} onRetry={types.reload} /> : !types.data?.items.length ? <Empty title="尚未定义业务类型">新建变更集，手工添加类型与字段。完成验证和发布后即可创建对象。</Empty> : <div className="card-grid">{types.data.items.map(type => <article className="model-card" key={type.key}><div className="model-symbol">{type.label.slice(0, 1)}</div><h3>{type.label}</h3><code>{type.key}</code><p>{type.description || '暂无业务定义'}</p><div className="card-footer"><span>{type.properties.length} 个字段</span><span>{type.relations.length} 种关系</span></div></article>)}</div>}
    <div className="section-heading"><h2>变更集</h2><button className="text-button" onClick={changes.reload}>刷新</button></div>
    {changes.loading ? <Loading /> : changes.error ? <ErrorNotice error={changes.error} onRetry={changes.reload} /> : !changes.data?.items.length ? <Empty title="暂无变更记录" /> : <div className="table-wrap"><table><thead><tr><th>变更名称</th><th>状态</th><th>修订</th><th>类型数</th><th /></tr></thead><tbody>{changes.data.items.map(item => <tr key={item.id}><td>{item.title}</td><td><Status value={item.status} /></td><td>{item.revision}</td><td>{item.definition.types.length}</td><td><button className="text-button" disabled={editor !== undefined} onClick={() => setEditor({ draft: item, definition: item.definition, baseline: item.baseline_release_id ?? null })}>查看变更</button></td></tr>)}</tbody></table></div>}
    {editor && <OntologyEditor key={editor.draft?.id ?? 'new'} projectId={projectId} can={can} onDirty={onDirty} initialDraft={editor.draft} initialDefinition={editor.definition} initialBaselineReleaseId={editor.baseline} onClose={() => setEditor(undefined)} onUpdated={published => { changes.reload(); if (published) { types.reload(); management.reload(); } }} />}
  </>;
}

export interface OntologyFocus { typeKey: string; relationKey?: string }
interface EditorProps extends Props { initialDraft?: ChangeSet; initialDefinition: Definition; initialBaselineReleaseId: string | null; focus?: OntologyFocus; onClose: () => void; onUpdated: (published: boolean) => void }

export function OntologyEditor({ projectId, can, onDirty, initialDraft, initialDefinition, initialBaselineReleaseId, focus, onClose, onUpdated }: EditorProps) {
  const [baselineReleaseId] = useState(initialBaselineReleaseId);
  const [draft, setDraft] = useState<ChangeSet | null>(initialDraft ?? null);
  const [definition, setDefinition] = useState<TypeDef[]>(() => structuredClone(initialDefinition.types));
  const [mappings, setMappings] = useState<MappingDef[]>(() => structuredClone(initialDefinition.mappings ?? []));
  const [title, setTitle] = useState(initialDraft?.title ?? (focus ? `图谱修改：${focus.typeKey}${focus.relationKey ? ` / ${focus.relationKey}` : ''}` : ''));
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const [remote, setRemote] = useState<ChangeSet>();
  const [notice, setNotice] = useState('');
  useEffect(() => { onDirty(dirty); return () => onDirty(false); }, [dirty, onDirty]);
  useEffect(() => {
    if (draft?.status !== 'validating') return;
    const timer = window.setInterval(() => request<ChangeSet>(`/changesets/${encodeURIComponent(draft.id)}`).then(value => { setDraft(value); if (value.status !== 'validating') onUpdated(false); }).catch(setError), 2000);
    return () => window.clearInterval(timer);
  }, [draft?.id, draft?.status]);
  const validationStopped = !!draft?.validation_job && ['failed', 'cancelled'].includes(draft.validation_job.status);
  const editable = !draft || ['draft', 'validated'].includes(draft.status) || validationStopped;

  const focusedElement = useRef<HTMLElement | null>(null);
  const [focusIndex] = useState(() => {
    const type = initialDefinition.types.findIndex(t => t.key === focus?.typeKey);
    return { type, relation: focus?.relationKey ? initialDefinition.types[type]?.relations.findIndex(r => r.key === focus.relationKey) ?? -1 : -1 };
  });
  useEffect(() => {
    focusedElement.current?.scrollIntoView?.({ block: 'center' });
    focusedElement.current?.querySelector<HTMLInputElement>('input')?.focus({ preventScroll: true });
  }, []);
  const updateType = (index: number, patch: Partial<TypeDef>) => { setDefinition(items => items.map((item, i) => i === index ? { ...item, ...patch } : item)); setDirty(true); };
  const save = async () => {
    setBusy(true); setError(undefined); setNotice('');
    try {
      const body = { title, definition: { types: definition, mappings }, ...(draft ? { expected_revision: draft.revision } : { project_id: projectId, expected_baseline_release_id: baselineReleaseId }) };
      const result = await write<ChangeSet>(draft ? `/changesets/${encodeURIComponent(draft.id)}` : '/changesets', body, draft ? 'PATCH' : 'POST');
      setDraft(result); setDefinition(structuredClone(result.definition.types)); setTitle(result.title); setMappings(structuredClone(result.definition.mappings ?? [])); setDirty(false); onUpdated(false); setNotice('草稿已保存。尚未改变已发布模型。');
    } catch (e) { setError(e); } finally { setBusy(false); }
  };
  const transition = async (action: string) => {
    if (!draft) return;
    setBusy(true); setError(undefined); setNotice('');
    try {
      const result = await write<ChangeSet>(`/changesets/${encodeURIComponent(draft.id)}/${action}`, { expected_revision: draft.revision });
      setDraft(result); onUpdated(action === 'publish');
      setNotice(result.status === 'validating' ? '后台分批校验已启动，可关闭面板后查看结果。' : action === 'publish' ? '发布成功，已发布模型已刷新。' : action === 'approve' ? '审批已记录。' : '验证已完成，请查看验证结果。');
    } catch (e) { setError(e); } finally { setBusy(false); }
  };

  return <Drawer title={draft ? '变更集详情' : '新建模型变更'} onClose={() => { if (!busy && (!dirty || window.confirm('放弃尚未保存的模型修改？'))) onClose(); }}><div className="drawer-body">
      <ErrorNotice error={error} />{focus && <p className="notice">从图谱编辑 {focus.typeKey}{focus.relationKey ? ` / ${focus.relationKey}` : ''}。已载入完整模型与映射，保存后仍须验证、批准和发布。</p>}{notice && <div className="notice success" role="status">{notice}</div>}
      <ModelWorkflow status={draft?.status} dirty={dirty} />
      {draft && <div className="inline-info"><Status value={draft.status} /><span>修订 {draft.revision}</span></div>}
      {!draft && !definition.length && !mappings.length && can('ontology:write') && <ModelStarter projectId={projectId} disabled={busy} onChoose={template => {
        setDefinition(structuredClone(template.definition.types));
        setMappings(structuredClone(template.definition.mappings ?? []));
        if (!title.trim()) setTitle(`${template.title} · 初始模型`);
        setDirty(true);
        setNotice(`已载入“${template.title}”结构模板。请按实际业务修改，保存后继续校验、审批和发布。尚未创建任何业务对象。`);
      }} />}
      <Field label="变更名称"><input required disabled={!editable || busy || !can('ontology:write')} value={title} onChange={e => { setTitle(e.target.value); setDirty(true); }} /></Field>
      {definition.map((type, ti) => <fieldset ref={element => { if (ti === focusIndex.type && focusIndex.relation < 0) focusedElement.current = element; }} className={`type-editor${ti === focusIndex.type ? ' graph-edit-focus' : ''}`} key={ti} disabled={!editable || busy || !can('ontology:write')}><legend>{type.label || `类型 ${ti + 1}`}</legend>
        <div className="form-grid"><Field label="类型标识"><input value={type.key} onChange={e => updateType(ti, { key: e.target.value })} placeholder="例如 project" /></Field><Field label="显示名称"><input value={type.label} onChange={e => updateType(ti, { label: e.target.value })} /></Field></div>
        <Field label="业务定义"><textarea rows={2} value={type.description ?? ''} onChange={e => updateType(ti, { description: e.target.value })} /></Field>
        <h4>属性</h4>{type.properties.map((field, fi) => <div className="property-editor" key={fi}><div className="form-grid"><Field label="字段标识"><input value={field.key} onChange={e => updateType(ti, { properties: type.properties.map((f, i) => i === fi ? { ...f, key: e.target.value } : f) })} /></Field><Field label="字段名称"><input value={field.label} onChange={e => updateType(ti, { properties: type.properties.map((f, i) => i === fi ? { ...f, label: e.target.value } : f) })} /></Field><Field label="数据类型"><select value={field.data_type} onChange={e => updateType(ti, { properties: type.properties.map((f, i) => i === fi ? { ...f, data_type: e.target.value as DataType } : f) })}>{typeOptions.map(t => <option key={t}>{t}</option>)}</select></Field><label className="checkbox"><input type="checkbox" checked={!!field.required} onChange={e => updateType(ti, { properties: type.properties.map((f, i) => i === fi ? { ...f, required: e.target.checked } : f) })} />必填</label></div>{field.data_type === 'enum' && <Field label="选项（每行一个）"><textarea value={field.enum?.join('\n') ?? ''} onChange={e => updateType(ti, { properties: type.properties.map((f, i) => i === fi ? { ...f, enum: e.target.value.split('\n') } : f) })} /></Field>}<details><summary>单位、字段权限与来源管理</summary><Field label="单位"><input value={field.unit ?? ''} onChange={e => updateType(ti, { properties: type.properties.map((f, i) => i === fi ? { ...f, unit: e.target.value || undefined } : f) })} /></Field><Field label="可读角色（逗号分隔）" hint="留空使用项目权限。角色：owner、modeler、reviewer、operator、member、auditor。"><input value={field.read_roles?.join(',') ?? ''} onChange={e => updateType(ti, { properties: type.properties.map((f, i) => i === fi ? { ...f, read_roles: e.target.value ? e.target.value.split(',').map(v => v.trim()) : [] } : f) })} /></Field><Field label="可写角色（逗号分隔）"><input value={field.write_roles?.join(',') ?? ''} onChange={e => updateType(ti, { properties: type.properties.map((f, i) => i === fi ? { ...f, write_roles: e.target.value ? e.target.value.split(',').map(v => v.trim()) : [] } : f) })} /></Field><label className="checkbox"><input type="checkbox" checked={!!field.source_owned} onChange={e => updateType(ti, { properties: type.properties.map((f, i) => i === fi ? { ...f, source_owned: e.target.checked } : f) })} />字段由来源系统管理（平台对象表单只读）</label></details><button className="danger-link" onClick={() => updateType(ti, { properties: type.properties.filter((_, i) => i !== fi) })}>移除此字段</button></div>)}
        <button className="secondary" onClick={() => updateType(ti, { properties: [...type.properties, { key: '', label: '', data_type: 'string', required: false }] })}>添加字段</button>
        <h4>组合唯一键</h4><p className="muted">同一类型中，所选字段的非空组合不可重复。空值不用于身份绑定。</p>{(type.unique_keys ?? []).map((fields, ki) => <div className="property-editor" key={ki}><Field label={`唯一键 ${ki + 1} 字段`}><select multiple value={fields} onChange={e => updateType(ti, { unique_keys: type.unique_keys!.map((key, i) => i === ki ? Array.from(e.target.selectedOptions, o => o.value) : key) })}>{type.properties.filter(p => p.key && p.data_type !== 'json').map(p => <option key={p.key} value={p.key}>{p.label || p.key}</option>)}</select></Field><button className="danger-link" onClick={() => updateType(ti, { unique_keys: type.unique_keys!.filter((_, i) => i !== ki) })}>移除此唯一键</button></div>)}<button className="secondary" onClick={() => updateType(ti, { unique_keys: [...(type.unique_keys ?? []), []] })}>添加唯一键</button>
        <h4>关系定义</h4>{type.relations.map((rel, ri) => <div ref={element => { if (ti === focusIndex.type && ri === focusIndex.relation) focusedElement.current = element; }} className={`property-editor${ti === focusIndex.type && ri === focusIndex.relation ? ' graph-edit-focus' : ''}`} key={ri}><div className="form-grid"><Field label="关系标识"><input value={rel.key} onChange={e => updateType(ti, { relations: type.relations.map((r, i) => i === ri ? { ...r, key: e.target.value } : r) })} /></Field><Field label="业务角色"><input value={rel.label} onChange={e => updateType(ti, { relations: type.relations.map((r, i) => i === ri ? { ...r, label: e.target.value } : r) })} /></Field><Field label="目标类型"><select value={rel.target_type} onChange={e => updateType(ti, { relations: type.relations.map((r, i) => i === ri ? { ...r, target_type: e.target.value } : r) })}><option value="">选择类型</option>{definition.filter(t => t.key).map(t => <option key={t.key} value={t.key}>{t.label || t.key}</option>)}</select></Field><Field label="基数"><select value={rel.cardinality} onChange={e => updateType(ti, { relations: type.relations.map((r, i) => i === ri ? { ...r, cardinality: e.target.value as 'one' | 'many' } : r) })}><option value="one">一个</option><option value="many">多个</option></select></Field></div><button className="danger-link" onClick={() => updateType(ti, { relations: type.relations.filter((_, i) => i !== ri) })}>移除此关系</button></div>)}
        <button className="secondary" onClick={() => updateType(ti, { relations: [...type.relations, { key: '', label: '', target_type: '', cardinality: 'many' }] })}>添加关系</button><button className="danger-link" onClick={() => { setDefinition(items => items.filter((_, i) => i !== ti)); setDirty(true); }}>移除此类型</button>
      </fieldset>)}
      {editable && can('ontology:write') && <button className="secondary" disabled={busy} onClick={() => { setDefinition(items => [...items, { key: '', label: '', properties: [], relations: [] }]); setDirty(true); }}>添加业务类型</button>}
      <MappingEditor projectId={projectId} types={definition} value={mappings} onChange={value => { setMappings(value); setDirty(true); }} disabled={!editable || busy || !can('ontology:write')} canReadSources={can('sources:write')} preview={can('ontology:write') && can('sources:write') ? { draft, dirty, busy } : undefined} />
      {draft?.status === 'validating' && <p role="status" className={validationStopped ? 'notice warning' : 'notice'}>{validationStopped ? `校验任务已停止：${draft.validation_job?.error ?? '已取消'}。可修改草稿重新校验，或在设置 → 任务中心重试。` : `正在后台校验对象与关系，已检查 ${draft.validation_job?.objects_checked ?? 0} 个对象。可关闭面板后查看结果。`}</p>}
      {draft?.status !== 'validating' && draft?.validation && typeof draft.validation.valid === 'boolean' && <section><h3>验证结果</h3><Status value={draft.validation.valid ? 'succeeded' : 'failed'} /><JsonView value={draft.validation.errors} /></section>}
      {remote && <section><h3>服务端最新版本 · {remote.revision}</h3><JsonView value={remote.definition} /><p className="muted">当前输入仍保留。关闭后重新打开可加载该版本。</p></section>}
      {error && draft ? <button className="secondary" onClick={async () => { try { const latest = await request<ChangeSet>(scoped(`/changesets/${encodeURIComponent(draft.id)}`, projectId)); setRemote(latest); } catch (e) { setError(e); } }}>查看最新版本（保留草稿）</button> : null}
    </div><footer className="drawer-actions"><span className="muted">{dirty ? '有未保存修改' : '已与当前修订同步'}</span>{editable && can('ontology:write') && <button disabled={busy || !title.trim() || (!dirty && !!draft)} onClick={save}>{busy ? '处理中…' : '保存草稿'}</button>}{draft && !dirty && <>{['draft', 'validated'].includes(draft.status) && can('ontology:write') && <button disabled={busy} onClick={() => transition('validate')}>验证</button>}{draft.status === 'validated' && draft.validation?.valid && can('ontology:approve') && <button disabled={busy} onClick={() => transition('approve')}>批准当前修订</button>}{draft.status === 'approved' && can('ontology:publish') && <button disabled={busy} onClick={() => transition('publish')}>发布已批准修订</button>}</>}</footer></Drawer>;
}




