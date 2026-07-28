import { signal } from '@preact/signals';
import { getSubscriptions } from '../api/subscriptions';

// Whether a spawned Claude session would have a working credential — a
// non-expired subscription OAuth login or a saved/env ANTHROPIC_API_KEY (#494).
// The onboarding first-win step and the AI CTO welcome gate on this so a
// keyless user is guided to connect Claude instead of firing a build that dies
// with a raw provider error. `null` = not yet known (don't gate before the
// first probe lands, so an existing authenticated user sees no flash).
export const claudeReady = signal<boolean | null>(null);

// Whether the first readiness probe has *settled* — resolved or failed (#500).
// `claudeReady === null` can't answer that: it also means "the probe failed and
// we kept the last-known nothing". Surfaces that must not offer a build before
// the gate can appear (the CTO welcome chips) stay inert until this flips, so a
// keyless deep-link to /cto can't fire a doomed build in the probe window —
// while a failed probe still releases them rather than locking them forever.
export const claudeProbed = signal(false);

export async function refreshClaudeReady(): Promise<boolean | null> {
  try {
    const r = await getSubscriptions();
    claudeReady.value = !!r.claude_ready;
  } catch {
    // Server unreachable (tests, dev_server, outage) — leave the last-known
    // value. Never flip a real user into the gated state on a transient error.
  } finally {
    claudeProbed.value = true;
  }
  return claudeReady.value;
}
