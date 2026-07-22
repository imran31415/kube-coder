/**
 * Pure voice logic for the voice-first Walkie-Talkie (issue #401): the
 * push-to-talk phase machine, mic-level → visualizer mapping, the hands-free
 * voice-activity endpointer (issue #406), and the orb's state copy. No DOM,
 * no audio APIs — the mobile counterpart of the dashboard's
 * web/src/routes/hypervisor/walkieVoice.ts, kept line-for-line in sync (same
 * rule as voice.ts, so both clients behave identically).
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
  | 'cancel'
  /** Hands-free VAD heard sustained speech (open-mic wake / voice barge-in). */
  | 'voice-start'
  /** Hands-free VAD heard end-of-speech while capturing. */
  | 'voice-end';

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
    case 'voice-start':
      // The endpointer heard the user — same reach as a press from the settled
      // phases (voice barge-in included), but it never disturbs an in-flight
      // capture/send, so a stray VAD event can't double-send.
      return phase === 'idle' || phase === 'speaking' || phase === 'thinking'
        ? 'listening'
        : phase;
    case 'voice-end':
      // End-of-speech only ever ends an active capture.
      return phase === 'listening' ? 'transcribing' : phase;
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

// ── voice-activity endpointer (issue #406, end-point latency: #408) ─────────
// A small time-driven state machine over the level samples the clients already
// compute (AnalyserNode RMS on web, recorder dB metering on mobile). It turns
// a noisy 0..1 level stream into two clean events: 'speech-start' after
// sustained speech (rejects clicks and coughs) and 'speech-end' after enough
// silence. "Enough" adapts to utterance length (issue #408): a short utterance
// is almost always a complete command, so it end-points near the fast
// hangoverMs floor, while long dictation — where mid-sentence thinking pauses
// live — ramps up to the conservative maxHangoverMs, so a pause never splits a
// capture. Speech resuming inside the hangover continues the same run, no
// event — but only after surviving a short qualification window (resumeMs), so
// a lone breath blip no longer restarts the whole silence countdown.
// Hysteresis between the onset and release thresholds keeps breath noise from
// flapping the state.

export type EndpointerEvent = 'speech-start' | 'speech-end';

export interface EndpointerOptions {
  /** Level at or above this counts as speech (entering a run). */
  onsetThreshold: number;
  /** Level below this counts as silence (leaving a run) — lower than onset,
   *  so the band between the two never flips the state (hysteresis). */
  releaseThreshold: number;
  /** Speech must sustain this long before 'speech-start' fires. */
  minSpeechMs: number;
  /** Silence that ends the shortest utterance — the fast floor of the
   *  adaptive hangover (a short utterance is almost always a complete
   *  command, so it should send promptly). */
  hangoverMs: number;
  /** Silence that ends a long utterance — the slow ceiling of the adaptive
   *  hangover, protecting dictation-style thinking pauses from splitting
   *  the capture. Equal to hangoverMs ⇒ fixed (non-adaptive) hangover. */
  maxHangoverMs: number;
  /** Speech duration over which the hangover ramps linearly from hangoverMs
   *  (at 0ms of speech) up to maxHangoverMs (at this many ms or more). */
  hangoverRampMs: number;
  /** A level back above onsetThreshold during the hangover must hold this
   *  long before it cancels the pending end and rejoins the utterance —
   *  shorter blips (a breath, a chair creak) leave the countdown running. */
  resumeMs: number;
  /** Hard cap on one utterance — 'speech-end' fires even mid-speech. */
  maxUtteranceMs: number;
}

/** Which situation the endpointer is listening for. */
export type EndpointMode =
  /** Endpointing an active capture: when has the user finished talking? */
  | 'listen'
  /** Open-mic wake while idle/thinking: slightly stricter onset. */
  | 'wake'
  /** Barge-in while TTS plays: raised onset + longer minimum, so echo bleed
   *  from the speakers can't grab the floor — the user must speak up. */
  | 'barge';

/**
 * Starting constants per mode, tuned on the level curves above: with
 * levelFromTimeDomain/levelFromDb, quiet speech lands ≈ 0.45, normal speech
 * ≈ 0.9, and (suppressed) room noise ≲ 0.25. End-of-speech is adaptive
 * (issue #408): listen/wake ramp 550→1100ms of required silence with
 * utterance length, so short commands send well under 1s while dictation
 * keeps its thinking-pause headroom; barge keeps a fixed conservative
 * hangover because TTS echo bleed makes its level stream the least
 * trustworthy. The adaptive noise floor remains a separate follow-up.
 */
export const ENDPOINT_OPTS: Record<EndpointMode, EndpointerOptions> = {
  listen: {
    onsetThreshold: 0.4,
    releaseThreshold: 0.22,
    minSpeechMs: 250,
    hangoverMs: 550,
    maxHangoverMs: 1100,
    hangoverRampMs: 4000,
    resumeMs: 150,
    maxUtteranceMs: 30000,
  },
  wake: {
    onsetThreshold: 0.5,
    releaseThreshold: 0.25,
    minSpeechMs: 350,
    hangoverMs: 550,
    maxHangoverMs: 1100,
    hangoverRampMs: 4000,
    resumeMs: 150,
    maxUtteranceMs: 30000,
  },
  barge: {
    onsetThreshold: 0.7,
    releaseThreshold: 0.3,
    minSpeechMs: 550,
    hangoverMs: 1250,
    maxHangoverMs: 1250,
    hangoverRampMs: 4000,
    resumeMs: 150,
    maxUtteranceMs: 30000,
  },
};

