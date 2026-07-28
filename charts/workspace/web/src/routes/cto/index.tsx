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
  seedCtoConfig,
  config as hvConfig,
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
import { justOnboarded } from '../../store/onboarding';
import { claudeReady, claudeProbed, refreshClaudeReady } from '../../store/claude';
import { ClaudeCredentialSetup } from '../../components/ClaudeCredentialSetup';
import { ProjectRail } from './ProjectRail';
import { CtoConfig } from './CtoConfig';
import { BriefPanel, BriefTab } from './BriefPanel';
import {
  clampCtoRailW,
  initialCtoRailW,
  ctoGridTemplate,
  readPaneCollapsed,
  writePaneCollapsed,
  resolvePaneCollapsed,
  CTO_RAIL_W_KEY,
  CTO_RAIL_W_DEFAULT,
  CTO_RAIL_W_MIN,
  CTO_RAIL_W_MAX,
  CTO_RAIL_COLLAPSED_KEY,
  CTO_BRIEF_COLLAPSED_KEY,
  CTO_AUTO_COLLAPSE_MAX,
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

/** First-win starter chips (#487) — build-first prompts for a just-onboarded
 *  user. Tapping one sends a one-sentence build request that the #486 fast-path
 *  builds immediately, with the #484/#485 preview auto-surfacing. */
const FIRST_WIN_CHIPS = [
  'Portfolio site',
  'To-do app',
  'Landing page',
];

/** Read a persisted pane-collapse choice, tolerating a blocked localStorage. */
function readPane(key: string): boolean | null {
  try {
    return readPaneCollapsed(localStorage.getItem(key));
  } catch {
    return null;
  }
}

export function CtoRoute() {
  // The 3-pane layout collapses to stacked (rail strip + chat + brief sheet)
  // at 860px — the SAME threshold as cto.css, so the split handle + desktop
  // brief aside never render in the cramped 721–860px band (design review #1).
  const narrow = useMediaQuery('(max-width: 860px)');
  // Auto-collapse band (#530): wide enough to render all three panes, too
  // narrow for the chat to be comfortable between them.
  const cramped = useMediaQuery(`(max-width: ${CTO_AUTO_COLLAPSE_MAX}px)`);
  const disabled = serverMode.value.ctoEnabled === false;
  // First-win landing (#487): a user routed here straight from onboarding gets
  // a warm build-first opener. Capture the transient flag at mount and clear
  // the signal so a later visit in the same session shows the normal welcome.
  const [firstWin] = useState(() => justOnboarded.value);
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

  // Collapsible side panes (#530). `null` = the user has never toggled this
  // pane, so the heuristics decide; an explicit choice is persisted and wins
  // from then on.
  const [railChoice, setRailChoice] = useState<boolean | null>(() => readPane(CTO_RAIL_COLLAPSED_KEY));
  const [briefChoice, setBriefChoice] = useState<boolean | null>(() => readPane(CTO_BRIEF_COLLAPSED_KEY));
  // Set once a message goes out: the conversation is now the focus, so the
  // brief steps aside until the user asks for it back.
  const [briefStoodAside, setBriefStoodAside] = useState(false);
  // A "Discuss with CTO" handoff from the Feed (#470): its context prefix is
  // queued here and sent once the target project's thread is ready.
  const pendingHandoff = useRef<string | null>(null);

  // Consume the one-shot first-win flag so it never re-triggers on a later
  // same-session visit (the captured `firstWin` local still drives this mount).
  useEffect(() => {
    if (justOnboarded.value) justOnboarded.value = false;
  }, []);

  function persistRailW(px: number) {
    try {
      localStorage.setItem(CTO_RAIL_W_KEY, String(Math.round(px)));
    } catch {
      /* noop */
    }
  }

  function persistPane(key: string, collapsed: boolean) {
    try {
      localStorage.setItem(key, writePaneCollapsed(collapsed));
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
    // First-win gate (#494): know whether Claude is connected so the welcome
    // can offer the connect panel instead of firing a doomed build.
    void refreshClaudeReady();
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

  // Seed the CTO's own assistant/model/effort from the selected project's stored
  // defaults (#483) — falling back to the workspace default when it has none.
  // Keyed on the three stored fields (plus the config's arrival, which is what
  // makes the per-assistant model/effort lists knowable) so switching projects
  // re-seeds, but a user's in-session pick isn't stomped on every render.
  const cfgLoaded = !!hvConfig.value;
  const pAssistant = selectedProject?.default_assistant ?? '';
  const pModel = selectedProject?.default_model ?? '';
  const pEffort = selectedProject?.default_effort ?? '';
  useEffect(() => {
    if (disabled || !cfgLoaded) return;
    seedCtoConfig({
      default_assistant: pAssistant,
      default_model: pModel,
      default_effort: pEffort,
    });
  }, [selId, pAssistant, pModel, pEffort, cfgLoaded, disabled]);

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

  // Once a message is on its way the chat is what matters — stand the brief
  // aside (heuristic only; an explicit toggle below still wins).
  const isSending = sending.value;
  useEffect(() => {
    if (isSending) setBriefStoodAside(true);
  }, [isSending]);

  // Stacked/mobile keeps its own layout: the rail is a horizontal strip and the
  // brief is a bottom sheet, so neither collapses there.
  const railCollapsed = !narrow && resolvePaneCollapsed(railChoice, cramped);
  const briefCollapsed =
    !narrow && resolvePaneCollapsed(briefChoice, cramped || briefStoodAside);

  function toggleRail() {
    const next = !railCollapsed;
    setRailChoice(next);
    persistPane(CTO_RAIL_COLLAPSED_KEY, next);
  }

  function toggleBrief() {
    const next = !briefCollapsed;
    setBriefChoice(next);
    persistPane(CTO_BRIEF_COLLAPSED_KEY, next);
    // Re-opening clears the stood-aside flag too, so the next send doesn't have
    // to fight a choice the user just made.
    if (!next) setBriefStoodAside(false);
  }

  const chips = firstWin ? FIRST_WIN_CHIPS : starterChips(projectName);
  const welcomeLead = firstWin
    ? "Welcome aboard — I'm your AI CTO. Tell me in one sentence what you'd like to build, and I'll get started right away."
    : projectName
      ? `I'm across ${projectName}. What do you want to move on?`
      : "I already know your workspace. What do you want to build or move on?";

  const brandLine = 'Engineering judgment for your whole workspace';

  // Keyless chip-flash (#500): the connect gate keys off `claudeReady === false`,
  // but the signal starts `null`, so a keyless user who deep-links to /cto used
  // to see live build chips for the length of the probe — tapping one in that
  // window fired a build that dies on a raw provider error. The chips stay inert
  // until the first probe *settles*; `claudeProbed` (not `claudeReady !== null`)
  // is the condition so a failed probe, which deliberately leaves the value
  // unknown, releases them rather than disabling them forever.
  const chipsPending = !claudeProbed.value;

  const welcome =
    claudeReady.value === false ? (
      <div class="cto-welcome">
        <p class="cto-welcome-lead">
          Connect Claude and I'll start building. Sign in with your Claude
          subscription, or paste an API key.
        </p>
        <div class="cto-connect">
          <ClaudeCredentialSetup
            ready={claudeReady.value}
            onConnected={() => void refreshClaudeReady()}
          />
        </div>
      </div>
    ) : (
      <div class="cto-welcome">
        <p class="cto-welcome-lead">{welcomeLead}</p>
        <div class="cto-chips" aria-busy={chipsPending}>
          {chips.map((c) => (
            <button
              key={c}
              type="button"
              class="cto-chip"
              disabled={sending.value || chipsPending}
              onClick={() => chip(c)}
            >
              {c}
            </button>
          ))}
        </div>
      </div>
    );

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
          ? { gridTemplateColumns: ctoGridTemplate({ railW, railCollapsed, briefCollapsed }) }
          : undefined
      }
    >
      <ProjectRail
        selectedId={selId}
        onSelect={(id) => void selectProject(id)}
        collapsed={railCollapsed}
        onToggleCollapse={narrow ? undefined : toggleRail}
      />

      {!narrow && !railCollapsed && (
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
            <span class="route-title cto-masthead">AI CTO</span>
            <span class="cto-brand">{projectName ? projectName : brandLine}</span>
          </div>
          <div class="cto-topbar-meta">
            {/* Per-project provider/model/effort (#483, #362). Compact by
                design — the CTO surface stays executive-clean and the dials
                live one tap behind the gear. */}
            <CtoConfig project={selectedProject} />
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

        {/* The welcome is handed to <Chat> rather than rendered above it, so it
            lands in the transcript's centring slot (#500) — opener, chips and
            composer then read as one hero instead of the opener pinning to the
            top with a dead gap above the composer. Chat renders it only while
            the thread is empty, which is also the old `!active` condition. */}
        <Chat hideEmptyState welcome={welcome} />
      </section>

      {!narrow &&
        (briefCollapsed ? (
          <BriefTab onExpand={toggleBrief} delta={showDelta} />
        ) : (
          <BriefPanel onCollapse={toggleBrief} />
        ))}

      {narrow && (
        <BottomSheet open={briefOpen} onClose={() => setBriefOpen(false)} title="Project brief" initialSnap="full">
          <BriefPanel />
        </BottomSheet>
      )}
    </div>
  );
}
