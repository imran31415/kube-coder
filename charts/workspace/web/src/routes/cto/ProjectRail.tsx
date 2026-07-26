import { Icon } from '../../components/Icon';
import { PopoverMenu, PopoverItem } from '../../components/PopoverMenu';
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
}: {
  project: Project;
  selected: boolean;
  onSelect: () => void;
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
              onClick={() => {
                void archiveProject(project.id);
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

export function ProjectRail({ selectedId, onSelect }: Props) {
  const list = projects.value.filter((p) => p.status !== 'archived');
  const loading = projectsLoading.value && projects.value.length === 0;

  return (
    <nav class="cto-rail" aria-label="Projects">
      <div class="cto-rail-head">
        <span class="cto-eyebrow">Projects</span>
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
            <span class="cto-card-sub">the whole /home/dev</span>
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
            <ProjectCard project={p} selected={selectedId === p.id} onSelect={() => onSelect(p.id)} />
          </div>
        ))}
      </div>

      <p class="cto-rail-foot">Starting something new? Just tell your CTO.</p>
    </nav>
  );
}
