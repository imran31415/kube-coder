/**
 * AI CTO (#471) — native parity for the web /cto page. A project switcher, a
 * project-scoped CTO chat (rides the hypervisor thread API with persona=cto),
 * and a deterministic brief bottom-sheet. Mirrors the web CtoRoute's onboarding:
 * auto-provisioned projects, a Workspace root scope, starter chips, and lazy
 * thread creation. No SSE on mobile — the active thread polls every 2s.
 */
import { useNavigation, useRoute } from '@react-navigation/native';
import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  KeyboardAvoidingView, Modal, Platform, Pressable, ScrollView,
  StyleSheet, Text, TextInput, View,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { Ionicons } from '@expo/vector-icons';
import {
  listProjects, getProjectBrief, listThreads, createThread,
  sendThreadMessage, getThreadDetail, getHypervisorConfig,
} from '../api/client';
import { Button, EmptyState, Loading, ScreenHeader } from '../components/ui';
import { Markdown } from '../components/Markdown';
import type { HvEvent, Project, ProjectBrief } from '../api/types';
import {
  buildTurns,
  groupActivity,
  sameTranscript,
  turnWindow,
  TURN_WINDOW,
  TURN_WINDOW_STEP,
  type HvRenderBlock,
  type HvTurn,
} from '../util/hvTranscript';
import { colors, font, radius, space } from '../theme';
import { relativeTime } from '../util/format';

type Nav = { navigate: (tab: string, opts?: object) => void };

const STARTERS = (project: string | null) => [
  'What should I focus on?',
  project ? `Where is ${project} at?` : 'What are we building?',
  'Break a goal into tasks',
];

