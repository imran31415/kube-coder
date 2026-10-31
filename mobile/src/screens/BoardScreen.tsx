/**
 * Board review (#588 Phase 6) — decide staged writes from a phone.
 *
 * Mobile LEADS this design rather than mirroring the desktop: approving five
 * staged replies while away from a desk is the realistic workflow, and running
 * a board from a phone is not. Three constraints shape the screen:
 *
 * 1. **One item decidable on one screen** — proposed writes, the reason,
 *    evidence chips, and a deep link to the real ticket. No transcript. If a
 *    decision needs the agent's full log, the agent has not summarised well
 *    enough, and scrolling one on a phone is not a fix.
 * 2. **Approve / reject in one tap**, edit deliberately absent — editing a
 *    customer-visible reply on a phone keyboard is the desktop's job.
 * 3. **Offline-tolerant** — every decision goes through the local queue
 *    (util/approvalQueue) and carries an approval_id the server consumes once,
 *    so airplane-mode-then-reconnect posts exactly one comment.
 *
 * Three tabs (#709 Part 3) — **Needs you · Working · Done** — so the phone
 * also answers "what is still being worked, and what finished while I was
 * away". Needs you is the review queue above, unchanged. Working and Done come
 * from one local read (`/recent`); a Done row carries one result line and an
 * unread dot, which is the Feed's own read flag — opening the row marks that
 * Feed item read. There is still no "start a run" control: runs start from
 * the dashboard or a trigger, and the phone decides.
 *
 * Polling uses the shared focus-aware `usePolling`, not a raw setInterval:
 * tab screens stay mounted, and a backgrounded tab must not keep hitting the
 * workspace.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  FlatList,
  KeyboardAvoidingView,
  Linking,
  Modal,
  Platform,
  Pressable,
  RefreshControl,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import AsyncStorage from '@react-native-async-storage/async-storage';
import { useNavigation } from '@react-navigation/native';
import {
  getBoardRecent,
  getBoardReview,
  getBoardStanding,
  listBoards,
  listFeed,
  decideBoardItem,
  markFeedRead,
} from '../api/client';
import { Card, EmptyState, ErrorBanner, Loading, ScreenHeader, StaleNote } from '../components/ui';
import { useOnAwake, useWorkspaceState } from '../store/useWorkspaceState';
import { colors, font, radius, space } from '../theme';
import { isDownError } from '../util/workspaceReach';
import { relativeTime } from '../util/format';
import { usePolling } from '../util/usePolling';
import { formatDiffStat, worktreeRemoved } from '../util/worktree';
import { clearBoardFocus, useBoardFocus } from '../store/boardFocus';
import { pickBoard, rememberBoard } from '../util/lastBoard';
import {
  drain,
  enqueue,
  pendingItemIds,
  readQueue,
  type Decision,
  type QueuedApproval,
} from '../util/approvalQueue';
import {
  BOARD_TABS,
  clearDot,
  decisionRequest,
  markRowRead,
  needsYouCount,
  splitRecent,
  tabA11yLabel,
  type BoardTab,
} from '../util/boardTabs';
import type {
  BoardRecentRow,
  BoardReviewGroup,
  BoardReviewItem,
  BoardStanding,
  BoardSummary,
} from '../api/types';

const DISPOSITION_LABEL: Record<string, string> = {
  needs_review: 'Needs review',
  needs_rescoping: 'Needs rescoping',
  blocked: 'Blocked',
  failed: 'Failed',
  completed: 'Completed',
  rejected: 'Rejected',
  unreported: 'No disposition',
};

const DISPOSITION_RULE: Record<string, string> = {
  needs_review: colors.accent,
  needs_rescoping: colors.info,
  blocked: colors.danger,
  failed: colors.danger,
  completed: colors.success,
};

type Row =
  | { type: 'header'; key: string; label: string; count: number; rule: string }
  | { type: 'item'; key: string; item: BoardReviewItem };

/** The state badge's colour, by state. Everything else is muted: a board that
 *  is idle should not shout. */
const STATE_RULE: Record<BoardStanding['state'], string> = {
  running: colors.accent,
  awaiting_human: colors.warning,
  needs_credential: colors.danger,
  never_run: colors.textMuted,
  idle: colors.textMuted,
};

/** Read the standing, or null. A workspace that predates `/standing` answers
 *  404, and that must cost the screen nothing but the banner. */
async function standingOrNull(boardId: string): Promise<BoardStanding | null> {
  try {
    return await getBoardStanding(boardId);
  } catch {
    return null;
  }
}

/** Working and Done, or null. A workspace older than `/recent` answers 404,
 *  and that must cost the phone its two new tabs, not its review queue. */
async function recentOrNull(boardId: string): Promise<BoardRecentRow[] | null> {
  try {
    return await getBoardRecent(boardId);
  } catch {
    return null;
  }
}

/** The Needs-you count, from the Feed — or null when the Feed could not be
 *  read, which shows no count rather than a wrong one. */
async function waitingOrNull(boardId: string): Promise<number | null> {
  try {
    return needsYouCount(await listFeed(), boardId);
  } catch {
    return null;
  }
}

/** Cross-tab navigation, as Mission Control types it. */
type Nav = { navigate: (tab: string, opts?: object) => void };

