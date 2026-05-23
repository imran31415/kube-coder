import type { ComponentChildren } from 'preact';
import { serverMode } from '../store/server-mode';

/**
 * Renders children only when the workspace is *not* in read-only public-demo
 * mode. Use to wrap mutation UI (New/Edit/Delete/Kill/Rename/Send) so the
 * public deploy hides them entirely instead of letting users click and
 * collect 403s. Server still enforces the gate; this is presentation only.
 */
export function MutatorOnly({ children }: { children: ComponentChildren }) {
  if (serverMode.value.readOnly) return null;
  return <>{children}</>;
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
