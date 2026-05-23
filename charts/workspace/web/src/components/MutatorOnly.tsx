import type { ComponentChildren } from 'preact';
import { serverMode } from '../store/server-mode';
import { pushToast } from '../store/ui';

/**
 * Wraps mutation UI (New/Edit/Delete/Kill/Rename/Send). Behaviour by mode:
 *   - writable deploy           → render children normally.
 *   - read-only demo, show-all  → render children, but dimmed and
 *     click-intercepted (data-demo-disabled) so visitors see the full UI
 *     surface and get a "sign up to enable" toast instead of a silent 403.
 *   - read-only deploy, default → hide children entirely.
 * The server enforces the real gate (READONLY_MODE → 403); this is purely
 * presentation.
 */
export function MutatorOnly({ children }: { children: ComponentChildren }) {
  const mode = serverMode.value;
  if (!mode.readOnly) return <>{children}</>;
  if (mode.demoShowAll) {
    return (
      <span
        class="demo-disabled"
        data-demo-disabled="true"
        title="Demo — sign up to enable"
        // Capture-phase so the toast fires before any child handler and the
        // underlying mutation never runs (it would 403 anyway). preventDefault
        // also stops <a> navigations (e.g. the "New terminal" link).
        onClickCapture={(e: MouseEvent) => {
          e.preventDefault();
          e.stopPropagation();
          pushToast(
            'Demo — sign up to enable. Deploy your own: github.com/imran31415/kube-coder',
            { kind: 'info' },
          );
        }}
      >
        {children}
      </span>
    );
  }
  return null;
}

/** Inverse of MutatorOnly — renders only inside read-only deployments. */
export function ReadOnlyOnly({ children }: { children: ComponentChildren }) {
  if (!serverMode.value.readOnly) return null;
  return <>{children}</>;
}

/** Topbar pill that flags the public-demo deployment. Doubles as the
 *  conversion CTA — clicking opens the kube-coder repo on GitHub where
 *  visitors can find the helm chart and deploy their own writable
 *  workspace. Without this, demo visitors had no in-context path off
 *  the read-only deck (the prior version was a plain <span>). */
export function ReadOnlyPill() {
  if (!serverMode.value.readOnly) return null;
  return (
    <a
      class="readonly-pill readonly-pill-link"
      href="https://github.com/imran31415/kube-coder#quick-start"
      target="_blank"
      rel="noopener noreferrer"
      title="Public demo — every mutation returns 403. Click to deploy your own writable workspace."
    >
      Demo · Deploy your own ↗
    </a>
  );
}