export default function BoardScreen() {
  const nav = useNavigation<Nav>();
  // The Build that worked an item, opened on its changes (#701). initial:
  // false keeps TaskList beneath it, so the detail has a back button.
  const viewChanges = useCallback((taskId: string) => {
    nav.navigate('Tasks', {
      screen: 'TaskDetail', params: { id: taskId, tab: 'changes' }, initial: false,
    });
  }, [nav]);
  // The Build working an item, opened on its live transcript.
  const viewTask = useCallback((taskId: string) => {
    nav.navigate('Tasks', {
      screen: 'TaskDetail', params: { id: taskId }, initial: false,
    });
  }, [nav]);
  const [boards, setBoards] = useState<BoardSummary[] | null>(null);
  const [tab, setTab] = useState<BoardTab>('needs_you');
  // Working and Done rows (#709). Null until read, and stays null on a
  // workspace that predates the endpoint.
  const [recent, setRecent] = useState<BoardRecentRow[] | null>(null);
  // The Needs-you count, read off the Feed like the drawer's badge.
  const [waiting, setWaiting] = useState<number | null>(null);
  // The Done row opened to show its detail.
  const [opened, setOpened] = useState<string | null>(null);
  const [boardId, setBoardId] = useState<string | null>(null);
  const [groups, setGroups] = useState<BoardReviewGroup[] | null>(null);
  // Which board `groups` actually belongs to. While a switch is in flight the
  // queue on screen is still the PREVIOUS board's, and a deep link that
  // searched it would decide its item was gone.
  const [groupsBoard, setGroupsBoard] = useState<string | null>(null);
  // What the board is DOING — see the banner below. Null while it has not been
  // read yet, or when the workspace is older than the endpoint (#712).
  const [standing, setStanding] = useState<BoardStanding | null>(null);
  const [highlight, setHighlight] = useState<string | null>(null);
  // Kept apart from `notice`, which carries queue errors and is meant to
  // persist. This one answers "I tapped a notification and nothing happened",
  // so it is a passing remark and clears itself.
  const [focusNote, setFocusNote] = useState<string | null>(null);
  const [queued, setQueued] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [syncedAt, setSyncedAt] = useState<number | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const away = useWorkspaceState().reach !== 'awake';
  const [refreshing, setRefreshing] = useState(false);
  const [sendingBack, setSendingBack] = useState<BoardReviewItem | null>(null);
  const [note, setNote] = useState('');
  const listRef = useRef<FlatList<Row>>(null);
  /** The board the last fetch was for — see the board-switch effect below. */
  const loadedFor = useRef<string | null>(null);

  /** Send one queued decision. Returns the status so the queue can decide
   *  whether it is worth retrying. */
  const send = useCallback(async (entry: QueuedApproval) => {
    // Send-back's text is a NOTE — an instruction to the agent, which the
    // server requires. Reject's is a reason and stays optional. Same stored
    // field, different meaning, so the wire name differs (decisionRequest).
    const res = await decideBoardItem(
      entry.board_id,
      entry.item_id,
      entry.decision,
      decisionRequest(entry),
    );
    // Away is not a failure of this decision — the WakeBanner says it once.
    if (res.error && !isDownError(res)) setNotice(res.error);
    return { status: res.status, code: res.code };
  }, []);

  const load = useCallback(async () => {
    try {
      // Drain FIRST. There is no NetInfo in this app, so nothing can react to
      // reconnection — every refresh is the opportunity to flush what the last
      // dropped connection left behind.
      const result = await drain(AsyncStorage, send);
      if (result.errors.length > 0) setNotice(result.errors[0]);

      const list = boards ?? (await listBoards());
      if (!boards) setBoards(list);
      // The board this phone was last on, not whichever sorts first (#712).
      const active = boardId ?? (await pickBoard(AsyncStorage, list));
      if (!boardId && active) setBoardId(active);
      // Claim the board before awaiting, so the switch effect below sees this
      // pass already covers it and does not fire a duplicate fetch for the
      // board this very call is resolving.
      loadedFor.current = active;

      setQueued(pendingItemIds(await readQueue(AsyncStorage)));
      // Standing is read in its own try: it is the newest of these endpoints,
      // and a phone talking to a workspace that predates it must still get its
      // review queue rather than an error screen.
      setStanding(active ? await standingOrNull(active) : null);
      const review = active ? await getBoardReview(active) : [];
      // Both in their own try, like standing: the newest reads, and a phone
      // on an older workspace keeps its review queue whatever they answer.
      const [rows, count] = active
        ? await Promise.all([recentOrNull(active), waitingOrNull(active)])
        : [[], 0];
      setGroups(review);
      // A failed read keeps what was on screen (dimmed while away), and a
      // first one that fails is an empty tab — never a spinner that waits on
      // an endpoint an older workspace does not have.
      setRecent((prev) => rows ?? prev ?? []);
      setWaiting((prev) => count ?? prev);
      setGroupsBoard(active);
      setSyncedAt(Date.now());
      setError(null);
    } catch (e) {
      setError((e as Error).message);
      setGroups((prev) => prev ?? []);
      setRecent((prev) => prev ?? []);
    }
  }, [boardId, boards, send]);

  usePolling(load, 15000);
  // Waking up flushes decisions queued while asleep: load() drains first.
  useOnAwake(load);

  // usePolling holds `fn` in a ref keyed only on the interval, so selecting a
  // different board does not restart the poll: the queue simply stayed blank
  // until the next 15-second tick. That was survivable while the only way to
  // switch was tapping a chip; a deep link that lands on the other board would
  // spend those 15 seconds on a spinner with nothing to show for it.
  const loadRef = useRef(load);
  loadRef.current = load;
  useEffect(() => {
    if (!boardId || loadedFor.current === boardId) return;
    void loadRef.current();
  }, [boardId]);

  const onRefresh = async () => {
    setRefreshing(true);
    await load();
    setRefreshing(false);
  };

  const commit = useCallback(
    async (item: BoardReviewItem, decision: Decision, reason: string) => {
      await enqueue(AsyncStorage, {
        board_id: item.board_id,
        item_id: item.item_id,
        decision,
        // The hash the CARD carried, so approving something that has since
        // changed is refused rather than written.
        content_hash: item.content_hash,
        reason,
      });
      // Grey the card immediately: the decision is durable now even if the
      // request has not gone out yet.
      setQueued((prev) => new Set(prev).add(item.item_id));
      await load();
    },
    [load],
  );

  const decide = useCallback(
    async (item: BoardReviewItem, decision: Decision) => {
      // Send back needs a note before it can be queued at all — queueing one
      // without would mean a decision that is durable locally and can only
      // ever 400 on the way out, which is the worst of both.
      if (decision === 'send-back') {
        setSendingBack(item);
        return;
      }
      await commit(item, decision, '');
    },
    [commit],
  );

  // Memoized: the focus effect below depends on it, and a bare const would
  // hand it a new array on every render — including every 15-second poll tick.
  const rows = useMemo<Row[] | null>(
    () =>
      groups
        ? groups.flatMap((g) => [
            {
              type: 'header' as const,
              key: `h:${g.disposition}`,
              label: DISPOSITION_LABEL[g.disposition] ?? g.disposition,
              count: g.count,
              rule: DISPOSITION_RULE[g.disposition] ?? colors.border,
            },
            ...g.items.map((item) => ({
              type: 'item' as const,
              key: item.item_id,
              item,
            })),
          ])
        : null,
    [groups],
  );

  const split = useMemo(() => splitRecent(recent ?? []), [recent]);

  // A board review push, or a board chip in the Feed, names ONE item on ONE
  // board. Both park that request in the focus store rather than passing a
  // route param, because this is a tab screen that stays mounted: a param set
  // on the first tap would still be there, stale, on the second.
  const focus = useBoardFocus();
  useEffect(() => {
    if (!focus) return;

    // A stale push can name a board that has since been disconnected. Say so,
    // rather than poll a 404 forever.
    if (boards && !boards.some((b) => b.id === focus.boardId)) {
      setFocusNote('That board is no longer connected to this workspace.');
      clearBoardFocus(focus.seq);
      return;
    }

    // Usually the item is on the board that is NOT on screen. Switch and
    // return; the effect above fetches, and this runs again when it lands.
    if (boardId !== focus.boardId) {
      setBoardId(focus.boardId);
      setGroups(null);
      setRecent(null);
      setWaiting(null);
      // The old board's banner must not sit over the new board's queue.
      setStanding(null);
      // A deep link is a visit like any other: coming back later should open
      // the board the notification took you to (#712).
      void rememberBoard(AsyncStorage, focus.boardId);
      return;
    }

    if (rows === null || groupsBoard !== focus.boardId) return;

    const index = rows.findIndex(
      (r) => r.type === 'item' && r.item.item_id === focus.itemId,
    );
    if (index < 0) {
      // Not awaiting a decision any more — but a Done row's Feed item links
      // here too (#709), and so does a review someone already decided. If
      // the item is on Working or Done, land there instead.
      const other = recent?.find((r) => r.item_id === focus.itemId);
      const where = other
        ? split.done.includes(other) ? 'done' : split.working.includes(other) ? 'working' : null
        : null;
      if (other && where) {
        setTab(where);
        setHighlight(focus.itemId);
        if (where === 'done') setOpened(focus.itemId);
        clearBoardFocus(focus.seq);
        return;
      }
      // Decided by someone else, dismissed, or never in this queue. One
      // sentence beats a screen waiting on a card that is not coming.
      setFocusNote('That item is no longer awaiting a decision.');
      clearBoardFocus(focus.seq);
      return;
    }

    // Highlight FIRST. Rows are variable-height with no getItemLayout, so the
    // scroll can miss; the mark is what actually lands the user on the card.
    // The card is on Needs you. Switch first and come back: the queue's list
    // is not mounted under another tab, so there is nothing yet to scroll.
    if (tab !== 'needs_you') {
      setTab('needs_you');
      return;
    }
    setHighlight(focus.itemId);
    listRef.current?.scrollToIndex({ index, animated: true, viewPosition: 0.1 });
    clearBoardFocus(focus.seq);
  }, [focus, boards, boardId, rows, groupsBoard, recent, split, tab]);

  // The mark is an arrival cue, not a state. Left up, it would still be there
  // after five minutes of scrolling, claiming to mean something.
  useEffect(() => {
    if (!highlight) return;
    const t = setTimeout(() => setHighlight(null), 4000);
    return () => clearTimeout(t);
  }, [highlight]);

  useEffect(() => {
    if (!focusNote) return;
    const t = setTimeout(() => setFocusNote(null), 6000);
    return () => clearTimeout(t);
  }, [focusNote]);

  const openCount = groups
    ? groups.flatMap((g) => g.items).filter((i) => i.open).length
    : 0;
  const current = boards?.find((b) => b.id === boardId) ?? null;
  const noBoards = boards !== null && boards.length === 0;
  const unreadDone = split.done.filter((r) => r.unread).length;
  const counts: Record<BoardTab, number | null> = {
    needs_you: waiting,
    working: recent ? split.working.length : null,
    done: recent ? split.done.length : null,
  };
  // What the open tab has on it, for the stale note and the empty states.
  const shown = tab === 'needs_you' ? rows : recent ? split[tab] : null;

  /** Open a Done row: show its detail, and clear its dot through the Feed's
   *  read API. The dot goes at once; a failed mark brings it back on the next
   *  poll, which is the truth. */
  const openDone = useCallback((row: BoardRecentRow) => {
    setOpened((prev) => (prev === row.item_id ? null : row.item_id));
    if (row.unread && row.feed_id) {
      const feedId = row.feed_id;
      setRecent((prev) => (prev ? markRowRead(prev, feedId) : prev));
    }
    void clearDot(row, markFeedRead);
  }, []);

  return (
    <SafeAreaView style={styles.safe} edges={['top', 'left', 'right']}>
      <ScreenHeader
        title="Board"
        // The board's NAME, because "Board" alone told you nothing about which
        // tracker you were looking at (#712).
        subtitle={
          current
            ? openCount > 0
              ? `${current.display_name} · ${openCount} awaiting your decision`
              : current.display_name
            : noBoards
              ? 'No boards connected'
              : 'Nothing waiting for you'
        }
      />

      {/* Shown for ONE board too. It is the only thing on the screen that says
          which tracker these cards belong to, and a single unlabelled queue is
          exactly how this screen came to look empty. */}
      {boards && boards.length > 0 && (
        <View style={styles.chips}>
          {boards.map((b) => (
            <Pressable
              key={b.id}
              onPress={() => {
                setBoardId(b.id);
                setGroups(null);
                setRecent(null);
                setWaiting(null);
                setStanding(null);
                void rememberBoard(AsyncStorage, b.id);
              }}
              accessibilityRole="button"
              accessibilityState={{ selected: boardId === b.id }}
              accessibilityLabel={`Show ${b.display_name}`}
              style={[styles.chip, boardId === b.id && styles.chipActive]}
            >
              <Text
                style={[
                  styles.chipText,
                  boardId === b.id && styles.chipTextActive,
                ]}
              >
                {b.display_name}
              </Text>
            </Pressable>
          ))}
        </View>
      )}

      {/* What the board is doing, above the queue and present whether or not
          anything is staged. This banner is the fix for "the Board page is
          empty on iOS" (#712): with nothing to review the screen used to carry
          one grey sentence and no way to tell a busy board from a broken one. */}
      {standing && <StandingBanner standing={standing} />}

      {!noBoards && boards && (
        <TabBar tab={tab} counts={counts} unreadDone={unreadDone} onSelect={setTab} />
      )}

      {away ? !!shown?.length && <StaleNote at={syncedAt} /> : error && <ErrorBanner message={error} />}
      {notice && <ErrorBanner message={notice} />}
      {focusNote && (
        <Text style={styles.focusNote} role="status">
          {focusNote}
        </Text>
      )}

      {noBoards ? (
        <EmptyState
          icon="clipboard-outline"
          title="No boards connected"
          subtitle="A board is an external tracker — Jira, GitHub, Linear, Zendesk. Connect one from the dashboard's Board page, then its items come here for review."
        />
      ) : tab !== 'needs_you' ? (
        <RecentList
          tab={tab}
          rows={recent ? split[tab] : null}
          away={away}
          live={!!standing?.live}
          highlight={highlight}
          opened={opened}
          refreshing={refreshing}
          onRefresh={onRefresh}
          onOpenDone={openDone}
          onViewTask={viewTask}
        />
      ) : rows === null ? (
        <Loading />
      ) : rows.length === 0 && away ? (
        <EmptyState
          icon="moon-outline"
          title="Workspace not available"
          subtitle="Wake it from the bar below to load the review queue."
        />
      ) : rows.length === 0 ? (
        <EmptyState
          title="Nothing to review"
          subtitle={
            standing?.live
              ? 'A run is working this board now. Anything an agent wants to write lands here for your approval.'
              : 'When an agent works a board item in propose mode, its proposed writes land here for you to approve.'
          }
        />
      ) : (
        <FlatList
          ref={listRef}
          data={rows}
          keyExtractor={(r) => r.key}
          style={away && styles.stale}
          contentContainerStyle={styles.list}
          refreshControl={
            <RefreshControl refreshing={refreshing} onRefresh={onRefresh} />
          }
          // Cards are variable-height and there is no getItemLayout to give,
          // so a scroll to an unmeasured row throws rather than scrolling.
          // Approximate it instead — the highlight already marks the card, so
          // landing near it is enough.
          onScrollToIndexFailed={({ index, averageItemLength }) => {
            listRef.current?.scrollToOffset({
              offset: index * averageItemLength,
              animated: true,
            });
          }}
          renderItem={({ item: row }) =>
            row.type === 'header' ? (
              <View style={styles.groupHead}>
                <View style={[styles.rule, { backgroundColor: row.rule }]} />
                <Text style={styles.groupLabel}>{row.label}</Text>
                <Text style={styles.groupCount}>{row.count}</Text>
              </View>
            ) : (
              <ReviewCard
                item={row.item}
                busy={queued.has(row.item.item_id)}
                focused={highlight === row.item.item_id}
                onDecide={decide}
                onViewChanges={viewChanges}
              />
            )
          }
        />
      )}

      {/* Send back needs a note; reject does not. The agent is about to work
          this item again and the note is the only thing telling it what to
          change — so this is the one decision that cannot be one tap. */}
      <Modal
        visible={sendingBack !== null}
        transparent
        animationType="fade"
        onRequestClose={() => setSendingBack(null)}
      >
        <View style={styles.modalBackdrop}>
          {/* The note field autofocuses, so the keyboard lands on top of the
              card's own buttons. Modals need their own KeyboardAvoidingView —
              the screen's does not reach into a Modal's view tree (#662). */}
          <KeyboardAvoidingView behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
          <View style={styles.modalCard}>
            <Text style={styles.modalTitle}>
              Send back {sendingBack?.item_key || sendingBack?.item_id}
            </Text>
            <Text style={styles.modalBody}>
              The agent works this again with your note. Nothing is written to
              the board.
            </Text>
            <TextInput
              style={styles.modalInput}
              value={note}
              onChangeText={setNote}
              placeholder="What should it do differently?"
              placeholderTextColor={colors.textMuted}
              multiline
              autoFocus
            />
            <View style={styles.modalActions}>
              <Pressable
                style={styles.btn}
                accessibilityRole="button"
                accessibilityLabel="Cancel"
                onPress={() => {
                  setSendingBack(null);
                  setNote('');
                }}
              >
                <Text style={styles.btnText}>Cancel</Text>
              </Pressable>
              <Pressable
                style={[styles.btn, styles.btnPrimary,
                        !note.trim() && styles.btnDisabled]}
                disabled={!note.trim()}
                accessibilityRole="button"
                accessibilityLabel="Confirm send back"
                accessibilityState={{ disabled: !note.trim() }}
                onPress={async () => {
                  const target = sendingBack;
                  const text = note.trim();
                  setSendingBack(null);
                  setNote('');
                  if (target) await commit(target, 'send-back', text);
                }}
              >
                <Text style={styles.btnPrimaryText}>Send back</Text>
              </Pressable>
            </View>
          </View>
          </KeyboardAvoidingView>
        </View>
      </Modal>
    </SafeAreaView>
  );
}

