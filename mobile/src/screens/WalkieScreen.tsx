/**
 * Walkie-Talkie — the voice-first push-to-talk screen (issue #401, redesigning
 * the #306 text parity port). Mobile counterpart of the dashboard SPA's
 * WalkieTalkie (charts/workspace/web/src/routes/hypervisor/WalkieTalkie.tsx):
 * one big orb — tap to talk (or long-press to hold-to-talk), speak, and the
 * recording goes through POST /api/hypervisor/transcribe (RN has no
 * SpeechRecognition, so STT is server-side and the mic only shows when
 * config.stt says a provider key is set). The transcript shows briefly with an
 * undo window (Send now / Edit / Cancel), then auto-sends through the SAME
 * Conversation Gateway core a real messaging channel would use. Replies render
 * on a response card and — with the speaker on — are read aloud via
 * expo-speech, with the shared voice.ts narration rules (never narrate
 * history, sentence-chunked). Rings around the orb track REAL recorder
 * metering (expo-audio isMeteringEnabled → dB), morph into a rotating sweep
 * while the agent thinks, and pulse while speaking; OS reduce-motion swaps
 * continuous motion for steady states. Animation is plain RN Animated — no
 * reanimated dependency. Transport: poll-only (usePolling, 2s, focus-aware),
 * same as every other screen here.
 *
 * Hands-free (issue #406, phase 1 on mobile): the recorder's dB metering —
 * already flowing for the visualizer — also feeds the shared VAD endpointer,
 * so pausing auto-stops the capture, and the undo strip shrinks to a brief
 * cancel flash. Open-mic barge-in stays web-only until the expo-audio /
 * expo-speech shared-session story is proven out (recording during playback
 * re-routes iOS to the quiet earpiece).
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  AccessibilityInfo,
  Animated,
  Easing,
  KeyboardAvoidingView,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Switch,
  Text,
  TextInput,
  Vibration,
  View,
} from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import { SafeAreaView, useSafeAreaInsets } from 'react-native-safe-area-context';
import * as Speech from 'expo-speech';
import { AudioModule, RecordingPresets, setAudioModeAsync, useAudioRecorder, useAudioRecorderState } from 'expo-audio';
import {
  fetchPreview,
  getHypervisorConfig,
  previewControl,
  sendPreview,
  transcribeAudio,
} from '../api/client';
import type { PreviewMessage, PreviewState } from '../api/types';
import { EmptyState, ErrorBanner, Loading, ScreenHeader } from '../components/ui';
import { usePolling } from '../util/usePolling';
import {
  speakText,
  stopSpeaking,
  stripForSpeech,
  readSpeakPref,
  writeSpeakPref,
  readHandsFreePref,
  writeHandsFreePref,
} from '../util/voice';
import {
  transition,
  levelFromDb,
  smoothLevel,
  createEndpointer,
  ENDPOINT_OPTS,
  orbCopy,
  orbMood,
  type Endpointer,
  type VoicePhase,
  type VoiceSignal,
} from '../util/walkieVoice';
import { colors, font, radius, space } from '../theme';

const SIGNAL_COLOR: Record<string, string> = {
  live: colors.success,
  busy: colors.warning,
  down: colors.danger,
  off: colors.textFaint,
};

const MOOD_COLOR: Record<string, string> = {
  idle: colors.accent,
  input: colors.accent,
  processing: colors.warning,
  output: colors.success,
};

/** How long the transcribed text is held for review before it auto-sends. */
const UNDO_MS = 2500;

/** Hands-free keeps only a brief cancel flash — the whole point is a flow
 *  that doesn't stall between turns. Manual PTT keeps the full window. */
const HANDS_FREE_UNDO_MS = 900;

