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
import { getBoardReview, listBoards, decideBoardItem } from '../api/client';
import { Card, EmptyState, ErrorBanner, Loading, ScreenHeader } from '../components/ui';
import { colors, font, radius, space } from '../theme';
import { relativeTime } from '../util/format';
import { usePolling } from '../util/usePolling';
import { clearBoardFocus, useBoardFocus } from '../store/boardFocus';
import {
  drain,
  enqueue,
  pendingItemIds,
  readQueue,
  type Decision,
  type QueuedApproval,
} from '../util/approvalQueue';
import type { BoardReviewGroup, BoardReviewItem, BoardSummary } from '../api/types';

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

export default function BoardScreen() {
  const [boards, setBoards] = useState<BoardSummary[] | null>(null);
  const [boardId, setBoardId] = useState<string | null>(null);
  const [groups, setGroups] = useState<BoardReviewGroup[] | null>(null);
  // Which board `groups` actually belongs to. While a switch is in flight the
  // queue on screen is still the PREVIOUS board's, and a deep link that
  // searched it would decide its item was gone.
  const [groupsBoard, setGroupsBoard] = useState<string | null>(null);
  const [highlight, setHighlight] = useState<string | null>(null);
  // Kept apart from `notice`, which carries queue errors and is meant to
  // persist. This one answers "I tapped a notification and nothing happened",
  // so it is a passing remark and clears itself.
  const [focusNote, setFocusNote] = useState<string | null>(null);
  const [queued, setQueued] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [sendingBack, setSendingBack] = useState<BoardReviewItem | null>(null);
  const [note, setNote] = useState('');
  const listRef = useRef<FlatList<Row>>(null);
  /** The board the last fetch was for — see the board-switch effect below. */
  const loadedFor = useRef<string | null>(null);

  /** Send one queued decision. Returns the status so the queue can decide
   *  whether it is worth retrying. */
  const send = useCallback(async (entry: QueuedApproval) => {
    const res = await decideBoardItem(
      entry.board_id,
      entry.item_id,
      entry.decision,
      {
        approval_id: entry.approval_id,
        ...(entry.decision === 'approve'
          ? { content_hash: entry.content_hash }
          : {}),
        // Send-back's text is a NOTE — an instruction to the agent, which the
        // server requires. Reject's is a reason and stays optional. Same
        // stored field, different meaning, so the wire name differs.
        ...(entry.reason
          ? entry.decision === 'send-back'
            ? { note: entry.reason }
            : { reason: entry.reason }
          : {}),
      },
    );
    if (res.error) setNotice(res.error);
    return { status: res.status };
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
      const active = boardId ?? list[0]?.id ?? null;
      if (!boardId && active) setBoardId(active);
      // Claim the board before awaiting, so the switch effect below sees this
      // pass already covers it and does not fire a duplicate fetch for the
      // board this very call is resolving.
      loadedFor.current = active;

      setQueued(pendingItemIds(await readQueue(AsyncStorage)));
      setGroups(active ? await getBoardReview(active) : []);
      setGroupsBoard(active);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
      setGroups((prev) => prev ?? []);
    }
  }, [boardId, boards, send]);

  usePolling(load, 15000);

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
      return;
    }

    if (rows === null || groupsBoard !== focus.boardId) return;

    const index = rows.findIndex(
      (r) => r.type === 'item' && r.item.item_id === focus.itemId,
    );
    if (index < 0) {
      // Decided by someone else, dismissed, or never in this queue. One
      // sentence beats a screen waiting on a card that is not coming.
      setFocusNote('That item is no longer awaiting a decision.');
      clearBoardFocus(focus.seq);
      return;
    }

    // Highlight FIRST. Rows are variable-height with no getItemLayout, so the
    // scroll can miss; the mark is what actually lands the user on the card.
    setHighlight(focus.itemId);
    listRef.current?.scrollToIndex({ index, animated: true, viewPosition: 0.1 });
    clearBoardFocus(focus.seq);
  }, [focus, boards, boardId, rows, groupsBoard]);

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

  return (
    <SafeAreaView style={styles.safe} edges={['top', 'left', 'right']}>
      <ScreenHeader
        title="Board"
        subtitle={
          openCount > 0
            ? `${openCount} awaiting your decision`
            : 'Nothing waiting for you'
        }
      />

      {boards && boards.length > 1 && (
        <View style={styles.chips}>
          {boards.map((b) => (
            <Pressable
              key={b.id}
              onPress={() => {
                setBoardId(b.id);
                setGroups(null);
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

      {error && <ErrorBanner message={error} />}
      {notice && <ErrorBanner message={notice} />}
      {focusNote && (
        <Text style={styles.focusNote} role="status">
          {focusNote}
        </Text>
      )}

      {rows === null ? (
        <Loading />
      ) : rows.length === 0 ? (
        <EmptyState
          title="Nothing to review"
          subtitle="When an agent works a board item in propose mode, its proposed writes land here for you to approve."
        />
      ) : (
        <FlatList
          ref={listRef}
          data={rows}
          keyExtractor={(r) => r.key}
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

function ReviewCard({
  item,
  busy,
  focused,
  onDecide,
}: {
  item: BoardReviewItem;
  busy: boolean;
  focused: boolean;
  onDecide: (item: BoardReviewItem, decision: Decision) => void;
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
  focusNote: {
    color: colors.textMuted,
    fontSize: font.size.sm,
    paddingHorizontal: space.md,
    paddingBottom: space.sm,
  },
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