/**
 * Where the board stands, in a badge and a sentence (#712).
 *
 * Deliberately not a progress bar: the question a phone is opened to answer is
 * "is anything happening, and does it need me", and a bar answers neither when
 * the run has already finished. The badge is the one-word state and the line
 * under it is the evidence. `blocked_reason` is deliberately left out: runs
 * are started from the dashboard, so "you cannot start another run" is not
 * advice a phone can act on.
 */
function StandingBanner({ standing }: { standing: BoardStanding }) {
  const rule = STATE_RULE[standing.state] ?? colors.textMuted;
  return (
    <View
      style={styles.standing}
      accessibilityRole="summary"
      accessibilityLabel={`Board state: ${standing.label}. ${standing.detail}`}
    >
      <View style={styles.standingHead}>
        <View style={[styles.standingDot, { backgroundColor: rule }]} />
        <Text style={[styles.standingLabel, { color: rule }]}>
          {standing.label}
        </Text>
        {standing.awaiting > 0 && (
          <Text style={styles.standingCount}>
            {standing.awaiting} awaiting you
          </Text>
        )}
      </View>
      <Text style={styles.standingDetail}>{standing.detail}</Text>
    </View>
  );
}

/**
 * Needs you · Working · Done (#709). Each tab announces its count, because a
 * bare number beside a word reads as nothing at all to a screen reader. Needs
 * you counts from the Feed, so it can be null (Feed unreadable): no count is
 * better than a wrong one.
 */