export default function CtoScreen() {
  const nav = useNavigation<Nav>();
  const route = useRoute();
  const params = (route.params ?? {}) as { discussProject?: string; discussText?: string };

  const [projects, setProjects] = useState<Project[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [brief, setBrief] = useState<ProjectBrief | null>(null);
  const [briefOpen, setBriefOpen] = useState(false);

  const [activeId, setActiveId] = useState<string | null>(null);
  const [events, setEvents] = useState<HvEvent[]>([]);
  const [status, setStatus] = useState('');
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const [defaultAssistant, setDefaultAssistant] = useState<string | undefined>();
  // Bounded render window over the tail of the transcript (#525) — ScrollView
  // mounts every child, so a long CTO thread kept every turn alive until the
  // app degraded. Reveal older turns a page at a time.
  const [visibleTurns, setVisibleTurns] = useState(TURN_WINDOW);

  const optimisticSeq = useRef(-1);
  const transcriptSource = useRef<'session_log' | 'capture' | null>(null);
  const pendingDiscuss = useRef<string | null>(params.discussText ?? null);
  const scrollRef = useRef<ScrollView | null>(null);
  // Set while revealing earlier turns so the onContentSizeChange auto-scroll
  // doesn't yank the reader back to the bottom on that one growth.
  const suppressAutoScroll = useRef(false);

  // Bootstrap: config + projects, then select (a Feed handoff wins, else the
  // most-active project, else the Workspace scope).
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const cfg = await getHypervisorConfig();
        if (alive) setDefaultAssistant(cfg.defaultAssistant);
      } catch { /* keep default */ }
      try {
        const ps = await listProjects();
        if (!alive) return;
        setProjects(ps);
        const active = [...ps].sort(
          (a, b) => (b.pulse?.last_activity_at ?? 0) - (a.pulse?.last_activity_at ?? 0),
        )[0];
        setSelected(params.discussProject || active?.id || null);
      } catch {
        if (alive) setProjects([]);
      }
    })();
    return () => { alive = false; };
  }, []);

  // On project change: load its brief, filter the CTO thread list to it, and
  // continue the latest thread (delivering any queued Feed-handoff prefix).
  useEffect(() => {
    if (projects === null) return;
    let alive = true;
    setBrief(null);
    setEvents([]);
    setActiveId(null);
    setVisibleTurns(TURN_WINDOW);
    (async () => {
      if (selected) {
        try { const b = await getProjectBrief(selected); if (alive) setBrief(b); } catch { /* */ }
      }
      let threads: Awaited<ReturnType<typeof listThreads>> = [];
      try { threads = await listThreads({ persona: 'cto', project: selected ?? undefined }); } catch { /* */ }
      if (!alive) return;
      if (threads.length) setActiveId(threads[0].id);
      const text = pendingDiscuss.current;
      if (text) { pendingDiscuss.current = null; void send(text); }
    })();
    return () => { alive = false; };
  }, [selected, projects]);

  // Poll the active thread (2s), mirroring HypervisorScreen.
  useEffect(() => {
    if (!activeId) return;
    let alive = true;
    const tick = async () => {
      try {
        const d = await getThreadDetail(activeId, 0);
        if (!alive || d.thread.id !== activeId) return;
        const src = d.source ?? null;
        const prev = transcriptSource.current;
        transcriptSource.current = src;
        setEvents((cur) => (sameTranscript(cur, d.events, prev, src) ? cur : d.events));
        setStatus(d.thread.status);
      } catch { /* transient */ }
    };
    void tick();
    const timer = setInterval(tick, 2000);
    return () => { alive = false; clearInterval(timer); };
  }, [activeId]);

  const send = useCallback(
    async (text?: string) => {
      const finalText = (text ?? draft).trim();
      if (!finalText || sending) return;
      setDraft('');
      setSending(true);
      setEvents((prev) => [
        ...prev,
        { seq: optimisticSeq.current--, ts: Date.now() / 1000, role: 'user', type: 'message', text: finalText },
      ]);
      setStatus('running');
      try {
        if (!activeId) {
          // A CTO thread omits workdir so the server defaults it to the project.
          const thread = await createThread(finalText, defaultAssistant, undefined, undefined, 'cto', selected ?? undefined);
          setActiveId(thread.id);
        } else {
          await sendThreadMessage(activeId, finalText);
        }
      } catch { /* surfaced by the next poll / status */ } finally {
        setSending(false);
      }
    },
    [draft, sending, activeId, defaultAssistant, selected],
  );

  const selectedProject = projects?.find((p) => p.id === selected) ?? null;
  const turns = activeId || events.length ? buildTurns(events) : [];
  const { start: turnStart, hidden: hiddenTurns } = turnWindow(turns.length, visibleTurns);
  const showWelcome = !activeId && events.length === 0;

  function revealEarlier() {
    suppressAutoScroll.current = true;
    setVisibleTurns((v) => v + TURN_WINDOW_STEP);
  }

  if (projects === null) return <Loading label="Meeting your workspace…" />;

  return (
    <SafeAreaView style={styles.container} edges={['top']}>
      <ScreenHeader
        title="AI CTO"
        subtitle={selectedProject ? selectedProject.name : 'Engineering judgment for your workspace'}
        right={
          selected ? (
            <Pressable style={styles.briefBtn} onPress={() => setBriefOpen(true)}>
              <Ionicons name="reader-outline" size={15} color={colors.textMuted} />
              <Text style={styles.briefBtnText}>Brief</Text>
            </Pressable>
          ) : undefined
        }
      />

      {/* Project pill switcher */}
      <View style={styles.railWrap}>
        <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.rail}>
          <Pill label="Workspace" on={selected === null} onPress={() => setSelected(null)} />
          {projects.filter((p) => p.status !== 'archived').map((p) => (
            <Pill
              key={p.id}
              label={p.name}
              on={selected === p.id}
              running={p.pulse?.running ?? 0}
              waiting={p.pulse?.waiting ?? 0}
              onPress={() => setSelected(p.id)}
            />
          ))}
        </ScrollView>
      </View>

      {/* keyboardVerticalOffset stays 0: the KAV extends to the screen bottom
          (its composer IS the bottom edge), so `padding` behaviour pads by
          exactly the keyboard height. Any non-zero offset is added on top of
          that, floating the composer a fixed gap above the keyboard (#492).
          Mirrors HypervisorScreen, which shares this SafeAreaView-top +
          in-flow ScreenHeader + bottom-composer layout. */}
      <KeyboardAvoidingView style={styles.chat} behavior={Platform.OS === 'ios' ? 'padding' : undefined} keyboardVerticalOffset={0}>
        <ScrollView
          ref={scrollRef}
          style={styles.transcript}
          contentContainerStyle={styles.transcriptInner}
          onContentSizeChange={() => {
            // Revealing earlier turns grows content upward — don't scroll to the
            // bottom on that one change (#525); every other growth still pins.
            if (suppressAutoScroll.current) {
              suppressAutoScroll.current = false;
              return;
            }
            scrollRef.current?.scrollToEnd({ animated: true });
          }}
        >
          {showWelcome ? (
            <View style={styles.welcome}>
              <Text style={styles.welcomeLead}>
                {selectedProject
                  ? `I'm across ${selectedProject.name}. What do you want to move on?`
                  : 'I already know your workspace. What do you want to build or move on?'}
              </Text>
              <View style={styles.chips}>
                {STARTERS(selectedProject?.name ?? null).map((c) => (
                  <Pressable key={c} style={styles.starter} disabled={sending} onPress={() => void send(c)}>
                    <Text style={styles.starterText}>{c}</Text>
                  </Pressable>
                ))}
              </View>
            </View>
          ) : (
            <>
              {hiddenTurns > 0 && (
                <Pressable
                  onPress={revealEarlier}
                  accessibilityRole="button"
                  accessibilityLabel={`Show ${hiddenTurns} earlier messages`}
                  style={({ pressed }) => [styles.earlierBtn, pressed && { opacity: 0.6 }]}
                >
                  <Ionicons name="chevron-up" size={14} color={colors.textMuted} />
                  <Text style={styles.earlierText}>
                    Show {Math.min(hiddenTurns, TURN_WINDOW_STEP)} earlier
                    {Math.min(hiddenTurns, TURN_WINDOW_STEP) === 1 ? ' message' : ' messages'}
                  </Text>
                  <Text style={styles.earlierCount}>{hiddenTurns} hidden</Text>
                </Pressable>
              )}
              {turns.slice(turnStart).map((t, wi) => (
                <Turn key={turnStart + wi} turn={t} onChoice={(opt) => void send(opt)} />
              ))}
            </>
          )}
          {status === 'running' && <Text style={styles.thinking}>Thinking…</Text>}
        </ScrollView>

        <View style={styles.composer}>
          <TextInput
            style={styles.input}
            value={draft}
            onChangeText={setDraft}
            placeholder={`Message your CTO${selectedProject ? ` about ${selectedProject.name}` : ''}…`}
            placeholderTextColor={colors.textFaint}
            multiline
          />
          <Button title="" icon="arrow-up" onPress={() => void send()} loading={sending} disabled={!draft.trim()} />
        </View>
      </KeyboardAvoidingView>

      <BriefSheet open={briefOpen} brief={brief} onClose={() => setBriefOpen(false)} nav={nav} />
    </SafeAreaView>
  );
}

