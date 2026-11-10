import { useEffect, useState } from 'react';
import { request, scoped, write } from '../../shared/api/api';
import { Drawer, Empty, ErrorNotice, Field, formatTime, JsonView, Loading, Pager, PageTitle, Status } from '../../shared/ui/components';
import { useResource } from '../../shared/hooks/hooks';
import type { BusinessObject, ListResult, RecordData, TypeList } from '../../shared/model/types';

const parameters = ['demand', 'inventory', 'reserved'] as const;
type Parameter = typeof parameters[number];
const names: Record<Parameter, string> = { demand: '需求量', inventory: '库存量', reserved: '预留量' };
interface Binding { type_key: string; field: string }
type Bindings = Record<Parameter, Binding>;
interface Kernel extends RecordData { key: string; version: number; self_tests?: { passed: number }; formula?: Record<string, string> }
export interface FunctionVersion { id: string; project_id: string; key: string; label: string; version: number; revision: number; status: string; kernel_key: string; kernel_version: number; unit: string; bindings: Bindings; baseline_release_id: string; author_id: string; reviewed_by?: string; snapshot_hash: string; reviewed_hash?: string; artifact: Kernel }
export interface FunctionRun { id: string; created_at?: string; function_version_id: string; model_release_id: string; request_hash: string; artifact_hash: string; inputs: Record<Parameter, { object_id: string; revision: number; type_key: string; field: string; value: unknown; evidence: unknown[] }>; result: { kind: string; values: { available: string; gap: string }; unit: string; formula: RecordData; null_policy: string } }
interface CursorList<T> { items: T[]; next_offset: number | null }
interface EditorSelection { record?: FunctionVersion; seed?: FunctionVersion; types: TypeList; key: number }
const blankBindings = (): Bindings => ({ demand: { type_key: '', field: '' }, inventory: { type_key: '', field: '' }, reserved: { type_key: '', field: '' } });

function CursorPager({ offset, next, onChange, disabled = false }: { offset: number; next: number | null | undefined; onChange: (value: number) => void; disabled?: boolean }) {
  return <div className="pager"><span>仅列出当前可见记录</span><button className="secondary" disabled={disabled || !offset} onClick={() => onChange(Math.max(0, offset - 25))}>上一页</button><button className="secondary" disabled={disabled || next == null} onClick={() => onChange(next!)}>下一页</button></div>;
}

export function FunctionsPage({ projectId, userId, can, onDirty }: { projectId: string; userId: string; can: (permission: string) => boolean; onDirty: (dirty: boolean) => void }) {
  const [offset, setOffset] = useState(0);
  const catalog = useResource<{ items: Kernel[]; user_code_execution: boolean }>(scoped('/functions/catalog', projectId));
  const versions = useResource<CursorList<FunctionVersion>>(scoped('/functions', projectId, { limit: 25, offset }));
  const kernel = catalog.data?.items.find(item => item.key === 'inventory_gap' && item.version === 1);
  const [editor, setEditor] = useState<EditorSelection>();
  const [error, setError] = useState<unknown>(); const [busy, setBusy] = useState(false);
  const open = async (record?: FunctionVersion, seed?: FunctionVersion) => {
    if (editor || busy) return;
    setBusy(true); setError(undefined);
    try {
      const [types, fresh] = await Promise.all([request<TypeList>(scoped('/ontology/types', projectId)), record ? request<FunctionVersion>(`/functions/${encodeURIComponent(record.id)}`) : Promise.resolve(undefined)]);
      if (!types.release_id) throw new Error('请先发布本体模型，再登记可信函数。');
      setEditor({ record: fresh, seed, types, key: Date.now() });
    } catch (e) { setError(e); } finally { setBusy(false); }
  };
  return <><PageTitle title="可信函数" description="把已发布数值字段绑定到受审核的计算规则，按对象修订运行并保留证据。"><button className="secondary" disabled={busy || !!editor} onClick={() => { catalog.reload(); versions.reload(); }}>刷新函数</button>{can('ontology:write') && <button disabled={!kernel || busy || !!editor} onClick={() => open()}>登记库存缺口函数</button>}</PageTitle>
    <ErrorNotice error={error ?? catalog.error} />{kernel && <section className="notice"><strong>库存缺口 · inventory_gap@1</strong><p>可用库存 = max(库存量 − 预留量, 0)；缺口 = max(需求量 − 可用库存, 0)。三个输入需使用相同显式单位，缺失值不能当作零。运行生成计算记录，不写回业务对象。</p><small>服务端内置自测：{kernel.self_tests?.passed ?? '未提供'} 项通过。仅可配置字段绑定，不支持上传或编辑执行代码。</small></section>}
    {versions.loading ? <Loading /> : versions.error ? <ErrorNotice error={versions.error} onRetry={versions.reload} /> : !versions.data?.items.length ? <Empty title="当前页没有可见函数">登记并通过异人审阅、发布后，项目成员可按授权运行。</Empty> : <div className="table-wrap"><table><thead><tr><th>名称 / 版本</th><th>状态</th><th>单位</th><th>配置修订</th><th /></tr></thead><tbody>{versions.data.items.map(row => <tr key={row.id}><td>{row.label}<code className="row-id">{row.key}@{row.version}</code></td><td><Status value={row.status} /></td><td>{row.unit}</td><td>{row.revision}</td><td><button className="text-button" disabled={busy || !!editor} onClick={() => open(row)}>查看配置与运行</button></td></tr>)}</tbody></table></div>}
    {versions.data && <CursorPager offset={offset} next={versions.data.next_offset} disabled={busy || !!editor} onChange={setOffset} />}
    {editor && <FunctionEditor key={editor.key} projectId={projectId} userId={userId} can={can} onDirty={onDirty} selection={editor} kernel={kernel} onClose={() => setEditor(undefined)} onUpdated={versions.reload} onNewVersion={row => { setEditor(undefined); void (async () => { setBusy(true); setError(undefined); try { const types = await request<TypeList>(scoped('/ontology/types', projectId)); if (!types.release_id) throw new Error('当前没有已发布本体模型。'); setEditor({ seed: row, types, key: Date.now() }); } catch (e) { setError(e); } finally { setBusy(false); } })(); }} />}
  </>;
}

