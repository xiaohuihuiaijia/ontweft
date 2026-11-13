import { Field } from '../../shared/ui/components';
import type { DataType, PropertyDef } from '../../shared/model/types';

const typeOptions: DataType[] = ['string', 'integer', 'number', 'boolean', 'date', 'datetime', 'enum', 'json'];

export function PropertyDefinitionEditor({ value, onChange, onRemove, relation = false }: {
  value: PropertyDef; onChange: (value: PropertyDef) => void; onRemove: () => void; relation?: boolean;
}) {
  const patch = (change: Partial<PropertyDef>) => onChange({ ...value, ...change });
  return <div className="property-editor"><div className="form-grid">
    <Field label={relation ? '关系字段标识' : '字段标识'}><input value={value.key} onChange={e => patch({ key: e.target.value })} /></Field>
    <Field label={relation ? '关系字段名称' : '字段名称'}><input value={value.label} onChange={e => patch({ label: e.target.value })} /></Field>
    <Field label="数据类型"><select value={value.data_type} onChange={e => patch({ data_type: e.target.value as DataType })}>{typeOptions.map(t => <option key={t}>{t}</option>)}</select></Field>
    {!relation && <label className="checkbox"><input type="checkbox" checked={!!value.required} onChange={e => patch({ required: e.target.checked })} />必填</label>}
  </div>{value.data_type === 'enum' && <Field label="选项（每行一个）"><textarea value={value.enum?.join('\n') ?? ''} onChange={e => patch({ enum: e.target.value.split('\n') })} /></Field>}
  <details><summary>{relation ? '单位与字段权限' : '单位、字段权限与来源管理'}</summary>
    <Field label="单位"><input value={value.unit ?? ''} onChange={e => patch({ unit: e.target.value || undefined })} /></Field>
    <Field label="可读角色（逗号分隔）" hint="留空使用项目权限。角色：owner、modeler、reviewer、operator、member、auditor。"><input value={value.read_roles?.join(',') ?? ''} onChange={e => patch({ read_roles: e.target.value ? e.target.value.split(',').map(v => v.trim()) : [] })} /></Field>
    <Field label="可写角色（逗号分隔）"><input value={value.write_roles?.join(',') ?? ''} onChange={e => patch({ write_roles: e.target.value ? e.target.value.split(',').map(v => v.trim()) : [] })} /></Field>
    {!relation && <label className="checkbox"><input type="checkbox" checked={!!value.source_owned} onChange={e => patch({ source_owned: e.target.checked })} />字段由来源系统管理（平台对象表单只读）</label>}
  </details><button type="button" className="danger-link" onClick={onRemove}>移除此字段</button></div>;
}
