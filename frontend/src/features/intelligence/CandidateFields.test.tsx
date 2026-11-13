import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { CandidateFields, RelationCandidateFields } from './CandidateFields';
import type { Definition } from '../../shared/model/types';

const definition: Definition = { types: [{ key: 'Product', label: '产品', properties: [
  { key: 'code', label: '编号', data_type: 'integer', source_owned: true },
  { key: 'flag', label: '适用', data_type: 'boolean' },
  { key: 'mode', label: '连接方式', data_type: 'enum', enum: ['螺纹', '卡扣'] },
  { key: 'when', label: '日期', data_type: 'date' },
  { key: 'extra', label: '附加数据', data_type: 'json' },
], relations: [{ key: 'fits', label: '适用于', target_type: 'Product', cardinality: 'many', properties: [{ key: 'voltage', label: '电压限制', data_type: 'number', unit: 'V' }] }] }], mappings: [] };

it('edits source-owned candidates with typed fields and derives the business key without integer precision loss', async () => {
  const save = vi.fn(async (_value: unknown) => {});
  render(<CandidateFields candidate={{ id: 'c', type_key: 'Product', selected: true, business_key: { code: '9007199254740993' }, properties: { code: '9007199254740993', extra: { note: 'retained' } } }} definition={definition} busy={false} error={undefined} onSave={save} onDirty={() => {}} onClose={() => {}} />);
  const code = screen.getByRole('textbox', { name: '编号' }) as HTMLInputElement;
  expect(code.disabled).toBe(false);
  fireEvent.change(code, { target: { value: '9007199254740995' } });
  fireEvent.change(screen.getByRole('combobox', { name: '适用' }), { target: { value: 'false' } });
  fireEvent.change(screen.getByRole('combobox', { name: '连接方式' }), { target: { value: '螺纹' } });
  fireEvent.change(screen.getByLabelText('日期', { selector: 'input[type=date]' }), { target: { value: '2026-11-13' } });
  fireEvent.click(screen.getByRole('button', { name: '保存候选' }));
  await waitFor(() => expect(save).toHaveBeenCalledOnce());
  expect(save.mock.calls[0][0]).toMatchObject({ business_key: { code: '9007199254740995' }, properties: { code: '9007199254740995', flag: false, mode: '螺纹', when: '2026-11-13', extra: { note: 'retained' } } });
});

it('saves relation conditions alongside unchanged evidence', async () => {
  const save = vi.fn(async (_value: unknown) => {});
  const evidence = [{ document_id: 'd', page: 1, quote: 'only at 12.5 V' }];
  render(<RelationCandidateFields relation={{ id: 'r', source_candidate_id: 'a', target_candidate_id: 'b', key: 'fits', properties: { voltage: '12.50' }, evidence }} candidates={['a', 'b'].map(id => ({ id, type_key: 'Product', properties: {} }))} definition={definition} busy={false} error={undefined} onSave={save} onDirty={() => {}} />);
  fireEvent.change(screen.getByLabelText('电压限制（V）'), { target: { value: '24.25' } });
  fireEvent.click(screen.getByRole('button', { name: '保存关系候选' }));
  await waitFor(() => expect(save).toHaveBeenCalledOnce());
  expect(save.mock.calls[0][0]).toMatchObject({ properties: { voltage: '24.25' }, evidence });
});