function FunctionEditor({ projectId, userId, can, onDirty, selection, kernel, onClose, onUpdated, onNewVersion }: { projectId: string; userId: string; can: (permission: string) => boolean; onDirty: (dirty: boolean) => void; selection: EditorSelection; kernel?: Kernel; onClose: () => void; onUpdated: () => void; onNewVersion: (row: FunctionVersion) => void }) {
  const initial = selection.record ?? selection.seed;
  const [current, setCurrent] = useState(selection.record);
  const [key, setKey] = useState(initial?.key ?? ''); const [label, setLabel] = useState(initial?.label ?? ''); const [unit, setUnit] = useState(initial?.unit ?? '');
  const [bindings, setBindings] = useState<Bindings>(() => initial ? structuredClone(initial.bindings) : blankBindings());
  const [baseline] = useState(selection.record?.baseline_release_id ?? selection.types.release_id!);
  const [dirty, setDirty] = useState(false); const [busy, setBusy] = useState(false); const [error, setError] = useState<unknown>(); const [message, setMessage] = useState(''); const [remote, setRemote] = useState<FunctionVersion>();
  const [tab, setTab] = useState<'definition' | 'run' | 'history'>('definition');
  useEffect(() => { onDirty(dirty); return () => onDirty(false); }, [dirty, onDirty]);
  const unitChoices = [...new Set(selection.types.items.flatMap(t => t.properties.filter(p => ['integer', 'number'].includes(p.data_type) && p.unit).map(p => p.unit!)))].sort();
  const stale = baseline !== selection.types.release_id;
  const editable = current?.status !== 'published' && can('ontology:write') && !stale;
  const valid = parameters.every(name => { const binding = bindings[name]; return selection.types.items.find(t => t.key === binding.type_key)?.properties.some(p => p.key === binding.field && ['integer', 'number'].includes(p.data_type) && p.unit === unit); });
  const changed = () => { setDirty(true); setMessage(''); };
  const perform = async (operation: () => Promise<void>) => { setBusy(true); setError(undefined); setMessage(''); try { await operation(); } catch (e) { setError(e); } finally { setBusy(false); } };
  const save = (event: React.FormEvent) => { event.preventDefault(); void perform(async () => {
    const body = { project_id: projectId, key, label, kernel_key: 'inventory_gap', kernel_version: 1, baseline_release_id: baseline, unit, bindings, ...(current ? { expected_revision: current.revision } : {}) };
    const result = await write<FunctionVersion>(current ? `/functions/${encodeURIComponent(current.id)}` : '/functions', body, current ? 'PATCH' : 'POST');
    setCurrent(result); setDirty(false); onUpdated(); setMessage('函数配置已保存为草稿，需要另一位审核人员审阅。修改会撤销旧审阅。');
  }); };
  const transition = (action: 'review' | 'publish') => void perform(async () => { const result = await write<FunctionVersion>(`/functions/${encodeURIComponent(current!.id)}/${action}`, { expected_revision: current!.revision }); setCurrent(result); onUpdated(); setMessage(action === 'review' ? '当前配置已由另一位人员审阅。' : '函数版本已发布，可以按对象修订运行。'); });
  return <Drawer title={current ? '函数配置与运行' : selection.seed ? '登记函数新版本' : '登记可信函数'} onClose={() => { if (!busy && (!dirty || window.confirm('放弃未保存的函数字段绑定？'))) onClose(); }}>
    <div className="tabs function-tabs" role="tablist" aria-label="函数详情">{(['definition', 'run', 'history'] as const).map((value, index) => <button role="tab" aria-selected={tab === value} key={value} className={tab === value ? 'active' : ''} disabled={busy || dirty || (value !== 'definition' && current?.status !== 'published')} onClick={() => setTab(value)}>{['配置与审核', '按对象运行', '结果历史'][index]}</button>)}</div>
    {tab === 'definition' ? <form onSubmit={save} className="drawer-form"><div className="drawer-body"><ErrorNotice error={error} />{message && <div role="status" className="notice success">{message}</div>}{current && <div className="inline-info"><Status value={current.status} /><span>版本 {current.version} · 配置修订 {current.revision}</span></div>}{stale && <p className="notice warning">此配置的本体基线已变化。请登记新版本并重新审阅；不会自动改绑较新模型。</p>}
      {!unitChoices.length && <Empty title="尚无带单位的已发布数值字段">请先在本体模型发布 integer 或 number 字段，并填写单位。</Empty>}
      <p className="notice">固定规则 inventory_gap@1：可用库存 = max(库存量 − 预留量, 0)；缺口 = max(需求量 − 可用库存, 0)。空值拒绝计算。</p>
      <Field label="函数稳定标识"><input pattern="[A-Za-z][A-Za-z0-9_]{0,79}" required disabled={busy || !editable || !!current || !!selection.seed} value={key} onChange={e => { setKey(e.target.value); changed(); }} placeholder="例如 inventory_shortage" /></Field>
      <Field label="函数名称"><input required maxLength={200} disabled={busy || !editable} value={label} onChange={e => { setLabel(e.target.value); changed(); }} /></Field>
      <Field label="共同单位"><select required disabled={busy || !editable} value={unit} onChange={e => { setUnit(e.target.value); setBindings(blankBindings()); changed(); }}><option value="">选择已发布字段的单位</option>{unit && !unitChoices.includes(unit) && <option value={unit}>{unit}（当前定义不可用）</option>}{unitChoices.map(value => <option key={value}>{value}</option>)}</select></Field>
      {parameters.map(name => { const binding = bindings[name]; const types = selection.types.items.filter(t => t.properties.some(p => ['integer', 'number'].includes(p.data_type) && p.unit === unit)); const fields = types.find(t => t.key === binding.type_key)?.properties.filter(p => ['integer', 'number'].includes(p.data_type) && p.unit === unit) ?? []; return <fieldset className="property-editor" key={name} disabled={busy || !editable}><legend>{names[name]} · {name}</legend><div className="form-grid"><Field label={`${names[name]}类型`}><select required value={binding.type_key} onChange={e => { setBindings(items => ({ ...items, [name]: { type_key: e.target.value, field: '' } })); changed(); }}><option value="">选择业务类型</option>{binding.type_key && !types.some(t => t.key === binding.type_key) && <option value={binding.type_key}>{binding.type_key}（当前不可用）</option>}{types.map(t => <option key={t.key} value={t.key}>{t.label}</option>)}</select></Field><Field label={`${names[name]}字段`}><select required value={binding.field} onChange={e => { setBindings(items => ({ ...items, [name]: { ...items[name], field: e.target.value } })); changed(); }}><option value="">选择数值字段</option>{binding.field && !fields.some(f => f.key === binding.field) && <option value={binding.field}>{binding.field}（当前不可用）</option>}{fields.map(f => <option key={f.key} value={f.key}>{f.label} · {f.key}（{f.unit}）</option>)}</select></Field></div></fieldset>; })}
      {current?.status === 'draft' && current.author_id === userId && <p className="notice warning">作者不能审阅自己的函数配置，请由另一位有本体审核权限的人员审阅。</p>}
      <details><summary>固定版本、规则和审阅摘要</summary><JsonView value={{ baseline_release_id: baseline, snapshot_hash: current?.snapshot_hash, reviewed_hash: current?.reviewed_hash, author_id: current?.author_id, reviewed_by: current?.reviewed_by, artifact: current?.artifact ?? kernel }} /></details>
      {!!error && current && <button type="button" className="secondary" onClick={() => void perform(async () => setRemote(await request<FunctionVersion>(`/functions/${encodeURIComponent(current.id)}`)))}>读取最新配置作比较</button>}{remote && <section><h3>服务端配置 · 修订 {remote.revision}</h3><JsonView value={remote} /><p className="muted">当前字段输入仍保留，未自动合并。</p></section>}
    </div><footer className="drawer-actions"><span className="muted">{dirty ? '有未保存配置' : '当前配置修订'}</span>{editable && <button disabled={busy || !key || !label.trim() || !unit || !valid || (!!current && !dirty)}>保存函数草稿</button>}{current && !dirty && !stale && <>{current.status === 'draft' && current.author_id !== userId && can('ontology:approve') && <button type="button" disabled={busy} onClick={() => transition('review')}>审阅当前配置</button>}{current.status === 'reviewed' && can('ontology:publish') && <button type="button" disabled={busy} onClick={() => transition('publish')}>发布已审阅版本</button>}</>}{current && !dirty && can('ontology:write') && (current.status === 'published' || stale) && <button type="button" disabled={busy} onClick={() => onNewVersion(current)}>登记新版本</button>}</footer></form> : current && tab === 'run' ? <FunctionRunner key={current.id + ':' + current.revision} projectId={projectId} version={current} stale={stale} onBusy={setBusy} /> : current ? <FunctionHistory versionId={current.id} /> : null}
  </Drawer>;
}

