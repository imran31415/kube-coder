import { Fragment } from 'preact';
import { useEffect, useRef, useState } from 'preact/hooks';
import { isErrorResponse } from '../../api/client';
import { subscribeEvents } from '../../api/events';
import {
  fetchPreview,
  sendPreview,
  previewControl,
  type PreviewMessage,
  type PreviewState,
} from '../../api/gatewayPreview';
import { Icon } from '../../components/Icon';
import {
  recognitionCtor,
  sttSupported,
  speakReplies,
  setSpeakReplies,
  handsFree,
  setHandsFree,
  stripForSpeech,
  speakText,
  stopSpeaking,
  type SpeechRecognitionLike,
} from './voice';
import {
  transition,
  levelFromTimeDomain,
  smoothLevel,
  createEndpointer,
  ENDPOINT_OPTS,
  orbCopy,
  orbMood,
  type Endpointer,
  type EndpointMode,
  type VoicePhase,
  type VoiceSignal,
} from './walkieVoice';
import './walkie.css';

/**
 * Walkie-Talkie — the voice-first push-to-talk surface for the in-app
 * loopback preview (issue #401, redesigning #306's text device).
 *
 * One big orb: tap it, speak, tap again (or let the engine's silence timeout
 * end the capture) and the transcript is sent through the SAME Conversation
 * Gateway core a real messaging channel would use, driving a real Hypervisor
 * turn. The reply renders as a response card and — when the speaker is on —
 * is read aloud. Concentric rings around the orb react to REAL mic amplitude
 * (a parallel getUserMedia → AnalyserNode; SpeechRecognition exposes no
 * samples), morph into a rotating sweep while the agent thinks, and pulse in
 * an output color while TTS speaks. Typing is the collapsed fallback, and the
 * utility controls (simulate out-of-window, reset, channel readout) live in a
 * settings popover. Transport is untouched: SSE + a 2s safety poll.
 *
 * Hands-free (issue #406, opt-in via the settings popover): the same
 * AnalyserNode tap stays open across turns (echoCancellation requested, a
 * MIC OPEN chip shows while it's live) and feeds the pure endpointer from
 * walkieVoice.ts — pausing while listening auto-stops the capture (which then
 * auto-sends), and sustained speech while idle/thinking/speaking grabs the
 * floor without a tap (barge-in during TTS is gated on the browser actually
 * granting echo cancellation, so playback can't barge in on itself). The tap
 * is released when the tab hides, the route unmounts, or the mode turns off.
 */

interface AudioTap {
  stream: MediaStream;
  ctx: AudioContext;
  raf: number;
}

/** Thread this transcript entry belongs to, or null when it's an untagged
 *  system notice (not-linked, expired token, workspace list, "nothing is
 *  running", …) that should always render regardless of which thread is
 *  currently live — those aren't conversational content tied to one chat. */
export function messageThreadId(m: PreviewMessage): string | null {
  const t = m.meta?.thread_id;
  return typeof t === 'string' && t ? t : null;
}

/** True when `m` belongs to the live thread, or carries no thread at all
 *  (untagged notices are always visible). Scopes the live card + narration
 *  to the current conversation so a foreign or since-rotated thread's reply
 *  never bleeds in or gets spoken aloud (issue #474). */
export function inLiveThread(m: PreviewMessage, liveThreadId: string | null): boolean {
  const t = messageThreadId(m);
  return t === null || t === liveThreadId;
}

// Defensive settle (#474): even a turn THIS surface started can't pin the
// orb on THINKING… forever — if nothing ever resolves it, give up rather
// than trust the server's busy flag indefinitely.
const MAX_THINKING_MS = 120_000;

