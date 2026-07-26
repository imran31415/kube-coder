import { useEffect, useRef, useState } from 'preact/hooks';
import { Icon } from '../../components/Icon';
import { Pill } from '../../components/primitives/Pill';
import { EmptyState } from '../../components/primitives/EmptyState';
import { BottomSheet } from '../../components/BottomSheet';
import { useMediaQuery } from '../../hooks/useMediaQuery';
import { serverMode } from '../../store/server-mode';
import { Chat } from '../hypervisor/Chat';
import {
  activeThreadId,
  activeStatus,
  initHypervisor,
  refreshThreads,
  openThread,
  newChat,
  closeThread,
  sendMessage,
  sending,
  threads,
  setChatContext,
} from '../../store/hypervisor';
import {
  projects,
  selectedProjectId,
  brief,
  lastSeenBaseline,
  selectProject,
  initCto,
  refreshProjects,
  startProjectsPolling,
  stopProjectsPolling,
} from '../../store/projects';
import { ctoHandoff } from '../../store/feed';
import { ProjectRail } from './ProjectRail';
import { BriefPanel } from './BriefPanel';
import {
  clampCtoRailW,
  initialCtoRailW,
  CTO_RAIL_W_KEY,
  CTO_RAIL_W_DEFAULT,
  CTO_RAIL_W_MIN,
  CTO_RAIL_W_MAX,
} from './railSplit';
import './cto.css';

const STATUS_TONE: Record<string, 'neutral' | 'success' | 'warn' | 'danger'> = {
  running: 'success',
  error: 'danger',
  idle: 'neutral',
};

/** Deterministic starter chips — no LLM call fires until one is tapped (F6). */
function starterChips(projectName: string | null): string[] {
  return [
    'What should I focus on?',
    projectName ? `Where is ${projectName} at?` : 'What are we building?',
    'Break a goal into tasks',
  ];
}

