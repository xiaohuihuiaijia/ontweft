export class ApiError extends Error {
  constructor(public status: number, message: string, public detail?: unknown) { super(message); this.name = 'ApiError'; }
}

export const protectedReadUnavailable = (error: unknown) => error instanceof ApiError && (
  [401, 403, 404].includes(error.status) || (error.status === 503 &&
    (error.detail as { error?: { code?: string } } | undefined)?.error?.code === 'source_authorization_unknown')
);

function errorText(value: unknown): string {
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) return value.map(errorText).join('；');
  if (value && typeof value === 'object') {
    const o = value as Record<string, unknown>;
    if (o.error) return errorText(o.error);
    if (typeof o.message === 'string') return o.message;
    if (typeof o.msg === 'string') return `${Array.isArray(o.loc) ? o.loc.join('.') + '：' : ''}${o.msg}`;
    if (o.detail) return errorText(o.detail);
  }
  return '请求失败，请重试或联系管理员。';
}

/** Same-origin cookie session. Never persist credentials or attach user identity headers. */
export async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  let response: Response;
  const controller = new AbortController();
  let timedOut = false;
  const cancel = () => controller.abort();
  options.signal?.addEventListener('abort', cancel, { once: true });
  if (options.signal?.aborted) controller.abort();
  const timer = window.setTimeout(() => { timedOut = true; controller.abort(); }, options.body instanceof FormData ? 120000 : 60000);
  try {
    response = await fetch(`/api${path}`, { ...options, signal: controller.signal, credentials: 'same-origin', headers: { Accept: 'application/json', ...(options.body && !(options.body instanceof FormData) ? { 'Content-Type': 'application/json' } : {}), ...options.headers } });
  } catch (error) {
    if (!timedOut && error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new ApiError(0, options.method && options.method !== 'GET' ? '网络中断或请求超时，提交结果尚未确认。请刷新列表核对后再操作；本地输入已保留。' : '无法连接服务或请求超时，请检查网络和后端服务。');
  } finally {
    window.clearTimeout(timer);
    options.signal?.removeEventListener('abort', cancel);
  }
  const body: unknown = response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok) {
    if (response.status === 401 && path !== '/auth/login' && path !== '/auth/me') window.dispatchEvent(new Event('session-expired'));
    throw new ApiError(response.status, errorText(body), body);
  }
  return body as T;
}

export const write = <T>(path: string, body: unknown, method = 'POST') => request<T>(path, { method, body: JSON.stringify(body) });
export function scoped(path: string, projectId: string, extra: Record<string, string | number | undefined> = {}): string {
  const params = new URLSearchParams({ project_id: projectId });
  for (const [key, value] of Object.entries(extra)) if (value !== undefined && value !== '') params.set(key, String(value));
  return `${path}?${params.toString()}`;
}
export const describeError = (error: unknown) => error instanceof Error ? error.message : '操作未完成，请重试。';
