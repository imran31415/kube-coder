import { Icon } from '../../components/Icon';
import { navigate } from '../../store/router';
import { brief, briefLoading, selectedProjectId } from '../../store/projects';
import type { BriefMemory } from '../../api/projects';

/**
 * The deterministic project-brief panel (#466). Renders the /brief JSON —
 * north star, goals, decision log, recent tasks, memories, git — with zero LLM
 * calls, so the page is useful even when the chat is idle. Tasks deep-link to
 * /tasks/<id>; decisions/goals/memories deep-link to the Memory tab.
 */

function Section({
  title,
  count,
  children,
}: {
  title: string;
  count?: number;
  children: preact.ComponentChildren;
}) {
  return (
    <section class="cto-brief-section">
      <h3 class="cto-brief-h">
        {title}
        {count != null && count > 0 && <span class="cto-brief-count">{count}</span>}
      </h3>
      {children}
    </section>
  );
}

function memoryHref(m: BriefMemory): string {
  // Deep-link to the Memory tab, scoped to the record's namespace so the user
  // lands near it. A record-precise anchor can come later.
  return `/memory?namespace=${encodeURIComponent(m.namespace)}`;
}

/** Short absolute date (e.g. "Jul 26") for the decision-log timeline (#467). */
function shortDate(epochSeconds: number | null): string {
  if (!epochSeconds) return '';
  try {
    return new Date(epochSeconds * 1000).toLocaleDateString(undefined, {
      month: 'short',
      day: 'numeric',
    });
  } catch {
    return '';
  }
}

function MemoryRow({ m, withDate = false }: { m: BriefMemory; withDate?: boolean }) {
  const date = withDate ? shortDate(m.updated_at) : '';
  return (
    <li class="cto-brief-item">
      {date && <span class="cto-brief-date">{date}</span>}
      <a
        href={memoryHref(m)}
        class="cto-brief-link"
        onClick={(e) => {
          e.preventDefault();
          navigate('/memory');
        }}
        title={`${m.namespace}/${m.key}`}
      >
        {m.value}
      </a>
    </li>
  );
}

export function BriefPanel() {
  const b = brief.value;
  const loading = briefLoading.value && !b;

  if (selectedProjectId.value === null) {
    return (
      <aside class="cto-brief" aria-label="Project brief">
        <div class="cto-brief-empty">
          <p class="cto-brief-empty-lead">Workspace scope</p>
          <p class="cto-brief-empty-sub">
            The CTO can see everything under <code>/home/dev</code>. Pick a
            project on the left for its brief, or just start talking — describe
            what you want to build and your CTO will set it up.
          </p>
        </div>
      </aside>
    );
  }

  if (loading) {
    return (
      <aside class="cto-brief" aria-label="Project brief" aria-busy="true">
        <div class="cto-brief-skel cto-skel" aria-hidden="true" />
        <div class="cto-brief-skel cto-skel" aria-hidden="true" />
        <div class="cto-brief-skel cto-skel" aria-hidden="true" />
      </aside>
    );
  }

  if (!b) {
    return (
      <aside class="cto-brief" aria-label="Project brief">
        <div class="cto-brief-empty">
          <p class="cto-brief-empty-sub">No brief available.</p>
        </div>
      </aside>
    );
  }

  const p = b.project;
  // A freshly-discovered project with nothing captured yet — invite instead of
  // showing a wall of zeros (design review #6).
  const isFresh =
    !p.north_star &&
    b.goals.length === 0 &&
    b.decisions.length === 0 &&
    b.memories.length === 0 &&
    b.tasks.total === 0;
  return (
    <aside class="cto-brief" aria-label="Project brief">
      <header class="cto-brief-top">
        <span class="cto-eyebrow">Brief</span>
        <h2 class="cto-brief-title">{p.name}</h2>
        {p.repo && <span class="cto-brief-repo">{p.repo}</span>}
      </header>

      {isFresh && (
        <p class="cto-brief-fresh">
          Nothing captured yet. Talk to your CTO to set a north star, capture
          decisions, and break goals into tasks — they'll show up here.
        </p>
      )}

      {p.north_star && (
        <Section title="North star">
          <p class="cto-brief-northstar">{p.north_star}</p>
        </Section>
      )}

      {!isFresh && (
      <Section title="Now">
        <div class="cto-brief-stats">
          <span class="cto-stat">
            <strong>{b.tasks.running}</strong> running
          </span>
          <span class="cto-stat cto-stat-waiting">
            <strong>{b.tasks.waiting}</strong> waiting
          </span>
          <span class="cto-stat">
            <strong>{b.tasks.total}</strong> tasks
          </span>
        </div>
      </Section>
      )}

      {b.goals.length > 0 && (
        <Section title="Goals" count={b.counts.goals}>
          <ul class="cto-brief-list">
            {b.goals.map((g) => (
              <MemoryRow key={`${g.namespace}/${g.key}`} m={g} />
            ))}
          </ul>
        </Section>
      )}

      {b.decisions.length > 0 && (
        <Section title="Decision log" count={b.counts.decisions}>
          <ul class="cto-brief-list cto-brief-decisions">
            {b.decisions.map((d) => (
              <MemoryRow key={`${d.namespace}/${d.key}`} m={d} withDate />
            ))}
          </ul>
        </Section>
      )}

      {b.tasks.recent.length > 0 && (
        <Section title="Recent tasks">
          <ul class="cto-brief-list">
            {b.tasks.recent.map((t) => (
              <li key={t.task_id} class="cto-brief-item">
                <a
                  href={`/tasks/${encodeURIComponent(t.task_id)}`}
                  class="cto-brief-link cto-brief-task"
                  onClick={(e) => {
                    e.preventDefault();
                    navigate(`/tasks/${encodeURIComponent(t.task_id)}`);
                  }}
                  title={t.prompt}
                >
                  <span class={`cto-dot cto-dot-${t.status}`} aria-hidden="true" />
                  <span class="cto-brief-task-text">{t.prompt || '(task)'}</span>
                </a>
              </li>
            ))}
          </ul>
        </Section>
      )}

      {b.memories.length > 0 && (
        <Section title="Memories" count={b.counts.memories}>
          <ul class="cto-brief-list">
            {b.memories.map((m) => (
              <MemoryRow key={`${m.namespace}/${m.key}`} m={m} />
            ))}
          </ul>
        </Section>
      )}

      {b.git.length > 0 && (
        <Section title="Git">
          <ul class="cto-brief-list cto-brief-git">
            {b.git.map((g) => (
              <li key={g.workdir} class="cto-brief-item cto-brief-gitrow">
                <Icon name="github" size={12} />
                <span class="cto-brief-branch">{g.branch || (g.exists ? '—' : 'missing')}</span>
                <span class="cto-brief-workdir">{g.workdir}</span>
              </li>
            ))}
          </ul>
        </Section>
      )}
    </aside>
  );
}
