import { useEffect, useMemo, useRef, useState } from 'react';
import { request, scoped, write } from '../../shared/api/api';
import type { BusinessObject, GraphResult, ListResult } from '../../shared/model/types';
import type { Position } from './graph-model';
import { GraphIcon } from './GraphIcon';

type Slice = GraphResult & { next_cursor?: string | null };
const empty: GraphResult = { nodes: [], edges: [], truncated: false };

export function useGraphExploration(projectId: string, userId: string, initial: GraphResult | undefined, enabled: boolean, budget: number, onError: (error: unknown) => void, disabled = false) {
  const [slices, setSlices] = useState<Record<string, Slice>>({});
  const [query, setQuery] = useState(''); const [hits, setHits] = useState<BusinessObject[]>([]);
  const [busy, setBusy] = useState(false); const [notice, setNotice] = useState('');
  const [pins, setPins] = useState<string[]>([]);
  const positions = useRef(new Map<string, Position>());
  const requestId = useRef(0);
  const scope = useRef({ projectId, userId, releaseId: initial?.release_id });
  const key = `ontweft.graph.v1:${userId}:${projectId}`;
  useEffect(() => {
    // useResource temporarily clears data during a reload; that is not a model change.
    const releaseId = initial?.release_id === undefined ? scope.current.releaseId : initial.release_id;
    if (scope.current.projectId !== projectId || scope.current.userId !== userId || scope.current.releaseId !== releaseId) {
      requestId.current++;
      setSlices({}); setHits([]); setQuery(''); setNotice(''); positions.current.clear(); setPins([]); setBusy(false);
    }
    scope.current = { projectId, userId, releaseId };
  }, [projectId, userId, initial?.release_id]);
  useEffect(() => () => { requestId.current++; }, []);
  const data = useMemo(() => {
    if (!enabled) return initial ?? empty;
    const parts: Slice[] = [...Object.values(slices).reverse(), ...(slices.restored ? [] : [initial ?? empty])].filter(p => !initial || p.release_id === initial.release_id);
    // Recent slices get both value precedence and room in the bounded canvas.
    const latest = <T extends { id: string }>(items: T[]) => {
      const seen = new Set<string>();
      return items.filter(item => { if (seen.has(item.id)) return false; seen.add(item.id); return true; });
    };
    const nodes = latest(parts.flatMap(p => p.nodes));
    const ids = new Set(nodes.slice(0, budget).map(n => n.id));
    const edges = latest(parts.flatMap(p => p.edges)).filter(e => ids.has(e.source) && ids.has(e.target));
    return { ...initial, nodes: nodes.slice(0, budget), edges: edges.slice(0, 1000), truncated: parts.some(p => p.truncated || p.next_cursor) || nodes.length > budget || edges.length > 1000 };
  }, [initial, slices, enabled, budget]);
  const run = async (action: (current: () => boolean) => Promise<void>) => {
    const id = ++requestId.current;
    const current = () => id === requestId.current;
    setBusy(true); onError(undefined);
    try { await action(current); } catch (error) { if (current()) onError(error); } finally { if (current()) setBusy(false); }
  };
  const putSlice = (id: string, page: Slice) => setSlices(current => { const next = { ...current }; delete next[id]; return { ...next, [id]: page }; });
  const expand = (id: string) => run(async current => {
    const previous = slices[id];
    const page = await request<Slice>(scoped('/graph/neighbors', projectId, { object_id: id, cursor: previous?.next_cursor ?? undefined, expected_model_release_id: initial?.release_id ?? undefined, limit: 50 }));
    if (!current()) return;
    putSlice(id, { ...page, nodes: [...new Map([...(previous?.nodes ?? []), ...page.nodes].map(n => [n.id, n])).values()], edges: [...new Map([...(previous?.edges ?? []), ...page.edges].map(e => [e.id, e])).values()] });
  });
  const collapse = (id: string) => setSlices(current => { const next = { ...current }; delete next[id]; return next; });
  const togglePin = (id: string) => setPins(current => current.includes(id) ? current.filter(p => p !== id) : [...current, id]);
  // Editing an object or its relationships also refreshes restored/expanded slices.
  const refresh = () => run(async current => {
    if (!data.nodes.length) return;
    const page = await write<GraphResult>('/graph/nodes', { project_id: projectId, ids: data.nodes.map(node => node.id) });
    if (!current()) return;
    setSlices({ restored: page });
    const allowed = new Set(page.nodes.map(node => node.id));
    setPins(current => current.filter(id => allowed.has(id)));
  });
  const locked = busy || disabled || !initial;
  const controls = enabled && <section className="graph-search-tools" aria-label="全库图谱探索">
    <div className="graph-command-row"><form className="graph-global-search" onSubmit={e => { e.preventDefault(); void run(async current => { const result = await write<ListResult<BusinessObject> & { index: { status: string } }>('/search/objects', { project_id: projectId, expected_release_id: initial?.release_id ?? null, expected_model_version: initial?.version ?? 0, q: query.trim(), limit: 20 }); if (!current()) return; setHits(result.items); setNotice(`找到 ${result.total} 个已索引的授权对象，列出前 20 个${result.index.status === 'ready' ? '' : '；索引尚未就绪或正在更新，结果可能不完整'}`); }); }}><label htmlFor="graph-global-query"><GraphIcon name="search" /><span>全库对象搜索</span></label><input disabled={locked} id="graph-global-query" aria-label="全库对象搜索" value={query} onChange={e => setQuery(e.target.value)} placeholder="按当前可读字段搜索…" /><button disabled={locked || !query.trim()}>搜索</button></form>

    {userId && <div className="graph-view-actions"><button className="secondary" disabled={locked} onClick={() => { try { localStorage.setItem(key, JSON.stringify({ ids: data.nodes.map(n => n.id), pins, positions: [...positions.current.values()].filter(p => data.nodes.some(n => n.id === p.id)) })); setNotice('个人视图已保存在本浏览器；不保存对象内容'); } catch (error) { onError(error); } }}><GraphIcon name="save" />保存个人视图</button><button className="secondary" disabled={locked} onClick={() => void run(async current => { const saved = JSON.parse(localStorage.getItem(key) ?? 'null'); if (!saved) { setNotice('本浏览器尚无保存的视图'); return; } const page = await write<GraphResult>('/graph/nodes', { project_id: projectId, ids: saved.ids }); if (!current()) return; const allowed = new Set(page.nodes.map(n => n.id)); positions.current = new Map((saved.positions as Position[]).filter(p => allowed.has(p.id) && Number.isFinite(p.x) && Number.isFinite(p.y)).map(p => [p.id, p])); setPins((saved.pins as string[]).filter(id => allowed.has(id))); setSlices({ restored: page }); setNotice('已按当前授权恢复视图'); })}><GraphIcon name="restore" />恢复个人视图</button></div>}
    </div>
    {hits.length > 0 && <div className="node-chips graph-search-hits">{hits.map(hit => <button key={hit.id} className="secondary" disabled={locked} onClick={() => void run(async current => { const page = await write<GraphResult>('/graph/nodes', { project_id: projectId, ids: [hit.id] }); if (!current()) return; putSlice(hit.id, page); })}>{String(hit.properties.name ?? hit.properties.title ?? hit.type_key)} · 加入画布</button>)}</div>}
    {notice && <p role="status" className="muted">{notice}</p>}
  </section>;
  return { data, controls, positions, pins, togglePin, expand, collapse, slices, busy, refresh };
}
