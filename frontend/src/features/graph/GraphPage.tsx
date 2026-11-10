import { lazy, Suspense, useMemo, useRef, useState } from 'react';
import { request, scoped } from '../../shared/api/api';
import { Empty, ErrorNotice, JsonView, Loading, PageTitle } from '../../shared/ui/components';
import { useResource } from '../../shared/hooks/hooks';
import { GraphCanvas } from './GraphCanvas';
import { useGraphExploration } from './useGraphExploration';
import { boundedGraph, filterGraph, groupColor, groupKey, groupLabel, relationDefinition, type Position } from './graph-model';
import { loadObjectEditSession, ObjectEditor, type ObjectEditSession } from '../objects/ObjectsPage';
import { OntologyEditor, type OntologyFocus } from '../modeling/OntologyPage';
import type { KnowledgePage } from '../knowledge/KnowledgePages';
const KnowledgeEditor = lazy(() => import('../knowledge/KnowledgePages').then(m => ({ default: m.KnowledgeEditor })));
import type { Definition, DefinitionResult, GraphEdge, GraphKind, GraphNode, GraphResult, TypeList } from '../../shared/model/types';

type GraphEditor =
  | { kind: 'object'; session: ObjectEditSession }
  | { kind: 'model'; definition: Definition; baselineReleaseId: string | null; focus: OntologyFocus }
  | { kind: 'knowledge'; value: KnowledgePage };