function Pill({ label, on, running = 0, waiting = 0, onPress }: {
  label: string; on: boolean; running?: number; waiting?: number; onPress: () => void;
}) {
  return (
    <Pressable style={[styles.pill, on && styles.pillOn]} onPress={onPress}>
      <Text style={[styles.pillText, on && styles.pillTextOn]} numberOfLines={1}>{label}</Text>
      {running > 0 && <View style={[styles.dot, { backgroundColor: colors.running }]} />}
      {waiting > 0 && <View style={[styles.dot, { backgroundColor: colors.waiting }]} />}
    </Pressable>
  );
}

function Turn({ turn, onChoice }: { turn: HvTurn; onChoice: (opt: string) => void }) {
  if (turn.role === 'user') {
    return (
      <View style={styles.userBubble}>
        <Text style={styles.userText}>{turn.text}</Text>
      </View>
    );
  }
  return (
    <View style={styles.agentTurn}>
      {groupActivity(turn.blocks).map((b, i) => <Block key={i} block={b} onChoice={onChoice} />)}
    </View>
  );
}

function Block({ block, onChoice }: { block: HvRenderBlock; onChoice: (opt: string) => void }) {
  if (block.kind === 'prose') return <Markdown text={block.text} />;
  if (block.kind === 'activity_group') return <ActivityGroup block={block} />;
  if (block.kind === 'activity') {
    return (
      <Text style={[styles.activity, block.error && { color: colors.error }]}>
        {block.label}{block.detail ? ` · ${block.detail}` : ''}
      </Text>
    );
  }
  if (block.kind === 'choice') {
    return (
      <View style={styles.choices}>
        {block.question ? <Text style={styles.choiceQ}>{block.question}</Text> : null}
        {block.options.map((o) => (
          <Pressable key={o} style={styles.choice} onPress={() => onChoice(o)}>
            <Text style={styles.choiceText}>{o}</Text>
          </Pressable>
        ))}
      </View>
    );
  }
  // embed / media / file — surface as a compact activity note on mobile.
  return <Text style={styles.activity}>[{block.kind}]</Text>;
}

