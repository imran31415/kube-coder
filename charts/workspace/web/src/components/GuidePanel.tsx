import type { ComponentChildren } from 'preact';
import { useState } from 'preact/hooks';
import { Icon } from './Icon';
import './GuidePanel.css';

export interface GuideStep {
  title: string;
  body: ComponentChildren;
}

export interface GuideScenario {
  /** What the user types or does. */
  prompt: string;
  /** What the page does in response. */
  outcome: string;
}

export interface GuidePanelProps {
  /** Heading shown on the toggle bar, e.g. "How the Hypervisor works". */
  title: string;
  intro: ComponentChildren;
  steps: GuideStep[];
  /** Optional "Try it" worked examples. */
  scenarios?: GuideScenario[];
  /** localStorage key so the open/closed choice sticks across visits. */
  storageKey?: string;
  defaultOpen?: boolean;
}

function readInitial(key: string | undefined, fallback: boolean): boolean {
  if (!key || typeof localStorage === 'undefined') return fallback;
  try {
    const v = localStorage.getItem(key);
    return v === null ? fallback : v === '1';
  } catch {
    return fallback;
  }
}

/**
 * Collapsible "How this works" guide — a short intro, numbered steps, and
 * optional sample scenarios. Mirrors the disclosure pattern used elsewhere
 * (chevron + label, conditional body) so it reads as one system, and remembers
 * whether the reader left it open (per `storageKey`).
 */
export function GuidePanel({
  title,
  intro,
  steps,
  scenarios,
  storageKey,
  defaultOpen = false,
}: GuidePanelProps) {
  const [open, setOpen] = useState(() => readInitial(storageKey, defaultOpen));

  function toggle() {
    const next = !open;
    setOpen(next);
    if (storageKey) {
      try {
        localStorage.setItem(storageKey, next ? '1' : '0');
      } catch {
        /* private mode / storage full — the toggle still works in-session */
      }
    }
  }

  return (
    <section class={`guide ${open ? 'guide-open' : ''}`}>
      <button
        type="button"
        class="guide-toggle"
        aria-expanded={open}
        onClick={toggle}
        title="How this page works — steps and sample scenarios"
      >
        <Icon name="info" size={14} class="guide-toggle-icon" />
        <span class="guide-toggle-title">{title}</span>
        <Icon
          name={open ? 'chevron-down' : 'chevron-right'}
          size={14}
          class="guide-toggle-chevron"
        />
      </button>

      {open && (
        <div class="guide-body">
          <p class="guide-intro muted">{intro}</p>

          <ol class="guide-steps">
            {steps.map((s, i) => (
              <li key={i} class="guide-step">
                <span class="guide-step-num" aria-hidden="true">
                  {i + 1}
                </span>
                <span class="guide-step-text">
                  <span class="guide-step-title">{s.title}</span>
                  <span class="guide-step-body muted">{s.body}</span>
                </span>
              </li>
            ))}
          </ol>

          {scenarios && scenarios.length > 0 && (
            <div class="guide-scenarios">
              <span class="guide-scenarios-head">Try it</span>
              <ul class="guide-scenario-list">
                {scenarios.map((sc, i) => (
                  <li key={i} class="guide-scenario">
                    <code class="guide-scenario-prompt">{sc.prompt}</code>
                    <span class="guide-scenario-arrow" aria-hidden="true">
                      →
                    </span>
                    <span class="guide-scenario-outcome muted">{sc.outcome}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
