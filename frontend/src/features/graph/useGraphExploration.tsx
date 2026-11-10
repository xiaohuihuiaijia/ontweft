import { useEffect, useMemo, useRef, useState } from 'react';
import { request, scoped, write } from '../../shared/api/api';
import type { BusinessObject, GraphResult, ListResult } from '../../shared/model/types';
import type { Position } from './graph-model';

type Slice = GraphResult & { next_cursor?: string | null };
const empty: GraphResult = { nodes: [], edges: [], truncated: false };

export function useGraphExploration(projectId: string, userId: string, initial: GraphResult | undefined, enabled: boolean, budget: number, onError: (error: unknown) => void) {
  const [slices, setSlices] = useState<Record<string, Slice>>({});
  const [query, setQuery] = useState(''); const [hits, setHits] = useState<BusinessObject[]>([]);
  const [busy, setBusy] = useState(false); const [notice, setNotice] = useState('');
  const [pins, setPins] = useState<string[]>([]);
  const positions = useRef(new Map<string, Position>());
  const key = `ontweft.graph.v1:${userId}:${projectId}`;
  useEffect(() => { setSlices({}); setHits([]); setNotice(''); positions.current.clear(); setPins([]); }, [projectId, initial?.release_id]);
  const data = useMemo(() => {
    if (!enabled) return initial ?? empty;
    const parts = [...Object.values(slices).reverse(), ...(slices.restored ? [] : [initial ?? empty])].filter(p => !initial || p.release_id === initial.release_id);
    const nodes = [...new Map(parts.flatMap(p => p.nodes).map(n => [n.id, n])).values()];
    const ids = new Set(nodes.slice(0, budget).map(n => n.id));
    const edges = [...new Map(parts.flatMap(p => p.edges).map(e => [e.id, e])).values()].filter(e => ids.has(e.source) && ids.has(e.target));
    return { ...initial, nodes: nodes.slice(0, budget), edges: edges.slice(0, 1000), truncated: !!initial?.truncated || nodes.length > budget || edges.length > 1000 };
  }, [initial, slices, enabled, budget]);
  const run = async (action: () => Promise<void>) => { setBusy(true); onError(undefined); try { await action(); } catch (error) { onError(error); } finally { setBusy(false); } };
  const expand = (id: string) => run(async () => {
    const previous = slices[id];
    const page = await request<Slice>(scoped('/graph/neighbors', projectId, { object_id: id, cursor: previous?.next_cursor ?? undefined, expected_model_release_id: initial?.release_id ?? undefined, limit: 50 }));
    setSlices(current => ({ ...current, [id]: { ...page, nodes: [...(previous?.nodes ?? []), ...page.nodes], edges: [...(previous?.edges ?? []), ...page.edges] } }));
  });
  const collapse = (id: string) => setSlices(current => { const next = { ...current }; delete next[id]; return next; });
  const togglePin = (id: string) => setPins(current => current.includes(id) ? current.filter(p => p !== id) : [...current, id]);
  const controls = enabled && <section className="graph-filter-panel" aria-label="全库图谱探索">
    <form className="toolbar" onSubmit={e => { e.preventDefault(); void run(async () => { const result = await write<ListResult<BusinessObject> & { index: { status: string } }>('/search/objects', { project_id: projectId, expected_release_id: initial?.release_id ?? null, expected_model_version: initial?.version ?? 0, q: query.trim(), limit: 20 }); setHits(result.items); setNotice(`找到 ${result.total} 个已索引的授权对象，列出前 20 个${result.index.status === 'ready' ? '' : '；索引尚未就绪或正在更新，结果可能不完整'}`); }); }}><label htmlFor="graph-global-query">全库对象搜索</label><input id="graph-global-query" value={query} onChange={e => setQuery(e.target.value)} placeholder="按当前可读字段搜索…" /><button disabled={busy || !query.trim()}>搜索</button></form>
    <div className="node-chips">{hits.map(hit => <button key={hit.id} className="secondary" disabled={busy} onClick={() => void run(async () => { const page = await write<GraphResult>('/graph/nodes', { project_id: projectId, ids: [hit.id] }); setSlices(current => ({ ...current, [hit.id]: page })); })}>{String(hit.properties.name ?? hit.properties.title ?? hit.type_key)} · 加入画布</button>)}</div>
    {userId && <div className="toolbar"><button className="secondary" disabled={busy} onClick={() => { try { localStorage.setItem(key, JSON.stringify({ ids: data.nodes.map(n => n.id), pins, positions: [...positions.current.values()].filter(p => data.nodes.some(n => n.id === p.id)) })); setNotice('个人视图已保存在本浏览器；不保存对象内容'); } catch (error) { onError(error); } }}>保存个人视图</button><button className="secondary" disabled={busy} onClick={() => void run(async () => { const saved = JSON.parse(localStorage.getItem(key) ?? 'null'); if (!saved) { setNotice('本浏览器尚无保存的视图'); return; } const page = await write<GraphResult>('/graph/nodes', { project_id: projectId, ids: saved.ids }); const allowed = new Set(page.nodes.map(n => n.id)); positions.current = new Map((saved.positions as Position[]).filter(p => allowed.has(p.id) && Number.isFinite(p.x) && Number.isFinite(p.y)).map(p => [p.id, p])); setPins((saved.pins as string[]).filter(id => allowed.has(id))); setSlices({ restored: page }); setNotice('已按当前授权恢复视图'); })}>恢复个人视图</button></div>}
    {notice && <p role="status" className="muted">{notice}</p>}
  </section>;
  return { data, controls, positions, pins, togglePin, expand, collapse, slices, busy };
}
