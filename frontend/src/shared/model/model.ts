import type { PropertyDef, RecordData } from './types';

export function integerText(raw: string, label: string): string {
  const value = raw.trim();
  if (!/^[+-]?\d+$/.test(value)) throw new Error(`${label}必须为十进制整数，不接受小数或指数形式`);
  if (value.replace(/^[+-]/, '').length > 1000) throw new Error(`${label}最多允许 1000 位整数`);
  return BigInt(value).toString();
}

export function scalarValue(field: PropertyDef, raw: string): unknown {
  switch (field.data_type) {
    case 'integer': return integerText(raw, field.label);
    case 'number':
      if (!/^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$/.test(raw.trim())) throw new Error(`${field.label}必须为十进制数字`);
      return raw.trim();
    case 'boolean':
      if (!['true', 'false'].includes(raw)) throw new Error(`${field.label}必须为是或否`);
      return raw === 'true';
    case 'json':
      try { return JSON.parse(raw); } catch { throw new Error(`${field.label}不是有效 JSON`); }
    case 'enum':
      if (!field.enum?.includes(raw)) throw new Error(`${field.label}不在允许选项中`);
      return raw;
    default: return raw;
  }
}

export function samePropertyValue(field: PropertyDef | undefined, left: unknown, right: unknown): boolean {
  if (field?.data_type === 'integer' && left != null && right != null) return integerText(String(left), field.label) === integerText(String(right), field.label);
  return JSON.stringify(left) === JSON.stringify(right);
}

/** Parse only declared fields. Empty optional inputs are omitted; JSON is data, never code. */
export function parseProperties(fields: PropertyDef[], values: Record<string, string>, base: RecordData = {}): RecordData {
  const result: RecordData = { ...base };
  for (const field of fields) {
    if (field.source_owned) continue;
    const raw = values[field.key] ?? '';
    if (!raw.trim()) {
      if (field.required) throw new Error(`请填写${field.label}`);
      if (field.key in base) result[field.key] = null;
      continue;
    }
    result[field.key] = scalarValue(field, raw);
  }
  return result;
}

export function propertyInputs(properties: RecordData): Record<string, string> {
  return Object.fromEntries(Object.entries(properties).map(([key, value]) => [key, value == null ? '' : typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value)]));
}