function InputObjectPicker({ projectId, name, binding, value, onChange, disabled }: { projectId: string; name: Parameter; binding: Binding; value?: BusinessObject; onChange: (object?: BusinessObject) => void; disabled: boolean }) {
  const [query, setQuery] = useState(''); const [search, setSearch] = useState(''); const [offset, setOffset] = useState(0);
  const objects = useResource<ListResult<BusinessObject>>(scoped('/objects', projectId, { type_key: binding.type_key, q: search, limit: 25, offset }));
  return <fieldset className="property-editor" disabled={disabled}><legend>{names[name]} · {binding.type_key}.{binding.field}</legend><div className="filter-bar"><input aria-label={`搜索${names[name]}对象`} value={query} onChange={e => setQuery(e.target.value)} placeholder="搜索授权对象…" /><button type="button" className="secondary" onClick={() => { setSearch(query); setOffset(0); onChange(undefined); }}>查询</button></div><ErrorNotice error={objects.error} />
    <Field label={`${names[name]}输入对象`}><select disabled={objects.loading} value={value?.id ?? ''} onChange={e => onChange(objects.data?.items.find(o => o.id === e.target.value))}><option value="">选择业务对象并固定修订</option>{value && !objects.data?.items.some(o => o.id === value.id) && <option value={value.id}>{String(value.properties.name ?? value.properties.title ?? value.id)} · 修订 {value.revision}</option>}{objects.data?.items.map(object => <option key={object.id} value={object.id}>{String(object.properties.name ?? object.properties.title ?? object.id)} · 修订 {object.revision}</option>)}</select></Field>
    {objects.data && objects.data.total > 25 && <Pager offset={offset} total={objects.data.total} onChange={setOffset} />}{value && <p className="notice">已选择修订 {value.revision}，字段 {binding.field} = {value.properties[binding.field] == null ? '缺失或 null（不可运行）' : String(value.properties[binding.field])}</p>}
    <button type="button" className="text-button" onClick={() => { onChange(undefined); objects.reload(); }}>清除选择并重新读取</button>
  </fieldset>;
}

