import type { HypervisorAssistant } from '../api/hypervisor';
import './AssistantPicker.css';

/**
 * The shared assistant / model / effort picker.
 *
 * One component for the three dials that always travel together — WHICH agent
 * (provider + harness), WHICH model, and HOW HARD it thinks — so a surface that
 * wants any of them gets the same control, the same gating rules, and the same
 * labels. Used by the AI CTO's per-project setup (#483); the Chat tab keeps its
 * own inline toolbar controls (its selects are laid out for a cramped topbar,
 * not a popover), and <EffortSelect/> is exported so a future surface can adopt
 * the effort dial alone without re-deriving the gating rules.
 *
 * Gating is entirely server-driven and identical for all three:
 *   • only assistants the server lists are offered (binary/key gated), so a
 *     keyless deployment never shows a dead provider;
 *   • the model select renders only when that assistant has a model list;
 *   • the effort select renders only when that assistant's CLI has an effort
 *     knob (`efforts` non-empty) — the rest simply never see the control.
 * Nothing here knows any provider's name.
 */

export interface AssistantPickerProps {
  /** Enabled assistants, server-ordered (default first). */
  assistants: HypervisorAssistant[];
  assistant: string;
  model: string;
  effort: string;
  /** Called with the new assistant id. The caller is expected to reset model +
   *  effort to that assistant's defaults (see setCtoAssistant in the store). */
  onAssistant: (id: string) => void;
  onModel: (model: string) => void;
  onEffort: (effort: string) => void;
  disabled?: boolean;
  /** Hide the provider select when the surface pins the assistant elsewhere. */
  showAssistant?: boolean;
  /** Unique per instance so two pickers on one page don't share label ids. */
  idPrefix?: string;
}

/** Sentence-case label for a canonical effort level. `xhigh` reads as "X-High"
 *  rather than "Xhigh"; everything else is just capitalised. */
export function effortLabel(level: string): string {
  if (level === 'xhigh') return 'X-High';
  return level ? level[0].toUpperCase() + level.slice(1) : level;
}

/**
 * The reasoning-effort select on its own (#362), for toolbars that already have
 * their own agent/model controls. Renders nothing when `levels` is empty, which
 * is how an assistant with no effort knob opts out.
 */
export function EffortSelect({
  levels,
  value,
  onChange,
  disabled,
  id,
  className = '',
  showLabel = true,
}: {
  levels: string[];
  value: string;
  onChange: (effort: string) => void;
  disabled?: boolean;
  id?: string;
  className?: string;
  showLabel?: boolean;
}) {
  if (levels.length === 0) return null;
  return (
    <label
      class={`ap-field ap-field-effort ${className}`}
      title="Reasoning effort — how much the agent thinks before it acts. Clamped to what the selected provider supports."
    >
      {showLabel && (
        <span class="ap-label" id={id ? `${id}-label` : undefined}>
          Effort
        </span>
      )}
      <select
        class="ap-select"
        id={id}
        value={value}
        disabled={disabled}
        onChange={(e) => onChange((e.target as HTMLSelectElement).value)}
        aria-label="Reasoning effort"
      >
        {levels.map((l) => (
          <option key={l} value={l}>
            {effortLabel(l)}
          </option>
        ))}
      </select>
    </label>
  );
}

export function AssistantPicker({
  assistants,
  assistant,
  model,
  effort,
  onAssistant,
  onModel,
  onEffort,
  disabled,
  showAssistant = true,
  idPrefix = 'ap',
}: AssistantPickerProps) {
  const current = assistants.find((a) => a.id === assistant);
  const models = current?.models ?? [];
  const efforts = current?.efforts ?? [];

  return (
    <div class="assistant-picker">
      {showAssistant && (
        <label class="ap-field">
          <span class="ap-label">Agent</span>
          <select
            class="ap-select"
            id={`${idPrefix}-assistant`}
            value={assistant}
            disabled={disabled}
            onChange={(e) => onAssistant((e.target as HTMLSelectElement).value)}
            aria-label="Assistant"
          >
            {/* Keep an off-list value selectable rather than silently showing
                the first option — a project can name a provider this workspace
                no longer has a key for, and hiding that would make the picker
                lie about what is stored. */}
            {assistant && !assistants.some((a) => a.id === assistant) && (
              <option value={assistant}>{assistant} (unavailable)</option>
            )}
            {assistants.map((a) => (
              <option key={a.id} value={a.id}>
                {a.label}
                {a.free ? ' · free' : ''}
              </option>
            ))}
          </select>
        </label>
      )}

      {models.length > 0 && (
        <label class="ap-field">
          <span class="ap-label">Model</span>
          <select
            class="ap-select"
            id={`${idPrefix}-model`}
            value={model}
            disabled={disabled}
            onChange={(e) => onModel((e.target as HTMLSelectElement).value)}
            aria-label="Model"
          >
            {model && !models.includes(model) && (
              <option value={model}>{model} (unavailable)</option>
            )}
            {models.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
        </label>
      )}

      <EffortSelect
        levels={efforts}
        value={effort}
        onChange={onEffort}
        disabled={disabled}
        id={`${idPrefix}-effort`}
      />

      {current?.trainingDisclosure && (
        <p class="ap-note" role="note">
          ⚠️ Free models may use your prompts + code for training — avoid
          confidential data.
        </p>
      )}
    </div>
  );
}