export default function WalkieScreen() {
  const insets = useSafeAreaInsets();
  const [state, setState] = useState<PreviewState | null>(null);
  const [draft, setDraft] = useState('');
  const [busySend, setBusySend] = useState(false);
  const [error, setError] = useState('');
  const linkTried = useRef(false);

  // ── voice state machine ────────────────────────────────────────────────────
  const [phase, setPhase] = useState<VoicePhase>('idle');
  const phaseRef = useRef<VoicePhase>('idle');
  const [sttOn, setSttOn] = useState(false);
  const [speakOn, setSpeakOn] = useState(false);
  const [handsFreeOn, setHandsFreeOn] = useState(false);
  const [micDenied, setMicDenied] = useState(false);
  const [voiceHint, setVoiceHint] = useState('');
  const [pending, setPending] = useState<string | null>(null);
  const [showText, setShowText] = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [reduceMotion, setReduceMotion] = useState(false);
  // Clamped long texts, tap to expand (issue #409): the "what you said" line
  // (keyed by message seq, so a new turn re-collapses it for free) and the
  // pending-transcript strip.
  const [youOpenSeq, setYouOpenSeq] = useState<number | null>(null);
  const [pendingOpen, setPendingOpen] = useState(false);

  const recorder = useAudioRecorder({ ...RecordingPresets.HIGH_QUALITY, isMeteringEnabled: true });
  const recorderState = useAudioRecorderState(recorder, 120);
  const pendingTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const speechTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const holdRef = useRef(false);
  // Newest outbound seq already narrated; null until the first snapshot lands
  // (history is never narrated — only messages that arrive while watching).
  const narratedSeq = useRef<number | null>(null);
  const levelPrev = useRef(0);
  // Hands-free VAD over the recorder metering (issue #406).
  const epRef = useRef<Endpointer | null>(null);

  function dispatch(sig: VoiceSignal): VoicePhase {
    const next = transition(phaseRef.current, sig);
    phaseRef.current = next;
    setPhase(next);
    return next;
  }

  // ── transport (unchanged from the text screen) ────────────────────────────
  const refresh = useCallback(async () => {
    try {
      const s = await fetchPreview(0);
      setError('');
      setState(s);
    } catch (e) {
      // Transient — the next poll retries. Surface a message only if we have
      // nothing to show yet, so a blip doesn't blank an existing transcript.
      setState((prev) => {
        if (!prev) setError(e instanceof Error ? e.message : 'Failed to load');
        return prev;
      });
    }
  }, []);

  usePolling(refresh, 2000);

  useEffect(() => {
    if (state && state.available && !state.linked && !linkTried.current) {
      linkTried.current = true;
      void previewControl('link').then(() => void refresh());
    }
  }, [state?.linked, state?.available, refresh]);

  // Mic gating + prefs + reduce-motion, once on mount.
  useEffect(() => {
    let dead = false;
    void getHypervisorConfig()
      .then((c) => !dead && setSttOn(!!c.stt))
      .catch(() => undefined);
    void readSpeakPref().then((v) => !dead && setSpeakOn(v));
    void readHandsFreePref().then((v) => !dead && setHandsFreeOn(v));
    void AccessibilityInfo.isReduceMotionEnabled().then((v) => !dead && setReduceMotion(v));
    const sub = AccessibilityInfo.addEventListener('reduceMotionChanged', setReduceMotion);
    return () => {
      dead = true;
      sub.remove();
      stopSpeaking();
      if (pendingTimer.current) clearTimeout(pendingTimer.current);
      if (speechTimer.current) clearInterval(speechTimer.current);
    };
  }, []);

  // ── narration: speak replies that arrive while watching ───────────────────
  useEffect(() => {
    if (!state) return;
    const outs = (state.messages ?? []).filter(
      (m) => m.direction === 'out' && m.kind !== 'notice',
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

  // Mirror the gateway busy flag into the phase machine, and settle `thinking`
  // once the turn is over and nothing is being narrated. (Runs after the
  // narration effect, so a just-queued reply keeps the floor.)
  useEffect(() => {
    if (!state) return;
    if (state.busy) {
      dispatch('busy');
      return;
    }
    if (phaseRef.current === 'thinking' && speechTimer.current === null) dispatch('quiet');
  }, [state?.busy, state?.cursor]);

  /** Poll expo-speech until it goes quiet, then settle the phase. The first
   *  reads are grace — the engine can report not-speaking before it starts. */
  function watchSpeech() {
    if (speechTimer.current) clearInterval(speechTimer.current);
    let quietReads = -2; // ~600ms grace before a "not speaking" read counts
    speechTimer.current = setInterval(() => {
      void Speech.isSpeakingAsync()
        .then((speaking) => {
          if (speaking) {
            quietReads = 0;
            return;
          }
          quietReads += 1;
          if (quietReads >= 2 && speechTimer.current) {
            clearInterval(speechTimer.current);
            speechTimer.current = null;
            dispatch('quiet');
          }
        })
        .catch(() => undefined);
    }, 300);
  }

  // ── push-to-talk ──────────────────────────────────────────────────────────
  async function startRecording() {
    stopSpeaking(); // barge-in: pressing the orb always wins
    if (speechTimer.current) {
      clearInterval(speechTimer.current);
      speechTimer.current = null;
    }
    cancelPending();
    const perm = await AudioModule.requestRecordingPermissionsAsync();
    if (!perm.granted) {
      setMicDenied(true);
      return;
    }
    setMicDenied(false);
    try {
      await setAudioModeAsync({ allowsRecording: true, playsInSilentMode: true });
      await recorder.prepareToRecordAsync();
      recorder.record();
      setVoiceHint('');
      Vibration.vibrate(10);
      dispatch('press');
    } catch (e) {
      setVoiceHint(e instanceof Error ? e.message : 'Could not start recording');
      dispatch('cancel');
    }
  }

  async function stopRecording(signal: 'press' | 'voice-end' = 'press') {
    epRef.current = null;
    dispatch(signal); // listening → transcribing
    Vibration.vibrate(10);
    try {
      await recorder.stop();
      // Hand the audio session back to playback so spoken replies use the
      // main speaker instead of the (quiet) earpiece route on iOS.
      await setAudioModeAsync({ allowsRecording: false, playsInSilentMode: true });
      const uri = recorder.uri;
      if (!uri) {
        dispatch('empty');
        return;
      }
      const text = (await transcribeAudio(uri)).trim();
      if (!text) {
        dispatch('empty');
        setVoiceHint('Didn’t catch that — tap and try again');
        return;
      }
      // Undo window: show the transcript briefly, then auto-send. "Edit" drops
      // it into the text composer instead; "Cancel" discards it. Hands-free
      // shortens this to a flash so the conversation keeps moving.
      setPending(text);
      pendingTimer.current = setTimeout(
        () => void sendPending(text),
        handsFreeOn ? HANDS_FREE_UNDO_MS : UNDO_MS,
      );
    } catch (e) {
      dispatch('cancel');
      setVoiceHint(e instanceof Error ? e.message : 'Transcription failed');
    }
  }

  function cancelPending() {
    if (pendingTimer.current) {
      clearTimeout(pendingTimer.current);
      pendingTimer.current = null;
    }
    setPending(null);
    setPendingOpen(false);
  }

  async function sendPending(text: string) {
    cancelPending();
    dispatch('captured');
    try {
      await sendPreview(text);
      dispatch('sent');
      await refresh();
    } catch (e) {
      setVoiceHint(e instanceof Error ? e.message : 'Send failed');
      dispatch('cancel');
    }
  }

  function onOrbPress() {
    const p = phaseRef.current;
    if (p === 'listening') {
      if (holdRef.current) return; // hold mode ends on press-out, not tap
      void stopRecording();
      return;
    }
    if (p === 'idle' || p === 'speaking' || p === 'thinking') void startRecording();
  }

  function onOrbLongPress() {
    const p = phaseRef.current;
    if (p === 'idle' || p === 'speaking' || p === 'thinking') {
      holdRef.current = true;
      void startRecording();
    }
  }

  function onOrbPressOut() {
    if (holdRef.current) {
      holdRef.current = false;
      if (phaseRef.current === 'listening') void stopRecording();
    }
  }

  // ── typed / quick-reply sends (the fallback path) ─────────────────────────
  async function send(text: string, button?: string) {
    const payload = (button ?? text).trim();
    if (!payload || busySend) return;
    stopSpeaking();
    setBusySend(true);
    try {
      await sendPreview(button ? '' : text, button);
      if (!button) setDraft('');
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to send');
    } finally {
      setBusySend(false);
    }
  }

  async function toggleSim() {
    if (!state) return;
    try {
      await previewControl('simulate', !state.simulate_out_of_window);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to toggle');
    }
  }

  async function reset() {
    linkTried.current = false;
    stopSpeaking();
    cancelPending();
    if (speechTimer.current) {
      clearInterval(speechTimer.current);
      speechTimer.current = null;
    }
    dispatch('cancel');
    narratedSeq.current = null;
    setVoiceHint('');
    try {
      await previewControl('reset');
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to reset');
    }
  }

  function toggleHandsFree() {
    setHandsFreeOn((v) => {
      const next = !v;
      void writeHandsFreePref(next);
      return next;
    });
  }

  function toggleSpeak() {
    setSpeakOn((v) => {
      const next = !v;
      void writeSpeakPref(next);
      if (!next) stopPlayback(); // off mid-reply silences immediately
      return next;
    });
  }

  function stopPlayback() {
    stopSpeaking();
    if (speechTimer.current) {
      clearInterval(speechTimer.current);
      speechTimer.current = null;
    }
    if (phaseRef.current === 'speaking') dispatch('quiet');
  }

  // ── visualizer (plain Animated; no reanimated dependency) ─────────────────
  const mood = orbMood(phase);
  const moodColor = MOOD_COLOR[mood];
  const levelAnim = useRef(new Animated.Value(0)).current;
  const spinAnim = useRef(new Animated.Value(0)).current;

  // Input mood: rings track REAL recorder metering (dB → 0..1). If the meter
  // gives nothing, fall back to a smooth simulated pulse so it never looks
  // broken. Reduce-motion holds the rings at a steady level.
  const metering = recorderState.metering;
  useEffect(() => {
    if (phase !== 'listening' || reduceMotion) return;
    if (metering == null) return; // sim pulse loop handles this case below
    const next = smoothLevel(levelPrev.current, levelFromDb(metering));
    levelPrev.current = next;
    Animated.timing(levelAnim, {
      toValue: next,
      duration: 110,
      easing: Easing.out(Easing.quad),
      useNativeDriver: true,
    }).start();
  }, [metering, phase, reduceMotion, levelAnim]);

  // Hands-free endpointing (issue #406): the same metering samples feed the
  // shared VAD, and end-of-speech stops the capture as if the orb was tapped.
  // Hold-to-talk keeps manual control (press-out decides), and this runs even
  // under reduce-motion — it's input handling, not decoration.
  useEffect(() => {
    if (phase !== 'listening' || !handsFreeOn || holdRef.current) {
      epRef.current = null;
      return;
    }
    if (metering == null) return; // no meter (simulator) — manual stop only
    if (!epRef.current) epRef.current = createEndpointer(ENDPOINT_OPTS.listen);
    const ev = epRef.current.feed(levelFromDb(metering), Date.now());
    if (ev === 'speech-end') void stopRecording('voice-end');
  }, [metering, phase, handsFreeOn]);

  // Mood loops: simulated input pulse (no meter), output pulse, idle breathe.
  useEffect(() => {
    levelPrev.current = 0;
    levelAnim.stopAnimation();
    spinAnim.stopAnimation();
    spinAnim.setValue(0);
    if (reduceMotion) {
      // Steady, readable states instead of motion.
      levelAnim.setValue(mood === 'idle' ? 0.15 : 0.55);
      return;
    }
    if (mood === 'processing') {
      levelAnim.setValue(0.25);
      Animated.loop(
        Animated.timing(spinAnim, {
          toValue: 1,
          duration: 1300,
          easing: Easing.linear,
          useNativeDriver: true,
        }),
      ).start();
      return;
    }
    const pulse = (lo: number, hi: number, ms: number) =>
      Animated.loop(
        Animated.sequence([
          Animated.timing(levelAnim, { toValue: hi, duration: ms, easing: Easing.inOut(Easing.quad), useNativeDriver: true }),
          Animated.timing(levelAnim, { toValue: lo, duration: ms, easing: Easing.inOut(Easing.quad), useNativeDriver: true }),
        ]),
      );
    if (mood === 'output') {
      pulse(0.25, 0.85, 550).start();
    } else if (mood === 'idle') {
      pulse(0.05, 0.22, 2100).start();
    } else if (mood === 'input' && metering == null) {
      // No level data (simulator / meter unavailable) → simulated pulse.
      pulse(0.3, 0.8, 800).start();
    }
    // `metering == null` only matters at listen-start; once real values flow
    // the timing effect above takes over the same Animated.Value.
  }, [mood, reduceMotion, levelAnim, spinAnim]);

  const ringScale = (k: number) =>
    levelAnim.interpolate({ inputRange: [0, 1], outputRange: [1, 1 + k] });
  const ringOpacity = (base: number, gain: number) =>
    levelAnim.interpolate({ inputRange: [0, 1], outputRange: [base, Math.min(1, base + gain)] });
  const spin = spinAnim.interpolate({ inputRange: [0, 1], outputRange: ['0deg', '360deg'] });

  // ── derived view state ────────────────────────────────────────────────────
  if (!state) {
    return (
      <SafeAreaView style={styles.root} edges={['top']}>
        <ScreenHeader title="Walkie-Talkie" subtitle="Voice-first loopback channel" />
        {error ? (
          <EmptyState icon="radio-outline" title="Gateway unavailable" subtitle={error} />
        ) : (
          <Loading label="Connecting…" />
        )}
      </SafeAreaView>
    );
  }

  const linked = !!state.linked;
  const busy = !!state.busy;
  const signal = !state.available ? 'off' : busy ? 'busy' : linked ? 'live' : 'down';
  const signalLabel = !state.available
    ? 'OFFLINE'
    : busy
      ? 'THINKING…'
      : linked
        ? 'LINKED'
        : 'NOT LINKED';

  const conversational = (state.messages ?? []).filter((m) => m.kind !== 'notice');
  const lastOutIdx = conversational.reduce(
    (acc, m, i) => (m.direction === 'out' ? i : acc),
    -1,
  );
  const card: PreviewMessage | null = lastOutIdx >= 0 ? conversational[lastOutIdx] : null;
  const lastIn = [...conversational].reverse().find((m) => m.direction === 'in') ?? null;
  const history = conversational.slice(0, Math.max(lastOutIdx, 0));
  const copy = orbCopy(phase, {
    available: !!state.available,
    linked,
    stt: sttOn,
    micDenied,
    handsFree: handsFreeOn,
    voiceWake: false, // open-mic wake/barge-in is web-only for now
  });
  const hint = pending
    ? 'Sending in a moment — Edit to change it'
    : voiceHint || copy.hint;
  const orbDisabled = copy.disabled && !pending;

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

  return (
    <SafeAreaView style={styles.root} edges={['top']}>
      <ScreenHeader
        title="Walkie-Talkie"
        subtitle="Press the orb and talk to your workspace"
        right={
          <View style={styles.headerRight}>
            <View style={styles.status} accessibilityRole="text">
              <View style={[styles.led, { backgroundColor: SIGNAL_COLOR[signal] }]} />
              <Text style={[styles.statusText, { color: SIGNAL_COLOR[signal] }]}>{signalLabel}</Text>
            </View>
            <Pressable
              onPress={() => setMenuOpen((v) => !v)}
              hitSlop={8}
              accessibilityRole="button"
              accessibilityLabel="Channel settings"
              style={({ pressed }) => [styles.menuBtn, pressed && { opacity: 0.6 }]}
            >
              <Ionicons name="options-outline" size={18} color={colors.textMuted} />
            </Pressable>
          </View>
        }
      />

      {menuOpen && (
        <>
          <Pressable style={styles.menuBackdrop} onPress={() => setMenuOpen(false)} />
          <View style={styles.menu}>
            <View style={styles.readout}>
              <Text style={styles.readoutLine}>
                <Text style={styles.readoutLabel}>CH </Text>Loopback
              </Text>
              <Text style={styles.readoutLine}>
                <Text style={styles.readoutLabel}>MODE </Text>INTERNAL LOOPBACK
              </Text>
              <Text style={styles.readoutLine}>
                <Text style={styles.readoutLabel}>WINDOW </Text>
                {state.simulate_out_of_window ? 'CLOSED (sim)' : 'OPEN'}
              </Text>
            </View>
            <Pressable style={styles.switchRow} onPress={toggleHandsFree} accessibilityRole="switch">
              <Switch
                value={handsFreeOn}
                onValueChange={toggleHandsFree}
                trackColor={{ true: colors.accent, false: colors.surface3 }}
                thumbColor={colors.text}
              />
              <View style={styles.switchLabelWrap}>
                <Text style={styles.switchLabel}>Hands-free</Text>
                <Text style={styles.switchHint}>auto-send when you pause</Text>
              </View>
            </Pressable>
            <Pressable style={styles.switchRow} onPress={() => void toggleSim()} accessibilityRole="switch">
              <Switch
                value={!!state.simulate_out_of_window}
                onValueChange={() => void toggleSim()}
                trackColor={{ true: colors.accent, false: colors.surface3 }}
                thumbColor={colors.text}
              />
              <View style={styles.switchLabelWrap}>
                <Text style={styles.switchLabel}>Simulate out-of-window</Text>
                <Text style={styles.switchHint}>show the template path</Text>
              </View>
            </Pressable>
            <Pressable
              onPress={() => {
                setMenuOpen(false);
                void reset();
              }}
              accessibilityRole="button"
              accessibilityLabel="Reset conversation"
              style={({ pressed }) => [styles.resetBtn, pressed && { opacity: 0.6 }]}
            >
              <Ionicons name="refresh" size={14} color={colors.textMuted} />
              <Text style={styles.resetText}>Reset conversation</Text>
            </Pressable>
          </View>
        </>
      )}

      {/* ── response stage: latest exchange, history behind a toggle ── */}
      <ScrollView style={styles.flex} contentContainerStyle={styles.main}>
        {history.length > 0 && (
          <Pressable
            onPress={() => setShowHistory((v) => !v)}
            accessibilityRole="button"
            accessibilityLabel={showHistory ? 'Hide transcript' : 'Show transcript'}
            style={styles.historyToggle}
          >
            <Ionicons
              name={showHistory ? 'chevron-up' : 'chevron-down'}
              size={12}
              color={colors.textFaint}
            />
            <Text style={styles.historyToggleText}>Transcript ({history.length})</Text>
          </Pressable>
        )}
        {showHistory && (
          /* Height-capped, so it must scroll itself — a plain View here
             silently clipped everything past the cap (issue #409). */
          <ScrollView style={styles.history} contentContainerStyle={styles.historyContent} nestedScrollEnabled>
            {history.map((m) => {
              const isOut = m.direction === 'out';
              return (
                <View key={m.seq} style={[styles.msgRow, isOut ? styles.msgRowOut : styles.msgRowIn]}>
                  <View style={[styles.bubble, isOut ? styles.bubbleOut : styles.bubbleIn]}>
                    {m.kind === 'template' && <Text style={styles.tmplTag}>TEMPLATE · out-of-window</Text>}
                    <Text style={styles.bubbleText}>{m.text}</Text>
                  </View>
                </View>
              );
            })}
          </ScrollView>
        )}

        {!card && (
          <View style={styles.welcome}>
            <EmptyState
              icon="radio-outline"
              title="Talk to your workspace"
              subtitle="Tap the orb and speak. Your words run through the real Conversation Gateway pipeline — locally, in internal loopback mode — and the answer comes back on this screen and out loud."
            />
          </View>
        )}

        {card && (
          <View style={styles.exchange}>
            {lastIn && (
              <Pressable
                onPress={() =>
                  setYouOpenSeq(youOpenSeq === lastIn.seq ? null : lastIn.seq)
                }
                accessibilityRole="button"
                accessibilityLabel={
                  youOpenSeq === lastIn.seq
                    ? 'Collapse your message'
                    : 'Show your full message'
                }
                style={styles.youWrap}
              >
                <Text style={styles.you} numberOfLines={youOpenSeq === lastIn.seq ? undefined : 2}>
                  “{lastIn.text}”
                </Text>
              </Pressable>
            )}
            <View style={[styles.card, card.kind === 'template' && styles.cardTemplate]}>
              {card.kind === 'template' && <Text style={styles.tmplTag}>TEMPLATE · out-of-window</Text>}
              <Text style={styles.cardText}>{card.text}</Text>
              {card.quick_replies.length > 0 && (
                <View style={styles.replies}>
                  {card.quick_replies.map((r, i) => (
                    <Pressable
                      key={i}
                      onPress={() => void send(r, r)}
                      disabled={busySend}
                      style={({ pressed }) => [styles.reply, pressed && styles.replyPressed, busySend && styles.replyOff]}
                    >
                      <Text style={styles.replyText}>{r}</Text>
                    </Pressable>
                  ))}
                </View>
              )}
            </View>
            {/* voice output controls: one glance, one tap */}
            <View style={styles.voiceCtls}>
              <Pressable
                onPress={toggleSpeak}
                accessibilityRole="button"
                accessibilityLabel={speakOn ? 'Turn voice replies off' : 'Turn voice replies on'}
                style={[styles.ctlBtn, speakOn && styles.ctlBtnOn]}
              >
                <Ionicons
                  name={speakOn ? 'volume-high' : 'volume-mute'}
                  size={16}
                  color={speakOn ? colors.accent : colors.textMuted}
                />
              </Pressable>
              <Pressable
                onPress={replay}
                accessibilityRole="button"
                accessibilityLabel="Replay reply"
                style={styles.ctlBtn}
              >
                <Ionicons name="play" size={16} color={colors.textMuted} />
              </Pressable>
              <Pressable
                onPress={stopPlayback}
                accessibilityRole="button"
                accessibilityLabel="Stop playback"
                style={styles.ctlBtn}
              >
                <Ionicons name="stop" size={16} color={colors.textMuted} />
              </Pressable>
            </View>
          </View>
        )}
      </ScrollView>

      {error ? <ErrorBanner message={error} /> : null}

      {/* ── undo window: transcript shown briefly before it auto-sends ── */}
      {pending && (
        <View style={styles.pendingStrip}>
          <Pressable
            onPress={() => setPendingOpen((v) => !v)}
            accessibilityRole="button"
            accessibilityLabel={pendingOpen ? 'Collapse transcript' : 'Show full transcript'}
          >
            <Text style={styles.pendingText} numberOfLines={pendingOpen ? undefined : 2}>
              “{pending}”
            </Text>
          </Pressable>
          <View style={styles.pendingBtns}>
            <Pressable onPress={() => void sendPending(pending)} style={styles.pendingSend}>
              <Text style={styles.pendingSendText}>Send now</Text>
            </Pressable>
            <Pressable
              onPress={() => {
                const text = pending;
                cancelPending();
                dispatch('empty');
                setDraft(text);
                setShowText(true);
              }}
              style={styles.pendingAlt}
            >
              <Text style={styles.pendingAltText}>Edit</Text>
            </Pressable>
            <Pressable
              onPress={() => {
                cancelPending();
                dispatch('empty');
              }}
              style={styles.pendingAlt}
            >
              <Text style={styles.pendingAltText}>Cancel</Text>
            </Pressable>
          </View>
        </View>
      )}

      {/* ── the stage: visualizer rings + the orb + hint ── */}
      <View style={styles.stage}>
        <View style={styles.orbWrap}>
          {[0.32, 0.2, 0.1].map((k, i) => (
            <Animated.View
              key={k}
              pointerEvents="none"
              style={[
                styles.ring,
                { margin: i * 13 },
                {
                  borderColor: moodColor,
                  opacity: ringOpacity(0.14 + i * 0.12, 0.5),
                  transform: [{ scale: ringScale(k) }],
                },
              ]}
            />
          ))}
          {mood === 'processing' && (
            <Animated.View
              pointerEvents="none"
              style={[styles.sweep, { borderTopColor: moodColor, transform: [{ rotate: spin }] }]}
            />
          )}
          <Pressable
            onPress={onOrbPress}
            onLongPress={onOrbLongPress}
            onPressOut={onOrbPressOut}
            delayLongPress={220}
            disabled={orbDisabled}
            accessibilityRole="button"
            accessibilityState={{ disabled: orbDisabled, selected: phase === 'listening' }}
            accessibilityLabel={phase === 'listening' ? 'Stop listening and send' : 'Push to talk'}
            accessibilityHint="Tap to toggle, or press and hold to talk"
            style={({ pressed }) => [
              styles.orb,
              { backgroundColor: orbDisabled ? colors.surface3 : moodColor },
              pressed && !orbDisabled && { transform: [{ scale: 0.96 }] },
            ]}
          >
            <Ionicons name="mic" size={30} color={orbDisabled ? colors.textMuted : colors.accentText} />
            <Text style={[styles.orbLabel, orbDisabled && { color: colors.textMuted }]}>{copy.label}</Text>
          </Pressable>
        </View>
        {/* Explicit stop controls (issue #409): a visible tap target while the
            mic is capturing or narration is playing — the orb tap stays, this
            just makes the escape hatch obvious. Both ride the existing phase
            machine (stopRecording → 'press', stopPlayback → 'quiet'). */}
        {phase === 'listening' && (
          <Pressable
            onPress={() => void stopRecording()}
            accessibilityRole="button"
            accessibilityLabel="Stop recording and send"
            style={({ pressed }) => [styles.stopBtn, pressed && { opacity: 0.7 }]}
          >
            <Ionicons name="stop" size={13} color={colors.text} />
            <Text style={styles.stopText}>{'Stop & send'}</Text>
          </Pressable>
        )}
        {phase === 'speaking' && (
          <Pressable
            onPress={stopPlayback}
            accessibilityRole="button"
            accessibilityLabel="Stop voice playback"
            style={({ pressed }) => [styles.stopBtn, pressed && { opacity: 0.7 }]}
          >
            <Ionicons name="stop" size={13} color={colors.text} />
            <Text style={styles.stopText}>Stop voice</Text>
          </Pressable>
        )}
        <Text style={styles.hint} accessibilityLiveRegion="polite">
          {hint}
        </Text>
      </View>

      {/* ── text fallback: collapsed by default, primary when STT is absent ──
          KeyboardAvoidingView so the iOS keyboard doesn't cover the composer
          (Android resizes the window itself); same pattern as TaskDetail. */}
      <KeyboardAvoidingView behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
        <View style={[styles.fallback, { paddingBottom: Math.max(insets.bottom, space.sm) }]}>
          {showText || !sttOn ? (
            <View style={styles.composer}>
              <TextInput
                style={styles.input}
                value={draft}
                onChangeText={setDraft}
                placeholder={linked ? 'Type a message…' : 'Linking…'}
                placeholderTextColor={colors.textFaint}
                editable={!busySend}
                returnKeyType="send"
                onSubmitEditing={() => void send(draft)}
                accessibilityLabel="Message"
              />
              <Pressable
                onPress={() => void send(draft)}
                disabled={busySend || !draft.trim()}
                accessibilityRole="button"
                accessibilityLabel="Send message"
                style={[styles.sendBtn, (busySend || !draft.trim()) && styles.sendOff]}
              >
                <Ionicons name="send" size={16} color={colors.accentText} />
              </Pressable>
              {sttOn && (
                <Pressable
                  onPress={() => setShowText(false)}
                  hitSlop={6}
                  accessibilityRole="button"
                  accessibilityLabel="Hide keyboard input"
                  style={styles.ctlBtn}
                >
                  <Ionicons name="close" size={16} color={colors.textMuted} />
                </Pressable>
              )}
            </View>
          ) : (
            <Pressable
              onPress={() => setShowText(true)}
              accessibilityRole="button"
              accessibilityLabel="Type instead"
              style={styles.typeLink}
            >
              <Text style={styles.typeLinkText}>Type instead</Text>
            </Pressable>
          )}
        </View>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: colors.bg },
  flex: { flex: 1 },
  headerRight: { flexDirection: 'row', alignItems: 'center', gap: space.md },
  status: { flexDirection: 'row', alignItems: 'center', gap: 6 },
  led: { width: 8, height: 8, borderRadius: 4 },
  statusText: { fontSize: font.size.xs, fontWeight: '700', letterSpacing: 0.4 },
  menuBtn: { padding: 2 },

  menuBackdrop: { position: 'absolute', top: 0, left: 0, right: 0, bottom: 0, zIndex: 9 },
  menu: {
    position: 'absolute',
    top: 92,
    right: space.lg,
    zIndex: 10,
    width: 268,
    gap: space.md,
    padding: space.md,
    backgroundColor: colors.bgElevated,
    borderWidth: 1,
    borderColor: colors.borderStrong,
    borderRadius: radius.lg,
    shadowColor: '#000',
    shadowOpacity: 0.35,
    shadowRadius: 14,
    shadowOffset: { width: 0, height: 6 },
    elevation: 8,
  },
  readout: {
    gap: 3,
    padding: space.sm,
    borderRadius: radius.md,
    backgroundColor: colors.surface2,
    borderWidth: 1,
    borderColor: colors.border,
  },
  readoutLine: { color: colors.text, fontSize: font.size.xs, fontFamily: font.mono, fontWeight: '600' },
  readoutLabel: { color: colors.textFaint, fontWeight: '700', letterSpacing: 0.5 },
  switchRow: { flexDirection: 'row', alignItems: 'center', gap: space.sm },
  switchLabelWrap: { flexShrink: 1 },
  switchLabel: { color: colors.text, fontSize: font.size.sm, fontWeight: '500' },
  switchHint: { color: colors.textFaint, fontSize: font.size.xs },
  resetBtn: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 5,
    paddingHorizontal: space.md,
    paddingVertical: 7,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.md,
    backgroundColor: colors.card,
    alignSelf: 'flex-start',
  },
  resetText: { color: colors.textMuted, fontSize: font.size.sm, fontWeight: '600' },

  main: { flexGrow: 1, justifyContent: 'flex-end', padding: space.lg, gap: space.md },
  historyToggle: {
    alignSelf: 'center',
    flexDirection: 'row',
    alignItems: 'center',
    gap: 5,
    paddingVertical: 4,
    paddingHorizontal: space.md,
  },
  historyToggleText: { color: colors.textFaint, fontSize: font.size.xs, fontWeight: '600' },
  history: {
    maxHeight: 260,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.lg,
    backgroundColor: colors.card,
    flexGrow: 0,
  },
  historyContent: { gap: space.sm, padding: space.sm },
  msgRow: { maxWidth: '86%' },
  msgRowOut: { alignSelf: 'flex-start' },
  msgRowIn: { alignSelf: 'flex-end' },
  bubble: {
    borderRadius: radius.lg,
    paddingHorizontal: space.md,
    paddingVertical: space.sm,
    borderWidth: 1,
  },
  bubbleOut: { backgroundColor: colors.surface2, borderColor: colors.border },
  bubbleIn: { backgroundColor: colors.accent, borderColor: colors.accent },
  bubbleText: { color: colors.text, fontSize: font.size.sm, lineHeight: 19 },
  tmplTag: {
    color: colors.warning,
    fontSize: 10,
    fontWeight: '700',
    letterSpacing: 0.4,
    marginBottom: 4,
    textTransform: 'uppercase',
  },
  welcome: { paddingVertical: space.xl },

  exchange: { alignItems: 'center', gap: space.sm },
  youWrap: { maxWidth: '90%' },
  you: {
    color: colors.textFaint,
    fontSize: font.size.xs,
    fontStyle: 'italic',
    textAlign: 'center',
  },
  card: {
    width: '100%',
    padding: space.lg,
    borderRadius: radius.lg,
    backgroundColor: colors.card,
    borderWidth: 1,
    borderColor: colors.borderStrong,
  },
  cardTemplate: { borderColor: colors.warning },
  cardText: { color: colors.text, fontSize: font.size.lg, lineHeight: 25 },
  replies: { flexDirection: 'row', flexWrap: 'wrap', gap: space.sm, marginTop: space.md },
  reply: {
    borderWidth: 1,
    borderColor: colors.accent,
    borderRadius: radius.pill ?? 999,
    paddingHorizontal: space.md,
    paddingVertical: 6,
  },
  replyPressed: { opacity: 0.7 },
  replyOff: { opacity: 0.5 },
  replyText: { color: colors.accent, fontSize: font.size.sm, fontWeight: '600' },
  voiceCtls: { flexDirection: 'row', gap: space.sm },
  ctlBtn: {
    width: 34,
    height: 34,
    borderRadius: 17,
    alignItems: 'center',
    justifyContent: 'center',
    borderWidth: 1,
    borderColor: colors.border,
    backgroundColor: colors.card,
  },
  ctlBtnOn: { borderColor: colors.accent },

  pendingStrip: {
    marginHorizontal: space.lg,
    marginBottom: space.sm,
    padding: space.md,
    gap: space.sm,
    borderRadius: radius.lg,
    borderWidth: 1,
    borderColor: colors.accent,
    backgroundColor: colors.surface2,
  },
  pendingText: { color: colors.text, fontSize: font.size.sm, fontStyle: 'italic' },
  pendingBtns: { flexDirection: 'row', gap: space.sm },
  pendingSend: {
    paddingHorizontal: space.md,
    paddingVertical: 6,
    borderRadius: radius.md,
    backgroundColor: colors.accent,
  },
  pendingSendText: { color: colors.accentText, fontSize: font.size.sm, fontWeight: '700' },
  pendingAlt: {
    paddingHorizontal: space.md,
    paddingVertical: 6,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: colors.border,
  },
  pendingAltText: { color: colors.textMuted, fontSize: font.size.sm, fontWeight: '600' },

  stage: { alignItems: 'center', gap: space.sm, paddingVertical: space.sm },
  orbWrap: {
    width: 190,
    height: 190,
    alignItems: 'center',
    justifyContent: 'center',
  },
  ring: {
    position: 'absolute', top: 0, left: 0, right: 0, bottom: 0,
    borderRadius: 95,
    borderWidth: 1.5,
  },
  sweep: {
    position: 'absolute', top: 0, left: 0, right: 0, bottom: 0,
    margin: 4,
    borderRadius: 91,
    borderWidth: 3,
    borderColor: 'transparent',
  },
  orb: {
    width: 124,
    height: 124,
    borderRadius: 62,
    alignItems: 'center',
    justifyContent: 'center',
    gap: 4,
    shadowColor: '#000',
    shadowOpacity: 0.3,
    shadowRadius: 12,
    shadowOffset: { width: 0, height: 6 },
    elevation: 6,
  },
  orbLabel: {
    color: colors.accentText,
    fontSize: 11,
    fontWeight: '800',
    letterSpacing: 1.2,
    fontFamily: font.mono,
  },
  stopBtn: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 6,
    minHeight: 40,
    paddingHorizontal: space.lg,
    paddingVertical: 8,
    borderRadius: radius.pill ?? 999,
    borderWidth: 1,
    borderColor: colors.borderStrong,
    backgroundColor: colors.card,
  },
  stopText: { color: colors.text, fontSize: font.size.sm, fontWeight: '700' },
  hint: {
    minHeight: 16,
    color: colors.textMuted,
    fontSize: font.size.xs,
    textAlign: 'center',
    paddingHorizontal: space.lg,
  },

  fallback: { alignItems: 'center', paddingHorizontal: space.md, paddingTop: 2 },
  composer: { flexDirection: 'row', alignItems: 'center', gap: space.sm, width: '100%' },
  input: {
    flex: 1,
    maxHeight: 120,
    color: colors.text,
    fontSize: font.size.md,
    backgroundColor: colors.surface2,
    borderRadius: radius.lg,
    paddingHorizontal: space.md,
    paddingVertical: space.sm,
  },
  sendBtn: {
    width: 40,
    height: 40,
    borderRadius: 20,
    backgroundColor: colors.accent,
    alignItems: 'center',
    justifyContent: 'center',
  },
  sendOff: { opacity: 0.4 },
  typeLink: { paddingVertical: 6, paddingHorizontal: space.md },
  typeLinkText: {
    color: colors.textFaint,
    fontSize: font.size.xs,
    fontWeight: '600',
    textDecorationLine: 'underline',
  },
});