export function GraphPage({ projectId, can, onDirty, role, knowledgeEnabled = false, userId = '' }: { projectId: string; can: (permission: string) => boolean; onDirty: (dirty: boolean) => void; role?: string; knowledgeEnabled?: boolean; userId?: string }) {
  const [kind, setKind] = useState<GraphKind>('instances');
  const [selection, setSelection] = useState<{ node?: string; edge?: string }>({});
  const [editor, setEditor] = useState<GraphEditor>();
  const [opening, setOpening] = useState(false);
  const [error, setError] = useState<unknown>();
  const [filter, setFilter] = useState('');
  const [hiddenTypes, setHiddenTypes] = useState<string[]>([]);
  const [nodeBudget, setNodeBudget] = useState(200);
  const layoutCache = useRef(new Map<string, Position[]>());
  const graph = useResource<GraphResult>(scoped('/graph', projectId, { kind, limit: nodeBudget }));
  const types = useResource<TypeList>(scoped('/ontology/types', projectId));
  // A later schema release must never silently explain an older graph response.
  const schemaMatches = !!graph.data && graph.data.release_id !== undefined && types.data?.release_id === graph.data.release_id;
  const matchedTypes = useMemo(() => schemaMatches ? types.data?.items ?? [] : [], [schemaMatches, types.data]);
  const explore = useGraphExploration(projectId, userId, graph.data, kind === 'instances', nodeBudget, setError);
  const base = useMemo(() => boundedGraph(explore.data), [explore.data]);
  const visible = useMemo(() => filterGraph(base, kind, matchedTypes, hiddenTypes, filter), [base, kind, matchedTypes, hiddenTypes, filter]);
  const groups = useMemo(() => { const values = new Map<string, { key: string; label: string; count: number }>(); for (const node of base.nodes) { const key = groupKey(node, kind); const existing = values.get(key); if (existing) existing.count++; else values.set(key, { key, label: groupLabel(node, kind, matchedTypes), count: 1 }); } return [...values.values()]; }, [base, kind, matchedTypes]);
  const selected = visible.nodes.find(n => n.id === selection.node);
  const selectedEdge = visible.edges.find(e => e.id === selection.edge);
  const selectedType = matchedTypes.find(t => t.key === selected?.type_key);
  const selectedRelation = selectedEdge && relationDefinition(selectedEdge, base, matchedTypes);
  const cardinality = selectedEdge?.cardinality ?? selectedRelation?.cardinality;
  const samePair = selectedEdge ? visible.edges.filter(e => (e.source === selectedEdge.source && e.target === selectedEdge.target) || (e.source === selectedEdge.target && e.target === selectedEdge.source)) : [];
  const locked = opening || !!editor || explore.busy;
  const nodeLabels = useMemo(() => new Map(base.nodes.map(node => [node.id, node.label])), [base]);
  const nodeName = (id: string) => nodeLabels.get(id) ?? id;
  const resetFilters = () => { setHiddenTypes([]); setFilter(''); setSelection({}); };
  const selectNode = (node: GraphNode) => { if (!locked) setSelection({ node: node.id }); };
  const selectEdge = (edge: GraphEdge) => { if (!locked) setSelection({ edge: edge.id }); };
  const openModel = async (focus: OntologyFocus) => {
    if (locked || !can('ontology:write')) return;
    setOpening(true); setError(undefined);
    try {
      // Always read the complete management definition; the graph and read schema are projections.
      const current = await request<DefinitionResult>(scoped('/ontology/definition', projectId));
      const type = current.definition.types.find(t => t.key === focus.typeKey);
      if (!type || (focus.relationKey && !type.relations.some(r => r.key === focus.relationKey))) throw new Error('图中的定义已变化，请刷新图谱后重新选择。');
      setEditor({ kind: 'model', definition: current.definition, baselineReleaseId: current.release_id ?? null, focus });
    } catch (e) { setError(e); } finally { setOpening(false); }
  };
  const openKnowledge = async (id: string) => {
    if (locked) return;
    setOpening(true); setError(undefined);
    try { setEditor({ kind: 'knowledge', value: await request<KnowledgePage>(`/knowledge/${encodeURIComponent(id)}`) }); }
    catch (e) { setError(e); } finally { setOpening(false); }
  };
  const openObject = async (id: string) => {
    if (locked) return;
    setOpening(true); setError(undefined);
    try {
      setEditor({ kind: 'object', session: await loadObjectEditSession(projectId, id) });
    }
    catch (e) { setError(e); } finally { setOpening(false); }
  };
  return <><PageTitle title="图谱探索" description="模型、业务实例与知识引用分别查看；编辑沿用各自的版本和发布流程。"><button className="secondary" disabled={locked} onClick={() => { graph.reload(); types.reload(); }}>刷新图谱</button></PageTitle>
    <div className="tabs" role="tablist" aria-label="图谱类型">{(['instances', 'model', ...(knowledgeEnabled ? ['knowledge' as const] : [])] as const).map((value, i) => <button disabled={locked} role="tab" aria-selected={kind === value} className={kind === value ? 'active' : ''} key={value} onClick={() => { setKind(value); resetFilters(); setError(undefined); }}>{['业务实例', '本体模型', '文档与知识'][i]}</button>)}</div>
    {explore.controls}
    <ErrorNotice error={error} />{opening && <Loading />}{graph.loading ? <Loading /> : graph.error ? <ErrorNotice error={graph.error} onRetry={graph.reload} /> : !base.nodes.length ? <Empty title="当前视图没有可见节点">图谱只包含服务端返回的已授权数据。</Empty> : <>
      {base.truncated && <div className="notice warning">当前显示有限子图，不代表全部关系。本次节点预算 {nodeBudget}，最多载入 1000 条关系。</div>}
      <div className="graph-budget"><label htmlFor="graph-node-budget">载入节点上限</label><select id="graph-node-budget" aria-label="图谱节点预算" disabled={locked || graph.loading} value={nodeBudget} onChange={event => { setNodeBudget(Number(event.target.value)); setSelection({}); }}><option value={100}>100 个</option><option value={200}>200 个</option><option value={500}>500 个</option></select><span className="muted">按当前权限载入；不代表全库数量。</span></div>
      <div className="graph-filter-panel"><div className="graph-filter-search"><label htmlFor="graph-search">查找当前子图</label><input id="graph-search" disabled={locked} aria-label="在当前子图查找" value={filter} onChange={e => { setFilter(e.target.value); setSelection({}); }} placeholder="节点名称、类型或标识…" /><button disabled={locked || (!filter && !hiddenTypes.length)} className="secondary" onClick={resetFilters}>重置筛选</button></div><div className="graph-legend" aria-label="类型图例与筛选">{groups.map(group => <button type="button" disabled={locked} aria-pressed={!hiddenTypes.includes(group.key)} className="secondary" key={group.key} onClick={() => { setHiddenTypes(current => current.includes(group.key) ? current.filter(key => key !== group.key) : [...current, group.key]); setSelection({}); }}><span className="graph-color-dot" style={{ background: groupColor(group.key) }} />{group.label}<span className="graph-legend-count">{group.count}</span></button>)}</div><p className="muted">图例数量来自已载入的授权子图；筛选同时作用于画布、节点和关系。{graph.data?.version !== undefined && ` 模型版本 v${graph.data?.version}。`}</p></div>
      <div className="graph-workspace"><div className="graph-stage"><GraphCanvas data={base} visible={visible} kind={kind} selection={selection} cache={layoutCache.current} positions={explore.positions.current} pins={explore.pins} onSelect={selectNode} onSelectEdge={selectEdge} onClear={() => { if (!locked) setSelection({}); }} /><div className="graph-caption" role="status">当前显示 {visible.nodes.length} 个节点 · {visible.edges.length} 条关系<br />已载入 {base.nodes.length} 个节点 · {base.edges.length} 条关系 · 仅授权范围{visible.edges.some(edge => edge.source === edge.target) && <><br />同一节点上的关系以环线显示，可逐条选择。</>}</div></div>
        <aside className="graph-inspector">{selected ? <>
          <div className="section-heading"><h2>{selected.label}</h2><button disabled={locked} aria-label="取消选择" className="icon-button" onClick={() => setSelection({})}>×</button></div><span className="graph-type-badge"><span className="graph-color-dot" style={{ background: groupColor(groupKey(selected, kind)) }} />{groupLabel(selected, kind, matchedTypes)}</span><p className="row-id">{selected.id}</p>
          {kind === 'model' ? <><p className="muted">{selectedType?.description || (typeof selected.properties.description === 'string' ? selected.properties.description : '已发布的类型定义')}</p><dl className="graph-properties"><dt>可读属性</dt><dd>{typeof selected.visible_property_count === 'number' ? selected.visible_property_count : selectedType?.properties.length ?? '等待同版本模型'}</dd><dt>可见出向关系</dt><dd>{visible.edges.filter(e => e.source === selected.id).length}</dd></dl>{selectedType?.properties.length ? <><h3>当前可读字段</h3><dl className="graph-properties">{selectedType.properties.map(field => <div key={field.key}><dt>{field.label}</dt><dd>{field.data_type}{field.unit ? ` · ${field.unit}` : ''}{field.required ? ' · 必填' : ''}</dd></div>)}</dl></> : null}</> : kind === 'instances' ? <><h3>当前可读属性</h3><dl className="graph-properties">{Object.entries(selected.properties).map(([key, value]) => <div key={key}><dt>{selectedType?.properties.find(p => p.key === key)?.label ?? key}</dt><dd>{value === null ? '空值' : typeof value === 'boolean' ? value ? '是' : '否' : typeof value === 'object' ? <code>{JSON.stringify(value)}</code> : String(value)}</dd></div>)}</dl></> : <p className="muted">{selected.kind === 'document' ? '文档证据节点；引用指向固定文档修订。' : '已发布的知识页及其引用。'}</p>}
          {kind === 'instances' && <div className="toolbar"><button className="secondary" disabled={locked || (Object.hasOwn(explore.slices, selected.id) && explore.slices[selected.id].next_cursor === null)} onClick={() => void explore.expand(selected.id)}>{explore.slices[selected.id]?.next_cursor ? '加载下一页邻居' : '展开邻域'}</button>{explore.slices[selected.id] && <button className="secondary" disabled={locked} onClick={() => explore.collapse(selected.id)}>收起邻域</button>}<button className="secondary" aria-pressed={explore.pins.includes(selected.id)} onClick={() => explore.togglePin(selected.id)}>{explore.pins.includes(selected.id) ? '取消固定' : '固定节点'}</button></div>}
          {kind !== 'knowledge' && !schemaMatches && <p className="muted">图与字段模型版本尚未对齐；仅显示本次图响应的信息。刷新图谱可重新读取。</p>}
          <details><summary>原始节点属性</summary><JsonView value={selected.properties} /></details>
          {kind === 'instances' && <button disabled={locked} className="secondary" onClick={() => openObject(selected.id)}>打开对象表单</button>}
          {kind === 'model' && <>{can('ontology:write') && <button disabled={locked} className="secondary" onClick={() => openModel({ typeKey: selected.type_key })}>编辑类型（变更集）</button>}<p className="muted">类型、字段与关系的修改将保存为草稿，经验证、批准、发布后更新此图。</p></>}
          {kind === 'knowledge' && <>{selected.kind === 'knowledge' && <button disabled={locked} className="secondary" onClick={() => openKnowledge(selected.id)}>{can('knowledge:write') ? '编辑知识页与引用' : '查看知识页'}</button>}<p className="muted">图展示已发布的知识引用；编辑器读取最新修订，保存草稿后需发布才更新引用图。文档节点不是本体实体。</p></>}
          <h3>当前可见子图中的关系</h3><ul className="relationship-list">{visible.edges.filter(e => e.source === selected.id || e.target === selected.id).map(edge => <li key={edge.id}><button disabled={locked} className="text-button" onClick={() => selectEdge(edge)}>{edge.source === selected.id ? '→' : '←'} {edge.label}{edge.source === edge.target ? '（自身）' : ''}{edge.cardinality ? ` · ${edge.cardinality === 'one' ? '一个' : '多个'}` : ''}</button><button disabled={locked} className="text-button" onClick={() => setSelection({ node: edge.source === selected.id ? edge.target : edge.source })}>{nodeName(edge.source === selected.id ? edge.target : edge.source)}</button></li>)}</ul>
        </> : selectedEdge ? <>
          <div className="section-heading"><h2>{selectedEdge.label}</h2><button disabled={locked} aria-label="取消选择" className="icon-button" onClick={() => setSelection({})}>×</button></div><span className="badge">{kind === 'model' ? '关系定义' : kind === 'knowledge' ? '证据引用' : '业务关系'}</span><div className="graph-edge-path"><strong>{nodeName(selectedEdge.source)}</strong><span>↓ {selectedEdge.label}</span><strong>{nodeName(selectedEdge.target)}</strong></div><dl className="graph-properties"><dt>方向</dt><dd>来源 → 目标</dd>{cardinality && <><dt>每个来源对应</dt><dd>{cardinality === 'one' ? '一个目标' : '多个目标'}</dd></>}</dl><p className="row-id">{selectedEdge.type_key}</p>
          {samePair.length > 1 && <div className="graph-parallel-note"><p>{selectedEdge.source === selectedEdge.target ? '这个节点上' : '这两个节点间'}有 {samePair.length} 条具名关系。可在画布或此处逐条选择：</p>{samePair.map(edge => <button key={edge.id} disabled={locked} className={edge.id === selectedEdge.id ? 'selected' : 'secondary'} onClick={() => selectEdge(edge)}>{nodeName(edge.source)} — {edge.label} → {nodeName(edge.target)}</button>)}</div>}
          {kind === 'model' && <>{can('ontology:write') && <button disabled={locked} className="secondary" onClick={() => openModel({ typeKey: selectedEdge.source, relationKey: selectedEdge.type_key })}>编辑关系定义（变更集）</button>}<p className="muted">编辑来源类型中的具名关系，包含业务角色、目标类型与基数。批准并发布后生效。</p></>}
          {kind === 'knowledge' && selectedEdge.kind === 'citation' && <><button disabled={locked} className="secondary" onClick={() => openKnowledge(selectedEdge.source)}>{can('knowledge:write') ? '编辑所属知识页与引用' : '查看所属知识页'}</button><p className="muted">增加或移除引用须保存知识页新修订并发布；不直接修改图中的边。</p></>}
          {kind === 'instances' && <button disabled={locked} className="secondary" onClick={() => openObject(selectedEdge.source)}>打开来源对象与关系</button>}
        </> : <Empty title="选择一个节点或关系">点击画布或下方列表，查看并进入对应编辑流程。</Empty>}</aside>
      </div>
      <details className="node-list" open={visible.nodes.length <= 40}><summary>节点列表（键盘可操作）</summary><div className="node-chips">{visible.nodes.map(node => <button disabled={locked} aria-pressed={node.id === selected?.id} title={`${groupLabel(node, kind, matchedTypes)} · ${node.id}`} className={node.id === selected?.id ? 'selected' : 'secondary'} key={node.id} onClick={() => selectNode(node)}><span className="graph-color-dot" style={{ background: groupColor(groupKey(node, kind)) }} />{node.label}</button>)}</div></details>
      <details className="node-list"><summary>关系列表（键盘可操作）</summary><div className="node-chips">{visible.edges.map(edge => <button disabled={locked} aria-pressed={edge.id === selectedEdge?.id} className={edge.id === selectedEdge?.id ? 'selected' : 'secondary'} key={edge.id} onClick={() => selectEdge(edge)}>{nodeName(edge.source)} — {edge.label} → {nodeName(edge.target)}</button>)}</div>{!visible.edges.length && <p className="muted">当前筛选没有可见关系。</p>}<p className="muted">同对节点的不同关系以分开的曲线显示，同一节点上的关系以环线显示；此列表可用键盘逐条查看。</p></details>
    </>}
    {editor?.kind === 'object' && <ObjectEditor key={editor.session.object?.id} {...editor.session} role={role} projectId={projectId} canWrite={can('objects:write')} canReadContributions={can('sources:write')} onSaved={graph.reload} onClose={() => setEditor(undefined)} onDirty={onDirty} />}
    {editor?.kind === 'model' && <OntologyEditor projectId={projectId} can={can} initialDefinition={editor.definition} initialBaselineReleaseId={editor.baselineReleaseId} focus={editor.focus} onDirty={onDirty} onClose={() => setEditor(undefined)} onUpdated={published => { if (published) { graph.reload(); types.reload(); } }} />}
    {editor?.kind === 'knowledge' && <Suspense fallback={<Loading />}><KnowledgeEditor key={editor.value.id} projectId={projectId} initialPage={editor.value} canWrite={can('knowledge:write')} canPublish={can('knowledge:publish')} onDirty={onDirty} onClose={() => setEditor(undefined)} onUpdated={graph.reload} /></Suspense>}
  </>;
}
