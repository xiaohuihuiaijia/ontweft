import { fireEvent, render, screen } from '@testing-library/react';
import { expect, it } from 'vitest';
import { DocumentPreview } from './DocumentPreview';

it('shows native block evidence and references without requesting or activating their resources', () => {
  const ir = { format: 'feishu_docx', parser: 'basic/feishu_docx', parser_version: '1.0', complete: false, warnings: ['附件未读取'], pages: [], elements: [
    { element_id: 'image', kind: 'image', text: '', evidence: { element_path: 'feishu/documents/remote/revisions/7/blocks/image' }, metadata: { block_id: 'image', resource_kind: 'image', resource_status: 'not_fetched', resource_reference: { url: 'https://private.invalid/image', token: 'resource-token' } } },
    { element_id: 'unknown', kind: 'paragraph', text: '', evidence: {}, metadata: { block_type: 999, unsupported_block: true } },
    { element_id: 'badtable', kind: 'table', text: '', evidence: {}, metadata: { layout_unresolved: true } },
  ] };
  const { container } = render(<DocumentPreview value={ir} />);
  expect(screen.getByText(/飞书原生块快照/)).not.toBeNull();
  expect(screen.queryByText(/逐页处理状态/)).toBeNull();
  expect(screen.getByText(/关联资源（image）/)).not.toBeNull();
  expect(screen.getByText(/尚未解释的块类型 999/)).not.toBeNull();
  expect(screen.getByText(/表格布局待核对/)).not.toBeNull();
  expect(container.querySelector('img, iframe, a')).toBeNull();
  fireEvent.click(screen.getAllByRole('button', { name: '来源定位' })[0]);
  expect(screen.getByText('块与关联资源')).not.toBeNull();
  expect(screen.getByText(/feishu\/documents\/remote\/revisions\/7\/blocks\/image/)).not.toBeNull();
  expect(container.querySelector('img, iframe, a')).toBeNull();
});

it('renders merged native cells once at their recorded row and column', () => {
  const ir = { format: 'feishu_docx', parser: 'basic/feishu_docx', parser_version: '1.0', complete: true, warnings: [], pages: [], elements: [
    { element_id: 'table', kind: 'table', text: '', evidence: {}, cells: [
      { row: 0, column: 0, row_span: 2, column_span: 1, text: '阀门' },
      { row: 0, column: 1, row_span: 1, column_span: 1, text: '季度检查' },
      { row: 1, column: 1, row_span: 1, column_span: 1, text: '年度更换' },
    ] },
  ] };
  render(<DocumentPreview value={ir} />);
  const cells = screen.getAllByRole('cell');
  expect(cells).toHaveLength(3);
  expect(cells[0].getAttribute('rowspan')).toBe('2');
  expect(cells[2].textContent).toBe('年度更换');
});

it('keeps rows fully covered by a merged cell in the table layout', () => {
  render(<DocumentPreview value={{ format: 'feishu_docx', parser: 'basic/feishu_docx', complete: true, warnings: [], pages: [], elements: [
    { element_id: 'table', kind: 'table', rows: 2, text: '', evidence: {}, cells: [{ row: 0, column: 0, row_span: 2, column_span: 1, text: '跨两行' }] },
  ] }} />);
  expect(screen.getAllByRole('row')).toHaveLength(2);
  expect(screen.getAllByRole('cell')).toHaveLength(1);
});
