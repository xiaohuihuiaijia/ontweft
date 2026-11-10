import { Brand, BRAND } from '../shared/brand/Brand';
import { NavigationIcon } from '../shared/ui/NavigationIcon';

export const navigation = [
  ['sources', '数据源'], ['ontology', '本体模型'], ['objects', '业务对象'], ['graph', '图谱探索'],
  ['documents', '文档资料'], ['knowledge', '知识页面'], ['content-search', '内容检索'],
  ['jobs', '任务中心'], ['functions', '可信函数'], ['actions', '动作与审批'], ['audit', '审计记录'], ['settings', '设置'],
] as const;
export type View = typeof navigation[number][0];

const groups: { label: string; views: View[] }[] = [
  { label: '数据与知识', views: ['sources', 'documents', 'knowledge', 'content-search'] },
  { label: '本体与业务', views: ['ontology', 'objects', 'graph', 'functions', 'actions'] },
  { label: '管理', views: ['jobs', 'audit', 'settings'] },
];

export function WorkspaceNavigation({ view, onNavigate, tenantName, mobile = false }: {
  view: View; onNavigate: (view: View) => void; tenantName: string; mobile?: boolean;
}) {
  return <>
    <a className="brand-link" href="#" aria-label={`${BRAND.name} ${BRAND.englishName}，返回本体模型`} onClick={e => { e.preventDefault(); onNavigate('ontology'); }}><Brand /></a>
    <nav aria-label={mobile ? '移动导航' : '主导航'}>
      {groups.map(group => <div className="navigation-group" key={group.label}>
        <p className="sidebar-label">{group.label}</p>
        {group.views.map(id => <button key={id} aria-label={navigation.find(item => item[0] === id)![1]} title={navigation.find(item => item[0] === id)![1]} className={view === id ? 'active' : ''} aria-current={view === id ? 'page' : undefined} onClick={() => onNavigate(id)}>
          <NavigationIcon name={id} /><span className="navigation-label">{navigation.find(item => item[0] === id)![1]}</span>
        </button>)}
      </div>)}
    </nav>
    <div className="sidebar-bottom"><NavigationIcon name="objects" /><div><small>已连接企业空间</small><span>{tenantName}</span></div></div>
  </>;
}
