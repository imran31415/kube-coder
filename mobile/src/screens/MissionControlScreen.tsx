/**
 * Mission Control (issue #425) — every agent across builds, hypervisor chats
 * and sub-agents in one prioritized queue, grouped into stacked swimlanes:
 * Waiting on you / Running / Needs review / Done. Fed by
 * GET /api/missioncontrol/queue (cards arrive pre-sorted, waiting first).
 *
 * Poll-render perf (#373): the cards array only changes state identity when
 * its JSON actually changed, and each card row is React.memo'd — so the
 * 4-second poll re-renders nothing but the pulse row while agents are idle.
 */
import { useNavigation } from '@react-navigation/native';
import React, { useCallback, useMemo, useRef, useState } from 'react';
import { FlatList, Pressable, RefreshControl, StyleSheet, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { getMissionQueue, killTask, sendMessage } from '../api/client';
import { Card, EmptyState, ErrorBanner, Loading, ScreenHeader, StatusPill } from '../components/ui';
import type { MissionCard, MissionCardState, MissionPulse } from '../api/types';
import { colors, font, radius, space, statusColor } from '../theme';
import { confirmAction } from '../util/confirm';
import { relativeTime } from '../util/format';
import { usePolling } from '../util/usePolling';

// Cross-tab jumps (the DesktopScreen pattern): typed loosely because the tab
// navigator has no shared param list — each target validates its own params.
type Nav = { navigate: (tab: string, opts?: object) => void };

const SECTION_ORDER: MissionCardState[] = ['waiting', 'running', 'review', 'done'];

const SECTION_TITLES: Record<MissionCardState, string> = {
  waiting: 'Waiting on you',
  running: 'Running',
  review: 'Needs review',
  done: 'Done',
};

const KIND_LABELS: Record<MissionCard['kind'], string> = {
  build: 'BUILD',
  chat: 'CHAT',
  subagent: 'SUB-AGENT',
};

/** Compact duration for the pulse row, e.g. 90 → "1m", 5400 → "1h 30m". */
function durationLabel(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  const rem = m % 60;
  return rem ? `${h}h ${rem}m` : `${h}h`;
}

/** "build:a1b2c3" → "build a1b2c3" for the lineage line. */
function lineageLabel(id: string): string {
  return id.replace(':', ' ');
}

/** The state pill: canonical states ride the shared StatusPill; `review` has
 *  no slot in statusColor's set, so it renders the same pill shape done-style
 *  (green) but labeled REVIEW — raw server strings never hit the UI. */
function StatePill({ state }: { state: MissionCardState }) {
  if (state !== 'review') return <StatusPill status={state} />;
  const c = colors.success;
  return (
    <View style={[styles.reviewPill, { borderColor: c + '59' }]}>
      <View style={[styles.reviewDot, { backgroundColor: c }]} />
      <Text style={[styles.reviewPillText, { color: c }]}>review</Text>
    </View>
  );
}

/** One agent card. Memo'd so the 4s poll never re-renders settled rows. */
const MissionCardRow = React.memo(function MissionCardRow({
  card,
  acting,
  onOpen,
  onOption,
  onKill,
}: {
  card: MissionCard;
  /** True while a reply/kill for THIS card is in flight — disables its buttons. */
  acting: boolean;
  onOpen: (card: MissionCard) => void;
  onOption: (card: MissionCard, index: number | string) => void;
  onKill: (card: MissionCard) => void;
}) {
  const active = card.state === 'running' || card.state === 'waiting';
  const killable = active && (card.kind === 'build' || card.kind === 'subagent');
  const accent =
    card.state === 'waiting' ? colors.warning : card.state === 'running' ? colors.running : undefined;

  return (
    <Card style={styles.card} accent={accent} onPress={() => onOpen(card)}>
      <View style={styles.topRow}>
        <View style={styles.topLeft}>
          <Text style={styles.kind}>{KIND_LABELS[card.kind]}</Text>
          <Text style={styles.agent} numberOfLines={1}>
            {card.assistant ?? 'claude'}
            {card.model ? ` · ${card.model}` : ''}
          </Text>
        </View>
        <Text style={styles.time}>{relativeTime(card.updated_at ?? card.created_at ?? undefined)}</Text>
      </View>

      <View style={styles.titleRow}>
        <StatePill state={card.state} />
        <Text style={styles.title} numberOfLines={2}>
          {card.title}
        </Text>
      </View>

      {card.headline ? (
        <Text style={styles.headline} numberOfLines={3}>
          {card.headline}
        </Text>
      ) : null}

      {card.repo || card.branch ? (
        <Text style={styles.repoLine} numberOfLines={1}>
          {card.repo}
          {card.branch ? `  ⎇ ${card.branch}` : ''}
        </Text>
      ) : null}

      {card.parent_id ? (
        <Text style={styles.lineage} numberOfLines={1}>
          ↳ spawned by {lineageLabel(card.parent_id)}
        </Text>
      ) : null}
      {card.children.length > 0 ? (
        <Text style={styles.lineage} numberOfLines={1}>
          └ {card.children.length} sub-agent{card.children.length === 1 ? '' : 's'}:{' '}
          {card.children.map((c) => c.title).join(' · ')}
        </Text>
      ) : null}

      {card.outcome && (card.state === 'review' || card.state === 'done') ? (
        <Text
          style={[styles.outcome, { color: card.outcome.ok ? colors.success : colors.danger }]}
          numberOfLines={2}
        >
          {card.outcome.ok ? '✓' : '✗'} {card.outcome.detail}
        </Text>
      ) : null}

      {card.state === 'waiting' && card.waiting_prompt ? (
        <View style={styles.prompt}>
          {card.waiting_prompt.question ? (
            <Text style={styles.promptQuestion}>{card.waiting_prompt.question}</Text>
          ) : null}
          <View style={styles.replies}>
            {card.waiting_prompt.options.map((o) => (
              <Pressable
                key={String(o.index)}
                onPress={() => onOption(card, o.index)}
                disabled={acting}
                accessibilityRole="button"
                accessibilityLabel={`Reply ${o.label}`}
                style={({ pressed }) => [
                  styles.reply,
                  pressed && styles.replyPressed,
                  acting && styles.replyOff,
                ]}
              >
                <Text style={styles.replyText}>{o.label}</Text>
              </Pressable>
            ))}
          </View>
        </View>
      ) : null}

      {killable ? (
        <View style={styles.actions}>
          <Pressable
            onPress={() => onKill(card)}
            disabled={acting}
            hitSlop={8}
            accessibilityRole="button"
            accessibilityLabel={`Kill ${card.title}`}
            style={({ pressed }) => [(pressed || acting) && { opacity: 0.6 }]}
          >
            <Text style={styles.kill}>Kill</Text>
          </Pressable>
        </View>
      ) : null}
    </Card>
  );
});

/** Flattened swimlane list: section-header items interleaved with card items —
 *  one FlatList keeps scroll state simple and pull-to-refresh trivial. */
type Row =
  | { type: 'header'; key: string; state: MissionCardState; count: number }
  | { type: 'card'; key: string; card: MissionCard };

export default function MissionControlScreen() {
  const nav = useNavigation<Nav>();
  const [cards, setCards] = useState<MissionCard[] | null>(null);
  const [pulse, setPulse] = useState<MissionPulse | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [actingId, setActingId] = useState<string | null>(null);
  // Poll diff-guard: cards keep their state identity (→ zero row re-renders)
  // unless the payload actually changed; the volatile pulse updates freely.
  const cardsJson = useRef('');

  const load = useCallback(async () => {
    try {
      const q = await getMissionQueue();
      const json = JSON.stringify(q.cards);
      if (json !== cardsJson.current) {
        cardsJson.current = json;
        setCards(q.cards);
      } else {
        setCards((prev) => prev ?? q.cards);
      }
      setPulse(q.pulse);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
      setCards((prev) => prev ?? []);
    }
  }, []);

  usePolling(load, 4000);

  const onRefresh = async () => {
    setRefreshing(true);
    await load();
    setRefreshing(false);
  };

  const rows = useMemo<Row[] | null>(() => {
    if (!cards) return null;
    const out: Row[] = [];
    for (const state of SECTION_ORDER) {
      const group = cards.filter((c) => c.state === state);
      if (!group.length) continue;
      out.push({ type: 'header', key: `h:${state}`, state, count: group.length });
      for (const c of group) out.push({ type: 'card', key: c.id, card: c });
    }
    return out;
  }, [cards]);

  const open = useCallback(
    (card: MissionCard) => {
      if (card.kind === 'chat') {
        // One-shot param — HypervisorScreen consumes openThreadId and clears it.
        nav.navigate('Hypervisor', { openThreadId: card.ref_id });
        return;
      }
      // initial: false → TaskList stays beneath the detail screen, so it opens
      // with a back button instead of becoming the stack's only (trapped) route.
      nav.navigate('Tasks', { screen: 'TaskDetail', params: { id: card.ref_id }, initial: false });
    },
    [nav],
  );

  const answer = useCallback(
    async (card: MissionCard, index: number | string) => {
      setActingId(card.id);
      try {
        await sendMessage(card.ref_id, String(index));
        await load();
      } catch (e) {
        setError((e as Error).message);
      } finally {
        setActingId(null);
      }
    },
    [load],
  );

  const kill = useCallback(
    (card: MissionCard) => {
      confirmAction({
        title: 'Kill this agent?',
        message: `${card.title} will be terminated. This can't be undone.`,
        confirmLabel: 'Kill',
        destructive: true,
        onConfirm: () => {
          setActingId(card.id);
          void killTask(card.ref_id)
            .then(load)
            .catch((e) => setError((e as Error).message))
            .finally(() => setActingId(null));
        },
      });
    },
    [load],
  );

  const waiting = pulse?.waiting ?? 0;

  return (
    <SafeAreaView style={styles.safe} edges={['top']}>
      <ScreenHeader title="Mission Control" subtitle="builds · chats · sub-agents" />

      {pulse ? (
        <View style={styles.pulse}>
          <Text style={styles.pulseText}>
            <Text style={styles.pulseNum}>{pulse.running}</Text> running
            <Text style={styles.pulseDot}> · </Text>
            <Text style={[styles.pulseNum, waiting > 0 && { color: colors.warning }]}>
              {waiting}
            </Text>
            <Text style={waiting > 0 ? { color: colors.warning } : undefined}> waiting on you</Text>
            <Text style={styles.pulseDot}> · </Text>
            <Text style={styles.pulseNum}>{pulse.review}</Text> review
          </Text>
          <Text style={styles.pulseSub}>
            {pulse.done_today} done today
            {waiting > 0 && pulse.oldest_wait_s > 0 ? (
              <>
                <Text style={styles.pulseDot}> · </Text>
                oldest wait{' '}
                <Text style={{ color: colors.warning }}>{durationLabel(pulse.oldest_wait_s)}</Text>
              </>
            ) : null}
          </Text>
        </View>
      ) : null}

      {error && cards !== null && cards.length > 0 ? <ErrorBanner message={error} /> : null}

      {rows === null ? (
        <Loading label="Loading the queue…" />
      ) : rows.length === 0 ? (
        error ? (
          <EmptyState icon="cloud-offline-outline" title="Couldn't reach your workspace" subtitle={error} />
        ) : (
          <EmptyState
            icon="telescope-outline"
            title="All quiet"
            subtitle="No agents in the queue. Builds, chats and sub-agents show up here the moment they start."
          />
        )
      ) : (
        <FlatList
          data={rows}
          keyExtractor={(r) => r.key}
          contentContainerStyle={styles.list}
          showsVerticalScrollIndicator={false}
          refreshControl={
            <RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={colors.accent} />
          }
          renderItem={({ item }) =>
            item.type === 'header' ? (
              <View style={styles.sectionHeader}>
                <View
                  style={[styles.sectionDot, { backgroundColor: statusColor(item.state === 'review' ? 'done' : item.state) }]}
                />
                <Text style={styles.sectionTitle}>{SECTION_TITLES[item.state]}</Text>
                <Text style={styles.sectionCount}>{item.count}</Text>
              </View>
            ) : (
              <MissionCardRow
                card={item.card}
                acting={actingId === item.card.id}
                onOpen={open}
                onOption={answer}
                onKill={kill}
              />
            )
          }
        />
      )}
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: colors.bg },
  pulse: { paddingHorizontal: space.lg, paddingBottom: space.md, gap: 2 },
  pulseText: { color: colors.textMuted, fontSize: font.size.sm },
  pulseNum: { color: colors.text, fontWeight: '700' },
  pulseDot: { color: colors.textFaint },
  pulseSub: { color: colors.textFaint, fontSize: font.size.xs },
  list: { paddingHorizontal: space.lg, paddingBottom: space.xl, gap: space.md },
  sectionHeader: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.sm,
    marginTop: space.sm,
  },
  sectionDot: { width: 7, height: 7, borderRadius: 4 },
  sectionTitle: {
    color: colors.textMuted,
    fontSize: font.size.xs,
    fontWeight: '700',
    textTransform: 'uppercase',
    letterSpacing: 0.7,
  },
  sectionCount: { color: colors.textFaint, fontSize: font.size.xs, fontFamily: font.mono },
  card: { gap: space.sm },
  topRow: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', gap: space.sm },
  topLeft: { flexDirection: 'row', alignItems: 'center', gap: space.sm, flexShrink: 1 },
  kind: { color: colors.textFaint, fontSize: font.size.xs, fontFamily: font.mono, letterSpacing: 0.5 },
  agent: { color: colors.textMuted, fontSize: font.size.xs, flexShrink: 1 },
  time: { color: colors.textFaint, fontSize: font.size.xs },
  titleRow: { flexDirection: 'row', alignItems: 'center', gap: space.sm },
  title: { flex: 1, color: colors.text, fontSize: font.size.md, fontWeight: '600', lineHeight: 21 },
  headline: { color: colors.textMuted, fontSize: font.size.sm, lineHeight: 19 },
  repoLine: { color: colors.textFaint, fontSize: font.size.xs, fontFamily: font.mono },
  lineage: { color: colors.textFaint, fontSize: font.size.xs },
  outcome: { fontSize: font.size.sm, lineHeight: 19 },
  prompt: {
    marginTop: space.xs,
    padding: space.md,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: colors.warning + '55',
    backgroundColor: colors.warning + '14',
    gap: space.sm,
  },
  promptQuestion: { color: colors.text, fontSize: font.size.sm, lineHeight: 19 },
  replies: { flexDirection: 'row', flexWrap: 'wrap', gap: space.sm },
  reply: {
    borderWidth: 1,
    borderColor: colors.accent,
    borderRadius: radius.pill,
    paddingHorizontal: space.md,
    paddingVertical: 6,
  },
  replyPressed: { opacity: 0.7 },
  replyOff: { opacity: 0.5 },
  replyText: { color: colors.accent, fontSize: font.size.sm, fontWeight: '600' },
  actions: { flexDirection: 'row', justifyContent: 'flex-end' },
  kill: { color: colors.danger, fontSize: font.size.sm, fontWeight: '700' },
  // Mirrors ui.tsx StatusPill's shape — needed because 'review' isn't in the
  // canonical status set (done-style colour, its own label).
  reviewPill: {
    flexDirection: 'row',
    alignItems: 'center',
    alignSelf: 'flex-start',
    borderRadius: radius.pill,
    borderWidth: 1,
    paddingVertical: 4,
    paddingHorizontal: 10,
    backgroundColor: colors.surface2,
  },
  reviewDot: { width: 7, height: 7, borderRadius: 4, marginRight: 6 },
  reviewPillText: {
    fontSize: font.size.xs,
    fontWeight: '600',
    textTransform: 'uppercase',
    letterSpacing: 0.4,
  },
});
