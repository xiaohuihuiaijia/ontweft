import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent } from 'react';
import { GraphIcon } from './GraphIcon';
import Graph from 'graphology';
import Sigma from 'sigma';
import { EdgeCurvedArrowProgram } from '@sigma/edge-curve';
import { drawGraphHover, drawGraphLabel, drawDarkGraphHover, drawDarkGraphLabel } from './graph-labels';
import { attachSelfLoopLayer } from './self-loop-layer';
import './self-loop-layer.css';
import { ErrorNotice } from '../../shared/ui/components';
import { edgeLanes, groupColor, groupKey, selectionContext, topologyKey, type GraphSelection, type Position } from './graph-model';
import type { GraphEdge, GraphKind, GraphNode, GraphResult } from '../../shared/model/types';

export function GraphCanvas({ data, visible, kind, selection, cache, positions: retained, pins = [], disabled = false, dark = false, openOnClick = false, onSelect, onSelectEdge, onOpen, onOpenEdge, onClear }: { data: GraphResult; visible: GraphResult; kind: GraphKind; selection: GraphSelection; cache: Map<string, Position[]>; positions?: Map<string, Position>; pins?: string[]; disabled?: boolean; dark?: boolean; openOnClick?: boolean; onOpen?: (node: GraphNode) => void; onOpenEdge?: (edge: GraphEdge) => void; onSelect: (node: GraphNode) => void; onSelectEdge: (edge: GraphEdge) => void; onClear: () => void }) {
  const remembered = useRef(new Map<string, Position>());
  const positionsStore = retained ?? remembered.current;
  const pinned = useRef(pins); pinned.current = pins;
  const container = useRef<HTMLDivElement>(null); const renderer = useRef<Sigma | undefined>(undefined);
  const loopLayer = useRef<ReturnType<typeof attachSelfLoopLayer> | undefined>(undefined);
  const currentPositions = useRef(positionsStore); currentPositions.current = positionsStore;
  const [error, setError] = useState<unknown>(); const [ready, setReady] = useState(0); const [zoom, setZoom] = useState(100);
  const [showEdgeLabels, setShowEdgeLabels] = useState(false);
  const latest = useRef({ data, visible, kind, selection, disabled, dark, showEdgeLabels, openOnClick, onSelect, onSelectEdge, onOpen, onOpenEdge, onClear }); latest.current = { data, visible, kind, selection, disabled, dark, showEdgeLabels, openOnClick, onSelect, onSelectEdge, onOpen, onOpenEdge, onClear };
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
        // A mobile panel can hide this canvas; keep its coordinates until it is visible again.
        const sigma = new Sigma(graph, container.current, { defaultDrawNodeLabel: drawGraphLabel, defaultDrawNodeHover: drawGraphHover, edgeProgramClasses: { curved: EdgeCurvedArrowProgram }, renderEdgeLabels: true, enableEdgeEvents: true, labelFont: 'Segoe UI, Microsoft YaHei, sans-serif', labelSize: 13, labelColor: { color: '#273f48' }, edgeLabelSize: 12, edgeLabelColor: { color: '#526974' }, labelDensity: 1, labelRenderedSizeThreshold: 0, stagePadding: 54, zIndex: true, minCameraRatio: 0.08, maxCameraRatio: 3, allowInvalidContainer: true });
        renderer.current = sigma;
        loopLayer.current = attachSelfLoopLayer(sigma, container.current, () => latest.current);
        let dragged: string | undefined; let moved = false; let origin = { x: 0, y: 0 };
        let lastClick: { node?: string; edge?: string } = {};
        const endDrag = () => { dragged = undefined; if (container.current) container.current.style.cursor = ''; };
        const drag = (point: { x: number; y: number }, event: { preventSigmaDefault: () => void; original: Event }) => {
          if (!dragged || latest.current.disabled) return;
          event.preventSigmaDefault(); event.original.preventDefault(); event.original.stopPropagation();
          if (!moved && Math.hypot(point.x - origin.x, point.y - origin.y) < 4) return;
          if (!moved) { const node = latest.current.visible.nodes.find(item => item.id === dragged); if (node) latest.current.onSelect(node); }
          lastClick = {}; moved = true;
          if (pinned.current.includes(dragged)) return;
          const position = sigma.viewportToGraph(point);
          graph.mergeNodeAttributes(dragged, position);
          currentPositions.current.set(dragged, { id: dragged, ...position });
          if (container.current) container.current.style.cursor = 'grabbing';
        };
        sigma.on('downNode', ({ node, event }) => {
          endDrag(); moved = false;
          if (latest.current.disabled || (event && 'touches' in event.original && event.original.touches.length > 1)) return;
          dragged = node; origin = event ? { x: event.x, y: event.y } : { x: 0, y: 0 };
          sigma.setCustomBBox(sigma.getCustomBBox() ?? sigma.getBBox());
          container.current?.focus({ preventScroll: true });
        });
        sigma.getMouseCaptor().on('mousemovebody', event => drag(event, event));
        sigma.getMouseCaptor().on('mouseup', endDrag);
        sigma.getTouchCaptor().on('touchmove', event => { if (event.touches.length !== 1) { endDrag(); return; } drag(event.touches[0], event); });
        sigma.getTouchCaptor().on('touchup', endDrag);
        sigma.on('clickNode', ({ node }) => {
          if (moved) { moved = false; lastClick = {}; return; }
          const current = latest.current; const item = current.visible.nodes.find(n => n.id === node);
          if (item && !current.disabled) { lastClick = { node }; if (current.openOnClick && current.onOpen) current.onOpen(item); else current.onSelect(item); }
        });
        sigma.on('doubleClickNode', ({ node, preventSigmaDefault }) => {
          preventSigmaDefault();
          const current = latest.current; const item = current.visible.nodes.find(n => n.id === node);
          if (item && !current.disabled && !moved && lastClick.node === node) current.onOpen?.(item);
          lastClick = {};
        });
        sigma.on('clickEdge', ({ edge }) => { const current = latest.current; const item = current.visible.edges.find(e => e.id === edge); if (item && !current.disabled && !moved) { lastClick = { edge }; current.onSelectEdge(item); } });
        sigma.on('doubleClickEdge', ({ edge, preventSigmaDefault }) => {
          preventSigmaDefault();
          const current = latest.current; const item = current.visible.edges.find(e => e.id === edge);
          if (item && !current.disabled && !moved && lastClick.edge === edge) current.onOpenEdge?.(item);
          lastClick = {};
        });
        sigma.on('downEdge', () => { endDrag(); moved = false; });
        sigma.on('downStage', () => { moved = false; lastClick = {}; });
        sigma.on('clickStage', () => { lastClick = {}; if (!latest.current.disabled && !moved) latest.current.onClear(); });
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
  // Dispose before React removes the canvas; queued Sigma frames must not measure detached DOM.
  useLayoutEffect(() => () => { loopLayer.current?.destroy(); renderer.current?.kill(); renderer.current = undefined; }, []);

  useEffect(() => {
    const sigma = renderer.current; const node = container.current;
    if (!sigma || !node || !ready || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry.contentRect.width > 0 && entry.contentRect.height > 0) sigma.refresh();
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, [ready]);

  // Updating selection/filters refreshes display reducers; topology and coordinates stay fixed.
  useEffect(() => {
    const sigma = renderer.current; if (!sigma || !ready) return;
    const graph = sigma.getGraph(); const nodes = new Map(data.nodes.map(n => [n.id, n])); const edges = new Map(data.edges.map(e => [e.id, e]));
    const visibleNodes = new Set(visible.nodes.map(n => n.id)); const visibleEdges = new Set(visible.edges.map(e => e.id));
    const context = selectionContext(visible, selection);
    sigma.setSettings({
      defaultDrawNodeLabel: dark ? drawDarkGraphLabel : drawGraphLabel,
      defaultDrawNodeHover: dark ? drawDarkGraphHover : drawGraphHover,
      edgeLabelColor: { color: dark ? '#c0d8c9' : '#526974' },
      nodeReducer: (id, attrs) => { const node = nodes.get(id); const selected = selection.node === id && context.active; const related = context.nodes.has(id); const color = groupColor(node ? groupKey(node, kind) : id); const bright = dark ? '#' + [1, 3, 5].map(start => Math.round(parseInt(color.slice(start, start + 2), 16) * .6 + 255 * .4).toString(16).padStart(2, '0')).join('') : color; return { ...attrs, hidden: !visibleNodes.has(id), label: !context.active || related || (!!selection.edge && visible.nodes.length <= 40) ? node?.label ?? '' : '', color: context.active && !related ? dark ? '#789889' : '#d6dfdf' : bright, size: selected ? 13 : related ? 10 : 9, highlighted: selected, forceLabel: selected || (!context.active && visible.nodes.length <= 6), zIndex: selected ? 3 : related ? 2 : 1 }; },
      edgeReducer: (id, attrs) => { const edge = edges.get(id); const related = context.edges.has(id); return { ...attrs, hidden: !visibleEdges.has(id), color: dark ? context.active ? related ? '#9dd7b5' : '#354e42' : '#668878' : context.active ? related ? '#236c56' : '#e2e8e8' : '#617a73', size: related ? 2.7 : 1.6, label: (context.active ? related : showEdgeLabels) ? edge?.label ?? '' : '', forceLabel: related || showEdgeLabels, zIndex: related ? 2 : 1 }; },
      inertiaDuration: reduced ? 0 : 200, zoomDuration: reduced ? 0 : 200, doubleClickZoomingDuration: reduced ? 0 : 200,
    });
    // Sigma's reducer uses full node attributes, including its original x/y coordinates.
    if (graph.order) sigma.refresh();
  }, [data, visible, kind, selection, reduced, ready, showEdgeLabels, dark]);

  const move = (state: { x?: number; y?: number; ratio?: number; angle?: number }) => { const camera = renderer.current?.getCamera(); if (!camera) return; if (reduced) camera.setState(state); else void camera.animate(state, { duration: 220 }); };
  const fit = (ids: string[]) => {
    const points = ids.map(id => renderer.current?.getNodeDisplayData(id)).filter(p => !!p);
    if (!points.length) return;
    const minX = Math.min(...points.map(p => p.x)); const maxX = Math.max(...points.map(p => p.x)); const minY = Math.min(...points.map(p => p.y)); const maxY = Math.max(...points.map(p => p.y));
    move({ x: (minX + maxX) / 2, y: (minY + maxY) / 2, angle: 0, ratio: Math.max(0.18, Math.min(2, Math.max(maxX - minX, maxY - minY) * 1.25)) });
  };
  const handleKey = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.target !== event.currentTarget || disabled) return;
    if (event.key === 'Escape') { onClear(); return; }
    const node = visible.nodes.find(item => item.id === selection.node);
    const edge = visible.edges.find(item => item.id === selection.edge);
    if (event.key === 'Enter') { if (node) onOpen?.(node); else if (edge) onOpenEdge?.(edge); event.preventDefault(); return; }
    const direction: Record<string, [number, number]> = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
    const delta = direction[event.key]; const sigma = renderer.current;
    if (!delta || !sigma || !ready || event.ctrlKey || event.metaKey || event.altKey) return;
    if (!node) { if (visible.nodes[0]) { event.preventDefault(); onSelect(visible.nodes[0]); } return; }
    event.preventDefault();
    if (pins.includes(node.id)) return;
    sigma.setCustomBBox(sigma.getCustomBBox() ?? sigma.getBBox());
    const point = sigma.graphToViewport(sigma.getGraph().getNodeAttributes(node.id) as Position);
    const step = event.shiftKey ? 48 : 16;
    const position = sigma.viewportToGraph({ x: point.x + delta[0] * step, y: point.y + delta[1] * step });
    sigma.getGraph().mergeNodeAttributes(node.id, position);
    positionsStore.set(node.id, { id: node.id, ...position });
  };
  const selectedIds = [...selectionContext(visible, selection).nodes];
  return <><div ref={container} className="graph-canvas" role="group" tabIndex={0} aria-label="授权子图画布" aria-describedby="graph-interaction-help" onKeyDown={handleKey} />
    <div className="graph-controls" role="toolbar" aria-label="图谱视图控制"><button type="button" className="secondary" aria-label="放大图谱" disabled={!ready || !!error} onClick={() => move({ ratio: Math.max(0.08, (renderer.current?.getCamera().ratio ?? 1) / 1.4) })}>＋</button><span aria-label="图谱缩放比例">{zoom}%</span><button type="button" className="secondary" aria-label="缩小图谱" disabled={!ready || !!error} onClick={() => move({ ratio: Math.min(3, (renderer.current?.getCamera().ratio ?? 1) * 1.4) })}>−</button><button type="button" className="secondary" disabled={!ready || !!error || !visible.nodes.length} onClick={() => fit(visible.nodes.map(n => n.id))}><GraphIcon name="fit" />适配可见图</button><button type="button" className="secondary" disabled={!ready || !!error || !selectedIds.length} onClick={() => fit(selectedIds)}><GraphIcon name="focus" />定位所选</button><button type="button" className="secondary" disabled={!ready || !!error} aria-pressed={showEdgeLabels} onClick={() => setShowEdgeLabels(value => !value)}>关系名称</button><span className="graph-canvas-hint">拖动节点 · {openOnClick ? '单击编辑' : '双击查看'}</span></div>
    {!visible.nodes.length && <div className="graph-no-match">当前筛选没有可见节点，可清除筛选。</div>}
    {error ? <div className="graph-fallback"><ErrorNotice error={error} /><p>下方节点与关系列表仍可使用。</p></div> : null}
  </>;
}
