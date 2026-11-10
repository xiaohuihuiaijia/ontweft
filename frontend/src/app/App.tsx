import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react';
import { request, write } from '../shared/api/api';
import { Empty, ErrorNotice, Loading } from '../shared/ui/components';
import { useUnsaved } from '../shared/hooks/hooks';
import { Login } from '../features/identity/Login';
const SettingsPage = lazy(() => import('../features/identity/SettingsPage').then(m => ({ default: m.SettingsPage })));
const ActionsPage = lazy(() => import('../features/actions/ActionsPage').then(m => ({ default: m.ActionsPage })));
const FunctionsPage = lazy(() => import('../features/functions/FunctionsPage').then(m => ({ default: m.FunctionsPage })));
const OntologyPage = lazy(() => import('../features/modeling/OntologyPage').then(m => ({ default: m.OntologyPage })));
const ObjectsPage = lazy(() => import('../features/objects/ObjectsPage').then(m => ({ default: m.ObjectsPage })));
const GraphPage = lazy(() => import('../features/graph/GraphPage').then(m => ({ default: m.GraphPage })));
const ResourcePage = lazy(() => import('../features/activity/ResourcePage').then(m => ({ default: m.ResourcePage })));
const SourcesPage = lazy(() => import('../features/sources/SourcesPage').then(m => ({ default: m.SourcesPage })));
const KnowledgePages = lazy(() => import('../features/knowledge/KnowledgePages').then(m => ({ default: m.KnowledgePages })));
const DocumentsPage = lazy(() => import('../features/documents/DocumentsPage').then(m => ({ default: m.DocumentsPage })));
const ContentSearchPage = lazy(() => import('../features/search/ContentSearchPage').then(m => ({ default: m.ContentSearchPage })));
import type { Session } from '../shared/model/types';
import { Brand, BRAND } from '../shared/brand/Brand';
import { navigation, sectionViews, WorkspaceNavigation, type View } from './Navigation';


