import { Brand, BRAND } from '../shared/brand/Brand';
import { NavigationIcon } from '../shared/ui/NavigationIcon';

export const navigation = [
  ['sources', '数据源'], ['ontology', '本体模型'], ['objects', '业务对象'], ['graph', '图谱探索'],
  ['documents', '文档资料'], ['knowledge', '知识页面'], ['content-search', '内容检索'],
  ['jobs', '任务中心'], ['functions', '可信函数'], ['actions', '动作与审批'], ['audit', '审计记录'], ['settings', '设置'],
] as const;
export type View = typeof navigation[number][0];

export function sectionViews(view: View, knowledge: boolean, actions: boolean): (typeof navigation[number])[] {
  const ids: View[] = ['settings', 'jobs', 'audit'].includes(view) ? ['settings', 'jobs', 'audit']
    : knowledge && ['documents', 'knowledge', 'content-search'].includes(view) ? ['documents', 'knowledge', 'content-search']
      : ['functions', 'actions'].includes(view) ? ['functions', ...(actions ? ['actions' as const] : [])] : [];
  return ids.map(id => navigation.find(item => item[0] === id)!);
}

export function WorkspaceNavigation({ view, onNavigate, tenantName, features = [], mobile = false }: {
  view: View; onNavigate: (view: View) => void; tenantName: string; features?: string[]; mobile?: boolean;
}) {
  const primary: [View, string][] = [['sources', '数据'], ['ontology', '本体'], ['objects', '对象'], ['graph', '图谱'], ['settings', '设置']];
  const extensions: [View, string][] = [];
  if (features.includes('knowledge')) extensions.push(['documents', '知识资料']);
  if (features.includes('automation')) extensions.push(['functions', '自动化']);
  const current = ['jobs', 'audit'].includes(view) ? 'settings' : ['knowledge', 'content-search'].includes(view) ? 'documents' : view === 'actions' ? 'functions' : view;
  const links = (items: [View, string][]) => items.map(([id, label]) => <button key={id} aria-label={navigation.find(item => item[0] === id)![1]} title={label} className={current === id ? 'active' : ''} aria-current={current === id ? 'page' : undefined} onClick={() => onNavigate(id)}><NavigationIcon name={id} /><span className="navigation-label">{label}</span></button>);
  return <>
    <a className="brand-link" href="#" aria-label={`${BRAND.name} ${BRAND.englishName}，返回本体模型`} onClick={e => { e.preventDefault(); onNavigate('ontology'); }}><Brand /></a>
    <nav aria-label={mobile ? '移动导航' : '主导航'}>
      <div className="navigation-group"><p className="sidebar-label">工作空间</p>{links(primary)}</div>
      {extensions.length > 0 && <div className="navigation-group"><p className="sidebar-label">已启用扩展</p>{links(extensions)}</div>}
    </nav>
    <div className="sidebar-bottom"><NavigationIcon name="objects" /><div><small>已连接企业空间</small><span>{tenantName}</span></div></div>
  </>;
}
