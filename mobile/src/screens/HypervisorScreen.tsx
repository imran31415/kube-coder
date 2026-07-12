/**
 * Hypervisor — the workspace-aware chat tab, on mobile. Talks to the same
 * /api/hypervisor facade as the dashboard SPA: a thread is a structured agent
 * session and the server returns a canonical event stream (no terminal, no
 * scraping), which buildTurns() folds into user bubbles + agent turns (prose +
 * expandable tool-activity chips). See charts/workspace/hypervisor_session.py.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  KeyboardAvoidingView,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import {
  createThread,
  deleteThread,
  getHypervisorConfig,
  getThreadDetail,
  listThreads,
  sendThreadMessage,
} from '../api/client';
import type { HvEvent, HypervisorConfig, HypervisorThread } from '../api/types';
import { buildTurns, type HvBlock } from '../util/hvTranscript';
import { EmptyState, ErrorBanner, ScreenHeader } from '../components/ui';
import { colors, font, radius, space } from '../theme';

const SUGGESTIONS = [
  "What's running and how much CPU am I using?",
  'Spin up a task to run the tests',
  'Remember that I deploy with `make ship`',
];

export default function HypervisorScreen() {
  const insets = useSafeAreaInsets();
  const [config, setConfig] = useState<HypervisorConfig | null>(null);
  const [threads, setThreads] = useState<HypervisorThread[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [events, setEvents] = useState<HvEvent[]>([]);
  const [status, setStatus] = useState<string>('');
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const scrollRef = useRef<ScrollView | null>(null);
  const optimisticSeq = useRef(-1);

  const refreshThreads = useCallback(async () => {
    try {
      setThreads(await listThreads());
    } catch {
      /* keep last-good */
    }
  }, []);

  useEffect(() => {
    void getHypervisorConfig()
      .then(setConfig)
      .catch((e) => setError(e instanceof Error ? e.message : 'Failed to load config'));
    void refreshThreads();
  }, [refreshThreads]);

  // Poll the open thread's canonical events while it's active.
  useEffect(() => {
    if (!activeId) return;
    let alive = true;
    const tick = async () => {
      try {
        const d = await getThreadDetail(activeId, 0);
        if (!alive || d.thread.id !== activeId) return;
        setEvents(d.events);
        setStatus(d.thread.status);
      } catch {
        /* transient */
      }
    };
    void tick();
    const timer = setInterval(tick, 2000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [activeId]);

  useEffect(() => {
    const t = setTimeout(() => scrollRef.current?.scrollToEnd({ animated: true }), 60);
    return () => clearTimeout(t);
  }, [events]);

  function openThread(id: string) {
    setActiveId(id);
    setEvents([]);
    setStatus('');
    setError(null);
  }

  function newChat() {
    setActiveId(null);
    setEvents([]);
    setStatus('');
    setError(null);
    setDraft('');
  }

  async function removeThread(id: string) {
    try {
      await deleteThread(id);
    } catch {
      /* best effort */
    }
    if (activeId === id) newChat();
    void refreshThreads();
  }

  async function send(text?: string) {
    const msg = (text ?? draft).trim();
    if (!msg || sending) return;
    setSending(true);
    setError(null);
    setDraft('');
    setStatus('running');
    // Optimistic user turn (negative seq so it never collides with server seqs).
    setEvents((prev) => [
      ...prev,
      { seq: optimisticSeq.current--, ts: Date.now() / 1000, role: 'user', type: 'message', text: msg },
    ]);
    try {
      if (!activeId) {
        const thread = await createThread(msg, config?.defaultAssistant);
        await refreshThreads();
        openThread(thread.id);
      } else {
        await sendThreadMessage(activeId, msg);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to send');
    } finally {
      setSending(false);
    }
  }

  if (config && config.enabled === false) {
    return (
      <View style={styles.root}>
        <ScreenHeader title="Hypervisor" />
        <EmptyState icon="hardware-chip-outline" title="Hypervisor is disabled" subtitle="Enable it in the workspace chart (hypervisor.enabled)." />
      </View>
    );
  }

  const turns = buildTurns(events);
  const agentName = config?.defaultAssistant || 'claude';
  const activeThread = threads.find((t) => t.id === activeId) || null;
  const working = status === 'running';
  const empty = !activeId && events.length === 0;

  return (
    <View style={styles.root}>
      <ScreenHeader
        title={activeThread ? activeThread.title || 'Chat' : 'Hypervisor'}
        subtitle={activeThread ? `via ${activeThread.assistant || agentName}` : 'Talk to your workspace'}
        right={
          <Pressable onPress={newChat} hitSlop={8} style={styles.newBtn}>
            <Ionicons name="add" size={18} color={colors.accentText} />
            <Text style={styles.newBtnText}>New</Text>
          </Pressable>
        }
      />

      {threads.length > 0 && (
        <View style={styles.threadBar}>
          <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.threadBarInner}>
            {threads.map((t) => {
              const on = t.id === activeId;
              return (
                <Pressable
                  key={t.id}
                  onPress={() => openThread(t.id)}
                  onLongPress={() => removeThread(t.id)}
                  style={[styles.threadChip, on && styles.threadChipOn]}
                >
                  <View style={[styles.dot, { backgroundColor: t.status === 'running' ? colors.running : colors.killed }]} />
                  <Text numberOfLines={1} style={[styles.threadChipText, on && styles.threadChipTextOn]}>
                    {t.title || 'New chat'}
                  </Text>
                </Pressable>
              );
            })}
          </ScrollView>
        </View>
      )}

      <KeyboardAvoidingView
        style={styles.flex}
        behavior={Platform.OS === 'ios' ? 'padding' : undefined}
        keyboardVerticalOffset={insets.top + 44}
      >
        <ScrollView ref={scrollRef} style={styles.flex} contentContainerStyle={styles.transcript}>
          {empty ? (
            <View style={styles.welcome}>
              <EmptyState
                icon="hardware-chip-outline"
                title="Kube-Coder"
                subtitle="Ask about your workspace or tell it what to do — it reads live state and acts on it through your tools."
              />
              <View style={styles.suggests}>
                {SUGGESTIONS.map((s) => (
                  <Pressable key={s} onPress={() => void send(s)} style={styles.suggest} disabled={sending}>
                    <Text style={styles.suggestText}>{s}</Text>
                  </Pressable>
                ))}
              </View>
            </View>
          ) : (
            turns.map((turn, i) =>
              turn.role === 'user' ? (
                <View key={i} style={styles.userRow}>
                  <View style={styles.userBubble}>
                    <Text style={styles.userText}>{turn.text}</Text>
                  </View>
                </View>
              ) : (
                <View key={i} style={styles.agentRow}>
                  <View style={styles.agentHead}>
                    <Ionicons name="hardware-chip-outline" size={13} color={colors.textMuted} />
                    <Text style={styles.agentName}>Kube-Coder</Text>
                    <Text style={styles.agentVia}>via {agentName}</Text>
                    {working && i === turns.length - 1 && <Text style={styles.working}>· working…</Text>}
                  </View>
                  {turn.blocks.map((b, j) => (
                    <Block key={j} block={b} id={`${i}:${j}`} expanded={expanded} setExpanded={setExpanded} />
                  ))}
                </View>
              ),
            )
          )}
          {!empty && working && turns[turns.length - 1]?.role !== 'agent' && (
            <View style={styles.agentRow}>
              <View style={styles.agentHead}>
                <Ionicons name="hardware-chip-outline" size={13} color={colors.textMuted} />
                <Text style={styles.agentName}>Kube-Coder</Text>
                <Text style={styles.working}>· working…</Text>
              </View>
            </View>
          )}
        </ScrollView>

        {error && <ErrorBanner message={error} />}

        <View style={[styles.composer, { paddingBottom: Math.max(insets.bottom, space.sm) }]}>
          <TextInput
            style={styles.input}
            value={draft}
            onChangeText={setDraft}
            placeholder="Message Kube-Coder…"
            placeholderTextColor={colors.textFaint}
            multiline
            editable={!sending}
          />
          <Pressable
            onPress={() => void send()}
            disabled={sending || !draft.trim()}
            style={[styles.sendBtn, (sending || !draft.trim()) && styles.sendBtnOff]}
          >
            <Ionicons name="arrow-up" size={20} color={colors.accentText} />
          </Pressable>
        </View>
      </KeyboardAvoidingView>
    </View>
  );
}

function Block({
  block,
  id,
  expanded,
  setExpanded,
}: {
  block: HvBlock;
  id: string;
  expanded: Set<string>;
  setExpanded: (s: Set<string>) => void;
}) {
  if (block.kind === 'prose') {
    return <Text style={styles.prose}>{block.text}</Text>;
  }
  const open = expanded.has(id);
  const toggle = () => {
    const next = new Set(expanded);
    open ? next.delete(id) : next.add(id);
    setExpanded(next);
  };
  return (
    <View style={[styles.activity, block.error && styles.activityErr]}>
      <Pressable onPress={toggle} style={styles.activityHead}>
        <Ionicons name="terminal-outline" size={13} color={block.error ? colors.danger : colors.textMuted} />
        <Text style={[styles.activityLabel, block.error && { color: colors.danger }]} numberOfLines={1}>
          {block.label}
        </Text>
        <Ionicons name={open ? 'chevron-up' : 'chevron-down'} size={14} color={colors.textFaint} />
      </Pressable>
      {open && block.detail ? <Text style={styles.activityDetail}>{block.detail}</Text> : null}
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: colors.bg },
  flex: { flex: 1 },
  newBtn: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 3,
    backgroundColor: colors.accent,
    paddingHorizontal: space.md,
    paddingVertical: space.xs + 2,
    borderRadius: radius.md,
  },
  newBtnText: { color: colors.accentText, fontWeight: '700', fontSize: font.size.sm },
  threadBar: { borderBottomWidth: 1, borderBottomColor: colors.border },
  threadBarInner: { paddingHorizontal: space.md, paddingVertical: space.sm, gap: space.sm },
  threadChip: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 6,
    maxWidth: 180,
    paddingHorizontal: space.md,
    paddingVertical: space.xs + 2,
    borderRadius: radius.sm,
    borderWidth: 1,
    borderColor: colors.border,
    backgroundColor: colors.card,
  },
  threadChipOn: { borderColor: colors.borderStrong, backgroundColor: colors.accentSoft },
  threadChipText: { color: colors.textMuted, fontSize: font.size.sm, flexShrink: 1 },
  threadChipTextOn: { color: colors.text },
  dot: { width: 7, height: 7, borderRadius: 4 },
  transcript: { padding: space.lg, gap: space.md },
  welcome: { paddingTop: space.xxl, gap: space.lg },
  suggests: { gap: space.sm, paddingHorizontal: space.lg },
  suggest: {
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.md,
    paddingHorizontal: space.md,
    paddingVertical: space.md,
    backgroundColor: colors.card,
  },
  suggestText: { color: colors.textMuted, fontSize: font.size.sm },
  userRow: { alignItems: 'flex-end' },
  userBubble: {
    maxWidth: '86%',
    backgroundColor: colors.surface2,
    borderRadius: radius.lg,
    paddingHorizontal: space.md,
    paddingVertical: space.sm,
  },
  userText: { color: colors.text, fontSize: font.size.md, lineHeight: 21 },
  agentRow: { gap: space.sm },
  agentHead: { flexDirection: 'row', alignItems: 'center', gap: 6 },
  agentName: { color: colors.text, fontWeight: '700', fontSize: font.size.sm },
  agentVia: { color: colors.textFaint, fontSize: font.size.xs },
  working: { color: colors.textFaint, fontSize: font.size.xs, fontStyle: 'italic' },
  prose: { color: colors.text, fontSize: font.size.md, lineHeight: 22 },
  activity: {
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.md,
    backgroundColor: colors.card,
    overflow: 'hidden',
  },
  activityErr: { borderColor: colors.danger },
  activityHead: { flexDirection: 'row', alignItems: 'center', gap: 8, padding: space.sm },
  activityLabel: { flex: 1, color: colors.textMuted, fontSize: font.size.sm, fontWeight: '600' },
  activityDetail: {
    color: colors.textMuted,
    fontSize: font.size.xs,
    fontFamily: Platform.OS === 'ios' ? 'Menlo' : 'monospace',
    padding: space.sm,
    borderTopWidth: 1,
    borderTopColor: colors.border,
  },
  composer: {
    flexDirection: 'row',
    alignItems: 'flex-end',
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
  sendBtn: {
    width: 40,
    height: 40,
    borderRadius: radius.md,
    backgroundColor: colors.accent,
    alignItems: 'center',
    justifyContent: 'center',
  },
  sendBtnOff: { opacity: 0.4 },
});