function TabBar({
  tab,
  counts,
  unreadDone,
  onSelect,
}: {
  tab: BoardTab;
  counts: Record<BoardTab, number | null>;
  unreadDone: number;
  onSelect: (tab: BoardTab) => void;
}) {
  return (
    <View style={styles.tabs} accessibilityRole="tablist">
      {BOARD_TABS.map((t) => {
        const selected = tab === t.key;
        const count = counts[t.key];
        const unread = t.key === 'done' ? unreadDone : 0;
        return (
          <Pressable
            key={t.key}
            accessibilityRole="tab"
            accessibilityState={{ selected }}
            accessibilityLabel={tabA11yLabel(t.key, count, unread)}
            onPress={() => onSelect(t.key)}
            style={[styles.tab, selected && styles.tabActive]}
          >
            <Text style={[styles.tabText, selected && styles.tabTextActive]}>
              {t.label}
            </Text>
            {count !== null && (
              <Text
                style={[
                  styles.tabCount,
                  t.key === 'needs_you' && count > 0 && styles.tabCountHot,
                ]}
              >
                {count}
              </Text>
            )}
            {unread > 0 && <View style={styles.tabDot} />}
          </Pressable>
        );
      })}
    </View>
  );
}

const EMPTY: Record<'working' | 'done', { title: string; idle: string; live?: string }> = {
  working: {
    title: 'Nothing being worked',
    idle: 'Runs start from the dashboard or a trigger. Items an agent is working on show here.',
    live: 'A run is starting on this board. Items show here as agents pick them up.',
  },
  done: {
    title: 'Nothing finished yet',
    idle: 'Finished items show here with their result, newest first.',
  },
};