function FunctionRunner({ projectId, version, stale, onBusy }: { projectId: string; version: FunctionVersion; stale: boolean; onBusy: (value: boolean) => void }) {
  const [selected, setSelected] = useState<Partial<Record<Parameter, BusinessObject>>>({}); const [busy, setBusy] = useState(false); const [error, setError] = useState<unknown>(); const [result, setResult] = useState<FunctionRun>();
  const ready = parameters.every(name => selected[name] && selected[name]!.properties[version.bindings[name].field] != null);
  return <div className="drawer-body"><h3>{version.label}</h3><p className="muted">{version.key}@{version.version} · 配置修订 {version.revision} · 单位：{version.unit}</p><p className="notice">每个参数固定到所选对象修订。服务端重新检查版本和字段权限后计算；不会把空值视为零，也不会写回库存。</p><ErrorNotice error={error} />{stale ? <p className="notice warning">本体基线已变化，请登记新版本并重新审阅后运行。</p> : <>{parameters.map(name => <InputObjectPicker key={name} projectId={projectId} name={name} binding={version.bindings[name]} value={selected[name]} disabled={busy} onChange={object => { setSelected(items => ({ ...items, [name]: object })); setResult(undefined); }} />)}<button disabled={!ready || busy} onClick={async () => { setBusy(true); onBusy(true); setError(undefined); setResult(undefined); try { setResult(await write<FunctionRun>(`/functions/${encodeURIComponent(version.id)}/runs`, { expected_function_revision: version.revision, objects: Object.fromEntries(parameters.map(name => [name, { object_id: selected[name]!.id, expected_revision: selected[name]!.revision }])) })); } catch (e) { setError(e); } finally { setBusy(false); onBusy(false); } }}>{busy ? '正在运行…' : '按所选修订运行'}</button></>}{result && <FunctionResult value={result} />}</div>;
}