export interface Endpointer {
  /** Advance the machine with one level sample. Returns an event or null. */
  feed(level: number, nowMs: number): EndpointerEvent | null;
  /** Whether the machine currently considers the user to be speaking. */
  speaking(): boolean;
  /** Back to quiet, forgetting any in-progress run (no event). */
  reset(): void;
}

export function createEndpointer(opts: Partial<EndpointerOptions> = {}): Endpointer {
  const o = { ...ENDPOINT_OPTS.listen, ...opts };
  // quiet → onset (level ≥ onsetThreshold, not yet minSpeechMs) → speech
  // speech → hangover (level under release), and back over onset the run
  //   detours through resume — the blip must hold resumeMs before it rejoins
  //   speech; if it dies first the original silence countdown keeps running
  // hangover → quiet + 'speech-end' once silence outlasts the adaptive
  //   hangover for this utterance's length.
  let state: 'quiet' | 'onset' | 'speech' | 'hangover' | 'resume' = 'quiet';
  let onsetAt = 0; // when the candidate speech run began
  let speechAt = 0; // when the confirmed utterance began
  let silenceAt = 0; // when the current silence run began
  let resumeAt = 0; // when the candidate resume (blip) began
  // Adaptive hangover (issue #408): required silence grows linearly with how
  // long the user spoke before pausing, from the hangoverMs floor at 0ms of
  // speech to the maxHangoverMs ceiling at hangoverRampMs and beyond.
  const hangoverFor = (speechMs: number): number => {
    const ceiling = Math.max(o.hangoverMs, o.maxHangoverMs);
    const t = o.hangoverRampMs > 0 ? Math.min(1, speechMs / o.hangoverRampMs) : 1;
    return o.hangoverMs + (ceiling - o.hangoverMs) * t;
  };
  return {
    speaking: () => state === 'speech' || state === 'hangover' || state === 'resume',
    reset() {
      state = 'quiet';
    },
    feed(level, nowMs) {
      switch (state) {
        case 'quiet':
          if (level >= o.onsetThreshold) {
            state = 'onset';
            onsetAt = nowMs;
          }
          return null;
        case 'onset':
          if (level < o.releaseThreshold) {
            state = 'quiet'; // too short — a click, not speech
            return null;
          }
          if (nowMs - onsetAt >= o.minSpeechMs) {
            state = 'speech';
            speechAt = onsetAt;
            return 'speech-start';
          }
          return null;
        case 'speech':
          if (nowMs - speechAt >= o.maxUtteranceMs) {
            state = 'quiet';
            return 'speech-end';
          }
          if (level < o.releaseThreshold) {
            state = 'hangover';
            silenceAt = nowMs;
          }
          return null;
        case 'hangover':
          if (nowMs - speechAt >= o.maxUtteranceMs) {
            state = 'quiet';
            return 'speech-end';
          }
          if (level >= o.onsetThreshold) {
            state = 'resume'; // maybe resumed — must qualify before it counts
            resumeAt = nowMs;
            return null;
          }
          if (nowMs - silenceAt >= hangoverFor(silenceAt - speechAt)) {
            state = 'quiet';
            return 'speech-end';
          }
          return null;
        case 'resume':
          if (nowMs - speechAt >= o.maxUtteranceMs) {
            state = 'quiet';
            return 'speech-end';
          }
          if (level < o.releaseThreshold) {
            state = 'hangover'; // a blip, not speech — the countdown never stopped
            return null;
          }
          if (nowMs - resumeAt >= o.resumeMs) {
            state = 'speech'; // held long enough — same capture continues
          }
          return null;
      }
    },
  };
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
  /** Hands-free mode is on: pausing auto-sends the capture. */
  handsFree: boolean;
  /** Hands-free open mic is live right now: speech wakes/barges in without a
   *  tap. (False when the mode is on but the mic isn't — e.g. tab hidden, or
   *  barge-in gated off because echo cancellation isn't available.) */
  voiceWake: boolean;
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
      return flags.voiceWake
        ? { label: 'TALK', hint: 'Just start talking — or tap', disabled: false }
        : { label: 'TALK', hint: 'Tap, then speak', disabled: false };
    case 'listening':
      return flags.handsFree
        ? { label: 'LISTENING', hint: 'Pause when you’re done — it sends itself', disabled: false }
        : { label: 'LISTENING', hint: 'Tap again when you’re done', disabled: false };
    case 'transcribing':
      return { label: '· · ·', hint: 'Catching that…', disabled: true };
    case 'sending':
      return { label: '· · ·', hint: 'Sending…', disabled: true };
    case 'thinking':
      return flags.voiceWake
        ? { label: 'WORKING', hint: 'Agent is thinking — speak to talk over it', disabled: false }
        : { label: 'WORKING', hint: 'Agent is thinking — tap to talk over it', disabled: false };
    case 'speaking':
      return flags.voiceWake
        ? { label: 'TALK', hint: 'Speak to interrupt — or tap', disabled: false }
        : { label: 'TALK', hint: 'Tap to interrupt and speak', disabled: false };
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
