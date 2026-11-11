import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { DocumentWorkbench } from './DocumentWorkbench';

vi.mock('../intelligence/AIProposalPanel', () => ({ AIProposalPanel: ({ onEvidence }: { onEvidence: (value: unknown) => void }) => <button onClick={() => onEvidence({ document_id: 'd1', page: 27, quote: '已授权的原文引文' })}>查看候选证据</button> }));
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });

it('pages original material and clears source text and images when access is revoked', async () => {
  let revoked = false;
  const paths: string[] = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
    const path = String(input); paths.push(path);
    if (path.includes('/documents?')) return json({ items: [{ id: 'd1', filename: '合成多页资料.pdf', state: 'ready' }], total: 1 });
    if (path.includes('/pages?')) {
      if (revoked) return json({ error: { message: '文档权限已撤销' } }, 403);
      const start = Number(new URL(path, 'http://test').searchParams.get('offset')) + 1;
      return json({ items: Array.from({ length: Math.min(25, 31 - start) }, (_, i) => ({ page_number: start + i, label: `页面 ${start + i}`, text: `原文 ${start + i}`, elements: [], status: 'parsed', has_image: true, image_count: 2 })), total: 30, revision: 1, content_hash: 'abc' });
    }
    return json({});
  });
  render(<DocumentWorkbench projectId="p" can={() => false} onDirty={() => {}} />);
  await screen.findByRole('button', { name: '查看第 1 页' });
  fireEvent.click(screen.getByRole('button', { name: '下一页' }));
  await screen.findByRole('button', { name: '查看第 26 页' });
  await waitFor(() => expect(screen.getAllByRole('img')[1].getAttribute('src')).toContain('/pages/26/image?'));
  expect(screen.getAllByRole('img')[1].getAttribute('src')).toContain('image_index=1');
  fireEvent.click(screen.getByRole('button', { name: '查看候选证据' }));
  await screen.findByText('已授权的原文引文');
  expect(screen.getByRole('button', { name: '查看第 27 页' }).getAttribute('aria-pressed')).toBe('true');
  revoked = true;
  fireEvent.click(screen.getByRole('button', { name: '刷新文档' }));
  await screen.findByText('文档权限已撤销');
  await waitFor(() => expect(screen.queryByText('已授权的原文引文')).toBeNull());
  expect(screen.queryAllByRole('img')).toHaveLength(0);
  expect(paths.some(path => path.endsWith('offset=25'))).toBe(true);
});
