/**
 * Walkie-Talkie — the in-app WhatsApp gateway preview, on mobile. Parity with
 * the dashboard SPA's WalkieTalkie (charts/workspace/web/src/routes/hypervisor/
 * WalkieTalkie.tsx): type a message and it runs through the SAME Conversation
 * Gateway core the real WhatsApp webhook uses — driving a real Hypervisor turn —
 * and comes back rendered the way WhatsApp would show it (bubbles, tap-buttons,
 * out-of-window templates). Each bubble can reveal the raw provider "wire"
 * payload. Only the transport is simulated; the agent and pipeline are real.
 *
 * Transport: poll-only (usePolling, 2s, focus-aware). The web component layers
 * SSE on top of a 2s safety poll, but EventSource can't send a Bearer header
 * (see src/api/client.ts), so mobile — like every other screen here — just
 * polls. It is TEXT + quick-reply only: no device audio, no microphone.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  KeyboardAvoidingView,
  type NativeScrollEvent,
  type NativeSyntheticEvent,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Switch,
  Text,
  TextInput,
  View,
} from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import { SafeAreaView, useSafeAreaInsets } from 'react-native-safe-area-context';
import { fetchPreview, previewControl, sendPreview } from '../api/client';
import type { PreviewMessage, PreviewState } from '../api/types';
import { EmptyState, ErrorBanner, Loading, ScreenHeader } from '../components/ui';
import { usePolling } from '../util/usePolling';
import { useKeyboardVisible } from '../util/useKeyboard';
import { colors, font, radius, space } from '../theme';

/** The raw provider payload a bubble becomes on the wire — outbound messages
 *  carry the Meta/Twilio message objects, inbound carries the webhook shape. */
function fmtWire(m: PreviewMessage): string {
  if (!m.wire) return '';
  const payload = m.direction === 'in' ? m.wire.inbound ?? {} : m.wire.payloads ?? [];
  try {
    return JSON.stringify(payload, null, 2);
  } catch {
    return String(payload);
  }
}

const SIGNAL_COLOR: Record<string, string> = {
  live: colors.success,
  busy: colors.warning,
  down: colors.danger,
  off: colors.textFaint,
};

