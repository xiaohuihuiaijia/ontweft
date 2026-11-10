import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { SourceModelBuilder, UploadedTablePreview, UploadTables } from './SourceIntake';

const source = { id: 'source-1', name: '我的数据', kind: 'tabular', revision: 1, config: {}, resources: [{ resource_id: 'sheet_1', name: '工作表一', primary_key: ['_source_row'], foreign_keys: [], fields: [{ name: 'c1', data_type: 'string', nullable: true, metadata: { label: '名称' } }], metadata: { row_count: 2, formula_cells_as_text: 1 } }] };
const noop = () => {};

it('shows table labels and exact text values without numeric coercion', () => {
  render(<UploadedTablePreview resource={source.resources[0]} sample={{ records: [{ values: { c1: '001' } }, { values: { c1: '9223372036854775809' } }, { values: { c1: null } }], sample_json: '{}' }} />);
  expect(screen.getByRole('columnheader', { name: '名称' })).toBeTruthy();
  expect(screen.getByRole('cell', { name: '001' })).toBeTruthy();
  expect(screen.getByRole('cell', { name: '9223372036854775809' })).toBeTruthy();
  expect(screen.getByRole('cell', { name: '—' })).toBeTruthy();
});

it('uploads the selected original file with explicit CSV settings, then clears only after success', async () => {
  const uploaded = vi.fn(); const dirty = vi.fn();
  const file = new File(['编号;名称\n001;设备'], '设备.csv', { type: 'text/csv' });
  let sent: FormData | undefined;
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, init) => {
    sent = init?.body as FormData;
    return new Response(JSON.stringify(source), { status: 201 });
  });
  render(<UploadTables projectId="p" disabled={false} onUploaded={uploaded} onDirty={dirty} onBusy={noop} />);
  fireEvent.change(screen.getByLabelText('CSV / XLSX 文件'), { target: { files: [file] } });
  fireEvent.change(screen.getByLabelText('数据源名称（选填）'), { target: { value: '我的设备' } });
  fireEvent.change(screen.getByLabelText('CSV 分隔符'), { target: { value: ';' } });
  fireEvent.submit(screen.getByLabelText('CSV / XLSX 文件').closest('form')!);
  await waitFor(() => expect(uploaded).toHaveBeenCalledWith(source));
  expect(sent?.get('file')).toBe(file);
  expect(sent?.get('project_id')).toBe('p'); expect(sent?.get('delimiter')).toBe(';');
  expect(sent?.get('name')).toBe('我的设备');
  expect((screen.getByLabelText('数据源名称（选填）') as HTMLInputElement).value).toBe('');
  expect((screen.getByRole('button', { name: '上传并查看数据' }) as HTMLButtonElement).disabled).toBe(true);
});

it('preserves upload inputs when the server rejects malformed data', async () => {
  const uploaded = vi.fn();
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ error: { message: '表头重复，请区分列名' } }), { status: 422 }));
  render(<UploadTables projectId="p" disabled={false} onUploaded={uploaded} onDirty={noop} onBusy={noop} />);
  fireEvent.change(screen.getByLabelText('CSV / XLSX 文件'), { target: { files: [new File(['a,a\n1,2'], 'bad.csv')] } });
  fireEvent.change(screen.getByLabelText('数据源名称（选填）'), { target: { value: '待修正' } });
  fireEvent.submit(screen.getByLabelText('CSV / XLSX 文件').closest('form')!);
  await screen.findByText('表头重复，请区分列名');
  expect(uploaded).not.toHaveBeenCalled();
  expect((screen.getByLabelText('数据源名称（选填）') as HTMLInputElement).value).toBe('待修正');
  expect((screen.getByRole('button', { name: '上传并查看数据' }) as HTMLButtonElement).disabled).toBe(false);
});

it('opens an editable model proposal without implicitly creating a draft, approving or importing', async () => {
  const calls: { path: string; body: unknown; method: string }[] = [];
  const definition = { types: [{ key: 'Item', label: '工作表一', properties: [{ key: 'name', label: '名称', data_type: 'string', source_owned: true }], relations: [] }], mappings: [{ key: 'from_table', source_id: source.id, resource_id: 'sheet_1', type_key: 'Item', schema_fingerprint: 'a'.repeat(64), fields: { name: { source_field: 'c1', transform: 'identity' } } }] };
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, init) => {
    const path = String(url); calls.push({ path, body: init?.body, method: init?.method ?? 'GET' });
    return new Response(JSON.stringify(path.endsWith('/model-proposal') ? { definition, baseline_release_id: 'release-old', warnings: ['未推断关系'], resources: [{ resource_id: 'sheet_1', label: '工作表一', type_key: 'Item' }] } : { items: [], total: 0 }));
  });
  render(<SourceModelBuilder source={source} projectId="p" can={() => true} disabled={false} onDirty={noop} onBusy={noop} />);
  expect(screen.getByText(/1 个公式单元格/)).toBeTruthy();
  fireEvent.click(screen.getByRole('checkbox'));
  fireEvent.click(screen.getByRole('button', { name: '生成可编辑建模建议' }));
  await screen.findByText('本次新增 1 个类型');
  expect(JSON.parse(calls[0].body as string)).toEqual({ expected_source_revision: 1, resource_ids: ['sheet_1'] });
  fireEvent.click(screen.getByRole('button', { name: '编辑模型与来源映射' }));
  await screen.findByRole('button', { name: '保存草稿' });
  expect(screen.queryByRole('button', { name: '发布已批准修订' })).toBeNull();
  expect(calls.some(call => call.method !== 'GET' && /changesets|snapshots/.test(call.path))).toBe(false);
});
