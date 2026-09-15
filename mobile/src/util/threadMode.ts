/**
 * Thread "mode" labels — the phone's half of #683, mirroring
 * charts/workspace/web/src/routes/hypervisor/threadMode.ts.
 *
 * Persona has always been a server concept: a thread is created with one and
 * the preamble for it is delivered on turn 1. The AI CTO was a whole extra
 * screen for a single persona value; it is now a picker in Chat and a badge on
 * the thread rows, so every chat-management affordance the Chat screen has
 * applies to CTO threads too.
 *
 * Pure data — no react-native imports — so the node-side vitest suite can
 * assert it without a renderer.
 */

/** Human label for a thread's persona. '' (a plain workspace chat) has no
 *  badge — the common case stays visually quiet. */
export function personaLabel(persona: string | undefined | null): string {
  switch ((persona || '').toLowerCase()) {
    case 'cto':
      return 'CTO';
    // Board Processor threads (#588/#589) are machine-created, so they are
    // badge-only: never offered in the Mode picker, but honestly labelled.
    case 'board':
      return 'Board';
    case 'board-gen':
      return 'Board gen';
    default:
      return '';
  }
}

/** The options the Mode picker offers for a NEW chat. Board personas are
 *  deliberately absent: the Board Processor creates those, not a human. */
export const MODE_OPTIONS: { value: string; label: string }[] = [
  { value: '', label: 'Workspace' },
  { value: 'cto', label: 'CTO' },
];
