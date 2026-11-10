import { useEffect, useRef, useState } from 'react';
import { write } from '../../../shared/api/api';
import { ErrorNotice, Loading } from '../../../shared/ui/components';
import { Brand } from '../../../shared/brand/Brand';

export type CallbackInput = { state: string; code?: string; error?: string };

/** Capture once before rendering; no OAuth values in browser storage or return links. */
export function captureFeishuCallback(): CallbackInput | undefined {
  if (window.location.pathname !== '/integrations/feishu/callback') return;
  const params = new URLSearchParams(window.location.search);
  const input = { state: params.get('state') ?? '', ...(params.has('code') ? { code: params.get('code') ?? '' } : {}), ...(params.has('error') ? { error: params.get('error') ?? '' } : {}) };
  window.history.replaceState(null, '', window.location.pathname);
  return input;
}

export function FeishuCallback({ input }: { input: CallbackInput }) {
  const pending = useRef<Promise<unknown> | undefined>(undefined);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<unknown>();
  useEffect(() => {
    let live = true;
    // React StrictMode mounts effects twice. Reuse the in-flight operation;
    // a one-use authorization code must never be exchanged automatically twice.
    pending.current ??= write('/integrations/feishu/callback', input);
    pending.current.then(() => { if (live) setDone(true); }).catch(e => { if (live) setError(e); });
    return () => { live = false; };
  }, [input]);
  return <main className="boot"><section className="panel"><div className="callback-brand"><Brand /></div><h1>连接飞书账号</h1>
    {!done && !error && <Loading />}
    <ErrorNotice error={error} />
    {done ? <p role="status">当前员工账号已连接，可以返回数据源浏览知识库。</p> : error ? <p>请返回数据源重新发起授权。此回调不会自动重试。</p> : <p>正在验证企业与员工身份…</p>}
    <a href="/#sources">返回数据源</a>
  </section></main>;
}
