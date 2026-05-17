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

/** Topbar pill that flags the public-demo deployment. */
export function ReadOnlyPill() {
  if (!serverMode.value.readOnly) return null;
  return (
    <span
      class="readonly-pill"
      title="This workspace is a public demo — every mutation endpoint returns 403."
    >
      Demo · Read-only
    </span>
  );
}
