import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, request, scoped, write } from '../../shared/api/api';
import { Drawer, Empty, ErrorNotice, Field, JsonView, Loading, Pager, PageTitle } from '../../shared/ui/components';
import { useResource } from '../../shared/hooks/hooks';
import { parseProperties, propertyInputs, samePropertyValue } from '../../shared/model/model';
import { ObjectRelations } from './ObjectRelations';
import { ObjectDocumentEvidence } from './ObjectDocumentEvidence';
import { ObjectContributions } from './contributions/ObjectContributions';
import { ObjectFulltext } from './ObjectFulltext';
import { ObjectTable } from './ObjectTable';
import { compileObjectQuery, newQueryDraft, ObjectQueryEditor, type ObjectQuery, type QueryDraft } from './ObjectQueryEditor';
import type { BusinessObject, ListResult, PropertyDef, TypeDef, TypeList } from '../../shared/model/types';

export function PropertyFields({ fields, values, onChange, disabled }: { fields: PropertyDef[]; values: Record<string, string>; onChange: (key: string, value: string) => void; disabled: boolean }) {
  return <>{fields.map(field => <Field key={field.key} label={`${field.label}${field.required ? ' *' : ''}${field.unit ? `（${field.unit}）` : ''}`} hint={field.source_owned ? '来源系统管理的字段，此处只读；外部修改须通过 Action。' : field.data_type === 'datetime' ? '使用含时区的 ISO 时间，例如 2026-10-28T09:00:00+08:00。' : undefined}>{field.data_type === 'boolean' || field.data_type === 'enum' ? <select disabled={disabled || field.source_owned} required={field.required} value={values[field.key] ?? ''} onChange={e => onChange(field.key, e.target.value)}><option value="">请选择</option>{field.data_type === 'boolean' ? <><option value="true">是</option><option value="false">否</option></> : field.enum?.map(option => <option key={option}>{option}</option>)}</select> : field.data_type === 'json' ? <textarea className="code-input" rows={5} disabled={disabled || field.source_owned} required={field.required} value={values[field.key] ?? ''} onChange={e => onChange(field.key, e.target.value)} /> : <input disabled={disabled || field.source_owned} required={field.required} type={field.data_type === 'date' ? 'date' : field.data_type === 'datetime' ? 'text' : field.data_type === 'number' ? 'number' : 'text'} inputMode={field.data_type === 'integer' ? 'numeric' : undefined} step="any" value={values[field.key] ?? ''} onChange={e => onChange(field.key, e.target.value)} />}</Field>)}</>;
}

export interface ObjectEditSession { object?: BusinessObject; types: TypeDef[]; modelReleaseId: string | null; modelVersion?: number }
export async function loadObjectEditSession(projectId: string, id?: string): Promise<ObjectEditSession> {
  const [object, currentTypes] = await Promise.all([id ? request<BusinessObject>(scoped(`/objects/${encodeURIComponent(id)}`, projectId)) : Promise.resolve(undefined), request<TypeList>(scoped('/ontology/types', projectId))]);
  if (currentTypes.release_id === undefined || (object && object.model_release_id === undefined)) throw new Error('服务未返回对象编辑所需的模型基线，无法安全打开表单。请更新服务后重试。');
  if (object && object.model_release_id !== currentTypes.release_id) throw new ApiError(409, '读取对象期间模型已发布新版本。请重新打开表单；未自动绑定新模型。');
  if (object && !currentTypes.items.some(type => type.key === object.type_key)) throw new Error('对象类型当前不可读，请刷新后重试。');
  return { object, types: currentTypes.items, modelReleaseId: currentTypes.release_id, modelVersion: currentTypes.version };
}

