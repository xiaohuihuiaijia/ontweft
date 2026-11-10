import { act, renderHook, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { useResource } from './hooks';

it('discards late responses from a previous project even when transport ignores abort', async () => {
  let resolveOld!: (response: Response) => void;
  vi.spyOn(globalThis, 'fetch').mockImplementation(url => String(url).includes('old') ? new Promise(resolve => { resolveOld = resolve; }) : Promise.resolve(new Response(JSON.stringify({ name: 'new project' }))));
  const { result, rerender } = renderHook(({ path }) => useResource<{ name: string }>(path), { initialProps: { path: '/objects?project_id=old' } });
  rerender({ path: '/objects?project_id=new' });
  await waitFor(() => expect(result.current.data?.name).toBe('new project'));
  await act(async () => { resolveOld(new Response(JSON.stringify({ name: 'old private result' }))); });
  expect(result.current.data?.name).toBe('new project');
});