export function WalkieTalkie() {
  const [state, setState] = useState<PreviewState | null>(null);
  const [draft, setDraft] = useState('');
  const [busySend, setBusySend] = useState(false);
  const [error, setError] = useState('');
  const linkTried = useRef(false);

  // ── voice state machine ────────────────────────────────────────────────────
  const [phase, setPhase] = useState<VoicePhase>('idle');
  const phaseRef = useRef<VoicePhase>('idle');
  const [interim, setInterim] = useState('');
  const interimRef = useRef('');
  const [micDenied, setMicDenied] = useState(false);
  const [voiceHint, setVoiceHint] = useState('');
  const [showText, setShowText] = useState(!sttSupported());
  const [showHistory, setShowHistory] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  // "What you said" is clamped to two lines; this holds the seq of the
  // utterance the user expanded — a new turn re-collapses it for free
  // because the seq no longer matches (issue #409).
  const [youOpenSeq, setYouOpenSeq] = useState<number | null>(null);
  // Real-level tap unavailable (no getUserMedia / AudioContext, or it failed)
  // → the rings run a smooth simulated pulse instead so nothing looks broken.
  const [simPulse, setSimPulse] = useState(false);
  // The level tap is live (drives the MIC OPEN chip while it's open outside a
  // capture — an always-on mic must be visible, never implied).
  const [micOpen, setMicOpen] = useState(false);

  const recRef = useRef<SpeechRecognitionLike | null>(null);
  const interimElRef = useRef<HTMLDivElement>(null);
  const finalRef = useRef('');
  const tapRef = useRef<AudioTap | null>(null);
  // Hands-free VAD: the endpointer for the current phase-derived mode, and
  // whether the open stream actually got echo cancellation (gates barge-in).
  const epRef = useRef<Endpointer | null>(null);
  const epModeRef = useRef<EndpointMode | null>(null);
  const aecRef = useRef(false);
  const stageRef = useRef<HTMLDivElement>(null);
  const historyRef = useRef<HTMLDivElement>(null);
  const speechTimer = useRef<number | null>(null);
  // Newest outbound seq already narrated; null until the first snapshot lands
  // (history is never narrated — only messages that arrive while watching).
  const narratedSeq = useRef<number | null>(null);
  // Turn ownership (#474): set the moment THIS surface sends a message,
  // cleared once its answer lands (or the watchdog below fires). The gateway
  // busy flag only drives the orb into THINKING… while this is set — never
  // on its own — so activity elsewhere on the bound thread (Hypervisor chat,
  // WhatsApp, cron, a stuck session) can never spin the orb for a query the
  // user never asked here.
  const pendingTurn = useRef<{ sinceCursor: number } | null>(null);
  const busyWatchdog = useRef<number | null>(null);
  // The thread this surface is currently showing. undefined until the first
  // snapshot; tracked so a rotation (new chat / silent re-bind) can reset the
  // narration + turn-ownership bookkeeping instead of bleeding into the new
  // conversation.
  const liveThreadRef = useRef<string | null | undefined>(undefined);

  const speakOn = speakReplies.value;
  const handsFreeOn = handsFree.value;

  function dispatch(sig: VoiceSignal): VoicePhase {
    const next = transition(phaseRef.current, sig);
    phaseRef.current = next;
    setPhase(next);
    return next;
  }

  // ── transport (unchanged from the text device) ────────────────────────────
  async function refresh() {
    try {
      const s = await fetchPreview(0);
      if (isErrorResponse(s)) {
        setError(s.error);
        return;
      }
      setError('');
      setState(s);
    } catch {
      /* transient — the next tick retries */
    }
  }

  useEffect(() => {
    void refresh();
    const unsub = subscribeEvents((ev) => {
      if (ev.type === 'gateway.preview') void refresh();
    });
    // Safety poll: catches the async turn-complete final even if the SSE frame
    // is dropped, and reflects the "thinking" state promptly.
    const timer = window.setInterval(() => void refresh(), 2000);
    return () => {
      unsub();
      clearInterval(timer);
    };
  }, []);

  // Auto-provision the internal link once, so the preview is usable immediately.
  useEffect(() => {
    if (state && state.available && !state.linked && !linkTried.current) {
      linkTried.current = true;
      void previewControl('link').then(() => void refresh());
    }
  }, [state?.linked, state?.available]);

  // Reset narration + turn-ownership bookkeeping whenever the LIVE thread
  // changes (new chat / a silent re-bind because the old thread vanished,
  // #474). Without this, an old thread's still-arriving reply keeps the
  // narration watermark advancing and a pending turn can be "answered" by a
  // conversation that isn't even the one on screen anymore.
  useEffect(() => {
    const tid = state?.thread_id ?? null;
    if (liveThreadRef.current === tid) return;
    liveThreadRef.current = tid;
    narratedSeq.current = null;
    pendingTurn.current = null;
    clearBusyWatchdog();
    if (phaseRef.current === 'thinking') dispatch('quiet');
  }, [state?.thread_id]);

  // ── narration: speak replies that arrive while watching ───────────────────
  useEffect(() => {
    if (!state) return;
    const liveId = state.thread_id;
    const outs = state.messages.filter(
      (m) => m.direction === 'out' && m.kind !== 'notice' && inLiveThread(m, liveId),
    );
    if (narratedSeq.current === null) {
      // First snapshot — everything on screen is history.
      narratedSeq.current = state.cursor;
      return;
    }
    const fresh = outs.filter((m) => m.seq > (narratedSeq.current as number));
    if (fresh.length === 0) return;
    narratedSeq.current = fresh[fresh.length - 1].seq;
    const p = phaseRef.current;
    if (p === 'listening' || p === 'transcribing') return; // never talk over the user
    if (!speakOn) return;
    const text = fresh.map((m) => stripForSpeech(m.text)).filter(Boolean).join('\n');
    if (!text) return;
    speakText(text);
    dispatch('reply');
    watchSpeech();
  }, [state?.cursor, speakOn]);

  // Mirror the gateway busy flag into the phase machine — but ONLY while this
  // surface owns the turn currently in flight (#474). Server `busy` is
  // confirmation, never the trigger: activity elsewhere on the bound thread
  // (Hypervisor chat, WhatsApp, cron, a stuck/crashed session) can no longer
  // spin the orb, and the chip/orb label are both derived from `phase` below
  // so they can't disagree with each other either. Runs after the narration
  // effect, so a just-queued reply keeps the floor.
  useEffect(() => {
    if (!state) return;
    // Resolve our own pending turn the moment its answer lands, regardless of
    // what `state.busy` currently says — this consumes the "I sent
    // something" watermark so a later, unrelated busy can't re-trigger it.
    if (pendingTurn.current) {
      const watermark = pendingTurn.current.sinceCursor;
      const answered = state.messages.some(
        (m) => m.direction === 'out' && m.kind !== 'notice' && m.seq > watermark,
      );
      if (answered) {
        pendingTurn.current = null;
        clearBusyWatchdog();
      }
    }
    const owned = pendingTurn.current !== null;
    if (state.busy && owned) {
      dispatch('busy');
      return;
    }
    const p = phaseRef.current;
    const speaking = ttsBusy();
    if (p === 'thinking' && !speaking && speechTimer.current === null) dispatch('quiet');
  }, [state?.busy, state?.cursor]);

  function ttsBusy(): boolean {
    try {
      // `pending` covers utterances queued but not yet started — without it a
      // poll landing in that gap would settle the phase mid-reply.
      const s = window.speechSynthesis;
      return !!s && (s.speaking || s.pending);
    } catch {
      return false;
    }
  }

  function clearBusyWatchdog() {
    if (busyWatchdog.current !== null) {
      clearTimeout(busyWatchdog.current);
      busyWatchdog.current = null;
    }
  }

  /** Arm the defensive-settle timer for a turn this surface just sent. */
  function armBusyWatchdog() {
    clearBusyWatchdog();
    busyWatchdog.current = window.setTimeout(() => {
      pendingTurn.current = null;
      busyWatchdog.current = null;
      if (phaseRef.current === 'thinking') dispatch('quiet');
    }, MAX_THINKING_MS);
  }

  /** Poll the TTS engine until it goes quiet, then settle the phase. */
  function watchSpeech() {
    if (speechTimer.current) clearInterval(speechTimer.current);
    speechTimer.current = window.setInterval(() => {
      if (ttsBusy()) return;
      if (speechTimer.current) clearInterval(speechTimer.current);
      speechTimer.current = null;
      dispatch('quiet');
    }, 250);
  }

  // ── mic level → visualizer + hands-free VAD (one shared tap) ──────────────

  /** Open the shared mic tap (visualizer levels + endpointer). Idempotent. */
  async function openTap(): Promise<boolean> {
    if (tapRef.current) return true;
    const AC =
      (window as unknown as { AudioContext?: typeof AudioContext }).AudioContext;
    if (!navigator.mediaDevices?.getUserMedia || !AC) return false;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        // AEC matters for hands-free: it strips (some of) our own TTS from
        // the mic signal so barge-in doesn't trigger on the agent's voice.
        audio: { echoCancellation: true, noiseSuppression: true },
      });
      if (tapRef.current) {
        // Raced a concurrent open — keep the first tap.
        stream.getTracks().forEach((t) => t.stop());
        return true;
      }
      aecRef.current =
        stream.getAudioTracks()[0]?.getSettings?.().echoCancellation === true;
      const ctx = new AC();
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 512;
      ctx.createMediaStreamSource(stream).connect(analyser);
      const buf = new Uint8Array(analyser.fftSize);
      let level = 0;
      const tick = () => {
        analyser.getByteTimeDomainData(buf);
        const raw = levelFromTimeDomain(buf);
        level = smoothLevel(level, raw);
        stageRef.current?.style.setProperty('--wt-level', level.toFixed(3));
        // Feed the endpointer the raw level — the visualizer's slow release
        // would otherwise stretch every pause past the hangover.
        feedEndpointer(raw, performance.now());
        if (tapRef.current) tapRef.current.raf = requestAnimationFrame(tick);
      };
      tapRef.current = { stream, ctx, raf: requestAnimationFrame(tick) };
      setMicOpen(true);
      return true;
    } catch {
      return false; // mic denied or busy
    }
  }

  function closeTap() {
    const tap = tapRef.current;
    tapRef.current = null;
    if (tap) {
      cancelAnimationFrame(tap.raf);
      tap.stream.getTracks().forEach((t) => t.stop());
      void tap.ctx.close().catch(() => undefined);
    }
    epRef.current = null;
    epModeRef.current = null;
    setMicOpen(false);
    stageRef.current?.style.setProperty('--wt-level', '0');
  }

  async function startLevels() {
    const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    // Under reduced motion the tap is only worth opening when hands-free
    // needs it for VAD — CSS swaps the ring motion for opacity either way.
    if (reduced && !handsFree.value) return;
    if (!(await openTap())) {
      setSimPulse(true); // no tap — pulse the rings so nothing looks broken
      return;
    }
    if (!handsFree.value && phaseRef.current !== 'listening') {
      // Capture already ended while the permission prompt was up.
      closeTap();
    }
  }

  function stopLevels() {
    setSimPulse(false);
    // Hands-free keeps the tap open between captures — it powers the
    // endpointer (wake/barge-in) and the MIC OPEN chip.
    if (handsFree.value && document.visibilityState !== 'hidden') return;
    closeTap();
  }

  /**
   * Route a level sample into the endpointer for the current phase and map
   * its events onto the phase machine. The pure logic lives in walkieVoice.ts;
   * this picks the mode — endpointing while capturing, wake while idle or
   * thinking, raised-threshold barge-in while TTS plays (and only when echo
   * cancellation was actually granted, so playback can't barge in on itself).
   */
  function feedEndpointer(level: number, now: number) {
    if (!handsFree.value) {
      epRef.current = null;
      epModeRef.current = null;
      return;
    }
    const p = phaseRef.current;
    const mode: EndpointMode | null =
      p === 'listening'
        ? 'listen'
        : p === 'speaking'
          ? aecRef.current
            ? 'barge'
            : null
          : p === 'idle' || p === 'thinking'
            ? 'wake'
            : null; // transcribing | sending — nothing to listen for
    if (mode === null) {
      epRef.current = null;
      epModeRef.current = null;
      return;
    }
    if (!epRef.current || epModeRef.current !== mode) {
      epRef.current = createEndpointer(ENDPOINT_OPTS[mode]);
      epModeRef.current = mode;
    }
    const ev = epRef.current.feed(level, now);
    if (!ev) return;
    if (mode === 'listen') {
      if (ev === 'speech-end') {
        // Auto end-of-speech: same path as tap-to-stop — onend assembles the
        // transcript and auto-sends (or dispatches 'empty').
        dispatch('voice-end');
        try {
          recRef.current?.stop();
        } catch {
          /* already stopped */
        }
      }
    } else if (ev === 'speech-start') {
      startListening('voice-start'); // wake / barge-in: speaking takes the floor
    }
  }

  // ── push-to-talk ──────────────────────────────────────────────────────────
  function startListening(signal: 'press' | 'voice-start' = 'press') {
    const Ctor = recognitionCtor();
    if (!Ctor) return;
    stopSpeaking(); // barge-in: pressing the orb always wins
    if (speechTimer.current) {
      clearInterval(speechTimer.current);
      speechTimer.current = null;
    }
    const rec = new Ctor();
    rec.lang = navigator.language || 'en-US';
    rec.interimResults = true;
    rec.continuous = true;
    finalRef.current = '';
    interimRef.current = '';
    rec.onresult = (e) => {
      let live = '';
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const r = e.results[i];
        const piece = r[0]?.transcript ?? '';
        if (r.isFinal) finalRef.current = `${finalRef.current} ${piece}`.trim();
        else live += piece;
      }
      interimRef.current = live.trim();
      setInterim([finalRef.current, interimRef.current].filter(Boolean).join(' '));
    };
    rec.onerror = (e) => {
      // 'no-speech' (silence timeout) and 'aborted' are routine, not failures.
      if (e.error === 'not-allowed' || e.error === 'service-not-allowed') {
        setMicDenied(true);
      } else if (e.error && e.error !== 'no-speech' && e.error !== 'aborted') {
        setVoiceHint(`Voice input failed: ${e.error}`);
      }
    };
    // Fires on tap-to-stop AND when the engine ends itself (silence timeout).
    rec.onend = () => {
      recRef.current = null;
      stopLevels();
      const text = [finalRef.current, interimRef.current].filter(Boolean).join(' ').trim();
      setInterim('');
      interimRef.current = '';
      if (text) {
        dispatch('captured');
        void sendVoice(text);
      } else {
        const p = dispatch('empty');
        if (p === 'idle' && !micDenied) setVoiceHint('Didn’t catch that — tap and try again');
      }
    };
    recRef.current = rec;
    setVoiceHint('');
    try {
      rec.start();
      dispatch(signal);
      void startLevels();
    } catch {
      recRef.current = null;
    }
  }

  // Hands-free lifecycle: hold the tap open across turns while the mode is
  // on and the tab is visible; release it on hide or when the mode turns off.
  // (PTT captures manage the tap per-capture via startLevels/stopLevels.)
  useEffect(() => {
    if (!handsFreeOn) {
      if (phaseRef.current !== 'listening') closeTap();
      return;
    }
    const open = () =>
      void openTap().then((ok) => {
        // No tap → no VAD: hands-free degrades to plain PTT, and the orb's
        // mic-denied copy explains why.
        if (!ok) setMicDenied(true);
      });
    open();
    const onVis = () => {
      if (document.hidden) closeTap();
      else open();
    };
    document.addEventListener('visibilitychange', onVis);
    return () => document.removeEventListener('visibilitychange', onVis);
  }, [handsFreeOn]);

  /** End the active capture — same path as tapping the orb while listening:
   *  onend assembles the transcript and auto-sends (or dispatches 'empty'). */
  function stopCapture() {
    if (phaseRef.current !== 'listening') return;
    dispatch('press'); // → transcribing; onend delivers the transcript
    try {
      recRef.current?.stop();
    } catch {
      /* already stopped */
    }
  }

  function onOrbPress() {
    const p = phaseRef.current;
    if (p === 'listening') {
      stopCapture();
      return;
    }
    if (p === 'idle' || p === 'speaking' || p === 'thinking') {
      setMicDenied(false); // pressing retries the permission prompt
      startListening();
    }
  }

  async function sendVoice(text: string) {
    try {
      const res = await sendPreview(text);
      // Own this turn (#474) — the busy-mirror effect only shows THINKING…
      // while we're waiting on OUR OWN reply, using this as the watermark.
      pendingTurn.current = { sinceCursor: res.cursor };
      armBusyWatchdog();
      dispatch('sent');
      await refresh();
    } catch {
      setVoiceHint('Send failed — check the gateway and try again');
      pendingTurn.current = null;
      clearBusyWatchdog();
      dispatch('cancel');
    }
  }

  // ── typed / quick-reply sends (the fallback path) ─────────────────────────
  async function send(text: string, button?: string) {
    const payload = (button ?? text).trim();
    if (!payload || busySend) return;
    stopSpeaking();
    setBusySend(true);
    try {
      const res = await sendPreview(button ? '' : text, button);
      // Own this turn (#474) — see sendVoice above for why.
      pendingTurn.current = { sinceCursor: res.cursor };
      armBusyWatchdog();
      if (!button) setDraft('');
      await refresh();
    } catch (err) {
      pendingTurn.current = null;
      clearBusyWatchdog();
      throw err;
    } finally {
      setBusySend(false);
    }
  }

  async function toggleSim() {
    if (!state) return;
    await previewControl('simulate', !state.simulate_out_of_window);
    await refresh();
  }

  async function reset() {
    linkTried.current = false;
    try {
      recRef.current?.abort();
    } catch {
      /* noop */
    }
    stopSpeaking();
    stopLevels();
    setInterim('');
    setVoiceHint('');
    dispatch('cancel');
    narratedSeq.current = null;
    pendingTurn.current = null;
    clearBusyWatchdog();
    await previewControl('reset');
    await refresh();
  }

  // Kill the mic, level tap and queued speech when the page unmounts —
  // closeTap unconditionally, so hands-free never outlives the route.
  useEffect(
    () => () => {
      try {
        recRef.current?.abort();
      } catch {
        /* noop */
      }
      stopSpeaking();
      closeTap();
      if (speechTimer.current) clearInterval(speechTimer.current);
      clearBusyWatchdog();
    },
    [],
  );

  // Keep the history panel pinned to its newest message while open.
  useEffect(() => {
    const el = historyRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [state?.cursor, showHistory]);

  // The interim transcript is height-capped; keep the newest words in view.
  useEffect(() => {
    const el = interimElRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [interim]);

  // ── derived view state ────────────────────────────────────────────────────
  const linked = !!state?.linked;
  // THINKING… is derived from `phase`, not straight from state.busy (#474) —
  // phase is already gated on turn ownership above, so the chip and the orb
  // (whose own label also comes from `phase` via orbCopy below) can never
  // disagree about whether this surface is actually waiting on something.
  const thinking = phase === 'thinking';
  const signal = !state?.available ? 'off' : thinking ? 'busy' : linked ? 'live' : 'down';
  const signalLabel = !state?.available
    ? 'OFFLINE'
    : thinking
      ? 'THINKING…'
      : linked
        ? 'LINKED'
        : 'NOT LINKED';

  // Thread scoping (#474): the live card/lastIn/"you" bubble only ever show
  // the CURRENT conversation — untagged system notices (not-linked, workspace
  // list, …) still always show. The History panel keeps every prior
  // conversation (segmented by a divider below) so nothing is lost, it's just
  // never mixed into the live exchange.
  const liveThreadId = state?.thread_id ?? null;
  const messages = state?.messages ?? [];
  const allConversational = messages.filter((m) => m.kind !== 'notice');
  const liveConversational = allConversational.filter((m) => inLiveThread(m, liveThreadId));
  const lastOutIdx = liveConversational.reduce(
    (acc, m, i) => (m.direction === 'out' ? i : acc),
    -1,
  );
  const card: PreviewMessage | null = lastOutIdx >= 0 ? liveConversational[lastOutIdx] : null;
  // The user's line this reply answers — the newest inbound before/after the card.
  const lastIn = [...liveConversational].reverse().find((m) => m.direction === 'in') ?? null;
  // Everything before the live card, across every thread — the divider below
  // marks where one conversation ends and the next begins.
  const history = card ? allConversational.filter((m) => m.seq < card.seq) : allConversational;
  const youOpen = lastIn !== null && youOpenSeq === lastIn.seq;
  const copy = orbCopy(phase, {
    available: !!state?.available,
    linked,
    stt: sttSupported(),
    micDenied,
    handsFree: handsFreeOn,
    // Only promise voice wake-up when the open mic can actually deliver it.
    voiceWake: handsFreeOn && micOpen && (phase !== 'speaking' || aecRef.current),
  });
  const mood = orbMood(phase);

  function replay() {
    if (!card) return;
    const text = stripForSpeech(card.text);
    if (!text) return;
    stopSpeaking();
    speakText(text);
    if (phaseRef.current === 'idle' || phaseRef.current === 'speaking') {
      dispatch('reply');
      watchSpeech();
    }
  }

  function stopPlayback() {
    stopSpeaking();
    if (speechTimer.current) {
      clearInterval(speechTimer.current);
      speechTimer.current = null;
    }
    if (phaseRef.current === 'speaking') dispatch('quiet');
  }

  return (
    <div class="wt" data-mood={mood} data-phase={phase}>
      {/* ── top strip: channel status + settings popover ── */}
      <div class="wt-top">
        <div class="wt-chips">
          <div class="wt-chip" role="status" aria-live="polite">
            <span class={`wt-led wt-led-${signal}`} aria-hidden="true" />
            <span class="wt-chip-label">{signalLabel}</span>
          </div>
          {/* An open mic outside a capture must be visible, not implied. */}
          {micOpen && phase !== 'listening' && (
            <div class="wt-chip wt-chip-mic" role="status">
              <span class="wt-led wt-led-mic" aria-hidden="true" />
              <span class="wt-chip-label">MIC OPEN</span>
            </div>
          )}
        </div>
        <div class="wt-menu-wrap">
          <button
            type="button"
            class="wt-icon-btn"
            aria-label="Channel settings"
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen((v) => !v)}
          >
            <Icon name="settings" size={16} />
          </button>
          {menuOpen && (
            <div class="wt-menu">
              <div class="wt-menu-readout">
                <div class="wt-readout-row">
                  <span class="wt-readout-label">CH</span>
                  <span class="wt-readout-value">Loopback</span>
                </div>
                <div class="wt-readout-row">
                  <span class="wt-readout-label">MODE</span>
                  <span class="wt-readout-value">INTERNAL LOOPBACK</span>
                </div>
                <div class="wt-readout-row">
                  <span class="wt-readout-label">WINDOW</span>
                  <span class="wt-readout-value">
                    {state?.simulate_out_of_window ? 'CLOSED (sim)' : 'OPEN'}
                  </span>
                </div>
              </div>
              <label class="wt-switch">
                <input
                  type="checkbox"
                  checked={handsFreeOn}
                  onChange={() => setHandsFree(!handsFreeOn)}
                />
                <span class="wt-switch-track" aria-hidden="true">
                  <span class="wt-switch-thumb" />
                </span>
                <span class="wt-switch-label">
                  Hands-free
                  <span class="wt-switch-hint">open mic: auto-send on pause, speak to interrupt</span>
                </span>
              </label>
              <label class="wt-switch">
                <input
                  type="checkbox"
                  checked={!!state?.simulate_out_of_window}
                  onChange={() => void toggleSim()}
                />
                <span class="wt-switch-track" aria-hidden="true">
                  <span class="wt-switch-thumb" />
                </span>
                <span class="wt-switch-label">
                  Simulate out-of-window
                  <span class="wt-switch-hint">show the template path</span>
                </span>
              </label>
              <button type="button" class="wt-ctl-btn" onClick={() => void reset()}>
                Reset conversation
              </button>
            </div>
          )}
        </div>
      </div>

      {/* ── response stage: latest exchange, history behind a toggle ── */}
      <div class="wt-main">
        {error && <div class="wt-error">{error}</div>}
        {history.length > 0 && (
          <button
            type="button"
            class="wt-history-toggle"
            aria-expanded={showHistory}
            onClick={() => setShowHistory((v) => !v)}
          >
            <Icon name="chevron-down" size={12} />
            Transcript ({history.length})
          </button>
        )}
        {showHistory && (
          <div class="wt-history" ref={historyRef}>
            {history.map((m, i) => {
              // A divider marks where one conversation ends and the next
              // begins (#474) — only drawn between two REAL, differing
              // threads; an untagged system notice never breaks the run.
              const tid = messageThreadId(m);
              const prevTid = i > 0 ? messageThreadId(history[i - 1]) : null;
              const showDivider = tid !== null && prevTid !== null && tid !== prevTid;
              return (
                <Fragment key={m.seq}>
                  {showDivider && (
                    <div class="wt-thread-divider" role="separator">
                      <span>New conversation</span>
                    </div>
                  )}
                  <div
                    class={`wt-msg wt-msg-${m.direction} ${m.kind === 'template' ? 'wt-msg-template' : ''}`}
                  >
                    <div class="wt-bubble">
                      {m.kind === 'template' && (
                        <span class="wt-tag">TEMPLATE · out-of-window</span>
                      )}
                      <div class="wt-bubble-text">{m.text}</div>
                    </div>
                  </div>
                </Fragment>
              );
            })}
          </div>
        )}

        {!card && !interim && (
          <div class="wt-empty">
            <p class="wt-empty-title">Talk to your workspace</p>
            <p class="wt-empty-sub">
              Tap the orb and speak. Your words run through the real Conversation
              Gateway pipeline — locally, in internal loopback mode — and the
              answer comes back on this screen and out loud.
            </p>
          </div>
        )}

        {card && (
          <div class="wt-exchange">
            {lastIn && (
              <button
                type="button"
                class="wt-you"
                aria-expanded={youOpen}
                title={youOpen ? 'Collapse your message' : 'Show your full message'}
                onClick={() => setYouOpenSeq(youOpen ? null : lastIn.seq)}
              >
                “{lastIn.text}”
              </button>
            )}
            <div
              class={`wt-card ${card.kind === 'template' ? 'wt-card-template' : ''}`}
              key={card.seq}
            >
              {card.kind === 'template' && (
                <span class="wt-tag">TEMPLATE · out-of-window</span>
              )}
              <div class="wt-card-text">{card.text}</div>
              {card.quick_replies.length > 0 && (
                <div class="wt-replies">
                  {card.quick_replies.map((r, i) => (
                    <button
                      key={i}
                      type="button"
                      class="wt-reply"
                      disabled={busySend}
                      onClick={() => void send(r, r)}
                    >
                      {r}
                    </button>
                  ))}
                </div>
              )}
            </div>
            {/* voice output controls: one glance, one tap */}
            <div class="wt-voice-ctls">
              <button
                type="button"
                class={`wt-icon-btn ${speakOn ? 'is-on' : ''}`}
                aria-pressed={speakOn}
                aria-label={speakOn ? 'Voice replies on' : 'Voice replies off'}
                title={speakOn ? 'Voice replies on — tap to mute' : 'Voice replies off'}
                onClick={() => {
                  setSpeakReplies(!speakOn); // off → also silences mid-reply
                  if (speakOn) stopPlayback();
                }}
              >
                <Icon name="speaker" size={15} />
              </button>
              <button
                type="button"
                class="wt-icon-btn"
                aria-label="Replay reply"
                title="Replay this reply from the start"
                disabled={!ttsSupportedSafe()}
                onClick={replay}
              >
                <Icon name="play" size={15} />
              </button>
              <button
                type="button"
                class="wt-icon-btn"
                aria-label="Stop playback"
                title="Stop playback (keeps voice replies on)"
                onClick={stopPlayback}
              >
                <Icon name="kill" size={15} />
              </button>
            </div>
          </div>
        )}
      </div>

      {/* ── the stage: interim ghost, visualizer rings, the orb ── */}
      <div
        class="wt-stage"
        ref={stageRef}
        data-sim={simPulse ? '1' : undefined}
        style="--wt-level:0"
      >
        {interim && (
          <div class="wt-interim" ref={interimElRef} aria-hidden="true">
            {interim}
          </div>
        )}
        <div class="wt-orb-wrap">
          <div class="wt-rings" aria-hidden="true">
            <span class="wt-ring wt-ring-1" />
            <span class="wt-ring wt-ring-2" />
            <span class="wt-ring wt-ring-3" />
            <span class="wt-sweep" />
          </div>
          <button
            type="button"
            class="wt-orb"
            disabled={copy.disabled}
            aria-pressed={phase === 'listening'}
            aria-label={
              phase === 'listening' ? 'Stop listening and send' : 'Push to talk'
            }
            onClick={onOrbPress}
          >
            <Icon name="mic" size={26} />
            <span class="wt-orb-label">{copy.label}</span>
          </button>
        </div>
        {/* Explicit stop controls (issue #409): a visible tap target while
            the mic is capturing or TTS is playing — the orb tap / barge-in
            gestures stay, this just makes the escape hatch obvious. Both go
            through the existing phase machine (stopCapture → 'press',
            stopPlayback → 'quiet'). */}
        {phase === 'listening' && (
          <button type="button" class="wt-stop-btn" onClick={stopCapture}>
            <Icon name="kill" size={13} /> Stop &amp; send
          </button>
        )}
        {phase === 'speaking' && (
          <button type="button" class="wt-stop-btn" onClick={stopPlayback}>
            <Icon name="kill" size={13} /> Stop voice
          </button>
        )}
        <div class="wt-hint" role="status" aria-live="polite">
          {voiceHint || copy.hint}
        </div>
      </div>

      {/* ── text fallback: collapsed by default, primary when STT is absent ── */}
      <div class="wt-fallback">
        {showText ? (
          <div class="wt-deck">
            <input
              class="wt-input"
              value={draft}
              placeholder={linked ? 'Type a message…' : 'Linking…'}
              aria-label="Message"
              onInput={(e) => setDraft((e.target as HTMLInputElement).value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault();
                  void send(draft);
                }
              }}
            />
            <button
              type="button"
              class="wt-send"
              disabled={busySend || !draft.trim()}
              onClick={() => void send(draft)}
            >
              Send
            </button>
            {sttSupported() && (
              <button
                type="button"
                class="wt-icon-btn"
                aria-label="Hide keyboard input"
                title="Back to voice"
                onClick={() => setShowText(false)}
              >
                <Icon name="close" size={14} />
              </button>
            )}
          </div>
        ) : (
          <button type="button" class="wt-type-link" onClick={() => setShowText(true)}>
            Type instead
          </button>
        )}
      </div>
    </div>
  );
}

function ttsSupportedSafe(): boolean {
  return typeof window !== 'undefined' && 'speechSynthesis' in window;
}
