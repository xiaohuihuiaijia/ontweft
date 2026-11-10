import { useCallback, useEffect, useRef, useState } from 'react';
import { request, scoped, write } from '../../../shared/api/api';
import { Empty, ErrorNotice, Field, Loading } from '../../../shared/ui/components';

type Account = { status: string; display_name: string; revision: number };
type Application = { id: string; name: string; service: string; app_id: string; external_tenant_key: string; revision: number; enabled: boolean; account: Account | null };
type Applications = { items: Application[]; redirect_uri: string; required_scopes: string[] };
type Item = { space_id?: string; name?: string; description?: string; node_token?: string; title?: string; obj_type?: string; has_child?: boolean };
type Directory = { items: Item[]; has_more: boolean; next_cursor: string | null; account_revision: number };
type Location = { space?: string; parent?: string; label: string };
type Selection = { space_id: string; node_token: string };
type SelectionResult = { items: { node_token: string; title?: string; status: string; revision_id?: number; permissions?: { view: boolean; copy?: boolean }; error_code?: string }[]; eligible_unique_resources: number; selected_nodes: number; account_revision: number };
const selectionLabels: Record<string, string> = { eligible: '阅读与复制权限通过', denied: '缺少所需权限', unknown: '暂时无法确认', location_changed: '节点位置已改变', format_pending: '此格式的内容导入尚未接通' };
const base = '/integrations/feishu/applications';
const labels: Record<string, string> = { pending: '等待授权', active: '已连接', refreshing: '正在刷新', reauthorize: '需要重新授权', revoked: '已断开' };

