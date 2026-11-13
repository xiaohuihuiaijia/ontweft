import { Field } from '../ui/components';
import type { PropertyDef } from './types';

/** The same declared field controls, with a distinct draft-review ownership policy. */
export function PropertyFields({ fields, values, onChange, disabled, mode = 'object' }: {
  fields: PropertyDef[]; values: Record<string, string>; onChange: (key: string, value: string) => void;
  disabled: boolean; mode?: 'object' | 'candidate';
}) {
  return <>{fields.map(field => {
    const readOnly = disabled || (mode === 'object' && !!field.source_owned);
    const required = mode === 'object' && field.required;
    return <Field key={field.key} label={`${field.label}${field.required ? ' *' : ''}${field.unit ? `（${field.unit}）` : ''}`}
      hint={mode === 'object' && field.source_owned ? '来源系统管理的字段，此处只读；外部修改须通过 Action。' : field.data_type === 'datetime' ? '使用含时区的 ISO 时间，例如 2026-10-28T09:00:00+08:00。' : undefined}>
      {field.data_type === 'boolean' || field.data_type === 'enum'
        ? <select disabled={readOnly} required={required} value={values[field.key] ?? ''} onChange={event => onChange(field.key, event.target.value)}><option value="">请选择</option>{field.data_type === 'boolean' ? <><option value="true">是</option><option value="false">否</option></> : field.enum?.map(option => <option key={option}>{option}</option>)}</select>
        : field.data_type === 'json'
          ? <textarea className="code-input" rows={5} disabled={readOnly} required={required} value={values[field.key] ?? ''} onChange={event => onChange(field.key, event.target.value)} />
          : <input disabled={readOnly} required={required} type={field.data_type === 'date' ? 'date' : field.data_type === 'number' ? 'number' : 'text'} inputMode={field.data_type === 'integer' ? 'numeric' : undefined} step="any" value={values[field.key] ?? ''} onChange={event => onChange(field.key, event.target.value)} />}
    </Field>;
  })}</>;
}
