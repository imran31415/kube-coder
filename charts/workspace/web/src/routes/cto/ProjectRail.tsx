import { useState } from 'preact/hooks';
import { Icon } from '../../components/Icon';
import { PopoverMenu, PopoverItem } from '../../components/PopoverMenu';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { projects, projectsLoading, archiveProject } from '../../store/projects';
import type { Project } from '../../api/projects';

/**
 * The AI CTO projects rail (#466). Cards come from GET /api/projects with the
 * pulse counts embedded there (no N×/brief fan-out). A always-present
 * **Workspace** root scope keeps the page usable with zero registered projects.
 * Per the UX addendum there is NO "New project" button — projects are born by
 * discovery or conversation; wrong guesses are archived from the card overflow.
 */

interface Props {
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  /** Render as a narrow icon strip instead of the full card list (#530). */
  collapsed?: boolean;
  /** Supplied on desktop only — mobile stacks the rail and never collapses it. */
  onToggleCollapse?: () => void;
}

/** Up to two letters for a collapsed rail avatar: the initials of the first two
 *  words ("kube-coder" → "KC"), or the first two letters of a single word. */
export function projectInitials(name: string): string {
  const parts = (name || '').split(/[^A-Za-z0-9]+/).filter(Boolean);
  if (parts.length === 0) return '?';
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return `${parts[0][0]}${parts[1][0]}`.toUpperCase();
}

/** The rail's collapse/expand chevron — labelled for both states so a screen
 *  reader hears what the control does, not just where it is. */
function RailToggle({ collapsed, onToggle }: { collapsed: boolean; onToggle: () => void }) {
  const label = collapsed ? 'Expand projects rail' : 'Collapse projects rail';
  return (
    <button
      type="button"
      class="cto-rail-toggle"
      onClick={onToggle}
      aria-expanded={!collapsed}
      aria-controls="cto-project-rail"
      aria-label={label}
      title={label}
    >
      <Icon name={collapsed ? 'chevron-right' : 'chevron-left'} size={14} />
    </button>
  );
}

function pulseSummary(p: Project): string {
  const bits = [`${p.workdirs.length || 0} dir${p.workdirs.length === 1 ? '' : 's'}`];
  if (p.repo) bits.unshift('git');
  return bits.join(' · ');
}

function ProjectCard({
  project,
  selected,
  onSelect,
  onArchive,
}: {
  project: Project;
  selected: boolean;
  onSelect: () => void;
  onArchive: () => void;
}) {
  const running = project.pulse?.running ?? 0;
  const waiting = project.pulse?.waiting ?? 0;
  return (
    <div class={`cto-card ${selected ? 'cto-card-selected' : ''}`}>
      <button type="button" class="cto-card-open" onClick={onSelect} title={project.name}>
        <span class="cto-card-head">
          <span class="cto-card-name">{project.name}</span>
          <span class="cto-card-pulse" aria-hidden="true">
            {running > 0 && <span class="cto-dot cto-dot-running" title={`${running} running`} />}
            {waiting > 0 && <span class="cto-dot cto-dot-waiting" title={`${waiting} waiting`} />}
          </span>
        </span>
        <span class="cto-card-sub">{pulseSummary(project)}</span>
        {(running > 0 || waiting > 0) && (
          <span class="cto-card-counts">
            {running > 0 && <span>{running} running</span>}
            {waiting > 0 && <span class="cto-count-waiting">{waiting} waiting</span>}
          </span>
        )}
      </button>
      <div class="cto-card-menu">
        <PopoverMenu
          align="right"
          width={180}
          trigger={({ onClick, ref, 'aria-expanded': ex }) => (
            <button
              type="button"
              class="cto-card-menu-btn"
              onClick={onClick}
              ref={ref as (el: HTMLButtonElement | null) => void}
              aria-expanded={ex}
              aria-haspopup="menu"
              aria-label={`Actions for ${project.name}`}
              title="Project actions"
            >
              <Icon name="more" size={14} />
            </button>
          )}
        >
          {(close) => (
            <PopoverItem
              danger
              onClick={() => {
                onArchive();
                close();
              }}
            >
              Archive project
            </PopoverItem>
          )}
        </PopoverMenu>
      </div>
    </div>
  );
}

