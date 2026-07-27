import { signal } from '@preact/signals';

// Set true by the onboarding wizard's final step just before it routes a
// newly-onboarded user into the AI CTO front door (#487). The CTO welcome reads
// it to show a build-first "first-win" opener + starter chips instead of the
// returning-user greeting, then clears it on mount. In-memory only: a page
// reload drops it, which is correct — a reload is no longer the first-run
// moment, so the regular welcome takes over.
export const justOnboarded = signal<boolean>(false);