export function FeishuBrowser({ projectId, isAdmin, canSelect = true, onDirty }: { projectId: string; isAdmin: boolean; canSelect?: boolean; onDirty: (dirty: boolean) => void }) {
  const [applications, setApplications] = useState<Applications>();
  const [selected, setSelected] = useState('');
  const [directory, setDirectory] = useState<Directory>();
  const [trail, setTrail] = useState<Location[]>([{ label: '知识空间' }]);
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState(false);
  const [editing, setEditing] = useState<Application>();
  const [name, setName] = useState(''); const [service, setService] = useState('feishu');
  const [appId, setAppId] = useState(''); const [tenantKey, setTenantKey] = useState('');
  const [secret, setSecret] = useState(''); const [enabled, setEnabled] = useState(true);
  const [chosen, setChosen] = useState<Selection[]>([]); const [chosenRevision, setChosenRevision] = useState<number>();
  const [selectionResult, setSelectionResult] = useState<SelectionResult>();
  const epoch = useRef(0); const pending = useRef<AbortController | undefined>(undefined);
  const application = applications?.items.find(a => a.id === selected);
  const location = trail[trail.length - 1];
  const reset = useCallback(() => { epoch.current++; pending.current?.abort(); setDirectory(undefined); setBusy(false); setChosen([]); setChosenRevision(undefined); setSelectionResult(undefined); setTrail([{ label: '知识空间' }]); }, []);
  const reload = useCallback(async () => {
    reset(); const current = epoch.current; const controller = new AbortController(); pending.current = controller;
    setBusy(true); setError(undefined);
    try { const result = await request<Applications>(scoped(base, projectId), { signal: controller.signal });
      if (!Array.isArray(result?.items) || typeof result.redirect_uri !== 'string' || !Array.isArray(result.required_scopes)) throw new Error('飞书应用配置响应不完整，请刷新重试。');
      new URL(result.redirect_uri);
      if (current === epoch.current) { setApplications(result); setSelected(value => result.items.some(a => a.id === value) ? value : result.items[0]?.id ?? ''); } }
    catch (e) { if (current === epoch.current && !controller.signal.aborted) { setApplications(undefined); setError(e); } }
    finally { if (current === epoch.current) setBusy(false); }
  }, [projectId, reset]);
  useEffect(() => { void reload(); return () => { epoch.current++; pending.current?.abort(); }; }, [reload]);
  const load = async (target: Location, cursor?: string | null, append = false) => {
    pending.current?.abort(); const controller = new AbortController(); pending.current = controller;
    const current = ++epoch.current; setBusy(true); setError(undefined);
    if (!append) setDirectory(undefined);
    try {
      const result = await request<Directory>(scoped(`${base}/${encodeURIComponent(selected)}/${target.space ? 'nodes' : 'spaces'}`, projectId, { space_id: target.space, parent_node: target.parent, cursor: cursor ?? undefined }), { signal: controller.signal });
      if (current !== epoch.current) return;
      if (chosen.length && chosenRevision !== result.account_revision) { setChosen([]); setChosenRevision(undefined); setSelectionResult(undefined); }
      if (append && directory && directory.account_revision !== result.account_revision) {
        setDirectory(undefined); setError(new Error('账号授权已改变，请重新加载目录。')); return;
      }
      setDirectory(previous => {
        const items = append && previous ? [...previous.items, ...result.items] : result.items;
        const unique = Array.from(new Map(items.map(item => [item.node_token ?? item.space_id, item])).values());
        return { ...result, items: unique };
      });
    } catch (e) { if (current === epoch.current && !controller.signal.aborted) { setDirectory(undefined); setSelectionResult(undefined); setTrail([{ label: '知识空间' }]); setError(e); } }
    finally { if (current === epoch.current) setBusy(false); }
  };
  const connect = async () => {
    reset(); setBusy(true); setError(undefined); const current = epoch.current;
    try { const result = await write<{ authorization_url: string }>(`${base}/${encodeURIComponent(selected)}/authorize`, { project_id: projectId }); if (current === epoch.current) window.location.assign(result.authorization_url); }
    catch (e) { if (current === epoch.current) setError(e); }
    finally { if (current === epoch.current) setBusy(false); }
  };
  const choose = (item: Item, checked: boolean) => {
    if (!item.node_token || !location.space || !directory) return;
    if (checked && chosen.length >= 20) { setError(new Error('每次最多检查 20 个节点，请分批选择。')); return; }
    setChosenRevision(directory.account_revision); setSelectionResult(undefined); setError(undefined);
    setChosen(previous => checked ? [...previous, { space_id: location.space!, node_token: item.node_token! }] : previous.filter(value => value.node_token !== item.node_token));
  };
  const previewSelection = async () => {
    pending.current?.abort(); const controller = new AbortController(); pending.current = controller;
    const current = ++epoch.current; setBusy(true); setError(undefined); setSelectionResult(undefined);
    try {
      const result = await request<SelectionResult>(`${base}/${encodeURIComponent(selected)}/selection-preview`, { method: 'POST', signal: controller.signal, body: JSON.stringify({ project_id: projectId, expected_account_revision: chosenRevision, selections: chosen }) });
      if (current !== epoch.current) return;
      setSelectionResult(result);
      if (result.items.some(item => item.status !== 'eligible')) { setDirectory(undefined); setTrail([{ label: '知识空间' }]); }
    } catch (e) { if (current === epoch.current && !controller.signal.aborted) { setDirectory(undefined); setTrail([{ label: '知识空间' }]); setError(e); } }
    finally { if (current === epoch.current) setBusy(false); }
  };
  const disconnect = async () => {
    reset(); setBusy(true); setError(undefined); const current = epoch.current;
    try { await write(`${base}/${encodeURIComponent(selected)}/disconnect`, { project_id: projectId }); if (current === epoch.current) await reload(); }
    catch (e) { if (current === epoch.current) setError(e); }
    finally { if (current === epoch.current) setBusy(false); }
  };
  const openForm = (app?: Application) => {
    setEditing(app); setName(app?.name ?? ''); setService(app?.service ?? 'feishu'); setAppId(app?.app_id ?? '');
    setTenantKey(app?.external_tenant_key ?? ''); setEnabled(app?.enabled ?? true); setSecret(''); setForm(true);
  };
  const changed = () => onDirty(true);
  const save = async (event: React.FormEvent) => {
    event.preventDefault(); setBusy(true); setError(undefined); const current = epoch.current;
    try {
      await write(editing ? `${base}/${encodeURIComponent(editing.id)}` : base, { project_id: projectId, name, service, app_id: appId, external_tenant_key: tenantKey, app_secret: secret, enabled, ...(editing ? { expected_revision: editing.revision } : {}) });
      if (current === epoch.current) { setSecret(''); setForm(false); onDirty(false); await reload(); }
    } catch (e) { if (current === epoch.current) setError(e); }
    finally { if (current === epoch.current) setBusy(false); }
  };
  const matchingOrigin = applications && new URL(applications.redirect_uri).origin === window.location.origin;
  return <section className="source-detail" aria-label="飞书知识库"><div className="section-heading"><div><h2>飞书知识库</h2><p className="muted">连接当前员工账号，浏览有权访问的知识空间和文档目录。</p></div>
    <div className="toolbar"><button className="secondary" disabled={busy || form} onClick={() => void reload()}>刷新账号状态</button>{isAdmin && <button disabled={busy || form} onClick={() => openForm()}>配置企业应用</button>}</div></div>
    <ErrorNotice error={error} />
    {form && <form onSubmit={save} className="panel"><h3>{editing ? '更新企业应用' : '配置企业应用'}</h3><p>在飞书开放平台配置以下回调地址和只读权限。保存应用配置后，每位员工分别授权自己的账号。</p>
      <p>回调地址：<code>{applications?.redirect_uri}</code></p><p>所需权限：<code>{applications?.required_scopes.join(' ')}</code></p>
      <div className="form-grid"><Field label="应用名称"><input required maxLength={200} value={name} onChange={e => { setName(e.target.value); changed(); }} /></Field><Field label="服务区域"><select value={service} onChange={e => { setService(e.target.value); changed(); }}><option value="feishu">飞书</option><option value="lark">Lark</option></select></Field>
      <Field label="App ID"><input required value={appId} autoComplete="off" onChange={e => { setAppId(e.target.value); changed(); }} /></Field><Field label="企业 tenant_key"><input required value={tenantKey} autoComplete="off" onChange={e => { setTenantKey(e.target.value); changed(); }} /></Field>
      <Field label="App Secret"><input type="password" required value={secret} autoComplete="new-password" onChange={e => { setSecret(e.target.value); changed(); }} /></Field></div>
      {editing && <><label><input type="checkbox" checked={enabled} onChange={e => { setEnabled(e.target.checked); changed(); }} />启用应用</label><p>保存更新会使此应用下的员工授权失效，需要重新连接。</p></>}
      <div className="toolbar"><button disabled={busy}>保存应用配置</button><button type="button" className="secondary" disabled={busy} onClick={() => { setSecret(''); setForm(false); onDirty(false); }}>取消</button></div></form>}
    {applications && !applications.items.length && <Empty title="尚未配置飞书应用">请企业管理员配置自建应用与企业标识。</Empty>}
    {!!applications?.items.length && <><Field label="企业飞书应用"><select value={selected} disabled={busy || form} onChange={e => { reset(); setSelected(e.target.value); setTrail([{ label: '知识空间' }]); setError(undefined); }}>{applications.items.map(app => <option value={app.id} key={app.id}>{app.name}{!app.enabled ? '（已停用）' : ''}</option>)}</select></Field>
      {application && <><p>当前员工：{application.account?.display_name || '尚未连接'} · {labels[application.account?.status ?? ''] || '未授权'}</p>
        {!matchingOrigin && <p role="alert">请从配置的前端地址 <a href={new URL(applications!.redirect_uri).origin + '/#sources'}>{new URL(applications!.redirect_uri).origin}</a> 打开并登录后连接账号。</p>}
        <div className="toolbar"><button disabled={busy || form || !application.enabled || !matchingOrigin} onClick={() => void connect()}>{application.account ? '重新授权当前账号' : '连接我的飞书账号'}</button>
          {application.account && <button className="secondary" disabled={busy || form} onClick={() => void disconnect()}>断开本平台连接</button>}
          <button className="secondary" disabled={busy || form || !application.enabled || application.account?.status !== 'active'} onClick={() => { setTrail([{ label: '知识空间' }]); void load({ label: '知识空间' }); }}>浏览知识库</button>
          {isAdmin && <button className="text-button" disabled={busy || form} onClick={() => openForm(application)}>修改应用配置</button>}</div>
        <p className="muted">断开会删除本平台保存的员工凭据；飞书侧的应用授权可在飞书中管理。</p></>}
      {trail.length > 1 && <nav aria-label="飞书目录路径" className="toolbar">{trail.map((part, index) => <button className="text-button" disabled={busy || form} key={index} onClick={() => { setTrail(trail.slice(0, index + 1)); void load(part); }}>{part.label}</button>)}</nav>}
      {directory && <><div className="table-wrap"><table><thead><tr><th>选择</th><th>名称</th><th>类型</th><th>目录</th></tr></thead><tbody>{directory.items.map(item => <tr key={item.node_token ?? item.space_id}><td>{canSelect && item.node_token && location.space && <input type="checkbox" aria-label={`选择 ${item.title ?? '未命名'}`} checked={chosen.some(value => value.node_token === item.node_token)} disabled={busy || form} onChange={e => choose(item, e.target.checked)} />}</td><td>{item.title ?? item.name ?? '未命名'}</td><td>{item.obj_type ?? '知识空间'}</td><td>{item.space_id || item.has_child ? <button className="text-button" disabled={busy || form} onClick={() => { const next = { space: item.space_id ?? location.space, parent: item.node_token, label: item.title ?? item.name ?? '未命名' }; setTrail([...trail, next]); void load(next); }}>打开目录</button> : '文档'}</td></tr>)}</tbody></table></div>
        {!directory.items.length && <p>{directory.has_more ? '这一页暂无可见项，可继续加载。' : '当前目录暂无可见项。'}</p>}
        {directory.has_more && <button disabled={busy || form} className="secondary" onClick={() => void load(location, directory.next_cursor, true)}>继续加载</button>}
        <p className="muted">此处为实时目录，尚未导入文档。飞书的“我的文档库”不会出现在此空间列表中。</p></>}
      {chosen.length > 0 && <section className="panel" aria-label="飞书选择检查"><p>已选择 {chosen.length} 个节点，仅检查节点自身，不包含后代。</p><div className="toolbar"><button disabled={busy || form} onClick={() => void previewSelection()}>检查所选文档的导入条件</button><button className="secondary" disabled={busy || form} onClick={() => { setChosen([]); setChosenRevision(undefined); setSelectionResult(undefined); }}>清空选择</button></div><p className="muted">先重新解析真实资源身份并检查阅读、复制权限。检查通过不代表已导入，实际保存与整空间导入仍在接入中。</p></section>}
      {selectionResult && <section className="panel" aria-label="飞书导入条件结果"><h3>检查结果</h3><p>已检查 {selectionResult.selected_nodes} 个节点，{selectionResult.eligible_unique_resources} 份独立 Docx 文档符合当前检查条件；快捷方式按真实资源去重。</p><ul>{selectionResult.items.map((item, index) => <li key={item.node_token}>{item.title ?? `所选节点 ${index + 1}`}：{selectionLabels[item.status] ?? '暂时无法确认'}{item.revision_id !== undefined ? ` · 文档修订 ${item.revision_id}` : ''}</li>)}</ul><p className="muted">权限会变化，实际导入和读取时仍需重新核验。这里没有保存文档。</p></section>}
    </>}
    {busy && <Loading />}
  </section>;
}