function SkeletonCard() {
  // Hairline shimmer, never a spinner (UX F4) — the "meeting your workspace" beat.
  return (
    <div class="cto-card cto-card-skeleton" aria-hidden="true">
      <span class="cto-skel cto-skel-name" />
      <span class="cto-skel cto-skel-sub" />
    </div>
  );
}

export function ProjectRail({ selectedId, onSelect, collapsed = false, onToggleCollapse }: Props) {
  const list = projects.value.filter((p) => p.status !== 'archived');
  const loading = projectsLoading.value && projects.value.length === 0;
  // Archive is confirmed (design review #2) — it's the only way to undo a wrong
  // discovery guess, so a stray tap shouldn't silently drop a project.
  const [pendingArchive, setPendingArchive] = useState<Project | null>(null);

  // Collapsed (#530): an icon strip of initials with tooltips. Selecting still
  // works; archiving doesn't — that lives in the expanded card's overflow.
  if (collapsed) {
    return (
      <nav id="cto-project-rail" class="cto-rail cto-rail-collapsed" aria-label="Projects">
        <div class="cto-rail-head">
          {onToggleCollapse && <RailToggle collapsed onToggle={onToggleCollapse} />}
        </div>
        <div class="cto-rail-list">
          <button
            type="button"
            class={`cto-icon-card ${selectedId === null ? 'cto-icon-card-selected' : ''}`}
            onClick={() => onSelect(null)}
            aria-label="Workspace"
            aria-current={selectedId === null ? 'true' : undefined}
            title="Workspace — everything in your workspace"
          >
            <Icon name="desktop" size={15} />
          </button>
          {list.map((p) => {
            const running = p.pulse?.running ?? 0;
            const waiting = p.pulse?.waiting ?? 0;
            return (
              <button
                key={p.id}
                type="button"
                class={`cto-icon-card ${selectedId === p.id ? 'cto-icon-card-selected' : ''}`}
                onClick={() => onSelect(p.id)}
                aria-label={p.name}
                aria-current={selectedId === p.id ? 'true' : undefined}
                title={p.name}
              >
                <span class="cto-icon-initials" aria-hidden="true">
                  {projectInitials(p.name)}
                </span>
                {(running > 0 || waiting > 0) && (
                  <span class="cto-icon-pulse" aria-hidden="true">
                    <span class={`cto-dot ${running > 0 ? 'cto-dot-running' : 'cto-dot-waiting'}`} />
                  </span>
                )}
              </button>
            );
          })}
        </div>
      </nav>
    );
  }

  return (
    <nav id="cto-project-rail" class="cto-rail" aria-label="Projects">
      <div class="cto-rail-head">
        <span class="cto-eyebrow">Projects</span>
        {onToggleCollapse && <RailToggle collapsed={false} onToggle={onToggleCollapse} />}
      </div>

      <div class="cto-rail-list">
        {/* Always-present Workspace root scope — the CTO is functional from
            message one even with zero registered projects (UX F3). */}
        <div class={`cto-card cto-card-workspace ${selectedId === null ? 'cto-card-selected' : ''}`}>
          <button type="button" class="cto-card-open" onClick={() => onSelect(null)}>
            <span class="cto-card-head">
              <span class="cto-card-name">
                <Icon name="desktop" size={13} /> Workspace
              </span>
            </span>
            <span class="cto-card-sub">Everything in your workspace</span>
          </button>
        </div>

        {loading && (
          <>
            <SkeletonCard />
            <SkeletonCard />
            <SkeletonCard />
          </>
        )}

        {list.map((p, i) => (
          <div key={p.id} class="cto-card-wrap" style={{ '--i': i } as Record<string, number>}>
            <ProjectCard
              project={p}
              selected={selectedId === p.id}
              onSelect={() => onSelect(p.id)}
              onArchive={() => setPendingArchive(p)}
            />
          </div>
        ))}
      </div>

      <p class="cto-rail-foot">Starting something new? Just tell your CTO.</p>

      <ConfirmDialog
        open={pendingArchive !== null}
        title={`Archive ${pendingArchive?.name ?? 'project'}?`}
        body="It drops out of the rail. Your tasks, memories, and git are untouched — re-discovered projects can come back."
        confirmLabel="Archive"
        destructive
        onConfirm={() => {
          const p = pendingArchive;
          setPendingArchive(null);
          if (p) void archiveProject(p.id);
        }}
        onCancel={() => setPendingArchive(null)}
      />
    </nav>
  );
}
