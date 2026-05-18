import { useEffect, useMemo, useRef, useState } from 'preact/hooks';
import {
  forceSimulation,
  forceManyBody,
  forceCenter,
  forceCollide,
  forceX,
  forceY,
  type Simulation,
  type SimulationNodeDatum,
} from 'd3-force';
import { select } from 'd3-selection';
import { drag } from 'd3-drag';
import { scaleOrdinal, scaleSqrt } from 'd3-scale';
import type { MemoryRecord } from '../../api/memory';

interface Node extends SimulationNodeDatum {
  id: number;
  ns: string;
  key: string;
  kind: string;
  importance: number;
  record: MemoryRecord;
}

interface Props {
  memories: MemoryRecord[];
  selectedId: number | null;
  onSelect: (m: MemoryRecord) => void;
}

const KIND_COLORS: Record<string, string> = {
  semantic: '#5eead4',
  episodic: '#fbbf24',
  procedural: '#60a5fa',
};
const DEFAULT_COLOR = '#adadb5';

function colorForKind(kind: string): string {
  return KIND_COLORS[kind] ?? DEFAULT_COLOR;
}

// Top-level namespace (e.g. "claude.home-dev.user_name" -> "claude").
function rootNamespace(ns: string): string {
  const i = ns.indexOf('.');
  return i === -1 ? ns : ns.slice(0, i);
}

