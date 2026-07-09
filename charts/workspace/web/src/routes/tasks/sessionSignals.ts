import { signal, type Signal } from '@preact/signals';

/**
 * Shared per-task session state so TaskDetail's settings menu can drive
 * TerminalPane (reattach, scroll mode) without prop-drilling, and the
 * status dot can reflect TerminalPane's phase without lifting state.
 *
 * Keyed by task_id so multiple tabs / quick task switches don't smear
 * state across sessions. Signals are created lazily on first access
 * and never freed — the working set is tiny and per-task signals are
 * cheap; reaping would risk a render seeing a stale signal.
 */

export type SessionPhase = 'preparing' | 'ready' | 'error';

export interface SessionSignals {
  phase: Signal<SessionPhase>;
  /** True while the user is in tmux copy-mode for this session. */
  scrollMode: Signal<boolean>;
  /** Bump to ask TerminalPane to re-prepare + reload the iframe.
   *  Pattern: any consumer reads the value in an effect dep list. */
  reattachCounter: Signal<number>;
  /** Carries clipboard text from the TaskBar "Paste from clipboard" action
   *  into the Send-message composer. `nonce` bumps per paste so an identical
   *  paste still fires the consumer's effect. Null until the first paste. */
  pasteRequest: Signal<{ text: string; nonce: number } | null>;
  /** Like pasteRequest, but carries clipboard IMAGES from the toolbar "Paste"
   *  action into the composer, where they become upload chips (issue #179).
   *  `nonce` bumps per paste so repeat pastes re-fire the consumer's effect. */
  imagePasteRequest: Signal<{ files: File[]; nonce: number } | null>;
}

const _store = new Map<string, SessionSignals>();

export function getSessionSignals(taskId: string): SessionSignals {
  let s = _store.get(taskId);
  if (!s) {
    s = {
      phase: signal<SessionPhase>('preparing'),
      scrollMode: signal(false),
      reattachCounter: signal(0),
      pasteRequest: signal<{ text: string; nonce: number } | null>(null),
      imagePasteRequest: signal<{ files: File[]; nonce: number } | null>(null),
    };
    _store.set(taskId, s);
  }
  return s;
}