export default function App() {
  const mobileNavigation = useRef<HTMLDialogElement>(null);
  const [navigationOpen, setNavigationOpen] = useState(false);
  const [session, setSession] = useState<Session>(); const [loading, setLoading] = useState(true); const [error, setError] = useState<unknown>(); const [projectId, setProjectId] = useState('');
  const [view, setView] = useState<View>(window.location.hash === '#sources' ? 'sources' : 'ontology'); const [dirty, setDirty] = useState(false); const [expired, setExpired] = useState(false);
  const acceptSession = useCallback((value: Session) => { setSession(value); setError(undefined); setProjectId(value.projects[0]?.id ?? ''); setExpired(false); setDirty(false); }, []);
  useUnsaved(dirty);
  useEffect(() => {
    const controller = new AbortController();
    request<Session>('/auth/me', { signal: controller.signal }).then(acceptSession).catch(e => { if (!controller.signal.aborted && e.status !== 401) setError(e); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    const handler = () => setExpired(true);
    window.addEventListener('session-expired', handler);
    return () => { controller.abort(); window.removeEventListener('session-expired', handler); };
  }, [acceptSession]);
  const navigate = (action: () => void) => { if (!dirty || window.confirm('当前有未保存修改，确定离开并放弃修改？')) { setDirty(false); action(); } };
  const projectPermissions = session?.projects.find(p => p.id === projectId)?.permissions ?? [];
  const can = (permission: string) => projectPermissions.includes('*') || projectPermissions.includes(permission);
  if (loading) return <main className="boot brand-boot"><Brand /><Loading /></main>;
  if (!session) return <>{error ? <div className="boot-error"><ErrorNotice error={error} /></div> : null}<Login onLogin={acceptSession} /></>;
  const features = session.features ?? [];
  const knowledgeEnabled = features.includes('knowledge');
  const automationEnabled = features.includes('automation');
  const section = sectionViews(view, knowledgeEnabled, automationEnabled && !!session.actions_available);
  const switchView = (next: View) => navigate(() => { setView(next); if (mobileNavigation.current?.open) mobileNavigation.current.close(); });
  return <><a className="skip-link" href="#main-content">跳转到主内容</a><div className="app-shell"><aside className="sidebar"><WorkspaceNavigation view={view} onNavigate={switchView} tenantName={session.tenant.name} features={features} /></aside>
    <dialog ref={mobileNavigation} id="workspace-navigation" className="mobile-navigation" aria-label="工作空间导航" onClose={() => setNavigationOpen(false)} onClick={e => { if (e.target === e.currentTarget) mobileNavigation.current?.close(); }}>
      <div className="mobile-navigation-panel"><button type="button" className="icon-button mobile-navigation-close" aria-label="关闭导航" onClick={() => mobileNavigation.current?.close()}>×</button><WorkspaceNavigation view={view} onNavigate={switchView} tenantName={session.tenant.name} features={features} mobile /></div>
    </dialog>
    <div className="main-shell"><header className="topbar"><div className="workspace-context"><button type="button" className="mobile-menu-button secondary" aria-label="打开导航" aria-expanded={navigationOpen} aria-controls="workspace-navigation" onClick={() => { mobileNavigation.current?.showModal(); setNavigationOpen(true); }}><svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h16" /></svg></button><div className="project-switcher"><span>项目</span><select aria-label="当前项目" value={projectId} onChange={e => navigate(() => setProjectId(e.target.value))}>{session.projects.map(project => <option key={project.id} value={project.id}>{project.name}</option>)}</select></div><span className="workspace-divider" aria-hidden="true">/</span><span className="workspace-location">{navigation.find(item => item[0] === view)?.[1]}</span></div><div className="user-menu"><span className="avatar">{session.user.display_name?.slice(0, 1) || session.user.email.slice(0, 1)}</span><div>{session.user.display_name}<small>{session.user.email}</small></div><button className="text-button" onClick={() => navigate(async () => { try { await write('/auth/logout', {}); setSession(undefined); setProjectId(''); } catch (e) { setError(e); } })}>退出</button></div></header>
      <main className="content" id="main-content" tabIndex={-1} key={`${projectId}:${view}`}><ErrorNotice error={error} />{expired && <div className="notice warning" role="alert"><strong>登录已失效</strong><p>当前草稿保留在页面中，请复制尚未提交的内容后重新登录。</p><button onClick={() => navigate(() => { setSession(undefined); setExpired(false); })}>返回登录</button></div>}
      {section.length > 1 && <nav className="tabs" aria-label="当前模块">{section.map(([id, label]) => <button key={id} className={view === id ? 'active' : ''} aria-current={view === id ? 'page' : undefined} onClick={() => switchView(id)}>{label}</button>)}</nav>}
      <Suspense fallback={<Loading />}>{!projectId && view !== 'settings' ? <Empty title="尚未分配项目">请联系企业管理员分配项目权限。</Empty> : view === 'ontology' ? <OntologyPage projectId={projectId} can={can} onDirty={setDirty} /> : view === 'objects' ? <ObjectsPage role={session.projects.find(p => p.id === projectId)?.role} projectId={projectId} canWrite={can('objects:write')} canReadContributions={can('sources:write')} onDirty={setDirty} /> : view === 'graph' ? <GraphPage userId={session.user.id} knowledgeEnabled={knowledgeEnabled} role={session.projects.find(p => p.id === projectId)?.role} projectId={projectId} can={can} onDirty={setDirty} /> : view === 'sources' ? <SourcesPage integrationsEnabled={features.includes('integrations')} can={can} isAdmin={session.user.admin} projectId={projectId} canWrite={can('sources:write')} canConfigureProjection={can('sources:write') && can('ontology:write')} onDirty={setDirty} /> : view === 'documents' && knowledgeEnabled ? <DocumentsPage projectId={projectId} canWrite={can('documents:write')} /> : view === 'knowledge' && knowledgeEnabled ? <KnowledgePages projectId={projectId} canWrite={can('knowledge:write')} canPublish={can('knowledge:publish')} onDirty={setDirty} /> : view === 'content-search' && knowledgeEnabled ? <ContentSearchPage projectId={projectId} canManage={can('sources:write')} /> : view === 'functions' && automationEnabled ? <FunctionsPage projectId={projectId} userId={session.user.id} can={can} onDirty={setDirty} /> : view === 'actions' && automationEnabled && session.actions_available ? <ActionsPage projectId={projectId} userId={session.user.id} can={can} onDirty={setDirty} /> : view === 'settings' ? <SettingsPage session={session} projectId={projectId} onDirty={setDirty} onSessionRefresh={async () => { const refreshed = await request<Session>('/auth/me'); setSession(refreshed); if (!projectId) setProjectId(refreshed.projects[0]?.id ?? ''); }} /> : <ResourcePage userId={session.user.id} canManageJobs={can('jobs:manage')} key={view} resource={view === 'audit' ? 'audit' : 'jobs'} projectId={projectId} />}
      </Suspense></main><footer className="app-footer">数据以服务端当前授权与已发布版本为准。<span>{dirty ? '● 有未保存修改' : `${BRAND.name} ${BRAND.englishName} · 私有部署`}</span></footer></div></div></>;
}
