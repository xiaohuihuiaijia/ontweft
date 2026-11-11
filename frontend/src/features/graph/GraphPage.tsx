import { lazy, Suspense, useMemo, useRef, useState } from 'react';
import { request, scoped } from '../../shared/api/api';
import { Empty, ErrorNotice, JsonView, Loading, PageTitle } from '../../shared/ui/components';
import { useResource } from '../../shared/hooks/hooks';
import { GraphCanvas } from './GraphCanvas';
import { GraphIcon } from './GraphIcon';
import './graph.css';
import { useGraphExploration } from './useGraphExploration';
import { boundedGraph, filterGraph, groupColor, groupKey, groupLabel, relationDefinition, type Position } from './graph-model';
import { loadObjectEditSession, ObjectEditor, type ObjectEditSession } from '../objects/ObjectsPage';
import { OntologyEditor, type OntologyFocus } from '../modeling/OntologyPage';
import type { KnowledgePage } from '../knowledge/KnowledgePages';
const KnowledgeEditor = lazy(() => import('../knowledge/KnowledgePages').then(m => ({ default: m.KnowledgeEditor })));
import type { Definition, DefinitionResult, GraphEdge, GraphKind, GraphNode, GraphResult, TypeList } from '../../shared/model/types';

type GraphEditor =
  | { kind: 'object'; session: ObjectEditSession; relationKey?: string }
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
  const explore = useGraphExploration(projectId, userId, graph.data, kind === 'instances', nodeBudget, setError, opening || !!editor, graph.error);
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
  const openObject = async (id: string, relationKey?: string) => {
    if (locked) return;
    setOpening(true); setError(undefined);
    try {
      setEditor({ kind: 'object', session: await loadObjectEditSession(projectId, id), relationKey });
    }
    catch (e) { setError(e); } finally { setOpening(false); }
  };
  const openNode = (node: GraphNode) => {
    if (locked) return;
    setSelection({ node: node.id });
    if (kind === 'instances') void openObject(node.id);
    else if (kind === 'model') void openModel({ typeKey: node.type_key });
    else if (node.kind === 'knowledge') void openKnowledge(node.id);
  };
  const openEdge = (edge: GraphEdge) => {
    if (locked) return;
    setSelection({ edge: edge.id });
    if (kind === 'instances') void openObject(edge.source, edge.type_key);
    else if (kind === 'model') void openModel({ typeKey: base.nodes.find(node => node.id === edge.source)?.type_key ?? edge.source, relationKey: edge.type_key });
    else if (edge.kind === 'citation') void openKnowledge(edge.source);
  };
  const refreshGraph = () => { graph.reload(); types.reload(); if (kind === 'instances') void explore.refresh(); };
  return <section className="graph-page"><PageTitle title="图谱探索" description="沿对象与关系理解业务，直接查看和编辑已授权数据。"><button className="secondary" disabled={locked} onClick={refreshGraph}>刷新图谱</button></PageTitle>
    <div className="graph-mode-bar"><div className="tabs" role="tablist" aria-label="图谱类型">{(['instances', 'model', ...(knowledgeEnabled ? ['knowledge' as const] : [])] as const).map((value, i) => <button disabled={locked} role="tab" aria-selected={kind === value} className={kind === value ? 'active' : ''} key={value} onClick={() => { setKind(value); resetFilters(); setError(undefined); }}>{['业务实例', '本体模型', '文档与知识'][i]}</button>)}</div><span className="graph-scope">{graph.data?.version !== undefined ? `已发布模型 v${graph.data.version} · ` : ''}授权视图</span></div>
    {explore.controls}
    <ErrorNotice error={error} />{opening && <Loading />}{graph.loading ? <Loading /> : graph.error ? <ErrorNotice error={graph.error} onRetry={graph.reload} /> : !base.nodes.length ? <Empty title="当前视图没有可见节点">图谱只包含服务端返回的已授权数据。</Empty> : <>
      {base.truncated && <div className="notice warning">当前显示有限子图，不代表全部关系。本次节点预算 {nodeBudget}，最多载入 1000 条关系。</div>}

      <div className="graph-filter-panel"><div className="graph-filter-row"><div className="graph-filter-search"><label htmlFor="graph-search">查找当前子图</label><input id="graph-search" disabled={locked} aria-label="在当前子图查找" value={filter} onChange={e => { setFilter(e.target.value); setSelection({}); }} placeholder="节点名称、类型或标识…" /><button disabled={locked || (!filter && !hiddenTypes.length)} className="secondary" onClick={resetFilters}>重置筛选</button></div><div className="graph-budget"><label htmlFor="graph-node-budget">载入节点上限</label><select id="graph-node-budget" aria-label="图谱节点预算" disabled={locked || graph.loading} value={nodeBudget} onChange={event => { setNodeBudget(Number(event.target.value)); setSelection({}); }}><option value={100}>100 个</option><option value={200}>200 个</option><option value={500}>500 个</option></select></div></div><div className="graph-legend" aria-label="类型图例与筛选">{groups.map(group => <button type="button" disabled={locked} aria-pressed={!hiddenTypes.includes(group.key)} className="secondary" key={group.key} onClick={() => { setHiddenTypes(current => current.includes(group.key) ? current.filter(key => key !== group.key) : [...current, group.key]); setSelection({}); }}><span className="graph-color-dot" style={{ background: groupColor(group.key) }} />{group.label}<span className="graph-legend-count">{group.count}</span></button>)}</div></div>
      <div className="graph-workspace"><div className="graph-stage graph-stage-dark"><GraphCanvas dark openOnClick data={base} visible={visible} kind={kind} selection={selection} cache={layoutCache.current} positions={explore.positions.current} pins={explore.pins} disabled={locked} onOpen={openNode} onOpenEdge={openEdge} onSelect={selectNode} onSelectEdge={selectEdge} onClear={() => { if (!locked) setSelection({}); }} /><div className="graph-caption" role="status"><span>当前显示 <strong>{visible.nodes.length}</strong> 个节点 · <strong>{visible.edges.length}</strong> 条关系</span><span>已载入 {base.nodes.length} / {nodeBudget} 节点 · 仅授权范围</span></div></div>
        <aside className="graph-inspector" aria-label="图谱详情"><div className="graph-inspector-label">{selected ? '节点详情' : selectedEdge ? '关系详情' : '当前工作视图'}</div>{selected ? <>
          <div className="section-heading"><h2>{selected.label}</h2><button disabled={locked} aria-label="取消选择" className="icon-button" onClick={() => setSelection({})}>×</button></div><span className="graph-type-badge"><span className="graph-color-dot" style={{ background: groupColor(groupKey(selected, kind)) }} />{groupLabel(selected, kind, matchedTypes)}</span><p className="row-id">{selected.id}</p><div className="graph-selection-actions">
            {kind === 'instances' && <button disabled={locked} onClick={() => openObject(selected.id)} aria-label="打开对象表单"><GraphIcon name="edit" />{can('objects:write') ? '编辑对象' : '查看对象'}</button>}
            {kind === 'instances' && <button className="secondary" disabled={locked} aria-pressed={explore.pins.includes(selected.id)} onClick={() => explore.togglePin(selected.id)}><GraphIcon name="pin" />{explore.pins.includes(selected.id) ? '取消固定' : '固定节点'}</button>}
          </div>
          {kind === 'model' ? <><p className="muted">{selectedType?.description || (typeof selected.properties.description === 'string' ? selected.properties.description : '已发布的类型定义')}</p><dl className="graph-properties"><dt>可读属性</dt><dd>{typeof selected.visible_property_count === 'number' ? selected.visible_property_count : selectedType?.properties.length ?? '等待同版本模型'}</dd><dt>可见出向关系</dt><dd>{visible.edges.filter(e => e.source === selected.id).length}</dd></dl>{selectedType?.properties.length ? <><h3>当前可读字段</h3><dl className="graph-properties">{selectedType.properties.map(field => <div key={field.key}><dt>{field.label}</dt><dd>{field.data_type}{field.unit ? ` · ${field.unit}` : ''}{field.required ? ' · 必填' : ''}</dd></div>)}</dl></> : null}</> : kind === 'instances' ? <><h3>当前可读属性</h3><dl className="graph-properties">{Object.entries(selected.properties).map(([key, value]) => <div key={key}><dt>{selectedType?.properties.find(p => p.key === key)?.label ?? key}</dt><dd>{value === null ? '空值' : typeof value === 'boolean' ? value ? '是' : '否' : typeof value === 'object' ? <code>{JSON.stringify(value)}</code> : String(value)}</dd></div>)}</dl></> : <p className="muted">{selected.kind === 'document' ? '文档证据节点；引用指向固定文档修订。' : '已发布的知识页及其引用。'}</p>}
          {kind === 'instances' && <div className="toolbar"><button className="secondary" disabled={locked || (Object.hasOwn(explore.slices, selected.id) && explore.slices[selected.id].next_cursor === null)} onClick={() => void explore.expand(selected.id)}>{explore.slices[selected.id]?.next_cursor ? '加载下一页邻居' : '展开邻域'}</button>{explore.slices[selected.id] && <button className="secondary" disabled={locked} onClick={() => explore.collapse(selected.id)}>收起邻域</button>}</div>}
          {kind !== 'knowledge' && !schemaMatches && <p className="muted">图与字段模型版本尚未对齐；仅显示本次图响应的信息。刷新图谱可重新读取。</p>}
          <details><summary>原始节点属性</summary><JsonView value={selected.properties} /></details>

          {kind === 'model' && <>{can('ontology:write') && <button disabled={locked} className="secondary" onClick={() => openModel({ typeKey: selected.type_key })}>编辑类型（变更集）</button>}<p className="muted">类型、字段与关系的修改将保存为草稿，经验证、批准、发布后更新此图。</p></>}
          {kind === 'knowledge' && <>{selected.kind === 'knowledge' && <button disabled={locked} className="secondary" onClick={() => openKnowledge(selected.id)}>{can('knowledge:write') ? '编辑知识页与引用' : '查看知识页'}</button>}<p className="muted">图展示已发布的知识引用；编辑器读取最新修订，保存草稿后需发布才更新引用图。文档节点不是本体实体。</p></>}
          <h3>当前可见子图中的关系</h3><ul className="relationship-list">{visible.edges.filter(e => e.source === selected.id || e.target === selected.id).map(edge => <li key={edge.id}><button disabled={locked} className="text-button" onClick={() => selectEdge(edge)}>{edge.source === selected.id ? '→' : '←'} {edge.label}{edge.source === edge.target ? '（自身）' : ''}{edge.cardinality ? ` · ${edge.cardinality === 'one' ? '一个' : '多个'}` : ''}</button><button disabled={locked} className="text-button" onClick={() => setSelection({ node: edge.source === selected.id ? edge.target : edge.source })}>{nodeName(edge.source === selected.id ? edge.target : edge.source)}</button></li>)}</ul>
        </> : selectedEdge ? <>
          <div className="section-heading"><h2>{selectedEdge.label}</h2><button disabled={locked} aria-label="取消选择" className="icon-button" onClick={() => setSelection({})}>×</button></div><span className="badge">{kind === 'model' ? '关系定义' : kind === 'knowledge' ? '证据引用' : '业务关系'}</span><div className="graph-edge-path"><strong>{nodeName(selectedEdge.source)}</strong><span>↓ {selectedEdge.label}</span><strong>{nodeName(selectedEdge.target)}</strong></div><dl className="graph-properties"><dt>方向</dt><dd>来源 → 目标</dd>{cardinality && <><dt>每个来源对应</dt><dd>{cardinality === 'one' ? '一个目标' : '多个目标'}</dd></>}</dl><p className="row-id">{selectedEdge.type_key}</p>
          {samePair.length > 1 && <div className="graph-parallel-note"><p>{selectedEdge.source === selectedEdge.target ? '这个节点上' : '这两个节点间'}有 {samePair.length} 条具名关系。可在画布或此处逐条选择：</p>{samePair.map(edge => <button key={edge.id} disabled={locked} className={edge.id === selectedEdge.id ? 'selected' : 'secondary'} onClick={() => selectEdge(edge)}>{nodeName(edge.source)} — {edge.label} → {nodeName(edge.target)}</button>)}</div>}
          {kind === 'model' && <>{can('ontology:write') && <button disabled={locked} className="secondary" onClick={() => openEdge(selectedEdge)}>编辑关系定义（变更集）</button>}<p className="muted">编辑来源类型中的具名关系，包含业务角色、目标类型与基数。批准并发布后生效。</p></>}
          {kind === 'knowledge' && selectedEdge.kind === 'citation' && <><button disabled={locked} className="secondary" onClick={() => openKnowledge(selectedEdge.source)}>{can('knowledge:write') ? '编辑所属知识页与引用' : '查看所属知识页'}</button><p className="muted">增加或移除引用须保存知识页新修订并发布；不直接修改图中的边。</p></>}
          {kind === 'instances' && <button disabled={locked} className="secondary" onClick={() => openEdge(selectedEdge)}>打开来源对象与关系</button>}
        </> : <div className="graph-overview">
          <h2>让关系变得清晰</h2><p>选择节点查看属性，选择连线查看关系方向与业务含义。</p>
          <dl className="graph-summary"><div><dt>可见节点</dt><dd>{visible.nodes.length}</dd></div><div><dt>可见关系</dt><dd>{visible.edges.length}</dd></div><div><dt>节点类型</dt><dd>{new Set(visible.nodes.map(node => groupKey(node, kind))).size}</dd></div></dl>
          <h3>从当前节点开始</h3><div className="graph-entry-list">{visible.nodes.slice(0, 5).map(node => <button className="secondary" key={node.id} disabled={locked} aria-label={`查看节点 ${node.label}`} onClick={() => selectNode(node)}><span className="graph-color-dot" style={{ background: groupColor(groupKey(node, kind)) }} /><span><strong>{node.label}</strong><small>{groupLabel(node, kind, matchedTypes)}</small></span><span aria-hidden="true">↗</span></button>)}</div>
          <div className="graph-gesture-guide"><h3>操作提示</h3><p><span>单击节点</span>打开对象或类型表单</p><p><span>双击关系 / Enter</span>打开对应表单</p><p><span>拖动 / 方向键</span>调整节点位置</p><p><span>滚轮 / 双指</span>缩放画布</p></div>
        </div>}</aside>
      </div>
      <p id="graph-interaction-help" className="graph-help">拖动调整布局；单击节点或双击关系打开对应表单。画布获得焦点后，方向键移动节点，Shift 加速，Enter 打开，Esc 取消选择。固定节点不会被移动。</p>
      <details className="node-list" open={visible.nodes.length <= 40}><summary>节点列表（键盘可操作）</summary><div className="node-chips">{visible.nodes.map(node => <button disabled={locked} aria-pressed={node.id === selected?.id} title={`${groupLabel(node, kind, matchedTypes)} · ${node.id}`} className={node.id === selected?.id ? 'selected' : 'secondary'} key={node.id} onDoubleClick={() => openNode(node)} onClick={() => selectNode(node)}><span className="graph-color-dot" style={{ background: groupColor(groupKey(node, kind)) }} />{node.label}</button>)}</div></details>
      <details className="node-list"><summary>关系列表（键盘可操作）</summary><div className="node-chips">{visible.edges.map(edge => <button disabled={locked} aria-pressed={edge.id === selectedEdge?.id} className={edge.id === selectedEdge?.id ? 'selected' : 'secondary'} key={edge.id} onClick={() => selectEdge(edge)}>{nodeName(edge.source)} — {edge.label} → {nodeName(edge.target)}</button>)}</div>{!visible.edges.length && <p className="muted">当前筛选没有可见关系。</p>}<p className="muted">同对节点的不同关系以分开的曲线显示，同一节点上的关系以环线显示；此列表可用键盘逐条查看。</p></details>
    </>}
    {editor?.kind === 'object' && <ObjectEditor key={editor.session.object?.id} {...editor.session} initialRelationKey={editor.relationKey} role={role} projectId={projectId} canWrite={can('objects:write')} canReadContributions={can('sources:write')} onSaved={() => void explore.refresh()} onClose={() => setEditor(undefined)} onDirty={onDirty} />}
    {editor?.kind === 'model' && <OntologyEditor projectId={projectId} can={can} initialDefinition={editor.definition} initialBaselineReleaseId={editor.baselineReleaseId} focus={editor.focus} onDirty={onDirty} onClose={() => setEditor(undefined)} onUpdated={published => { if (published) { graph.reload(); types.reload(); } }} />}
    {editor?.kind === 'knowledge' && <Suspense fallback={<Loading />}><KnowledgeEditor key={editor.value.id} projectId={projectId} initialPage={editor.value} canWrite={can('knowledge:write')} canPublish={can('knowledge:publish')} onDirty={onDirty} onClose={() => setEditor(undefined)} onUpdated={graph.reload} /></Suspense>}
  </section>;
}