/** The Working or Done list, with the same loading, empty and away states as
 *  the review queue. */
function RecentList({
  tab,
  rows,
  away,
  live,
  highlight,
  opened,
  refreshing,
  onRefresh,
  onOpenDone,
  onViewTask,
}: {
  tab: 'working' | 'done';
  rows: BoardRecentRow[] | null;
  away: boolean;
  live: boolean;
  highlight: string | null;
  opened: string | null;
  refreshing: boolean;
  onRefresh: () => void;
  onOpenDone: (row: BoardRecentRow) => void;
  onViewTask: (taskId: string) => void;
}) {
  if (rows === null && !away) return <Loading />;
  if (!rows?.length) {
    return away ? (
      <EmptyState
        icon="moon-outline"
        title="Workspace not available"
        subtitle={`Wake it from the bar below to load what is ${tab === 'done' ? 'done' : 'being worked'}.`}
      />
    ) : (
      <EmptyState
        title={EMPTY[tab].title}
        subtitle={(live && EMPTY[tab].live) || EMPTY[tab].idle}
      />
    );
  }
  return (
    <FlatList
      data={rows}
      keyExtractor={(r) => `${r.run_id}:${r.item_id}`}
      style={away && styles.stale}
      contentContainerStyle={styles.list}
      refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} />}
      renderItem={({ item: row }) =>
        tab === 'working' ? (
          <WorkingRow row={row} focused={highlight === row.item_id} onViewTask={onViewTask} />
        ) : (
          <DoneRow
            row={row}
            focused={highlight === row.item_id}
            open={opened === row.item_id}
            onOpen={onOpenDone}
            onViewTask={onViewTask}
          />
        )
      }
    />
  );
}

