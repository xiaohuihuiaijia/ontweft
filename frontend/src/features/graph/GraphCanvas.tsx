import { useEffect, useRef, useState } from 'react';
import Graph from 'graphology';
import Sigma from 'sigma';
import { EdgeCurvedArrowProgram } from '@sigma/edge-curve';
import { drawGraphHover, drawGraphLabel } from './graph-labels';
import { attachSelfLoopLayer } from './self-loop-layer';
import './self-loop-layer.css';
import { ErrorNotice } from '../../shared/ui/components';
import { edgeLanes, groupColor, groupKey, selectionContext, topologyKey, type GraphSelection, type Position } from './graph-model';
import type { GraphEdge, GraphKind, GraphNode, GraphResult } from '../../shared/model/types';

export function GraphCanvas({ data, visible, kind, selection, cache, positions: retained, pins = [], onSelect, onSelectEdge, onClear }: { data: GraphResult; visible: GraphResult; kind: GraphKind; selection: GraphSelection; cache: Map<string, Position[]>; positions?: Map<string, Position>; pins?: string[]; onSelect: (node: GraphNode) => void; onSelectEdge: (edge: GraphEdge) => void; onClear: () => void }) {
  const remembered = useRef(new Map<string, Position>());
  const positionsStore = retained ?? remembered.current;
  const pinned = useRef(pins); pinned.current = pins;
  const container = useRef<HTMLDivElement>(null); const renderer = useRef<Sigma | undefined>(undefined);
  const loopLayer = useRef<ReturnType<typeof attachSelfLoopLayer> | undefined>(undefined);
  const currentPositions = useRef(positionsStore); currentPositions.current = positionsStore;
  const [error, setError] = useState<unknown>(); const [ready, setReady] = useState(0); const [zoom, setZoom] = useState(100);
  const latest = useRef({ data, visible, kind, selection, onSelect, onSelectEdge, onClear }); latest.current = { data, visible, kind, selection, onSelect, onSelectEdge, onClear };
  const [reduced, setReduced] = useState(() => typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  useEffect(() => { if (!window.matchMedia) return; const media = window.matchMedia('(prefers-reduced-motion: reduce)'); const changed = () => setReduced(media.matches); media.addEventListener('change', changed); return () => media.removeEventListener('change', changed); }, []);
  const topology = topologyKey(data);
  useEffect(() => {
    if (!container.current) return;
    if (!data.nodes.length) { renderer.current?.getGraph().clear(); setReady(0); return; }
    let stopped = false; let worker: Worker | undefined; setReady(0); setError(undefined);
    const build = (positions: Position[]) => {
      if (stopped || !container.current) return;
      try {
        const graph = renderer.current?.getGraph() ?? new Graph({ multi: true, type: 'directed' }); const coordinates = new Map(positions.map(p => [p.id, positionsStore.get(p.id) ?? p]));
        for (const [id, point] of coordinates) positionsStore.set(id, point);
        const current = latest.current;
        if (renderer.current && graph.order) renderer.current.setCustomBBox(renderer.current.getCustomBBox() ?? renderer.current.getBBox());
        const ids = new Set(current.data.nodes.map(node => node.id));
        for (const id of graph.nodes()) if (!ids.has(id)) graph.dropNode(id);
        for (const id of positionsStore.keys()) if (!ids.has(id)) positionsStore.delete(id);
        for (const node of current.data.nodes) graph.mergeNode(node.id, { ...(coordinates.get(node.id) ?? { x: 0, y: 0 }), label: node.label, size: 9, color: groupColor(groupKey(node, current.kind)) });
        const lanes = edgeLanes(current.data.edges);
        for (const id of graph.edges()) if (!lanes.has(id)) graph.dropEdge(id);
        for (const edge of current.data.edges) if (graph.hasNode(edge.source) && graph.hasNode(edge.target) && lanes.has(edge.id)) graph.mergeEdgeWithKey(edge.id, edge.source, edge.target, { label: edge.label, size: 1.6, color: '#869a9d', ...lanes.get(edge.id) });
        if (!renderer.current) {
        const sigma = new Sigma(graph, container.current, { defaultDrawNodeLabel: drawGraphLabel, defaultDrawNodeHover: drawGraphHover, edgeProgramClasses: { curved: EdgeCurvedArrowProgram }, renderEdgeLabels: true, enableEdgeEvents: true, labelFont: 'sans-serif', labelSize: 12, labelColor: { color: '#273f48' }, edgeLabelSize: 11, edgeLabelColor: { color: '#526974' }, labelDensity: 1, labelRenderedSizeThreshold: 0, stagePadding: 54, zIndex: true, minCameraRatio: 0.08, maxCameraRatio: 3, allowInvalidContainer: false });
        renderer.current = sigma;
        loopLayer.current = attachSelfLoopLayer(sigma, container.current, () => latest.current);
        let dragged: string | undefined; let moved = false;
        sigma.on('downNode', ({ node }) => { if (!pinned.current.includes(node)) { dragged = node; moved = false; sigma.setCustomBBox(sigma.getBBox()); } });
        sigma.getMouseCaptor().on('mousemovebody', event => { if (!dragged) return; const point = sigma.viewportToGraph(event); graph.setNodeAttribute(dragged, 'x', point.x); graph.setNodeAttribute(dragged, 'y', point.y); currentPositions.current.set(dragged, { id: dragged, ...point }); moved = true; event.preventSigmaDefault(); event.original.preventDefault(); event.original.stopPropagation(); });
        sigma.getMouseCaptor().on('mouseup', () => { dragged = undefined; });
        sigma.on('clickNode', ({ node }) => { if (moved) { moved = false; return; } const current = latest.current; const item = current.visible.nodes.find(n => n.id === node); if (item) current.onSelect(item); });
        sigma.on('clickEdge', ({ edge }) => { const current = latest.current; const item = current.visible.edges.find(e => e.id === edge); if (item) current.onSelectEdge(item); });
        sigma.on('clickStage', () => latest.current.onClear());
        sigma.getCamera().on('updated', state => setZoom(Math.round(100 / state.ratio)));
        }
        cache.set(topology, positions); while (cache.size > 4) cache.delete(cache.keys().next().value!);
        setReady(value => value + 1);
      } catch (failure) { loopLayer.current?.destroy(); renderer.current?.kill(); renderer.current = undefined; setError(failure); }
    };
    const positions = data.nodes.every(node => positionsStore.has(node.id)) ? data.nodes.map(node => positionsStore.get(node.id)!) : cache.get(topology);
    if (positions) build(positions);
    else try {
      worker = new Worker(new URL('./graph-layout.worker.ts', import.meta.url), { type: 'module' });
      worker.onmessage = (event: MessageEvent<Position[]>) => build(event.data);
      worker.onerror = () => { if (!stopped) setError(new Error('图布局未能完成，请使用节点和关系列表。')); };
      worker.postMessage({ ids: data.nodes.map(n => n.id), edges: data.edges.map(e => ({ source: e.source, target: e.target })), positions: data.nodes.flatMap(n => positionsStore.has(n.id) ? [positionsStore.get(n.id)!] : []) });
    } catch { setError(new Error('浏览器未能启动图布局，请使用节点和关系列表。')); }
    return () => { stopped = true; worker?.terminate(); };
  }, [topology, cache, positionsStore]);
  useEffect(() => () => { loopLayer.current?.destroy(); renderer.current?.kill(); renderer.current = undefined; }, []);

  // Updating selection/filters refreshes display reducers; topology and coordinates stay fixed.
  useEffect(() => {
    const sigma = renderer.current; if (!sigma || !ready) return;
    const graph = sigma.getGraph(); const nodes = new Map(data.nodes.map(n => [n.id, n])); const edges = new Map(data.edges.map(e => [e.id, e]));
    const visibleNodes = new Set(visible.nodes.map(n => n.id)); const visibleEdges = new Set(visible.edges.map(e => e.id));
    const context = selectionContext(visible, selection);
    sigma.setSettings({
      nodeReducer: (id, attrs) => { const node = nodes.get(id); const selected = selection.node === id && context.active; const related = context.nodes.has(id); return { ...attrs, hidden: !visibleNodes.has(id), label: !context.active || related ? node?.label ?? '' : '', color: context.active && !related ? '#d6dfdf' : groupColor(node ? groupKey(node, kind) : id), size: selected ? 13 : related ? 10 : 9, highlighted: selected, forceLabel: related || (!context.active && visible.nodes.length <= 35), zIndex: selected ? 3 : related ? 2 : 1 }; },
      edgeReducer: (id, attrs) => { const edge = edges.get(id); const related = context.edges.has(id); return { ...attrs, hidden: !visibleEdges.has(id), color: context.active ? related ? '#285d6c' : '#e2e8e8' : '#879ea7', size: related ? 2.7 : 1.6, label: !context.active || related ? edge?.label ?? '' : '', forceLabel: related || (!context.active && visible.edges.length <= 40), zIndex: related ? 2 : 1 }; },
      inertiaDuration: reduced ? 0 : 200, zoomDuration: reduced ? 0 : 200, doubleClickZoomingDuration: reduced ? 0 : 200,
    });
    // Sigma's reducer uses full node attributes, including its original x/y coordinates.
    if (graph.order) sigma.refresh();
  }, [data, visible, kind, selection, reduced, ready]);

  const move = (state: { x?: number; y?: number; ratio?: number; angle?: number }) => { const camera = renderer.current?.getCamera(); if (!camera) return; if (reduced) camera.setState(state); else void camera.animate(state, { duration: 220 }); };
  const fit = (ids: string[]) => {
    const points = ids.map(id => renderer.current?.getNodeDisplayData(id)).filter(p => !!p);
    if (!points.length) return;
    const minX = Math.min(...points.map(p => p.x)); const maxX = Math.max(...points.map(p => p.x)); const minY = Math.min(...points.map(p => p.y)); const maxY = Math.max(...points.map(p => p.y));
    move({ x: (minX + maxX) / 2, y: (minY + maxY) / 2, angle: 0, ratio: Math.max(0.18, Math.min(2, Math.max(maxX - minX, maxY - minY) * 1.25)) });
  };
  const selectedIds = [...selectionContext(visible, selection).nodes];
  return <><div ref={container} className="graph-canvas" aria-label="授权子图画布" />
    <div className="graph-controls" role="toolbar" aria-label="图谱视图控制"><button type="button" className="secondary" aria-label="放大图谱" disabled={!ready || !!error} onClick={() => move({ ratio: Math.max(0.08, (renderer.current?.getCamera().ratio ?? 1) / 1.4) })}>＋</button><span aria-label="图谱缩放比例">{zoom}%</span><button type="button" className="secondary" aria-label="缩小图谱" disabled={!ready || !!error} onClick={() => move({ ratio: Math.min(3, (renderer.current?.getCamera().ratio ?? 1) * 1.4) })}>−</button><button type="button" className="secondary" disabled={!ready || !!error || !visible.nodes.length} onClick={() => fit(visible.nodes.map(n => n.id))}>适配可见图</button><button type="button" className="secondary" disabled={!ready || !!error || !selectedIds.length} onClick={() => fit(selectedIds)}>定位所选</button></div>
    {!visible.nodes.length && <div className="graph-no-match">当前筛选没有可见节点，可清除筛选。</div>}
    {error ? <div className="graph-fallback"><ErrorNotice error={error} /><p>下方节点与关系列表仍可使用。</p></div> : null}
  </>;
}
