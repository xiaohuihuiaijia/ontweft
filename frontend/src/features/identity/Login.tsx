import { useState } from 'react';
import { request, write } from '../../shared/api/api';
import { ErrorNotice, Field } from '../../shared/ui/components';
import { Brand, BRAND } from '../../shared/brand/Brand';
import type { Session } from '../../shared/model/types';
import { OntologyIllustration } from './OntologyIllustration';

export function Login({ onLogin }: { onLogin: (session: Session) => void }) {
  const [username, setUsername] = useState(''); const [password, setPassword] = useState(''); const [busy, setBusy] = useState(false); const [error, setError] = useState<unknown>();
  return <><a className="skip-link" href="#login-form">跳转到登录表单</a><main className="login-page">
    <header className="login-header"><Brand /><span>{BRAND.descriptor}</span></header>
    <div className="login-layout">
    <div className="login-intro">
      <div className="login-story"><h1>{BRAND.tagline}</h1><p>从可信来源到业务对象，<br />在统一工作台中构建、协作与行动。</p></div>
      <OntologyIllustration />
      <div className="intro-lines"><span>来源可追溯</span><span>定义可审核</span><span>操作可核对</span></div>
    </div>
    <section className="login-card" id="login-form" tabIndex={-1} aria-labelledby="login-heading">
      <h2 id="login-heading">登录工作台</h2><p className="muted">使用管理员分配的用户名和密码登录。</p>
      <form onSubmit={async e => { e.preventDefault(); setBusy(true); setError(undefined); try { await write('/auth/login', { username: username.trim(), password }); const session = await request<Session>('/auth/me'); setPassword(''); onLogin(session); } catch (err) { setError(err); } finally { setBusy(false); } }}>
        <ErrorNotice error={error} />
        <Field label="用户名" hint="也可使用原有邮箱登录"><input name="username" autoComplete="username" autoCapitalize="none" spellCheck={false} maxLength={200} required value={username} onChange={e => setUsername(e.target.value)} /></Field>
        <Field label="密码"><input name="password" type="password" autoComplete="current-password" required value={password} onChange={e => setPassword(e.target.value)} /></Field>
        <button className="full-width" disabled={busy}>{busy ? '正在登录…' : '登录'}</button>
      </form>
      <p className="login-footnote">账号与权限由服务端验证。浏览器不保存密码。</p>
    </section>
    </div>
    <footer className="login-product-note">{BRAND.name} {BRAND.englishName} / {BRAND.descriptor}</footer>
  </main></>;
}