export default function WalkieScreen() {
  const insets = useSafeAreaInsets();
  const keyboardVisible = useKeyboardVisible();
  const [state, setState] = useState<PreviewState | null>(null);
  const [draft, setDraft] = useState('');
  const [busySend, setBusySend] = useState(false);
  const [error, setError] = useState('');
  const [openWire, setOpenWire] = useState<Record<number, boolean>>({});
  const scrollRef = useRef<ScrollView | null>(null);
  const linkTried = useRef(false);
  // Only auto-scroll while pinned to the bottom, so reading history isn't yanked
  // down by the 2s poll (same rule as the Hypervisor transcript).
  const pinnedRef = useRef(true);

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

  // Auto-provision the internal link once, so the preview is usable immediately.
  // The pairing exchange still shows in the transcript (code → "✅ Linked").
  useEffect(() => {
    if (state && state.available && !state.linked && !linkTried.current) {
      linkTried.current = true;
      void previewControl('link').then(() => void refresh());
    }
  }, [state?.linked, state?.available, refresh]);

  // Keep pinned to the newest message when we're at the bottom.
  useEffect(() => {
    if (!pinnedRef.current) return;
    const t = setTimeout(() => scrollRef.current?.scrollToEnd({ animated: true }), 60);
    return () => clearTimeout(t);
  }, [state?.cursor]);

  function onScroll(e: NativeSyntheticEvent<NativeScrollEvent>) {
    const { contentOffset, contentSize, layoutMeasurement } = e.nativeEvent;
    pinnedRef.current = contentSize.height - contentOffset.y - layoutMeasurement.height < 80;
  }

  async function send(text: string, button?: string) {
    const payload = (button ?? text).trim();
    if (!payload || busySend) return;
    pinnedRef.current = true; // sending re-pins to the bottom
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
    try {
      await previewControl('reset');
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to reset');
    }
  }

  function toggleWire(seq: number) {
    setOpenWire((o) => ({ ...o, [seq]: !o[seq] }));
  }

  if (!state) {
    return (
      <SafeAreaView style={styles.root} edges={['top']}>
        <ScreenHeader title="Walkie-Talkie" subtitle="WhatsApp gateway preview" />
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
  const provider = (state.provider || 'meta').toUpperCase();
  const signal = !state.available ? 'off' : busy ? 'busy' : linked ? 'live' : 'down';
  const signalLabel = !state.available
    ? 'OFFLINE'
    : busy
      ? 'THINKING…'
      : linked
        ? 'LINKED'
        : 'NOT LINKED';
  const windowLabel = state.simulate_out_of_window ? 'CLOSED (sim)' : 'OPEN';
  const messages = state.messages ?? [];

  return (
    <SafeAreaView style={styles.root} edges={['top']}>
      <ScreenHeader
        title="Walkie-Talkie"
        subtitle="Talk to your workspace over WhatsApp"
        right={
          <View style={styles.status} accessibilityRole="text">
            <View style={[styles.led, { backgroundColor: SIGNAL_COLOR[signal] }]} />
            <Text style={[styles.statusText, { color: SIGNAL_COLOR[signal] }]}>{signalLabel}</Text>
          </View>
        }
      />

      {/* Channel readout — provider + in/out-of-window state, mirroring the web
          device's LCD row. */}
      <View style={styles.lcd}>
        <Text style={styles.lcdLabel}>CH</Text>
        <Text style={styles.lcdValue}>WhatsApp</Text>
        <Text style={styles.lcdSep}>·</Text>
        <Text style={styles.lcdLabel}>PROVIDER</Text>
        <Text style={styles.lcdValue}>{provider}</Text>
        <Text style={styles.lcdSep}>·</Text>
        <Text style={styles.lcdLabel}>WINDOW</Text>
        <Text style={styles.lcdValue}>{windowLabel}</Text>
      </View>

      <KeyboardAvoidingView
        style={styles.flex}
        behavior={Platform.OS === 'ios' ? 'padding' : undefined}
        keyboardVerticalOffset={0}
      >
        <ScrollView
          ref={scrollRef}
          style={styles.flex}
          contentContainerStyle={styles.transcript}
          onScroll={onScroll}
          scrollEventThrottle={100}
        >
          {messages.length === 0 ? (
            <View style={styles.welcome}>
              <EmptyState
                icon="radio-outline"
                title="Press to talk to your workspace"
                subtitle="Messages run through the real WhatsApp gateway pipeline — locally. Expand “wire” on any bubble to see the exact provider payload."
              />
            </View>
          ) : (
            messages.map((m) => {
              // Inbound notices (e.g. the injected pairing code) are internal — the
              // web hides them; show the outbound ones as centered status lines.
              if (m.kind === 'notice' && m.direction === 'in') return null;
              if (m.kind === 'notice') {
                return (
                  <Text key={m.seq} style={styles.notice}>
                    {m.text}
                  </Text>
                );
              }
              const isOut = m.direction === 'out';
              const wire = fmtWire(m);
              const wireOpen = !!openWire[m.seq];
              return (
                <View key={m.seq} style={[styles.msgRow, isOut ? styles.msgRowOut : styles.msgRowIn]}>
                  <View style={[styles.bubble, isOut ? styles.bubbleOut : styles.bubbleIn]}>
                    {m.kind === 'template' && <Text style={styles.tmplTag}>TEMPLATE · out-of-window</Text>}
                    <Text style={styles.bubbleText}>{m.text}</Text>
                    {m.quick_replies.length > 0 && (
                      <View style={styles.replies}>
                        {m.quick_replies.map((r, i) => (
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
                  {wire ? (
                    <View style={[styles.wireWrap, isOut ? styles.wireWrapOut : styles.wireWrapIn]}>
                      <Pressable
                        onPress={() => toggleWire(m.seq)}
                        hitSlop={6}
                        accessibilityRole="button"
                        accessibilityState={{ expanded: wireOpen }}
                        style={styles.wireToggle}
                      >
                        <Ionicons name={wireOpen ? 'chevron-down' : 'chevron-forward'} size={11} color={colors.textFaint} />
                        <Text style={styles.wireToggleText}>wire</Text>
                        <Text style={styles.wireProvider}>{provider}</Text>
                      </Pressable>
                      {wireOpen && (
                        <ScrollView horizontal style={styles.wireBody} contentContainerStyle={styles.wireBodyInner}>
                          <Text style={styles.wireCode}>{wire}</Text>
                        </ScrollView>
                      )}
                    </View>
                  ) : null}
                </View>
              );
            })
          )}
        </ScrollView>

        {error ? <ErrorBanner message={error} /> : null}

        {/* Controls: out-of-window simulation + reset. Parity with the web
            device's side controls (show the approved-template path / clear). */}
        <View style={styles.controls}>
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
            onPress={() => void reset()}
            hitSlop={6}
            accessibilityRole="button"
            accessibilityLabel="Reset transcript"
            style={({ pressed }) => [styles.resetBtn, pressed && { opacity: 0.6 }]}
          >
            <Ionicons name="refresh" size={14} color={colors.textMuted} />
            <Text style={styles.resetText}>Reset</Text>
          </Pressable>
        </View>

        <View
          style={[
            styles.composer,
            { paddingBottom: keyboardVisible ? space.sm : Math.max(insets.bottom, space.sm) },
          ]}
        >
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
            accessibilityLabel="Push to talk"
            style={[styles.ptt, (busySend || !draft.trim()) && styles.pttOff]}
          >
            <Text style={styles.pttText}>PTT</Text>
          </Pressable>
        </View>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: colors.bg },
  flex: { flex: 1 },
  status: { flexDirection: 'row', alignItems: 'center', gap: 6 },
  led: { width: 8, height: 8, borderRadius: 4 },
  statusText: { fontSize: font.size.xs, fontWeight: '700', letterSpacing: 0.4 },
  lcd: {
    flexDirection: 'row',
    alignItems: 'center',
    flexWrap: 'wrap',
    gap: 5,
    paddingHorizontal: space.lg,
    paddingBottom: space.sm,
  },
  lcdLabel: {
    color: colors.textFaint,
    fontSize: 10,
    fontWeight: '700',
    letterSpacing: 0.5,
    fontFamily: font.mono,
  },
  lcdValue: { color: colors.textMuted, fontSize: font.size.xs, fontWeight: '600', fontFamily: font.mono },
  lcdSep: { color: colors.border, fontSize: font.size.xs },
  transcript: { padding: space.lg, gap: space.md },
  welcome: { paddingTop: space.xxl },
  notice: {
    alignSelf: 'center',
    color: colors.textFaint,
    fontSize: font.size.xs,
    textAlign: 'center',
    paddingVertical: 2,
  },
  msgRow: { maxWidth: '86%', gap: 4 },
  msgRowOut: { alignSelf: 'flex-end', alignItems: 'flex-end' },
  msgRowIn: { alignSelf: 'flex-start', alignItems: 'flex-start' },
  bubble: {
    borderRadius: radius.lg,
    paddingHorizontal: space.md,
    paddingVertical: space.sm,
    borderWidth: 1,
  },
  // Outbound (from the workspace/agent) reads as the "received" WhatsApp bubble;
  // inbound (you) is the accent-tinted "sent" bubble on the right.
  bubbleOut: { backgroundColor: colors.card, borderColor: colors.border },
  bubbleIn: { backgroundColor: colors.surface2, borderColor: colors.borderStrong },
  bubbleText: { color: colors.text, fontSize: font.size.md, lineHeight: 21 },
  tmplTag: {
    color: colors.warning,
    fontSize: 10,
    fontWeight: '700',
    letterSpacing: 0.4,
    marginBottom: 4,
    textTransform: 'uppercase',
  },
  replies: { flexDirection: 'row', flexWrap: 'wrap', gap: space.sm, marginTop: space.sm },
  reply: {
    borderWidth: 1,
    borderColor: colors.borderStrong,
    borderRadius: radius.md,
    paddingHorizontal: space.md,
    paddingVertical: 6,
    backgroundColor: colors.surface2,
  },
  replyPressed: { opacity: 0.7 },
  replyOff: { opacity: 0.5 },
  replyText: { color: colors.info, fontSize: font.size.sm, fontWeight: '600' },
  wireWrap: { maxWidth: '100%' },
  wireWrapOut: { alignItems: 'flex-end' },
  wireWrapIn: { alignItems: 'flex-start' },
  wireToggle: { flexDirection: 'row', alignItems: 'center', gap: 4, paddingVertical: 2 },
  wireToggleText: { color: colors.textFaint, fontSize: font.size.xs, fontWeight: '600' },
  wireProvider: {
    color: colors.textFaint,
    fontSize: 9,
    fontWeight: '700',
    letterSpacing: 0.5,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.sm,
    paddingHorizontal: 4,
    paddingVertical: 1,
  },
  wireBody: {
    maxHeight: 220,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.sm,
    backgroundColor: colors.bg,
    marginTop: 2,
  },
  wireBodyInner: { padding: space.sm },
  wireCode: { color: colors.textMuted, fontSize: font.size.xs, fontFamily: font.mono, lineHeight: 17 },
  controls: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: space.md,
    paddingHorizontal: space.lg,
    paddingVertical: space.sm,
    borderTopWidth: 1,
    borderTopColor: colors.border,
    backgroundColor: colors.bgElevated,
  },
  switchRow: { flexDirection: 'row', alignItems: 'center', gap: space.sm, flexShrink: 1 },
  switchLabelWrap: { flexShrink: 1 },
  switchLabel: { color: colors.text, fontSize: font.size.sm, fontWeight: '500' },
  switchHint: { color: colors.textFaint, fontSize: font.size.xs },
  resetBtn: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 5,
    paddingHorizontal: space.md,
    paddingVertical: 6,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.md,
    backgroundColor: colors.card,
  },
  resetText: { color: colors.textMuted, fontSize: font.size.sm, fontWeight: '600' },
  composer: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.sm,
    paddingHorizontal: space.md,
    paddingTop: space.sm,
    borderTopWidth: 1,
    borderTopColor: colors.border,
    backgroundColor: colors.bgElevated,
  },
  input: {
    flex: 1,
    maxHeight: 120,
    color: colors.text,
    fontSize: font.size.md,
    backgroundColor: colors.surface2,
    borderRadius: radius.md,
    paddingHorizontal: space.md,
    paddingVertical: space.sm,
  },
  ptt: {
    width: 52,
    height: 40,
    borderRadius: radius.md,
    backgroundColor: colors.accent,
    alignItems: 'center',
    justifyContent: 'center',
  },
  pttOff: { opacity: 0.4 },
  pttText: { color: colors.accentText, fontWeight: '800', fontSize: font.size.sm, letterSpacing: 0.5 },
});
