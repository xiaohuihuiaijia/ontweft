import { describe, expect, it } from 'vitest';
import { integerText, parseProperties, samePropertyValue } from './model';
import type { PropertyDef } from './types';

describe('published-type field parsing', () => {
  const fields: PropertyDef[] = [
    { key: 'amount', label: '金额', data_type: 'number' },
    { key: 'quantity', label: '数量', data_type: 'integer' },
    { key: 'active', label: '启用', data_type: 'boolean' },
    { key: 'status', label: '状态', data_type: 'enum', enum: ['open', 'closed'] },
  ];
  it('preserves decimal precision and distinguishes false from missing', () => {
    expect(parseProperties(fields, { amount: '123456789012345678.123456', quantity: '0', active: 'false', status: 'open', unexpected: 'ignored' })).toEqual({ amount: '123456789012345678.123456', quantity: '0', active: false, status: 'open' });
  });
  it('rejects invalid enums, fractional integers and required empty fields', () => {
    expect(() => parseProperties(fields, { status: 'unknown' })).toThrow('允许选项');
    expect(() => parseProperties(fields, { quantity: '1.1' })).toThrow('十进制整数');
    expect(() => parseProperties([{ key: 'name', label: '名称', data_type: 'string', required: true }], {})).toThrow('请填写名称');
  });
  it('preserves large integers, normalizes signs and does not patch unchanged safe integers', () => {
    expect(parseProperties(fields, { quantity: '+009223372036854775807' })).toEqual({ quantity: '9223372036854775807' });
    expect(integerText('-000', '数量')).toBe('0');
    expect(() => integerText('1e3', '数量')).toThrow('指数');
    expect(() => integerText('9'.repeat(1001), '数量')).toThrow('1000');
    expect(samePropertyValue(fields[1], 42, '42')).toBe(true);
    expect(samePropertyValue(fields[1], '9223372036854775807', '9223372036854775808')).toBe(false);
  });
  it('does not modify a source-owned field', () => {
    expect(parseProperties([{ key: 'source', label: '源值', data_type: 'string', source_owned: true }], { source: 'new' }, { source: 'old' })).toEqual({ source: 'old' });
  });
});
