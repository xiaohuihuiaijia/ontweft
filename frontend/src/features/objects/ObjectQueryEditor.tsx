import { Field } from '../../shared/ui/components';
import { scalarValue } from '../../shared/model/model';
import type { PropertyDef, TypeList } from '../../shared/model/types';

export type FilterOp = 'eq' | 'ne' | 'gt' | 'gte' | 'lt' | 'lte' | 'in' | 'contains' | 'is_null';
export interface FilterDraft { field: string; op: FilterOp; value: string; nullValue: boolean }
export interface QueryDraft { typeKey: string; filters: FilterDraft[]; sortField: string; sortDesc: boolean; limit: number; releaseId: string | null }
export interface ObjectQuery { project_id: string; type_key: string; filters: { field: string; op: FilterOp; value: unknown }[]; sort_field: string | null; sort_desc: boolean; limit: number; offset: number; release_id: string }
const names: Record<FilterOp, string> = { eq: '等于', ne: '不等于', gt: '大于', gte: '大于或等于', lt: '小于', lte: '小于或等于', in: '属于列表', contains: '包含文本', is_null: '为空值' };
export const newQueryDraft = (types: TypeList): QueryDraft => ({ typeKey: types.items[0]?.key ?? '', filters: [], sortField: '', sortDesc: false, limit: 25, releaseId: types.release_id ?? null });

export function filterOperators(field?: PropertyDef): FilterOp[] {
  if (!field || field.data_type === 'json') return [];
  const basic: FilterOp[] = ['eq', 'ne'];
  if (['string', 'integer', 'number', 'date', 'datetime'].includes(field.data_type)) basic.push('gt', 'gte', 'lt', 'lte');
  if (field.data_type === 'string') basic.push('contains');
  return [...basic, 'in', 'is_null'];
}

export function compileObjectQuery(projectId: string, draft: QueryDraft, types: TypeList): ObjectQuery {
  if (!draft.releaseId || draft.releaseId !== types.release_id) throw new Error('查询本体基线与当前发布不同，请读取最新模型并明确重新绑定草稿。');
  const type = types.items.find(item => item.key === draft.typeKey);
  if (!type) throw new Error('请选择当前可读的已发布类型。');
  if (draft.filters.length > 20) throw new Error('最多设置 20 条筛选条件。');
  const fields = type.properties.filter(field => field.data_type !== 'json');
  const filters = draft.filters.map((item, index) => {
    const field = fields.find(value => value.key === item.field);
    if (!field || !filterOperators(field).includes(item.op)) throw new Error(`条件 ${index + 1} 的字段或比较方式不可用，请重新选择。`);
    if (item.op === 'is_null' || (item.nullValue && ['eq', 'ne'].includes(item.op))) return { field: item.field, op: item.op, value: null };
    if (item.op === 'in') {
      const lines = item.value.split(/\r?\n/).filter(value => value.trim() !== '');
      if (!lines.length || lines.length > 100) throw new Error(`条件 ${index + 1} 的列表须包含 1–100 行值。`);
      return { field: item.field, op: item.op, value: lines.map(value => scalarValue(field, value)) };
    }
    return { field: item.field, op: item.op, value: scalarValue(field, item.value) };
  });
  if (draft.sortField && !fields.some(field => field.key === draft.sortField)) throw new Error('排序字段不再可读或不支持排序，请重新选择。');
  return { project_id: projectId, type_key: draft.typeKey, filters, sort_field: draft.sortField || null, sort_desc: draft.sortDesc, limit: draft.limit, offset: 0, release_id: draft.releaseId };
}