function FunctionResult({ value }: { value: FunctionRun }) {
  return <section className="function-result"><h3>计算结果</h3><p className="muted">以下为计算记录，不是已采纳事实或外部业务写回。</p><div className="card-grid"><article className="model-card"><h4>可用库存</h4><p className="result-number">{value.result.values.available} <small>{value.result.unit}</small></p></article><article className="model-card"><h4>库存缺口</h4><p className="result-number">{value.result.values.gap} <small>{value.result.unit}</small></p></article></div><h4>输入修订与证据</h4>{parameters.map(name => { const input = value.inputs[name]; return <details key={name}><summary>{names[name]}：{String(input.value)} · 修订 {input.revision} · {input.field}</summary><p className="row-id">{input.object_id}</p>{input.evidence.length ? <JsonView value={input.evidence} /> : <p className="muted">该输入未登记来源证据。</p>}</details>; })}<details><summary>运行记录与可追溯摘要</summary><JsonView value={value} /></details></section>;
}

function FunctionHistory({ versionId }: { versionId: string }) {
  const [offset, setOffset] = useState(0); const [selected, setSelected] = useState<FunctionRun>(); const [error, setError] = useState<unknown>(); const [busy, setBusy] = useState(false);
  const history = useResource<CursorList<FunctionRun>>(`/functions/${encodeURIComponent(versionId)}/runs?limit=25&offset=${offset}`);
  return <div className="drawer-body"><div className="section-heading"><h3>计算记录</h3><button className="secondary" disabled={busy} onClick={() => { setSelected(undefined); history.reload(); }}>刷新记录</button></div><p className="muted">历史记录按当前权限重新核验，不显示无权读取的总数。</p><ErrorNotice error={error ?? history.error} />{history.loading ? <Loading /> : history.data && <>{!history.data.items.length ? <Empty title="当前页没有可见运行记录" /> : <div className="table-wrap"><table><thead><tr><th>时间</th><th>可用库存</th><th>缺口</th><th /></tr></thead><tbody>{history.data.items.map(run => <tr key={run.id}><td>{formatTime(run.created_at)}</td><td>{run.result.values.available}</td><td>{run.result.values.gap} {run.result.unit}</td><td><button className="text-button" disabled={busy} onClick={async () => { setBusy(true); setError(undefined); setSelected(undefined); try { setSelected(await request<FunctionRun>(`/function-runs/${encodeURIComponent(run.id)}`)); } catch (e) { setError(e); } finally { setBusy(false); } }}>查看结果与证据</button></td></tr>)}</tbody></table></div>}<CursorPager offset={offset} next={history.data.next_offset} disabled={busy} onChange={value => { setSelected(undefined); setOffset(value); }} /></>}{selected && <FunctionResult value={selected} />}</div>;
}
