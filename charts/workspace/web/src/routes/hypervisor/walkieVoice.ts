/**
 * Pure voice logic for the voice-first Walkie-Talkie (issue #401): the
 * push-to-talk phase machine, mic-level → visualizer mapping, and the orb's
 * state copy. No DOM, no audio APIs — everything here is unit-testable, and
 * kept line-for-line in sync with mobile/src/util/walkieVoice.ts (same rule
 * as voice.ts, so both clients behave identically).
 */

// ── phase machine ────────────────────────────────────────────────────────────
// idle → listening → transcribing → sending → thinking → speaking → idle
//
// `transcribing` is the gap between "stop talking" and "transcript in hand":
// near-instant on web (SpeechRecognition finishes locally), a real server
// round-trip on mobile. Errors don't get a phase — they surface as a hint on
// the orb and the machine returns to `idle` via 'cancel'.

export type VoicePhase =
  | 'idle'
  | 'listening'
  | 'transcribing'
  | 'sending'
  | 'thinking'
  | 'speaking';

export type VoiceSignal =
  /** The orb was pressed (tap, keyboard, or hold-release start). */
  | 'press'
  /** Capture finished with usable speech; the transport takes over. */
  | 'captured'
  /** Capture finished with nothing usable. */
  | 'empty'
  /** The transport accepted the message. */
  | 'sent'
  /** The gateway reports a turn in flight. */
  | 'busy'
  /** Narration of a reply started. */
  | 'reply'
  /** Narration finished (or there was nothing to narrate). */
  | 'quiet'
  /** Recognition/recording failed, or the page was reset. */
  | 'cancel';

/**
 * The next phase for a signal. Pressing the orb always wins: it interrupts
 * playback (barge-in — the CALLER must also stop TTS) and starts listening
 * from any settled phase; while capture/send are in flight a press is a
 * no-op so double-taps can't double-send.
 */
export function transition(phase: VoicePhase, signal: VoiceSignal): VoicePhase {
  switch (signal) {
    case 'press':
      if (phase === 'idle' || phase === 'speaking' || phase === 'thinking') return 'listening';
      if (phase === 'listening') return 'transcribing';
      return phase; // transcribing | sending — ignore until settled
    case 'captured':
      return phase === 'listening' || phase === 'transcribing' ? 'sending' : phase;
    case 'empty':
      return phase === 'listening' || phase === 'transcribing' ? 'idle' : phase;
    case 'sent':
      return phase === 'sending' ? 'thinking' : phase;
    case 'busy':
      // The gateway's own busy flag — trust it from any settled phase, but
      // never yank an active capture.
      return phase === 'idle' || phase === 'sending' || phase === 'speaking'
        ? 'thinking'
        : phase;
    case 'reply':
      return phase === 'listening' || phase === 'transcribing' ? phase : 'speaking';
    case 'quiet':
      return phase === 'speaking' || phase === 'thinking' ? 'idle' : phase;
    case 'cancel':
      return 'idle';
  }
}

// ── mic level → visualizer ───────────────────────────────────────────────────

/**
 * Normalized speech level (0..1) from an AnalyserNode's byte time-domain
 * buffer (samples centered on 128). RMS with a gain curve tuned so normal
 * speech swings visibly (~0.3–0.9) instead of hugging zero.
 */
export function levelFromTimeDomain(bytes: ArrayLike<number>): number {
  const n = bytes.length;
  if (n === 0) return 0;
  let sum = 0;
  for (let i = 0; i < n; i++) {
    const v = (bytes[i] - 128) / 128;
    sum += v * v;
  }
  const rms = Math.sqrt(sum / n);
  // Typical speech RMS on this scale is ~0.02–0.25; sqrt expands the low end.
  return Math.min(1, Math.sqrt(rms * 4));
}

/**
 * Normalized level (0..1) from a dB meter reading (expo-audio metering:
 * 0 dBFS = full scale, silence ≈ -60 and below). Non-finite/absent → 0.
 */
export function levelFromDb(db: number | null | undefined, floorDb = -60): number {
  if (db == null || !isFinite(db)) return 0;
  if (db >= 0) return 1;
  if (db <= floorDb) return 0;
  return 1 - db / floorDb;
}

/**
 * Asymmetric smoothing for the visualizer: rise fast (speech onsets should
 * feel immediate), fall slow (so the rings breathe instead of flickering).
 */
export function smoothLevel(prev: number, next: number, attack = 0.6, release = 0.15): number {
  const k = next > prev ? attack : release;
  return prev + (next - prev) * k;
}

// ── orb copy ─────────────────────────────────────────────────────────────────

export interface OrbFlags {
  /** Gateway preview reachable. */
  available: boolean;
  /** Internal loopback channel linked. */
  linked: boolean;
  /** A speech-capture path exists (browser SpeechRecognition / server STT). */
  stt: boolean;
  /** The mic permission was denied. */
  micDenied: boolean;
}

export interface OrbCopy {
  /** Short label rendered on the orb itself. */
  label: string;
  /** One-line hint below the orb. */
  hint: string;
  /** Pressing does nothing — style the orb inert. */
  disabled: boolean;
}

/**
 * What the orb says in each state. Degraded states win over the phase —
 * they're recoverable and explained in place, never a dead button.
 */
export function orbCopy(phase: VoicePhase, flags: OrbFlags): OrbCopy {
  if (!flags.available) {
    return { label: 'OFFLINE', hint: 'Gateway offline — reconnecting…', disabled: true };
  }
  if (!flags.linked) {
    return { label: 'LINKING', hint: 'Pairing with the loopback channel…', disabled: true };
  }
  if (!flags.stt) {
    return {
      label: 'TYPE',
      hint: 'Voice input isn’t supported here — use the keyboard below',
      disabled: true,
    };
  }
  if (flags.micDenied && (phase === 'idle' || phase === 'listening')) {
    return {
      label: 'MIC OFF',
      hint: 'Microphone blocked — allow mic access (HTTPS) or type instead',
      disabled: false, // pressing retries the permission prompt
    };
  }
  switch (phase) {
    case 'idle':
      return { label: 'TALK', hint: 'Tap, then speak', disabled: false };
    case 'listening':
      return { label: 'LISTENING', hint: 'Tap again when you’re done', disabled: false };
    case 'transcribing':
      return { label: '· · ·', hint: 'Catching that…', disabled: true };
    case 'sending':
      return { label: '· · ·', hint: 'Sending…', disabled: true };
    case 'thinking':
      return { label: 'WORKING', hint: 'Agent is thinking — tap to talk over it', disabled: false };
    case 'speaking':
      return { label: 'TALK', hint: 'Tap to interrupt and speak', disabled: false };
  }
}

// ── visualizer mood ──────────────────────────────────────────────────────────

/** One visual, four moods: which animation family the stage should run. */
export type OrbMood = 'idle' | 'input' | 'processing' | 'output';

export function orbMood(phase: VoicePhase): OrbMood {
  switch (phase) {
    case 'listening':
      return 'input';
    case 'transcribing':
    case 'sending':
    case 'thinking':
      return 'processing';
    case 'speaking':
      return 'output';
    default:
      return 'idle';
  }
}
