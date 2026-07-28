import { useState } from 'preact/hooks';
import { Icon } from '../../components/Icon';
import { PopoverMenu } from '../../components/PopoverMenu';
import { AssistantPicker } from '../../components/AssistantPicker';
import {
  config,
  ctoAssistant,
  ctoModel,
  ctoEffort,
  setCtoAssistant,
  setActiveThreadModel,
  setActiveThreadEffort,
} from '../../store/hypervisor';
import { setProjectAssistantDefaults } from '../../store/projects';
import type { Project } from '../../api/projects';

/**
 * Per-project assistant configuration for the AI CTO (#483, #362).
 *
 * A compact gear in the /cto topbar — the executive-clean look survives, and
 * the three dials (provider, model, reasoning effort) are one tap away. The
 * selection applies to this session immediately; "Set as project default"
 * persists it on the project record so it survives a reload, syncs across
 * devices, and is the same field the CTO can set itself via `update_project`.
 *
 * The Workspace scope (no project selected) has no record to write to, so the
 * picker still works for the session and the save affordance explains why it
 * can't stick.
 */

/** True when the live selection already matches what the project stores — the
 *  save button is pointless then, and the dirty dot must stay off. */
export function matchesProjectDefaults(
  project: Project | null,
  sel: { assistant: string; model: string; effort: string },
): boolean {
  if (!project) return false;
  return (
    (project.default_assistant || '') === sel.assistant &&
    (project.default_model || '') === sel.model &&
    (project.default_effort || '') === sel.effort
  );
}

export function CtoConfig({ project }: { project: Project | null }) {
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const assistants = config.value?.assistants ?? [];
  const assistant = ctoAssistant.value;
  const model = ctoModel.value;
  const effort = ctoEffort.value;
  const isDefault = matchesProjectDefaults(project, { assistant, model, effort });
  // A project that has never been configured is not "dirty" — it's just
  // inheriting the workspace default, which is the intended resting state.
  const unset =
    !project?.default_assistant &&
    !project?.default_model &&
    !project?.default_effort;
  const dirty = !!project && !isDefault && !unset;

  async function saveAsDefault() {
    if (!project) return;
    setSaving(true);
    setError(null);
    try {
      await setProjectAssistantDefaults(project.id, {
        default_assistant: assistant,
        default_model: model,
        default_effort: effort,
      });
      setSaved(true);
      window.setTimeout(() => setSaved(false), 2000);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to save');
    } finally {
      setSaving(false);
    }
  }

  const summary = [assistants.find((a) => a.id === assistant)?.label || assistant, model]
    .filter(Boolean)
    .join(' · ');

  return (
    <PopoverMenu
      width={260}
      trigger={(p) => (
        <button
          type="button"
          class={`cto-config-trigger ${dirty ? 'is-dirty' : ''}`}
          title={summary ? `Assistant: ${summary}` : 'Assistant setup'}
          aria-label="Assistant setup"
          {...p}
        >
          <Icon name="settings" size={14} />
          <span class="cto-config-trigger-text">{summary || 'Assistant'}</span>
        </button>
      )}
    >
      <div class="cto-config">
        <p class="cto-config-title">
          {project ? `Assistant for ${project.name}` : 'Assistant for this session'}
        </p>

        <AssistantPicker
          idPrefix="cto"
          assistants={assistants}
          assistant={assistant}
          model={model}
          effort={effort}
          onAssistant={setCtoAssistant}
          onModel={(m) => {
            ctoModel.value = m;
            void setActiveThreadModel(m);
          }}
          onEffort={(e) => {
            ctoEffort.value = e;
            void setActiveThreadEffort(e);
          }}
        />

        <p class="cto-config-hint">
          A new agent applies to your next chat; model and effort switch the
          open one from its next turn.
        </p>

        {project ? (
          <>
            <button
              type="button"
              class="cto-config-save"
              disabled={saving || isDefault}
              onClick={() => void saveAsDefault()}
            >
              {saved
                ? 'Saved'
                : isDefault
                  ? 'Is the project default'
                  : 'Set as project default'}
            </button>
            <p class="cto-config-hint">
              Persists on the project, so every CTO chat and every build it
              dispatches for {project.name} uses it.
            </p>
          </>
        ) : (
          <p class="cto-config-hint">
            Select a project to save this as its default.
          </p>
        )}

        {error && (
          <p class="cto-config-error" role="alert">
            {error}
          </p>
        )}
      </div>
    </PopoverMenu>
  );
}