export function ObjectEditor({ object, types: initialTypes, modelReleaseId, modelVersion, projectId, onSaved, onClose, onDirty, canWrite, canReadContributions = false, initialRelationKey, role }: { object?: BusinessObject; types: TypeDef[]; modelReleaseId: string | null; modelVersion?: number; projectId: string; onSaved: () => void; onClose: () => void; onDirty: (dirty: boolean) => void; canWrite: boolean; canReadContributions?: boolean; initialRelationKey?: string; role?: string }) {
  const [types] = useState(initialTypes);
  const [baseline] = useState({ releaseId: modelReleaseId, version: modelVersion });
  const [typeKey, setTypeKey] = useState(object?.type_key ?? types[0]?.key ?? '');
  const [values, setValues] = useState(propertyInputs(object?.properties ?? {}));
  const [current, setCurrent] = useState(object);
  const [remote, setRemote] = useState<BusinessObject>();
  const [dirty, setDirty] = useState(false);
  const [decisionDirty, setDecisionDirty] = useState(false);
  const [decisionSubmitting, setDecisionSubmitting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const [saved, setSaved] = useState(false);
  const dataStatus = remote?.data_status ?? current?.data_status ?? 'active';
  const canEdit = canWrite && dataStatus === 'active' && !decisionDirty;
  const dataReason = { conflict: '来源贡献存在冲突，可由有权限的人员在来源贡献面板核对并裁决。', incomplete: '必填数据不完整，需修复数据后重新物化。', withdrawn: '全部来源贡献已撤回，当前对象不能作为有效业务输入。' };
  const type = types.find(t => t.key === typeKey);
  // Type definitions are filtered by the server. Field write rules remain enforced there.
  const fields = type?.properties ?? [];
  const writable = fields.filter(f => !f.source_owned && (!f.write_roles?.length || (!!role && f.write_roles.includes(role))));
  const readOnly = fields.filter(f => !writable.includes(f));
  useEffect(() => { onDirty(dirty || decisionDirty); return () => onDirty(false); }, [dirty, decisionDirty, onDirty]);
  const save = async (event: React.FormEvent) => {
    event.preventDefault(); if (!type || !canEdit) return;
    setBusy(true); setError(undefined); setSaved(false);
    try {
      const parsed = parseProperties(writable, values, current ? Object.fromEntries(writable.filter(f => Object.hasOwn(current.properties, f.key)).map(f => [f.key, current.properties[f.key]])) : {});
      const properties = current ? Object.fromEntries(Object.entries(parsed).filter(([key, value]) => !samePropertyValue(writable.find(field => field.key === key), current.properties[key], value))) : parsed;
      const result = current
        ? await write<BusinessObject>(`/objects/${encodeURIComponent(current.id)}`, { expected_revision: current.revision, expected_model_release_id: baseline.releaseId, properties }, 'PATCH')
        : await write<BusinessObject>('/objects', { project_id: projectId, type_key: typeKey, expected_model_release_id: baseline.releaseId, properties, evidence: [] });
      setCurrent(result); setValues(propertyInputs(result.properties)); setDirty(false); setSaved(true); onSaved();
    } catch (e) { setError(e); } finally { setBusy(false); }
  };
  return <Drawer closeDisabled={decisionSubmitting} title={current ? '对象详情' : '新建业务对象'} onClose={() => { if ((!dirty && !decisionDirty) || window.confirm('放弃尚未保存的对象修改或字段裁决草稿？')) onClose(); }}><form onSubmit={save} className="drawer-form"><div className="drawer-body"><ErrorNotice error={error} />{saved && <div className="notice success" role="status">已保存至平台，未向外部系统写回。</div>}{current && <div className="resource-meta"><code>{current.id}</code><span>修订 {current.revision}</span></div>}<Field label="业务类型"><select disabled={!!current || busy || !canWrite} value={typeKey} onChange={e => { setTypeKey(e.target.value); setValues({}); setDirty(true); }}>{types.map(t => <option key={t.key} value={t.key}>{t.label}</option>)}</select></Field>
      <p className="muted">编辑模型{baseline.version !== undefined ? ` v${baseline.version}` : ''}：<code>{baseline.releaseId ?? '尚未发布'}</code>。保存与关系修改固定使用此基线；模型发布变化后须重新打开并比较。</p>
      {dataStatus !== 'active' && <div className="notice warning" role="status"><strong>数据状态：{{ conflict: '来源冲突', incomplete: '数据不完整', withdrawn: '来源已撤回' }[dataStatus]}</strong><p>{dataReason[dataStatus]}</p><p>普通属性表单当前只读；已保留的人工字段仍可查看。普通对象修改不能恢复数据状态，默认查询、图谱、关系候选、函数与动作不会把该对象作为有效输入。</p></div>}
      {decisionDirty && <p className="notice warning">有尚未提交的字段裁决草稿，普通属性和关系修改暂时禁用，避免互相覆盖。</p>}
      {!type ? <Empty title="没有可用的已发布类型" /> : <><PropertyFields fields={writable} values={values} disabled={busy || !canEdit} onChange={(key, value) => { setValues(v => ({ ...v, [key]: value })); setDirty(true); setSaved(false); }} /><PropertyFields fields={readOnly} values={values} disabled onChange={() => {}} /></>}
      {current?.evidence && <details><summary>来源证据</summary><ObjectDocumentEvidence evidence={current.evidence} visibleFields={fields.map(field => field.key)} canReadContributions={canReadContributions} /><JsonView value={current.evidence} /></details>}
      {current && <ObjectContributions objectId={current.id} objectRevision={current.revision} modelReleaseId={baseline.releaseId} canRead={canReadContributions} canDecide={canReadContributions && canWrite && !dirty && !busy} onDirty={setDecisionDirty} onSubmitting={setDecisionSubmitting} onDecided={result => {
        if (result.model_release_id !== baseline.releaseId) { setError(new ApiError(409, '裁决已提交，但当前编辑器模型与返回对象不同；请关闭并重新打开核对。')); onSaved(); return; }
        setCurrent(result); setValues(propertyInputs(result.properties)); setRemote(undefined); setError(undefined); setSaved(false); onSaved();
      }} />}
      {current && type && <ObjectRelations initialRelationKey={initialRelationKey} key={`${current.id}:${current.revision}`} object={current} type={type} modelReleaseId={baseline.releaseId} canWrite={canEdit && !dirty} onChanged={onSaved} />}
      {error && current ? <button type="button" className="secondary" onClick={async () => { try { setRemote(await request<BusinessObject>(scoped(`/objects/${encodeURIComponent(current.id)}`, projectId))); } catch (e) { setError(e); } }}>读取最新版本作比较</button> : null}
      {remote && <section><h3>远端修订 {remote.revision}</h3><JsonView value={remote.properties} /><p className="muted">本地输入保持不变。请对比后重新打开最新修订再修改。</p></section>}
    </div><footer className="drawer-actions"><span className="muted">{dirty || decisionDirty ? '有未保存修改' : '当前版本'}</span>{canEdit && <button disabled={busy || !type || (!!current && !dirty)}>{busy ? '保存中…' : '保存对象'}</button>}</footer></form></Drawer>;
}

interface QueryResult extends ListResult<BusinessObject> { release_id: string; version: number }

function useObjectQuery(query: ObjectQuery | null) {
  const body = query ? JSON.stringify(query) : null;
  const current = useRef(body); current.current = body;
  const [epoch, setEpoch] = useState(0);
  const [state, setState] = useState<{ data?: QueryResult; error?: unknown; loading: boolean }>({ loading: false });
  useEffect(() => {
    const controller = new AbortController(); setState({ loading: !!body });
    if (body) request<QueryResult>('/query', { method: 'POST', body, signal: controller.signal }).then(data => {
      if (!controller.signal.aborted && current.current === body) setState({ data, loading: false });
    }).catch(error => {
      if (!controller.signal.aborted && current.current === body) setState({ error: error instanceof ApiError && error.status === 0 ? new ApiError(0, '查询未完成，请检查网络后主动重试。') : error, loading: false });
    });
    return () => controller.abort();
  }, [body, epoch]);
  return { ...state, reload: useCallback(() => setEpoch(value => value + 1), []) };
}

export function ObjectsPage({ projectId, canWrite, canReadContributions = false, onDirty, role }: { projectId: string; canWrite: boolean; canReadContributions?: boolean; onDirty: (dirty: boolean) => void; role?: string }) {
  const [typeKey, setTypeKey] = useState(''); const [q, setQ] = useState(''); const [search, setSearch] = useState(''); const [offset, setOffset] = useState(0);
  const [dataScope, setDataScope] = useState<'active' | 'attention'>('active');
  const visibleScope = canReadContributions ? dataScope : 'active';
  const [editor, setEditor] = useState<ObjectEditSession | null>(null);
  const [opening, setOpening] = useState(false); const [openError, setOpenError] = useState<unknown>();
  const [mode, setMode] = useState<'text' | 'typed' | 'fulltext'>('text');
  const [fulltextRefresh, setFulltextRefresh] = useState(0);
  const [draft, setDraft] = useState<QueryDraft>(); const [savedDraft, setSavedDraft] = useState<QueryDraft>();
  const [queryDirty, setQueryDirty] = useState(false); const [objectDirty, setObjectDirty] = useState(false);
  const [applied, setApplied] = useState<ObjectQuery | null>(null); const [appliedDraft, setAppliedDraft] = useState('');
  const [queryError, setQueryError] = useState<unknown>(); const [message, setMessage] = useState('');
  const types = useResource<TypeList>(scoped('/ontology/types', projectId));
  const simple = useResource<ListResult<BusinessObject>>(mode === 'text' ? scoped('/objects', projectId, { type_key: typeKey, q: search, data_status: visibleScope, limit: 25, offset }) : null);
  const typed = useObjectQuery(mode === 'typed' ? applied : null);
  const objects = mode === 'typed' ? typed : simple;
  useEffect(() => { onDirty(queryDirty || objectDirty); return () => onDirty(false); }, [queryDirty, objectDirty, onDirty]);
  useEffect(() => { if (!canReadContributions) { setDataScope('active'); setOffset(0); } }, [canReadContributions]);
  const runQuery = () => {
    if (!draft || !types.data) return;
    try {
      const query = compileObjectQuery(projectId, draft, types.data);
      setQueryError(undefined); setMessage(''); setApplied(query); setAppliedDraft(JSON.stringify(draft)); typed.reload();
    } catch (error) { setQueryError(error); }
  };
  const queryChanged = !!draft && !!appliedDraft && JSON.stringify(draft) !== appliedDraft;
  const openEditor = async (id?: string) => { if (opening || editor) return; setOpening(true); setOpenError(undefined); try { setEditor(await loadObjectEditSession(projectId, id)); } catch (error) { setOpenError(error); } finally { setOpening(false); } };
  return <><PageTitle title="业务对象" description="按已发布模型维护对象，字段、版本和来源保持可追溯。">{canWrite && <button disabled={!types.data?.items.length || opening || !!editor} onClick={() => openEditor()}>新建对象</button>}</PageTitle>
    <ErrorNotice error={openError} />{opening && <Loading />}
    <div className="tabs" role="tablist" aria-label="对象查询方式"><button type="button" role="tab" aria-selected={mode === 'text'} className={mode === 'text' ? 'active' : ''} onClick={() => { setMode('text'); setApplied(null); }}>字段包含</button><button type="button" role="tab" aria-selected={mode === 'typed'} className={mode === 'typed' ? 'active' : ''} disabled={!types.data?.items.length} onClick={() => { if (!draft && types.data) setDraft(newQueryDraft(types.data)); setMode('typed'); }}>类型化筛选</button><button type="button" role="tab" aria-selected={mode === 'fulltext'} className={mode === 'fulltext' ? 'active' : ''} onClick={() => { setMode('fulltext'); setApplied(null); }}>全文检索</button></div>
    {mode === 'text' ? <form className="filter-bar" onSubmit={e => { e.preventDefault(); setSearch(q); setOffset(0); }}><select aria-label="对象类型" value={typeKey} onChange={e => { setTypeKey(e.target.value); setOffset(0); }}><option value="">全部类型</option>{types.data?.items.map(t => <option key={t.key} value={t.key}>{t.label}</option>)}</select>{canReadContributions && <select aria-label="数据范围" value={visibleScope} onChange={e => { setDataScope(e.target.value as 'active' | 'attention'); setOffset(0); }}><option value="active">有效数据</option><option value="attention">待核对数据（冲突、不完整、已撤回）</option></select>}<input aria-label="搜索对象" value={q} onChange={e => setQ(e.target.value)} placeholder="搜索已授权对象…" /><button className="secondary">查询</button><button type="button" className="text-button" onClick={simple.reload}>刷新</button></form> : mode === 'typed' && draft && <ObjectQueryEditor draft={draft} types={types.data} loading={types.loading} busy={typed.loading} dirty={queryDirty} hasSaved={!!savedDraft} message={message} onChange={value => { setDraft(value); setQueryDirty(true); setMessage(''); }} onRun={runQuery} onSave={() => { setSavedDraft(structuredClone(draft)); setQueryDirty(false); setMessage('查询草稿已暂存于本页内存，离开或刷新页面后会清除。'); }} onRestore={() => { if (savedDraft && (!queryDirty || window.confirm('以本页暂存草稿替换当前未暂存的条件？'))) { setDraft(structuredClone(savedDraft)); setQueryDirty(false); setQueryError(undefined); setMessage('已恢复本页暂存条件，尚未执行查询。'); } }} onRefreshTypes={() => { types.reload(); setMessage('已请求重新读取当前可读模型。草稿条件与原发布基线保持不变，请核对当前模型版本。'); }} />}
    {mode === 'text' && visibleScope === 'attention' && <p className="notice warning">此列表仅供来源管理者核对冲突、不完整或已撤回数据。普通属性只读，可展开来源贡献诊断；具有对象和来源写入及字段权限时可预览、确认单字段裁决。普通查询、图谱、函数和动作不使用这些待核对对象。</p>}
    <ErrorNotice error={types.error} onRetry={types.reload} /><ErrorNotice error={mode === 'typed' ? queryError : undefined} />
    {mode === 'typed' && queryChanged && <p className="notice warning">草稿条件已修改，下面结果仍对应上次执行的条件；分页也沿用上次查询。</p>}
    {mode === 'typed' && typed.data && <div className="inline-info"><span>本次结果模型版本 {typed.data.version} · <code>{typed.data.release_id}</code></span><button type="button" className="text-button" disabled={typed.loading} onClick={typed.reload}>重新执行已应用条件</button></div>}
    {mode === 'fulltext' ? <ObjectFulltext projectId={projectId} types={types.data} modelError={types.error} loadingTypes={types.loading} refreshTypes={types.reload} opening={opening || !!editor} onOpen={openEditor} refreshKey={fulltextRefresh} canManage={canReadContributions} /> : objects.loading ? <Loading /> : objects.error ? <ErrorNotice error={objects.error} onRetry={objects.error instanceof ApiError && objects.error.status === 409 ? undefined : objects.reload} /> : mode === 'typed' && !applied ? <Empty title="配置并执行类型化查询">条件表单使用当前权限下的模型；不会直接查询来源数据库。</Empty> : !objects.data?.items.length ? <Empty title="没有符合条件的对象">调整筛选条件，或在发布类型后创建第一个对象。</Empty> : <><ObjectTable items={objects.data.items} types={types.data?.items ?? []} disabled={opening || !!editor} onOpen={openEditor} /><Pager offset={mode === 'typed' ? applied?.offset ?? 0 : offset} total={objects.data.total} size={mode === 'typed' ? applied?.limit ?? 25 : 25} onChange={value => { if (mode === 'typed' && applied) setApplied({ ...applied, offset: value }); else setOffset(value); }} /></>}
    {editor && <ObjectEditor key={editor.object?.id ?? 'new'} {...editor} projectId={projectId} canWrite={canWrite} canReadContributions={canReadContributions} onSaved={() => { objects.reload(); setFulltextRefresh(value => value + 1); }} onClose={() => setEditor(null)} onDirty={setObjectDirty} role={role} />}
  </>;
}
