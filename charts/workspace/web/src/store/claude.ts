import { signal } from '@preact/signals';
import { getSubscriptions } from '../api/subscriptions';

// Whether a spawned Claude session would have a working credential — a
// non-expired subscription OAuth login or a saved/env ANTHROPIC_API_KEY (#494).
// The onboarding first-win step and the AI CTO welcome gate on this so a
// keyless user is guided to connect Claude instead of firing a build that dies
// with a raw provider error. `null` = not yet known (don't gate before the
// first probe lands, so an existing authenticated user sees no flash).
export const claudeReady = signal<boolean | null>(null);

export async function refreshClaudeReady(): Promise<boolean | null> {
  try {
    const r = await getSubscriptions();
    claudeReady.value = !!r.claude_ready;
  } catch {
    // Server unreachable (tests, dev_server, outage) — leave the last-known
    // value. Never flip a real user into the gated state on a transient error.
  }
  return claudeReady.value;
}