/** An item an agent holds right now. The whole row opens its Build. */
function WorkingRow({
  row,
  focused,
  onViewTask,
}: {
  row: BoardRecentRow;
  focused: boolean;
  onViewTask: (taskId: string) => void;
}) {
  const status =
    row.state === 'claimed' ? 'Picked up, starting a build' : 'An agent is working on it';
  return (
    <Card
      nativeID={`board-working-${row.item_id}`}
      style={StyleSheet.flatten([focused ? styles.cardFocused : null])}
    >
      <Pressable
        style={styles.rowHit}
        disabled={!row.task_id}
        accessibilityRole="button"
        accessibilityLabel={`${row.item_key}, ${row.item_title}. ${status}${row.task_id ? '. Open build' : ''}`}
        onPress={() => row.task_id && onViewTask(row.task_id)}
      >
        <View style={styles.cardHead}>
          <Text style={styles.itemKey}>{row.item_key}</Text>
          <Text style={styles.itemAge}>{relativeTime(row.updated_at)}</Text>
        </View>
        <Text style={styles.rowTitle} numberOfLines={2}>{row.item_title}</Text>
        <View style={styles.rowFoot}>
          <View style={[styles.standingDot, { backgroundColor: colors.accent }]} />
          <Text style={styles.rowStatus} numberOfLines={1}>{status}</Text>
          {!!row.task_id && <Text style={styles.link}>Build ›</Text>}
        </View>
      </Pressable>
    </Card>
  );
}

/** A finished item: one line of result and an unread dot. Opening it shows
 *  the detail and clears the dot — the Feed's own read flag. */
function DoneRow({
  row,
  focused,
  open,
  onOpen,
  onViewTask,
}: {
  row: BoardRecentRow;
  focused: boolean;
  open: boolean;
  onOpen: (row: BoardRecentRow) => void;
  onViewTask: (taskId: string) => void;
}) {
  const failed = row.state === 'failed' || row.result.startsWith('Failed');
  // Green only for something that landed: a rejection or a send-back wrote
  // nothing, and colouring it like success would say otherwise.
  const declined = row.review_state === 'rejected' || row.review_state === 'sent_back';
  const detail = row.error || row.reason;
  return (
    <Card
      nativeID={`board-done-${row.item_id}`}
      style={StyleSheet.flatten([focused ? styles.cardFocused : null])}
    >
      <Pressable
        style={styles.rowHit}
        accessibilityRole="button"
        accessibilityState={{ expanded: open }}
        accessibilityLabel={`${row.unread ? 'Unread. ' : ''}${row.item_key}, ${row.item_title}. ${row.result}`}
        onPress={() => onOpen(row)}
      >
        <View style={styles.cardHead}>
          <View style={styles.doneKey}>
            {row.unread && <View style={styles.unreadDot} testID="unread-dot" />}
            <Text style={styles.itemKey}>{row.item_key}</Text>
          </View>
          <Text style={styles.itemAge}>{relativeTime(row.updated_at)}</Text>
        </View>
        <Text style={styles.rowTitle} numberOfLines={2}>{row.item_title}</Text>
        <Text
          style={[
            styles.result,
            failed ? styles.resultFailed : declined ? styles.resultMuted : styles.resultOk,
          ]}
          numberOfLines={1}
        >
          {row.result}
        </Text>
      </Pressable>
      {open && (
        <View style={styles.doneDetail}>
          {!!detail && <Text style={styles.reason}>{detail}</Text>}
          <View style={styles.actions}>
            {!!row.item_url && (
              <Pressable
                style={styles.btn}
                accessibilityRole="link"
                accessibilityLabel="Open ticket"
                onPress={() => void Linking.openURL(row.item_url)}
              >
                <Text style={styles.btnText}>Open ticket ↗</Text>
              </Pressable>
            )}
            {!!row.task_id && (
              <Pressable
                style={styles.btn}
                accessibilityRole="button"
                accessibilityLabel="Open build"
                onPress={() => onViewTask(row.task_id)}
              >
                <Text style={styles.btnText}>Open build</Text>
              </Pressable>
            )}
          </View>
        </View>
      )}
    </Card>
  );
}

