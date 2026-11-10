import { describe, expect, it, vi } from 'vitest';
import { ApiError, request, scoped, write } from './api';

describe('API transport', () => {
  it('uses same-origin cookies, project encoding, and no asserted identity header', async () => {
    const fetcher = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ items: [], total: 0 }), { status: 200 }));
    await request(scoped('/objects', 'project & one', { q: '项目/名称', offset: 25 }));
    expect(fetcher.mock.calls[0][0]).toBe('/api/objects?project_id=project+%26+one&q=%E9%A1%B9%E7%9B%AE%2F%E5%90%8D%E7%A7%B0&offset=25');
    expect(fetcher.mock.calls[0][1]?.credentials).toBe('same-origin');
    expect(fetcher.mock.calls[0][1]?.headers).not.toHaveProperty('X-User-Id');
  });
  it('preserves conflict status and the real server message; does not retry a write', async () => {
    const fetcher = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ error: { code: 'revision_conflict', message: '基线已改变', details: { revision: 9 } } }), { status: 409 }));
    await expect(write('/objects/o1', { expected_revision: 8, properties: { name: '修改' } }, 'PATCH')).rejects.toMatchObject({ status: 409, message: '基线已改变' });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
  it('marks a network failure during mutation as an unknown result', async () => {
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new TypeError('offline'));
    await expect(write('/actions', {})).rejects.toThrow('提交结果尚未确认');
  });
  it('ends a stuck mutation as unknown without retrying or claiming failure to persist', async () => {
    vi.useFakeTimers();
    try {
      const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation((_url, options) => new Promise((_resolve, reject) => options?.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))));
      const assertion = expect(write('/knowledge/k1/publish', { expected_revision: 1 })).rejects.toMatchObject({ status: 0, message: expect.stringContaining('提交结果尚未确认') });
      await vi.advanceTimersByTimeAsync(60000);
      await assertion;
      expect(fetcher).toHaveBeenCalledTimes(1);
    } finally { vi.useRealTimers(); }
  });
  it('handles field validation errors', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ detail: [{ loc: ['body', 'title'], msg: 'Field required' }] }), { status: 422 }));
    await expect(request('/changesets')).rejects.toMatchObject({ status: 422, message: 'body.title：Field required' });
  });
  it('notifies expired session without swallowing the failed operation', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('{}', { status: 401 }));
    const listener = vi.fn(); window.addEventListener('session-expired', listener);
    await expect(request('/objects')).rejects.toBeInstanceOf(ApiError);
    expect(listener).toHaveBeenCalledOnce(); window.removeEventListener('session-expired', listener);
  });
});
