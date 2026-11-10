import { useCallback, useEffect, useRef, useState } from 'react';
import { request } from '../api/api';

export function useResource<T>(path: string | null) {
  const [state, setState] = useState<{ path: string | null; data?: T; error?: unknown; loading: boolean }>({ path, loading: !!path });
  const [epoch, setEpoch] = useState(0);
  const current = useRef<string | null>(path);
  current.current = path;
  useEffect(() => {
    const controller = new AbortController();
    setState({ path, loading: !!path });
    if (path) request<T>(path, { signal: controller.signal }).then(data => {
      if (!controller.signal.aborted && current.current === path) setState({ path, data, loading: false });
    }).catch(error => { if (!controller.signal.aborted && current.current === path) setState({ path, error, loading: false }); });
    return () => controller.abort();
  }, [path, epoch]);
  // Never render a result under the new scope while its effect is still pending.
  const visible: { data?: T; error?: unknown; loading: boolean } = state.path === path ? state : { loading: !!path };
  return { ...visible, reload: useCallback(() => setEpoch(n => n + 1), []) };
}

export function useUnsaved(dirty: boolean) {
  useEffect(() => {
    const handler = (event: BeforeUnloadEvent) => { if (dirty) { event.preventDefault(); event.returnValue = ''; } };
    window.addEventListener('beforeunload', handler);
    return () => window.removeEventListener('beforeunload', handler);
  }, [dirty]);
}
