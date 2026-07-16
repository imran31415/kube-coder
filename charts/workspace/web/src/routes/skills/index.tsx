import { useEffect, useState } from 'preact/hooks';
import {
  skills,
  filteredSkills,
  skillsFilter,
  skillsSystemFacet,
  skillsScopeFacet,
  skillSystems,
  skillScopes,
  divergentNames,
  selectedSkill,
  selectSkill,
  startSkillsPolling,
  stopSkillsPolling,
} from '../../store/skills';
import {
  syncTargetsFor,
  syncSkillToTargets,
  syncingSkill,
} from '../../store/skills';
import { sheetOpen } from '../../store/ui';
import { startChatFromPrompt } from '../../store/desktop';
import { useIsMobile } from '../../hooks/useMediaQuery';
import { Input } from '../../components/primitives/Input';
import { Button } from '../../components/primitives/Button';
import { Pill } from '../../components/primitives/Pill';
import { Icon } from '../../components/Icon';
import { EmptyState } from '../../components/primitives/EmptyState';
import { BottomSheet } from '../../components/BottomSheet';
import { MutatorOnly } from '../../components/MutatorOnly';
import type { SkillRecord, SyncConflict } from '../../api/skills';
import './skills.css';

/**
 * Skills tab (issue #187) — read-only surface over the SKILL.md-style
 * capabilities discovered from EVERY agent harness in the workspace
 * (Claude Code, OpenCode, Antigravity, …). One row = one logical skill;
 * the system badges show which harnesses share it, and a "divergent"
 * badge flags same-name skills whose content has drifted apart.
 */

// Explains the two ways to add a skill — shown as the header help tooltip and
// referenced by the "Add skill" button below it.
const ADD_SKILL_HELP =
  'Two ways to add a skill:\n' +
  '• Chat in the Hypervisor and ask an agent to create one\n' +
  '• Click “Add skill” to launch a chat that scaffolds one for you';

// Seed message for the chat the "Add skill" button starts.
const ADD_SKILL_PROMPT =
  'I want to add a new skill to this workspace. Ask me what the skill should ' +
  'do and which agent harness(es) it is for, then scaffold a SKILL.md under ' +
  'the appropriate skills directory (e.g. .claude/skills/) so it shows up in ' +
  'the Skills tab.';

export function SkillsRoute() {
  const isMobile = useIsMobile();

  useEffect(() => {
    startSkillsPolling(30000);
    return () => stopSkillsPolling();
  }, []);

  function onRowClick(s: SkillRecord) {
    selectSkill(s);
    if (isMobile) sheetOpen.value = 'skill-detail';
  }

  const list = filteredSkills.value;
  const hasFilter = !!(skillsFilter.value || skillsSystemFacet.value || skillsScopeFacet.value);

  return (
    <div class="route route-skills">
      <header class="route-header route-header-with-action">
        <div>
          <h1 class="route-title">Skills</h1>
          <p class="route-subtitle muted">
            Agent capabilities discovered across every harness — {skills.value.length} skills
            from {skillSystems.value.length || '…'} system{skillSystems.value.length === 1 ? '' : 's'}.
          </p>
        </div>
        <div class="skl-header-actions">
          <span
            class="skl-help"
            role="img"
            tabindex={0}
            aria-label={ADD_SKILL_HELP}
            title={ADD_SKILL_HELP}
          >
            <Icon name="info" size={15} />
          </span>
          <MutatorOnly>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => void startChatFromPrompt(ADD_SKILL_PROMPT, '/home/dev')}
              title="Launches a Hypervisor chat that scaffolds a new skill"
            >
              <Icon name="plus" size={12} /> Add skill
            </Button>
          </MutatorOnly>
        </div>
      </header>

      <div class="skl-layout">
        <div class="skl-master">
          <SkillsToolbar />
          {list.length === 0 ? (
            <EmptyState
              icon={<Icon name="skills" size={24} />}
              title={hasFilter ? 'No matches' : 'No skills found'}
              description={
                hasFilter
                  ? 'Try clearing the filter.'
                  : 'Skills are SKILL.md folders under .claude/skills/, ~/.config/opencode/skills/, and similar harness directories.'
              }
            />
          ) : (
            <SkillsList list={list} onRowClick={onRowClick} />
          )}
        </div>
        {!isMobile && (
          <div class="skl-detail-pane">
            <SkillDetail />
          </div>
        )}
      </div>

      <BottomSheet
        open={isMobile && sheetOpen.value === 'skill-detail'}
        onClose={() => {
          sheetOpen.value = null;
          selectSkill(null);
        }}
        initialSnap="full"
      >
        <SkillDetail />
      </BottomSheet>
    </div>
  );
}