function ReviewCard({
  item,
  busy,
  focused,
  onDecide,
  onViewChanges,
}: {
  item: BoardReviewItem;
  busy: boolean;
  focused: boolean;
  onDecide: (item: BoardReviewItem, decision: Decision) => void;
  onViewChanges: (taskId: string) => void;
}) {
  const evidence = Object.entries(item.evidence ?? {});
  return (
    <Card
      // The item id VERBATIM — it is what a "board:<board>:<item>" ref carries,
      // and GitHub's GraphQL ids contain characters that would not survive
      // being cleaned up.
      nativeID={`board-item-${item.item_id}`}
      style={StyleSheet.flatten([
        busy ? styles.cardBusy : null,
        focused ? styles.cardFocused : null,
      ])}
    >
      <View style={styles.cardHead}>
        <Text style={styles.itemKey}>{item.item_key || item.item_id}</Text>
        <Text style={styles.itemAge}>{relativeTime(item.created_at)}</Text>
      </View>
      <Text style={styles.itemTitle}>{item.item_title}</Text>

      {/* The code behind the proposed reply (#701): approving "Fixed on
          kc/…" from a phone should not mean approving it unseen. */}
      {item.worktree?.branch ? (
        <Pressable
          style={styles.wtRow}
          accessibilityRole="link"
          accessibilityLabel="View changes"
          onPress={() => onViewChanges(item.worktree!.task_id)}
        >
          <View style={{ flex: 1 }}>
            <Text style={styles.wtBranch} numberOfLines={1}>⎇ {item.worktree.branch}</Text>
            <Text style={styles.wtStat}>
              {worktreeRemoved(item.worktree)
                ? 'worktree removed — branch kept'
                : item.worktree.stat
                  ? formatDiffStat(item.worktree.stat)
                  : 'changes not recorded yet'}
            </Text>
          </View>
          <Text style={styles.link}>View changes ›</Text>
        </Pressable>
      ) : null}

      {item.pending_actions.map((action) => (
        <View key={action.id} style={styles.action}>
          <Text style={styles.actionName}>{action.action}</Text>
          {/* Plain <Text>, never markup: this is about to be shown to a
              customer, so what is read must be what is sent. */}
          <Text style={styles.actionPreview}>{action.preview}</Text>
        </View>
      ))}

      {!!item.reason && <Text style={styles.reason}>{item.reason}</Text>}

      {evidence.length > 0 && (
        <View style={styles.chipsRow}>
          {evidence.map(([key, value]) => (
            <View key={key} style={styles.evidenceChip}>
              <Text style={styles.evidenceText}>
                {key.replace(/_/g, ' ')} {String(value)}
              </Text>
            </View>
          ))}
        </View>
      )}

      {!!item.item_url && (
        <Pressable
          style={styles.linkHit}
          accessibilityRole="link"
          accessibilityLabel="Open ticket"
          onPress={() => void Linking.openURL(item.item_url)}
        >
          <Text style={styles.link}>Open ticket ↗</Text>
        </Pressable>
      )}

      {busy ? (
        <Text style={styles.queued}>
          Queued — will send as soon as the workspace is reachable.
        </Text>
      ) : (
        item.open && (
          <View style={styles.actions}>
            {/* With nothing staged there is nothing to approve — an item in
                needs_rescoping, say. Dimming the inked fill left the ONE
                unavailable action as the heaviest thing on the card; dropping
                the fill entirely puts it behind the two that still work. */}
            <Pressable
              style={[styles.btn,
                      item.pending_actions.length > 0 ? styles.btnPrimary : styles.btnDisabled]}
              disabled={item.pending_actions.length === 0}
              accessibilityRole="button"
              accessibilityLabel="Approve"
              accessibilityState={{ disabled: item.pending_actions.length === 0 }}
              onPress={() => onDecide(item, 'approve')}
            >
              <Text
                style={
                  item.pending_actions.length > 0 ? styles.btnPrimaryText : styles.btnText
                }
              >
                {item.pending_actions.length <= 1
                  ? 'Approve'
                  : `Approve ${item.pending_actions.length}`}
              </Text>
            </Pressable>
            <Pressable
              style={styles.btn}
              accessibilityRole="button"
              accessibilityLabel="Reject"
              onPress={() => onDecide(item, 'reject')}
            >
              <Text style={styles.btnText}>Reject</Text>
            </Pressable>
            <Pressable
              style={styles.btn}
              accessibilityRole="button"
              accessibilityLabel="Send back"
              onPress={() => onDecide(item, 'send-back')}
            >
              <Text style={styles.btnText}>Send back</Text>
            </Pressable>
          </View>
        )
      )}
    </Card>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: colors.bg },
  list: { padding: space.md, gap: space.sm },
  stale: { opacity: 0.6 },
  chips: {
    flexDirection: 'row',
    flexWrap: 'wrap',
    gap: space.xs,
    paddingHorizontal: space.md,
    paddingBottom: space.sm,
  },
  chip: {
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.pill,
    paddingHorizontal: space.md,
    // minHeight alone pins the label to the top of the taller box — centring
    // is half the fix, not a flourish.
    minHeight: 44,
    justifyContent: 'center',
  },
  chipActive: { backgroundColor: colors.accent, borderColor: colors.accent },
  chipText: { color: colors.textMuted, fontSize: font.size.sm },
  chipTextActive: { color: colors.bg },
  groupHead: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.xs,
    marginTop: space.md,
  },
  rule: { width: 3, height: 14, borderRadius: 2 },
  groupLabel: {
    color: colors.textMuted,
    fontSize: font.size.xs,
    textTransform: 'uppercase',
    letterSpacing: 0.6,
  },
  groupCount: { color: colors.textMuted, fontSize: font.size.xs },
  standing: {
    marginHorizontal: space.md,
    marginBottom: space.sm,
    padding: space.sm,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.sm,
    backgroundColor: colors.surface2,
    gap: 2,
  },
  standingHead: { flexDirection: 'row', alignItems: 'center', gap: space.xs },
  standingDot: { width: 8, height: 8, borderRadius: 4 },
  standingLabel: {
    fontSize: font.size.xs,
    textTransform: 'uppercase',
    letterSpacing: 0.6,
  },
  standingCount: { color: colors.warning, fontSize: font.size.xs },
  standingDetail: { color: colors.text, fontSize: font.size.sm },
  focusNote: {
    color: colors.textMuted,
    fontSize: font.size.sm,
    paddingHorizontal: space.md,
    paddingBottom: space.sm,
  },
  tabs: {
    flexDirection: 'row',
    marginHorizontal: space.md,
    marginBottom: space.sm,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.sm,
    overflow: 'hidden',
  },
  // Three equal segments, each a full 44pt target.
  tab: {
    flex: 1,
    minHeight: 44,
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
    gap: space.xs,
    paddingHorizontal: space.xs,
  },
  tabActive: { backgroundColor: colors.surface2 },
  tabText: { color: colors.textMuted, fontSize: font.size.sm },
  tabTextActive: { color: colors.text, fontWeight: '600' },
  tabCount: { color: colors.textMuted, fontSize: font.size.xs },
  tabCountHot: { color: colors.warning, fontWeight: '700' },
  tabDot: { width: 6, height: 6, borderRadius: 3, backgroundColor: colors.accent },
  // The whole summary of a Working or Done row is the tap target.
  rowHit: { minHeight: 44 },
  rowTitle: { color: colors.text, fontSize: font.size.md, marginTop: 2 },
  rowFoot: { flexDirection: 'row', alignItems: 'center', gap: space.xs, marginTop: space.xs },
  rowStatus: { flex: 1, color: colors.textMuted, fontSize: font.size.sm },
  doneKey: { flexDirection: 'row', alignItems: 'center', gap: space.xs },
  unreadDot: { width: 8, height: 8, borderRadius: 4, backgroundColor: colors.accent },
  result: { fontSize: font.size.sm, marginTop: space.xs },
  resultOk: { color: colors.success },
  resultFailed: { color: colors.danger },
  resultMuted: { color: colors.textMuted },
  doneDetail: { marginTop: space.xs },
  cardBusy: { opacity: 0.6 },
  // Where a deep link landed. An arrival cue, cleared after a few seconds —
  // see the timer in the screen above.
  cardFocused: {
    borderColor: colors.accent,
    borderLeftWidth: 3,
    borderLeftColor: colors.accent,
    backgroundColor: colors.surface2,
  },
  cardHead: { flexDirection: 'row', justifyContent: 'space-between' },
  itemKey: { color: colors.textMuted, fontSize: font.size.xs },
  itemAge: { color: colors.textMuted, fontSize: font.size.xs },
  // The one line that says WHAT is being decided; it was the same size as the
  // body copy under it.
  itemTitle: { color: colors.text, fontSize: font.size.lg, marginTop: 2 },
  action: {
    marginTop: space.sm,
    padding: space.sm,
    backgroundColor: colors.surface2,
    borderRadius: radius.sm,
  },
  actionName: { color: colors.textMuted, fontSize: font.size.xs },
  // About to be sent to a customer verbatim, so it has to be readable without
  // squinting — this is the text the decision is actually about.
  actionPreview: { color: colors.text, fontSize: font.size.md, marginTop: 2 },
  // Left one step down: the agent's rationale is supporting material, and at
  // the same size as the preview above it the card loses its hierarchy.
  reason: { color: colors.textMuted, fontSize: font.size.sm, marginTop: space.sm },
  chipsRow: { flexDirection: 'row', flexWrap: 'wrap', gap: space.xs, marginTop: space.sm },
  evidenceChip: {
    borderRadius: radius.pill,
    backgroundColor: colors.surface2,
    paddingHorizontal: space.sm,
    paddingVertical: 2,
  },
  evidenceText: { color: colors.textMuted, fontSize: font.size.xs },
  // The <Text> keeps its own size; the Pressable around it carries the hit
  // area, which was previously zero beyond the glyphs themselves.
  linkHit: { minHeight: 44, justifyContent: 'center', marginTop: space.xs },
  // The Build's branch (#701) — the whole row is the 44pt target.
  wtRow: {
    minHeight: 44,
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.sm,
    marginTop: space.sm,
    paddingHorizontal: space.md,
    paddingVertical: space.xs,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.md,
    backgroundColor: colors.bgElevated,
  },
  wtBranch: { color: colors.text, fontFamily: font.mono, fontSize: font.size.sm },
  wtStat: { color: colors.textMuted, fontSize: font.size.sm, marginTop: 2 },
  link: { color: colors.accent, fontSize: font.size.md },
  queued: { color: colors.textMuted, fontSize: font.size.sm, marginTop: space.sm },
  // Wraps: three 44pt buttons at the larger label size no longer fit one line
  // on a narrow phone, and a row that overflows would push "Send back" off the
  // card entirely.
  actions: { flexDirection: 'row', flexWrap: 'wrap', gap: space.sm, marginTop: space.md },
  btn: {
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.sm,
    paddingHorizontal: space.md,
    paddingVertical: space.sm,
    minHeight: 44,
    justifyContent: 'center',
    alignItems: 'center',
  },
  btnPrimary: { backgroundColor: colors.accent, borderColor: colors.accent },
  btnDisabled: { opacity: 0.45 },
  btnText: { color: colors.text, fontSize: font.size.md },
  btnPrimaryText: { color: colors.accentText, fontSize: font.size.md },
  modalBackdrop: {
    flex: 1,
    backgroundColor: 'rgba(0,0,0,0.6)',
    justifyContent: 'center',
    padding: space.md,
  },
  modalCard: {
    backgroundColor: colors.bgElevated,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: colors.border,
    padding: space.md,
    gap: space.sm,
  },
  modalTitle: { color: colors.text, fontSize: font.size.lg },
  modalBody: { color: colors.textMuted, fontSize: font.size.sm },
  modalInput: {
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.sm,
    color: colors.text,
    padding: space.sm,
    minHeight: 88,
    textAlignVertical: 'top',
  },
  modalActions: {
    flexDirection: 'row',
    gap: space.sm,
    justifyContent: 'flex-end',
  },
});