export function ObjectQueryEditor({ draft, types, loading, busy, hasSaved, dirty, message, onChange, onRun, onSave, onRestore, onRefreshTypes }: {
  draft: QueryDraft; types?: TypeList; loading: boolean; busy: boolean; hasSaved: boolean; dirty: boolean; message: string;
  onChange: (value: QueryDraft) => void; onRun: () => void; onSave: () => void; onRestore: () => void; onRefreshTypes: () => void;
}) {
  const fields = types?.items.find(type => type.key === draft.typeKey)?.properties.filter(field => field.data_type !== 'json') ?? [];
  const stale = !!types && types.release_id !== draft.releaseId;
  const disabled = loading || busy;
  const update = (index: number, patch: Partial<FilterDraft>) => onChange({ ...draft, filters: draft.filters.map((item, i) => i === index ? { ...item, ...patch } : item) });
  return <form className="query-editor" onSubmit={event => { event.preventDefault(); onRun(); }} aria-label="类型化对象查询">
    <div className="section-heading"><h2>类型化筛选</h2><button type="button" className="secondary" disabled={disabled} onClick={onRefreshTypes}>读取最新模型，保留草稿</button></div>
    <p className="muted">所有条件同时满足（AND）。只使用当前可读的已发布字段；JSON 属性不可筛选或排序。修改草稿不会自动重跑查询。</p>
    <p className="muted">草稿基线：<code>{draft.releaseId ?? '尚无发布'}</code> · 当前模型版本：{types?.version ?? '读取中'}</p>
    {stale && <div className="notice warning"><strong>模型发布版本已变化</strong><p>草稿条件保持原样，请检查字段与类型的变化后重新绑定；此操作不自动执行查询。</p><button type="button" className="secondary" disabled={disabled || !types?.release_id} onClick={() => onChange({ ...draft, releaseId: types!.release_id ?? null })}>按当前发布重新绑定草稿</button></div>}
    {message && <div className="notice" role="status">{message}</div>}
    <fieldset className="query-fields" disabled={disabled}>
      <div className="form-grid"><Field label="查询对象类型"><select required value={draft.typeKey} onChange={event => onChange({ ...draft, typeKey: event.target.value, filters: [], sortField: '' })}><option value="">选择已发布类型</option>{draft.typeKey && !types?.items.some(type => type.key === draft.typeKey) && <option value={draft.typeKey}>原类型当前不可用</option>}{types?.items.map(type => <option key={type.key} value={type.key}>{type.label}</option>)}</select></Field><Field label="每页条数"><select value={draft.limit} onChange={event => onChange({ ...draft, limit: Number(event.target.value) })}>{[25, 50, 100, 200].map(value => <option key={value} value={value}>{value} 条</option>)}</select></Field></div>
      {draft.filters.map((item, index) => {
        const field = fields.find(value => value.key === item.field); const operators = filterOperators(field);
        return <fieldset className="property-editor query-condition" key={index}><legend>条件 {index + 1}</legend>
          <div className="form-grid"><Field label={`条件 ${index + 1} 字段`}><select required value={item.field} onChange={event => update(index, { field: event.target.value, op: 'eq', value: '', nullValue: false })}><option value="">选择可读字段</option>{item.field && !field && <option value={item.field}>原字段当前不可用</option>}{fields.map(value => <option key={value.key} value={value.key}>{value.label} · {value.key}{value.unit ? `（${value.unit}）` : ''}</option>)}</select></Field><Field label={`条件 ${index + 1} 比较`}><select value={item.op} disabled={!field} onChange={event => update(index, { op: event.target.value as FilterOp, value: '', nullValue: false })}>{!operators.includes(item.op) && <option value={item.op}>请选择可用比较方式</option>}{operators.map(op => <option key={op} value={op}>{names[op]}</option>)}</select></Field></div>
          {['eq', 'ne'].includes(item.op) && <Field label={`条件 ${index + 1} 值模式`}><select value={item.nullValue ? 'null' : 'value'} onChange={event => update(index, { nullValue: event.target.value === 'null' })}><option value="value">指定值</option><option value="null">空值 null</option></select></Field>}
          {item.op !== 'is_null' && !(item.nullValue && ['eq', 'ne'].includes(item.op)) && <Field label={`条件 ${index + 1} 值`} hint={item.op === 'in' ? '每行一个值，最多 100 行；空白行忽略。布尔值填写 true 或 false，枚举填写已定义值。' : field?.data_type === 'datetime' ? '使用带时区的 ISO 时间，例如 2026-10-28T09:00:00+08:00。' : field?.data_type === 'integer' ? '精确十进制整数；不使用小数、指数或千位分隔符。' : field?.data_type === 'string' ? '允许空字符串；它与空值 null 不同。' : undefined}>
            {item.op === 'in' ? <textarea aria-label={`条件 ${index + 1} 值`} rows={3} value={item.value} onChange={event => update(index, { value: event.target.value })} /> : field?.data_type === 'boolean' || field?.data_type === 'enum' ? <select aria-label={`条件 ${index + 1} 值`} required value={item.value} onChange={event => update(index, { value: event.target.value })}><option value="">选择值</option>{field.data_type === 'boolean' ? <><option value="true">是</option><option value="false">否</option></> : field.enum?.map(value => <option key={value}>{value}</option>)}</select> : <input aria-label={`条件 ${index + 1} 值`} type={field?.data_type === 'date' ? 'date' : 'text'} inputMode={field?.data_type === 'integer' ? 'numeric' : field?.data_type === 'number' ? 'decimal' : undefined} required={field?.data_type !== 'string'} value={item.value} onChange={event => update(index, { value: event.target.value })} />}
          </Field>}
          <button type="button" className="danger-link" onClick={() => onChange({ ...draft, filters: draft.filters.filter((_, i) => i !== index) })}>删除条件 {index + 1}</button>
        </fieldset>;
      })}
      <button type="button" className="secondary" disabled={!fields.length || draft.filters.length >= 20} onClick={() => onChange({ ...draft, filters: [...draft.filters, { field: '', op: 'eq', value: '', nullValue: false }] })}>添加筛选条件</button>
      <div className="form-grid query-sort"><Field label="排序字段"><select value={draft.sortField} onChange={event => onChange({ ...draft, sortField: event.target.value })}><option value="">对象 ID（稳定顺序）</option>{draft.sortField && !fields.some(field => field.key === draft.sortField) && <option value={draft.sortField}>原排序字段当前不可用</option>}{fields.map(field => <option key={field.key} value={field.key}>{field.label} · {field.key}</option>)}</select></Field><Field label="排序方向"><select value={draft.sortDesc ? 'desc' : 'asc'} onChange={event => onChange({ ...draft, sortDesc: event.target.value === 'desc' })}><option value="asc">升序</option><option value="desc">降序</option></select></Field></div>
    </fieldset>
    <div className="toolbar"><button disabled={disabled || !types?.release_id || stale || !draft.typeKey}>{busy ? '查询中…' : '执行类型化查询'}</button><button type="button" className="secondary" disabled={disabled} onClick={onSave}>暂存本页查询草稿</button><button type="button" className="text-button" disabled={disabled || !hasSaved} onClick={onRestore}>恢复本页暂存草稿</button><span className="muted">{dirty ? '有未暂存的条件修改' : ''}</span></div>
    <p className="muted">暂存仅限当前页面内存，不会提交到服务器；刷新或离开此页面会清除草稿。</p>
  </form>;
}