/** A run of consecutive tool calls as one tappable summary line (#546). This
 *  screen has no chip UI, so expanding reveals the child *labels* only —
 *  matching its density. A run with a failure starts open. */
function ActivityGroup({ block }: { block: Extract<HvRenderBlock, { kind: 'activity_group' }> }) {
  const failed = block.errors > 0;
  const [open, setOpen] = useState(failed);
  return (
    <View>
      <Pressable
        onPress={() => setOpen((v) => !v)}
        accessibilityRole="button"
        accessibilityState={{ expanded: open }}
      >
        <Text style={[styles.activity, failed && { color: colors.error }]}>
          {open ? '▾' : '▸'} {block.label}
        </Text>
      </Pressable>
      {open
        ? block.items.map((b, i) => (
            <Text key={i} style={[styles.activityChild, b.error && { color: colors.error }]}>
              {b.label}
            </Text>
          ))
        : null}
    </View>
  );
}

function BriefSheet({ open, brief, onClose, nav }: {
  open: boolean; brief: ProjectBrief | null; onClose: () => void; nav: Nav;
}) {
  return (
    <Modal visible={open} animationType="slide" transparent onRequestClose={onClose}>
      <Pressable style={styles.sheetScrim} onPress={onClose} />
      <View style={styles.sheet}>
        <View style={styles.sheetHandle} />
        {!brief ? (
          <EmptyState title="No brief" subtitle="Pick a project to see its brief." icon="reader-outline" />
        ) : (
          <ScrollView contentContainerStyle={styles.sheetBody}>
            <Text style={styles.sheetTitle}>{brief.project.name}</Text>
            {!!brief.project.north_star && (
              <>
                <Text style={styles.sheetH}>NORTH STAR</Text>
                <Text style={styles.sheetText}>{brief.project.north_star}</Text>
              </>
            )}
            <Text style={styles.sheetH}>NOW</Text>
            <Text style={styles.sheetText}>
              {brief.tasks.running} running · {brief.tasks.waiting} waiting · {brief.tasks.total} tasks
            </Text>
            {brief.goals.length > 0 && (
              <>
                <Text style={styles.sheetH}>GOALS ({brief.counts.goals})</Text>
                {brief.goals.map((g) => <Text key={g.key} style={styles.sheetText}>• {g.value}</Text>)}
              </>
            )}
            {brief.decisions.length > 0 && (
              <>
                <Text style={styles.sheetH}>DECISION LOG ({brief.counts.decisions})</Text>
                {brief.decisions.map((d) => <Text key={d.key} style={styles.sheetText}>• {d.value}</Text>)}
              </>
            )}
            {brief.tasks.recent.length > 0 && (
              <>
                <Text style={styles.sheetH}>RECENT TASKS</Text>
                {brief.tasks.recent.map((t) => (
                  <Pressable
                    key={t.task_id}
                    onPress={() => { onClose(); nav.navigate('Tasks', { screen: 'TaskDetail', params: { id: t.task_id }, initial: false }); }}
                  >
                    <Text style={styles.sheetLink} numberOfLines={1}>→ {t.prompt || t.task_id}</Text>
                  </Pressable>
                ))}
              </>
            )}
          </ScrollView>
        )}
      </View>
    </Modal>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: colors.bg },
  briefBtn: { flexDirection: 'row', alignItems: 'center', gap: 5, paddingVertical: 5, paddingHorizontal: 10, borderRadius: radius.md, borderWidth: 1, borderColor: colors.border },
  briefBtnText: { color: colors.textMuted, fontSize: font.size.xs },
  railWrap: { borderBottomWidth: 1, borderBottomColor: colors.border },
  rail: { paddingHorizontal: space.lg, paddingVertical: space.sm, gap: 8 },
  pill: { flexDirection: 'row', alignItems: 'center', gap: 5, paddingVertical: 6, paddingHorizontal: 12, borderRadius: radius.pill, borderWidth: 1, borderColor: colors.border, backgroundColor: colors.card, maxWidth: 200 },
  pillOn: { borderColor: colors.accent, backgroundColor: colors.accentSoft },
  pillText: { color: colors.textMuted, fontSize: font.size.sm },
  pillTextOn: { color: colors.text, fontWeight: '600' },
  dot: { width: 6, height: 6, borderRadius: 3 },
  chat: { flex: 1 },
  transcript: { flex: 1 },
  transcriptInner: { padding: space.lg, gap: space.md },
  // "Show earlier messages" — head of a windowed transcript (#525).
  earlierBtn: {
    flexDirection: 'row', alignItems: 'center', alignSelf: 'center', gap: 6,
    paddingVertical: 6, paddingHorizontal: 12, borderRadius: radius.pill,
    borderWidth: 1, borderColor: colors.border, backgroundColor: colors.card,
  },
  earlierText: { color: colors.textMuted, fontSize: font.size.xs, fontWeight: '600' },
  earlierCount: {
    color: colors.textFaint, fontSize: font.size.xs, paddingLeft: 6, marginLeft: 2,
    borderLeftWidth: 1, borderLeftColor: colors.border,
  },
  welcome: { gap: space.md, paddingTop: space.lg },
  welcomeLead: { color: colors.text, fontSize: font.size.md, lineHeight: 22 },
  chips: { gap: 8 },
  starter: { paddingVertical: 10, paddingHorizontal: 14, borderRadius: radius.lg, borderWidth: 1, borderColor: colors.borderStrong, backgroundColor: colors.card, alignSelf: 'flex-start' },
  starterText: { color: colors.text, fontSize: font.size.sm },
  userBubble: { alignSelf: 'flex-end', maxWidth: '86%', backgroundColor: colors.surface2, borderRadius: radius.lg, paddingVertical: 8, paddingHorizontal: 12 },
  userText: { color: colors.text, fontSize: font.size.sm, lineHeight: 20 },
  agentTurn: { gap: 6 },
  activity: { color: colors.textFaint, fontSize: font.size.xs, fontStyle: 'italic' },
  activityChild: { color: colors.textFaint, fontSize: font.size.xs, fontStyle: 'italic', marginLeft: space.md },
  choices: { gap: 6, marginTop: 4 },
  choiceQ: { color: colors.textMuted, fontSize: font.size.sm },
  choice: { paddingVertical: 9, paddingHorizontal: 13, borderRadius: radius.md, borderWidth: 1, borderColor: colors.accent, backgroundColor: colors.accentSoft, alignSelf: 'flex-start' },
  choiceText: { color: colors.text, fontSize: font.size.sm },
  thinking: { color: colors.textFaint, fontSize: font.size.xs },
  composer: { flexDirection: 'row', alignItems: 'flex-end', gap: 8, padding: space.md, borderTopWidth: 1, borderTopColor: colors.border },
  input: { flex: 1, maxHeight: 120, color: colors.text, fontSize: font.size.sm, backgroundColor: colors.surface2, borderRadius: radius.md, paddingHorizontal: 12, paddingVertical: 9 },
  sheetScrim: { flex: 1, backgroundColor: 'rgba(0,0,0,0.5)' },
  sheet: { backgroundColor: colors.card, borderTopLeftRadius: radius.xl, borderTopRightRadius: radius.xl, maxHeight: '75%', paddingBottom: space.xl },
  sheetHandle: { alignSelf: 'center', width: 36, height: 4, borderRadius: 2, backgroundColor: colors.borderStrong, marginTop: space.sm, marginBottom: space.xs },
  sheetBody: { padding: space.lg, gap: 4 },
  sheetTitle: { color: colors.text, fontSize: font.size.lg, fontWeight: '700', marginBottom: space.sm },
  sheetH: { color: colors.textFaint, fontSize: font.size.xs, fontWeight: '600', letterSpacing: 0.5, marginTop: space.md },
  sheetText: { color: colors.textMuted, fontSize: font.size.sm, lineHeight: 20 },
  sheetLink: { color: colors.text, fontSize: font.size.sm, lineHeight: 22 },
});