function SkillsToolbar() {
  const systems = skillSystems.value;
  const scopes = skillScopes.value;
  // Debounced draft — same discipline as MemoryToolbar: fast typing must
  // not re-run the substring filter on every keystroke.
  const [draft, setDraft] = useState(skillsFilter.value);
  useEffect(() => {
    if (draft === skillsFilter.value) return;
    const id = window.setTimeout(() => {
      skillsFilter.value = draft;
    }, 120);
    return () => window.clearTimeout(id);
  }, [draft]);
  useEffect(() => {
    if (skillsFilter.value !== draft) setDraft(skillsFilter.value);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [skillsFilter.value]);

  return (
    <div class="skl-toolbar">
      <Input
        fullWidth
        placeholder="Search name, description, or system…"
        value={draft}
        onInput={(e) => setDraft((e.target as HTMLInputElement).value)}
        aria-label="Filter skills"
      />
      <div class="skl-facet-row" role="tablist" aria-label="System facets">
        <button
          class={`skl-facet ${skillsSystemFacet.value == null ? 'skl-facet-active' : ''}`}
          onClick={() => (skillsSystemFacet.value = null)}
        >
          All systems
        </button>
        {systems.map((s) => (
          <button
            key={s}
            class={`skl-facet ${skillsSystemFacet.value === s ? 'skl-facet-active' : ''}`}
            onClick={() => (skillsSystemFacet.value = skillsSystemFacet.value === s ? null : s)}
          >
            {s}
          </button>
        ))}
        {scopes.length > 1 && (
          <span class="skl-facet-sep" aria-hidden="true" />
        )}
        {scopes.length > 1 && scopes.map((s) => (
          <button
            key={`scope-${s}`}
            class={`skl-facet ${skillsScopeFacet.value === s ? 'skl-facet-active' : ''}`}
            onClick={() => (skillsScopeFacet.value = skillsScopeFacet.value === s ? null : s)}
          >
            {s}
          </button>
        ))}
      </div>
    </div>
  );
}

function SkillsList({ list, onRowClick }: { list: SkillRecord[]; onRowClick: (s: SkillRecord) => void }) {
  const divergent = divergentNames.value;
  return (
    <ul class="skl-list" role="list">
      {list.map((s) => {
        const active =
          selectedSkill.value?.name === s.name &&
          selectedSkill.value?.fingerprint === s.fingerprint;
        return (
          <li key={`${s.name}:${s.fingerprint}`}>
            <button class={`skl-row ${active ? 'skl-row-active' : ''}`} onClick={() => onRowClick(s)}>
              <div class="skl-row-head">
                <span class="skl-row-name mono">/{s.name}</span>
                <Pill tone="neutral" mono>{s.scope}</Pill>
                {divergent.has(s.name) && (
                  <Pill tone="warn" mono title="Same skill name has different content in different systems">
                    divergent
                  </Pill>
                )}
              </div>
              <div class="skl-row-desc muted">
                {s.description || <em>No description</em>}
              </div>
              <div class="skl-row-systems">
                {s.systems.map((sys) => (
                  <span class="skl-system" key={sys}>{sys}</span>
                ))}
                {s.user_invocable && <span class="skl-invocable muted">user-invocable</span>}
              </div>
            </button>
          </li>
        );
      })}
    </ul>
  );
}

function SkillDetail() {
  const s = selectedSkill.value;
  if (!s) {
    return (
      <EmptyState
        icon={<Icon name="skills" size={24} />}
        title="Select a skill"
        description="Pick a skill from the list to see its full definition, metadata, and every harness location it was discovered in."
      />
    );
  }
  return (
    <article class="skl-detail">
      <header class="skl-detail-header">
        <div class="skl-detail-headline">
          <span class="skl-detail-name mono">/{s.name}</span>
          <Pill tone="neutral" mono>{s.scope}</Pill>
          {s.user_invocable && <Pill tone="info" mono>user-invocable</Pill>}
        </div>
        {s.description && <p class="skl-detail-desc muted">{s.description}</p>}
      </header>

      <dl class="skl-meta">
        <dt>Systems</dt>
        <dd>
          {s.systems.map((sys) => (
            <span class="skl-system" key={sys}>{sys}</span>
          ))}
        </dd>
        {s.allowed_tools.length > 0 && (
          <>
            <dt>Allowed tools</dt>
            <dd class="mono">{s.allowed_tools.join(', ')}</dd>
          </>
        )}
        {s.argument_hint && (
          <>
            <dt>Arguments</dt>
            <dd class="mono">{s.argument_hint}</dd>
          </>
        )}
        <dt>Updated</dt>
        <dd>{s.updated_at ? new Date(s.updated_at * 1000).toLocaleString() : '—'}</dd>
        <dt>Fingerprint</dt>
        <dd class="mono">{s.fingerprint}</dd>
      </dl>

      <h3 class="skl-section-title">Sources</h3>
      <ul class="skl-sources">
        {s.sources.map((src) => (
          <li key={src.path} class={src.shadowed ? 'skl-source-shadowed' : ''}>
            <span class="skl-system">{src.system}</span>
            <span class="mono skl-source-path" title={src.path}>{src.path}</span>
            {src.shadowed && <span class="muted">(shadowed by {s.scope} scope)</span>}
          </li>
        ))}
      </ul>

      <MutatorOnly>
        <SyncPanel skill={s} />
      </MutatorOnly>

      <h3 class="skl-section-title">Definition</h3>
      <pre class="skl-body">{s.body || '(empty)'}</pre>
    </article>
  );
}

/**
 * "Sync to…" — install this skill's content into other harnesses so every
 * agent can use it. Wrapped in <MutatorOnly>, so the read-only public demo
 * hides it entirely. On a 409 (a target already has a divergent copy) it
 * surfaces the conflict and offers a one-click force-overwrite.
 */
function SyncPanel({ skill }: { skill: SkillRecord }) {
  const targets = syncTargetsFor(skill);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [conflicts, setConflicts] = useState<SyncConflict[] | null>(null);

  // Reset selection when the selected skill changes.
  const key = `${skill.name}:${skill.fingerprint}`;
  useEffect(() => {
    setPicked(new Set());
    setConflicts(null);
  }, [key]);

  if (targets.length === 0) {
    return (
      <div class="skl-sync">
        <h3 class="skl-section-title">Sync</h3>
        <p class="muted skl-sync-none">Already present in every available harness.</p>
      </div>
    );
  }

  const toggle = (sys: string) => {
    const next = new Set(picked);
    if (next.has(sys)) next.delete(sys);
    else next.add(sys);
    setPicked(next);
    setConflicts(null);
  };

  const run = async (force: boolean) => {
    const chosen = [...picked];
    if (!chosen.length) return;
    const outcome = await syncSkillToTargets(
      skill.name,
      skill.systems[0],
      skill.scope,
      chosen.map((system) => ({ system, scope: 'user' })),
      force,
    );
    if (outcome.ok) {
      setPicked(new Set());
      setConflicts(null);
    } else if ('conflicts' in outcome) {
      setConflicts(outcome.conflicts);
    }
  };

  return (
    <div class="skl-sync">
      <h3 class="skl-section-title">Sync to…</h3>
      <p class="muted skl-sync-hint">
        Install this skill into another harness so its agent can use it too.
      </p>
      <div class="skl-sync-targets">
        {targets.map((sys) => (
          <label class={`skl-sync-target ${picked.has(sys) ? 'skl-sync-target-on' : ''}`} key={sys}>
            <input
              type="checkbox"
              checked={picked.has(sys)}
              onChange={() => toggle(sys)}
            />
            <span class="skl-system">{sys}</span>
          </label>
        ))}
      </div>

      {conflicts && (
        <div class="skl-sync-conflict" role="alert">
          <Icon name="triggers" size={12} />{' '}
          {conflicts.map((c) => c.system).join(', ')} already {conflicts.length === 1 ? 'has' : 'have'} a
          different version. Overwrite?
          <div class="skl-sync-actions">
            <Button size="sm" variant="danger" disabled={syncingSkill.value} onClick={() => void run(true)}>
              Overwrite
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setConflicts(null)}>Cancel</Button>
          </div>
        </div>
      )}

      {!conflicts && (
        <div class="skl-sync-actions">
          <Button
            size="sm"
            variant="primary"
            disabled={picked.size === 0 || syncingSkill.value}
            onClick={() => void run(false)}
          >
            {syncingSkill.value ? 'Syncing…' : `Sync${picked.size ? ` to ${picked.size}` : ''}`}
          </Button>
        </div>
      )}
    </div>
  );
}
