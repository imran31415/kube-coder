import { useEffect } from 'preact/hooks';
import {
  missionPulse,
  missionError,
  missionCards,
  missionFilter,
  missionKindFilter,
  filteredMissionCards,
  startMissionPolling,
  stopMissionPolling,
  type MissionKindFilter,
} from '../../store/mission';
import type { MissionState } from '../../api/mission';
import { GuidePanel } from '../../components/GuidePanel';
import { Icon } from '../../components/Icon';
import { Input } from '../../components/primitives/Input';
import { EmptyState } from '../../components/primitives/EmptyState';
import { MissionCard } from './MissionCard';
import './mission.css';

// Priority order: Waiting on you leads — it's the column that needs a human.
const COLUMNS: { state: MissionState; label: string }[] = [
  { state: 'waiting', label: 'Waiting on you' },
  { state: 'running', label: 'Running' },
  { state: 'review', label: 'Needs review' },
  { state: 'done', label: 'Done' },
];

const KIND_CHIPS: { value: MissionKindFilter; label: string }[] = [
  { value: 'all', label: 'All kinds' },
  { value: 'build', label: 'Builds' },
  { value: 'chat', label: 'Chats' },
  { value: 'subagent', label: 'Sub-agents' },
];

/** "14m" / "2h" wait-age label for the pulse strip. */
function waitLabel(s: number): string {
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${Math.floor(s / 3600)}h`;
}

export function MissionRoute() {
  useEffect(() => {
    startMissionPolling(10000);
    return () => stopMissionPolling();
  }, []);

  const pulse = missionPulse.value;
  const cards = filteredMissionCards.value;
  const hasAny = missionCards.value.length > 0;
  const filtering = missionFilter.value.trim() !== '' || missionKindFilter.value !== 'all';

  return (
    <div class="route route-mission">
      <header class="route-header">
        <h1 class="route-title">Mission Control</h1>
        <p class="route-subtitle muted">
          Every agent across builds, chats and sub-agents — one queue.
        </p>
      </header>

      <GuidePanel
        title="How Mission Control works"
        storageKey="kc.guide.mission"
        intro="One board for everything the workspace is doing. Cards flow right as agents finish; the left column is where your attention pays off."
        steps={[
          {
            title: 'Waiting on you comes first',
            body: 'Agents paused on a permission menu or question land here, amber-edged. Answer with the inline quick-reply buttons — no need to open the session.',
          },
          {
            title: 'Running shows live work',
            body: 'Builds, hypervisor chats and spawned sub-agents currently executing, each with a one-line headline of what it is doing right now.',
          },
          {
            title: 'Needs review, then Done',
            body: 'Completed builds park under Needs review; errors, kills and idle chats settle in Done. Open any card to see the full session.',
          },
          {
            title: 'Filter to focus',
            body: 'Chips narrow by kind (builds / chats / sub-agents); the text box matches repo, branch, title or headline.',
          },
        ]}
      />

      <div class="mission-toolbar">
        <div class="mission-chips" role="group" aria-label="Filter by kind">
          {KIND_CHIPS.map((c) => (
            <button
              key={c.value}
              type="button"
              class={`mission-chip ${missionKindFilter.value === c.value ? 'mission-chip-on' : ''}`}
              aria-pressed={missionKindFilter.value === c.value}
              onClick={() => (missionKindFilter.value = c.value)}
            >
              {c.label}
            </button>
          ))}
        </div>
        <Input
          class="mission-filter"
          placeholder="Filter by repo, branch, headline…"
          value={missionFilter.value}
          onInput={(e) => (missionFilter.value = (e.target as HTMLInputElement).value)}
          aria-label="Filter cards by text"
        />
      </div>

      {pulse && (
        <div class="mission-pulse">
          <span><b>{pulse.running}</b> running</span>
          <span class={pulse.waiting > 0 ? 'mission-pulse-warn' : ''}>
            <b>{pulse.waiting}</b> waiting on you
          </span>
          <span><b>{pulse.review}</b> needs review</span>
          <span><b>{pulse.done_today}</b> done today</span>
          {pulse.oldest_wait_s > 0 && (
            <span>oldest wait <b class="mission-pulse-warn">{waitLabel(pulse.oldest_wait_s)}</b></span>
          )}
        </div>
      )}

      {missionError.value && (
        <div class="mission-error" role="alert">{missionError.value}</div>
      )}

      {!hasAny ? (
        <EmptyState
          icon={<Icon name="mission" size={24} />}
          title="No agents on the board"
          description="Start a build or a chat and it will appear here."
        />
      ) : cards.length === 0 && filtering ? (
        <EmptyState title="No matches" description="Try clearing the filters." />
      ) : (
        <div class="mission-board">
          {COLUMNS.map((col) => {
            const colCards = cards.filter((c) => c.state === col.state);
            return (
              <section class="mission-col" key={col.state} aria-label={col.label}>
                <div class="mission-col-h">
                  <span class={`mission-col-dot mission-col-dot-${col.state}`} aria-hidden="true" />
                  {col.label} <span class="mission-col-count">{colCards.length}</span>
                </div>
                <div class="mission-col-cards">
                  {colCards.map((c) => (
                    <MissionCard key={c.id} card={c} />
                  ))}
                  {colCards.length === 0 && <div class="mission-col-empty muted">Empty</div>}
                </div>
              </section>
            );
          })}
        </div>
      )}
    </div>
  );
}
