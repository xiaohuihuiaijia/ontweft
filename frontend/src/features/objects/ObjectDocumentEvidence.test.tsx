import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { ObjectDocumentEvidence } from './ObjectDocumentEvidence';
import { ObjectRelations } from './ObjectRelations';

const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
const ref = { document_id: 'doc', page: 27, quote: '合成规格 12 V', parse_id: 'ai-page-id' };
const page = { page_number: 27, label: '规格页', text: '合成原件第二十七页', elements: [], status: 'parsed', has_image: true, image_count: 2 };

it('reads source records only on demand with permission and limits evidence to currently visible contributing fields', async () => {
  let denied = false;
  const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
    if (String(input) === '/api/source-records/record') return json({ values: { __ontweft_field_evidence: { voltage: [ref], secret: [{ ...ref, document_id: 'hidden', page: 99, quote: '隐藏字段' }] } } });
    return denied ? json({ error: { message: '文档权限已撤销' } }, 403) : json({ items: [page], total: 40, revision: 3, content_hash: 'hash' });
  });
  const props = { evidence: [{ source_record_id: 'record', fields: ['voltage', 'secret'] }], visibleFields: ['voltage'] };
  const view = render(<ObjectDocumentEvidence {...props} canReadContributions={false} />);
  expect(fetch).not.toHaveBeenCalled();
  view.rerender(<ObjectDocumentEvidence {...props} canReadContributions />);
  expect(fetch).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '查看字段原文（voltage）' }));
  fireEvent.click(await screen.findByRole('button', { name: '查看原文 · 第 27 页' }));
  await screen.findByText('合成原件第二十七页');
  expect(screen.queryByText('隐藏字段')).toBeNull();
  expect(screen.queryByRole('button', { name: /99 页/ })).toBeNull();
  expect(fetch.mock.calls.map(([path]) => String(path))).toEqual(['/api/source-records/record', '/api/documents/doc/pages?limit=1&offset=26']);
  expect(screen.getAllByRole('img')).toHaveLength(2);
  expect(screen.getAllByRole('img')[1].getAttribute('src')).toBe('/api/documents/doc/pages/27/image?expected_revision=3&expected_hash=hash&image_index=1');
  denied = true; fireEvent.click(screen.getByRole('button', { name: '重新核对页面权限' }));
  await screen.findByText('文档权限已撤销');
  expect(screen.queryByText(ref.quote)).toBeNull(); expect(screen.queryAllByRole('img')).toHaveLength(0);
  view.rerender(<ObjectDocumentEvidence {...props} canReadContributions={false} />);
  expect(screen.queryByRole('button')).toBeNull();
});

it('opens directly authorized relation evidence at its original page without reading source records', async () => {
  const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
    if (String(input).includes('/relations?')) return json({ items: [{ id: 'r', source_id: 'o', target_id: 't', type_key: 'fits', evidence: [ref] }], total: 1 });
    if (String(input).includes('/documents/')) return json({ items: [page], total: 40, revision: 3, content_hash: 'hash' });
    return json({ items: [], total: 0 });
  });
  render(<ObjectRelations object={{ id: 'o', project_id: 'p', type_key: 'part', properties: {}, revision: 1 }} type={{ key: 'part', label: '部件', properties: [], relations: [] }} modelReleaseId="release" canWrite={false} onChanged={() => {}} initialRelationKey="fits" />);
  fireEvent.click(await screen.findByRole('button', { name: '查看原文 · 第 27 页' }));
  await screen.findByText('合成原件第二十七页');
  await waitFor(() => expect(fetch.mock.calls.some(([path]) => String(path).includes('offset=26'))).toBe(true));
  expect(fetch.mock.calls.some(([path]) => String(path).includes('/source-records/') || String(path).includes('/parses/ai-page-id'))).toBe(false);
});