export function CtoRoute() {
  // The 3-pane layout collapses to stacked (rail strip + chat + brief sheet)
  // at 860px — the SAME threshold as cto.css, so the split handle + desktop
  // brief aside never render in the cramped 721–860px band (design review #1).
  const narrow = useMediaQuery('(max-width: 860px)');
  const disabled = serverMode.value.ctoEnabled === false;
  const [briefOpen, setBriefOpen] = useState(false);
  const [deltaDismissed, setDeltaDismissed] = useState<Set<string>>(new Set());

  // Draggable rail↔chat splitter (desktop), mirroring the hypervisor sidebar.
  const rootRef = useRef<HTMLDivElement | null>(null);
  const [railW, setRailW] = useState<number>(() => {
    try {
      return initialCtoRailW(localStorage.getItem(CTO_RAIL_W_KEY));
    } catch {
      return CTO_RAIL_W_DEFAULT;
    }
  });
  const [dragging, setDragging] = useState(false);
  const railWRef = useRef(railW);
  railWRef.current = railW;
  // A "Discuss with CTO" handoff from the Feed (#470): its context prefix is
  // queued here and sent once the target project's thread is ready.
  const pendingHandoff = useRef<string | null>(null);

  function persistRailW(px: number) {
    try {
      localStorage.setItem(CTO_RAIL_W_KEY, String(Math.round(px)));
    } catch {
      /* noop */
    }
  }

  // Mount: enter the CTO chat surface, load config + threads, start the project
  // registry (discovery auto-provisions), and poll. Reset everything on leave
  // so the plain Chat tab is never left in CTO mode.
  useEffect(() => {
    if (disabled) return;
    setChatContext('cto', null);
    void initHypervisor();
    startProjectsPolling();
    // A Feed handoff overrides the usual most-active auto-select: jump straight
    // to the item's project and queue its context prefix to send.
    const handoff = ctoHandoff.value;
    if (handoff) {
      ctoHandoff.value = null;
      pendingHandoff.current = handoff.text;
      void refreshProjects().then(() => selectProject(handoff.projectId || null));
    } else {
      void initCto();
    }
    return () => {
      setChatContext('', null);
      closeThread();
      stopProjectsPolling();
    };
  }, [disabled]);

  // React to the selected project: rebind the chat context, re-filter the
  // thread list to this project, and continue its latest thread (or a fresh
  // one). Runs on auto-select and on every rail click.
  const selId = selectedProjectId.value;
  useEffect(() => {
    if (disabled) return;
    setChatContext('cto', selId);
    let cancelled = false;
    void refreshThreads().then(() => {
      if (cancelled) return;
      const list = threads.value;
      if (list.length) void openThread(list[0].id);
      else newChat();
      // Deliver a queued Feed handoff into the now-ready thread (continues the
      // latest, or seeds a new one) — the deterministic context prefix.
      const text = pendingHandoff.current;
      if (text) {
        pendingHandoff.current = null;
        void sendMessage(text);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [selId, disabled]);

  const active = activeThreadId.value;
  const status = activeStatus.value;
  const selectedProject = projects.value.find((p) => p.id === selId) ?? null;
  const projectName = selectedProject?.name ?? null;

  // Returning-visit delta strip: computed deterministically from the brief vs.
  // the last_seen_at baseline captured when this project was selected.
  const b = brief.value;
  const baseline = selId ? lastSeenBaseline.value[selId] ?? null : null;
  const newDecisions =
    b && baseline
      ? b.decisions.filter((d) => (d.updated_at ?? 0) > baseline).length
      : 0;
  const waiting = b?.tasks.waiting ?? 0;
  // Dismissal is per-project (design review #4) so hiding A's strip never
  // suppresses B's. The Workspace scope (selId null) has no baseline anyway.
  const showDelta =
    !!selId &&
    !deltaDismissed.has(selId) &&
    baseline != null &&
    (waiting > 0 || newDecisions > 0);

  function chip(text: string) {
    void sendMessage(text);
  }

  const chips = starterChips(projectName);

  const brandLine = 'Engineering judgment for your whole workspace';

  // Deep-linked to /cto on a deployment where the feature is off (#467).
  if (disabled) {
    return (
      <div class="route route-cto route-cto-disabled">
        <EmptyState
          icon={<Icon name="cto" size={26} />}
          title="AI CTO is disabled"
          description={
            <>
              Enable it in the workspace chart (<code>cto.enabled</code>). It
              rides the Hypervisor, so <code>hypervisor.enabled</code> must be on
              too.
            </>
          }
        />
      </div>
    );
  }

  return (
    <div
      ref={rootRef}
      class={`route route-cto ${dragging ? 'cto-split-dragging' : ''}`}
      style={
        !narrow
          ? { gridTemplateColumns: `${railW}px 6px minmax(0, 1fr) 320px` }
          : undefined
      }
    >
      <ProjectRail selectedId={selId} onSelect={(id) => void selectProject(id)} />

      {!narrow && (
        <div
          class="cto-split-handle"
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize projects rail — drag, arrow keys, or double-click to reset"
          aria-valuenow={Math.round(railW)}
          aria-valuemin={CTO_RAIL_W_MIN}
          aria-valuemax={CTO_RAIL_W_MAX}
          tabIndex={0}
          title="Drag to resize · double-click to reset"
          onPointerDown={(e: PointerEvent) => {
            e.preventDefault();
            (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
            setDragging(true);
          }}
          onPointerMove={(e: PointerEvent) => {
            if (!dragging) return;
            const el = rootRef.current;
            if (!el) return;
            setRailW(clampCtoRailW(e.clientX - el.getBoundingClientRect().left));
          }}
          onPointerUp={() => {
            if (!dragging) return;
            setDragging(false);
            persistRailW(railWRef.current);
          }}
          onPointerCancel={() => {
            if (!dragging) return;
            setDragging(false);
            persistRailW(railWRef.current);
          }}
          onDblClick={() => {
            setRailW(CTO_RAIL_W_DEFAULT);
            persistRailW(CTO_RAIL_W_DEFAULT);
          }}
          onKeyDown={(e: KeyboardEvent) => {
            if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
            e.preventDefault();
            const next = clampCtoRailW(railW + (e.key === 'ArrowLeft' ? -16 : 16));
            setRailW(next);
            persistRailW(next);
          }}
        />
      )}

      <section class="cto-main">
        <header class="cto-topbar">
          <div class="cto-topbar-title">
            <span class="cto-masthead">AI CTO</span>
            <span class="cto-brand">{projectName ? projectName : brandLine}</span>
          </div>
          <div class="cto-topbar-meta">
            {active && status && (
              <Pill tone={STATUS_TONE[status] ?? 'neutral'}>
                {status === 'running' ? 'thinking' : status || 'idle'}
              </Pill>
            )}
            {narrow && (
              <button
                type="button"
                class="cto-brief-toggle"
                onClick={() => setBriefOpen(true)}
                title="Project brief"
                aria-label="Open project brief"
              >
                <Icon name="mission" size={15} />
                Brief
              </button>
            )}
          </div>
        </header>

        {showDelta && (
          <div class="cto-delta" role="status">
            <span class="cto-delta-text">
              Since your last visit:
              {waiting > 0 && <strong> {waiting} waiting on you</strong>}
              {waiting > 0 && newDecisions > 0 && ' ·'}
              {newDecisions > 0 && <strong> {newDecisions} new decision{newDecisions === 1 ? '' : 's'}</strong>}
            </span>
            <button
              type="button"
              class="cto-delta-close"
              onClick={() =>
                selId &&
                setDeltaDismissed((prev) => new Set(prev).add(selId))
              }
              aria-label="Dismiss"
            >
              <Icon name="close" size={12} />
            </button>
          </div>
        )}

        {!active && (
          <div class="cto-welcome">
            <p class="cto-welcome-lead">
              {projectName
                ? `I'm across ${projectName}. What do you want to move on?`
                : "I already know your workspace. What do you want to build or move on?"}
            </p>
            <div class="cto-chips">
              {chips.map((c) => (
                <button
                  key={c}
                  type="button"
                  class="cto-chip"
                  disabled={sending.value}
                  onClick={() => chip(c)}
                >
                  {c}
                </button>
              ))}
            </div>
          </div>
        )}

        <Chat hideEmptyState />
      </section>

      {!narrow && <BriefPanel />}

      {narrow && (
        <BottomSheet open={briefOpen} onClose={() => setBriefOpen(false)} title="Project brief" initialSnap="full">
          <BriefPanel />
        </BottomSheet>
      )}
    </div>
  );
}