export function MemoryGraph({ memories, selectedId, onSelect }: Props) {
  const wrapperRef = useRef<HTMLDivElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const simRef = useRef<Simulation<Node, undefined> | null>(null);
  const [hover, setHover] = useState<Node | null>(null);
  const [size, setSize] = useState<{ w: number; h: number }>({ w: 600, h: 480 });

  // Track wrapper size for responsive layout.
  useEffect(() => {
    const el = wrapperRef.current;
    if (!el || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver((entries) => {
      const r = entries[0]?.contentRect;
      if (r) setSize({ w: Math.max(280, r.width), h: Math.max(320, r.height) });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const { nodes, roots } = useMemo(() => {
    const ns = new Set<string>();
    const ns_list: Node[] = memories.map((m) => {
      ns.add(rootNamespace(m.namespace));
      return {
        id: m.id,
        ns: m.namespace,
        key: m.key,
        kind: m.kind,
        importance: m.importance,
        record: m,
      };
    });
    return { nodes: ns_list, roots: [...ns].sort() };
  }, [memories]);

  const radius = useMemo(
    () => scaleSqrt<number>().domain([0, 1]).range([6, 18]).clamp(true),
    [],
  );

  // Cluster nodes horizontally by root namespace. Keep cluster centers well
  // inside the viewport so the outermost dots aren't pinned to the edge.
  const xScale = useMemo(() => {
    if (roots.length === 0) return null;
    const padding = Math.max(80, size.w * 0.18);
    return scaleOrdinal<string, number>()
      .domain(roots)
      .range(
        roots.map((_, i) => {
          if (roots.length === 1) return size.w / 2;
          const step = (size.w - padding * 2) / (roots.length - 1);
          return padding + step * i;
        }),
      );
  }, [roots, size.w]);

  // Keep the latest onSelect in a ref so the click handler binding can stay
  // stable — otherwise every parent re-render (which passes a fresh arrow
  // function) would re-bind handlers, re-trigger the simulation effect, and
  // make the graph visibly "jump".
  const onSelectRef = useRef(onSelect);
  useEffect(() => {
    onSelectRef.current = onSelect;
  }, [onSelect]);

  // Build / update the simulation when DATA changes. Selection styling is
  // handled in a separate effect below so clicking a row doesn't tear the
  // simulation down and re-run the layout from scratch.
  useEffect(() => {
    if (!svgRef.current) return;
    const w = size.w;
    const h = size.h;

    // Preserve positions for nodes that already exist between renders, so
    // editing or refreshing doesn't blow the layout away.
    const prev = simRef.current?.nodes() ?? [];
    const prevById = new Map(prev.map((n) => [n.id, n]));
    for (const n of nodes) {
      const p = prevById.get(n.id);
      if (p) {
        n.x = p.x;
        n.y = p.y;
        n.vx = p.vx;
        n.vy = p.vy;
      }
    }

    const sim = forceSimulation<Node>(nodes)
      .force('charge', forceManyBody<Node>().strength(-60))
      .force(
        'collide',
        forceCollide<Node>().radius((d) => radius(d.importance) + 4),
      )
      .force(
        'x',
        forceX<Node>((d) => xScale?.(rootNamespace(d.ns)) ?? w / 2).strength(0.18),
      )
      .force('y', forceY<Node>(h / 2).strength(0.06))
      .force('center', forceCenter<Node>(w / 2, h / 2).strength(0.02))
      .alpha(0.9)
      .alphaDecay(0.04);

    simRef.current = sim;

    const svg = select(svgRef.current);
    const nodeSel = svg.selectAll<SVGGElement, Node>('g.mg-node').data(nodes, (d) => String(d.id));

    nodeSel.exit().remove();

    const enter = nodeSel
      .enter()
      .append('g')
      .attr('class', 'mg-node')
      .style('cursor', 'pointer');

    enter
      .append('circle')
      .attr('class', 'mg-dot')
      .attr('r', (d) => radius(d.importance))
      .attr('fill', (d) => colorForKind(d.kind))
      .attr('fill-opacity', 0.85)
      .attr('stroke', 'var(--surface)')
      .attr('stroke-width', 1.5);

    const merged = enter.merge(nodeSel as ReturnType<typeof enter.merge>);

    merged
      .on('click', (_, d) => onSelectRef.current(d.record))
      .on('mouseenter', (_, d) => setHover(d))
      .on('mouseleave', () => setHover(null));

    merged
      .select('circle.mg-dot')
      .attr('r', (d) => radius(d.importance))
      .attr('fill', (d) => colorForKind(d.kind));

    // Drag behavior.
    const dragBehavior = drag<SVGGElement, Node>()
      .on('start', (event, d) => {
        if (!event.active) sim.alphaTarget(0.25).restart();
        d.fx = d.x;
        d.fy = d.y;
      })
      .on('drag', (event, d) => {
        d.fx = event.x;
        d.fy = event.y;
      })
      .on('end', (event, d) => {
        if (!event.active) sim.alphaTarget(0);
        d.fx = null;
        d.fy = null;
      });

    merged.call(dragBehavior);

    sim.on('tick', () => {
      // Clamp positions so nodes stay fully inside the SVG.
      for (const d of nodes) {
        const r = radius(d.importance) + 2;
        if (d.x != null) d.x = Math.max(r, Math.min(w - r, d.x));
        if (d.y != null) d.y = Math.max(r + 24, Math.min(h - r, d.y));
      }
      merged.attr('transform', (d) => `translate(${d.x ?? 0}, ${d.y ?? 0})`);
    });

    return () => {
      sim.stop();
    };
  }, [nodes, size.w, size.h, xScale, radius]);

  // Selection-only styling: update stroke without rebuilding the simulation.
  useEffect(() => {
    if (!svgRef.current) return;
    const svg = select(svgRef.current);
    svg
      .selectAll<SVGCircleElement, Node>('circle.mg-dot')
      .attr('stroke', (d) => (d.id === selectedId ? 'var(--accent)' : 'var(--surface)'))
      .attr('stroke-width', (d) => (d.id === selectedId ? 2.5 : 1.5));
  }, [selectedId]);

  // Cluster labels (root namespaces) along the top.
  const clusterLabels = useMemo(() => {
    if (!xScale) return [];
    return roots.map((r) => ({ name: r, x: xScale(r) }));
  }, [roots, xScale]);

  return (
    <div class="mem-graph" ref={wrapperRef}>
      {nodes.length === 0 ? (
        <div class="mem-graph-empty muted">No memories to graph.</div>
      ) : (
        <>
          <svg
            ref={svgRef}
            width={size.w}
            height={size.h}
            viewBox={`0 0 ${size.w} ${size.h}`}
            role="img"
            aria-label="Memory graph"
          >
            <g class="mg-cluster-labels">
              {clusterLabels.map((c) => (
                <text key={c.name} x={c.x} y={18} class="mg-cluster-label" textAnchor="middle">
                  {c.name}
                </text>
              ))}
            </g>
          </svg>
          <div class="mem-graph-legend" aria-hidden="true">
            {(['semantic', 'episodic', 'procedural'] as const).map((k) => (
              <span class="mem-graph-legend-item" key={k}>
                <span class="mem-graph-swatch" style={{ background: colorForKind(k) }} />
                {k}
              </span>
            ))}
            <span class="mem-graph-legend-hint muted">size = importance · click a node to open</span>
          </div>
          {hover && (
            <div class="mem-graph-tooltip" role="status">
              <div class="mono mem-graph-tip-title">
                {hover.ns}.{hover.key}
              </div>
              <div class="muted mem-graph-tip-sub">
                {hover.kind} · importance {Math.round(hover.importance * 100)}%
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
