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
import React, { useCallback, useState } from 'react';
import {
  FlatList,
  Linking,
  Modal,
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
  const [queued, setQueued] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [sendingBack, setSendingBack] = useState<BoardReviewItem | null>(null);
  const [note, setNote] = useState('');

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

      setQueued(pendingItemIds(await readQueue(AsyncStorage)));
      setGroups(active ? await getBoardReview(active) : []);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
      setGroups((prev) => prev ?? []);
    }
  }, [boardId, boards, send]);

  usePolling(load, 15000);

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

  const rows: Row[] | null = groups
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
    : null;

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

      {rows === null ? (
        <Loading />
      ) : rows.length === 0 ? (
        <EmptyState
          title="Nothing to review"
          subtitle="When an agent works a board item in propose mode, its proposed writes land here for you to approve."
        />
      ) : (
        <FlatList
          data={rows}
          keyExtractor={(r) => r.key}
          contentContainerStyle={styles.list}
          refreshControl={
            <RefreshControl refreshing={refreshing} onRefresh={onRefresh} />
          }
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
        </View>
      </Modal>
    </SafeAreaView>
  );
}

function ReviewCard({
  item,
  busy,
  onDecide,
}: {
  item: BoardReviewItem;
  busy: boolean;
  onDecide: (item: BoardReviewItem, decision: Decision) => void;
}) {
  const evidence = Object.entries(item.evidence ?? {});
  return (
    <Card style={busy ? styles.cardBusy : undefined}>
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
        <Pressable onPress={() => void Linking.openURL(item.item_url)}>
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
            <Pressable
              style={[styles.btn, styles.btnPrimary]}
              disabled={item.pending_actions.length === 0}
              onPress={() => onDecide(item, 'approve')}
            >
              <Text style={styles.btnPrimaryText}>
                {item.pending_actions.length <= 1
                  ? 'Approve'
                  : `Approve ${item.pending_actions.length}`}
              </Text>
            </Pressable>
            <Pressable
              style={styles.btn}
              onPress={() => onDecide(item, 'reject')}
            >
              <Text style={styles.btnText}>Reject</Text>
            </Pressable>
            <Pressable
              style={styles.btn}
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
    paddingHorizontal: space.sm,
    paddingVertical: 4,
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
  cardBusy: { opacity: 0.6 },
  cardHead: { flexDirection: 'row', justifyContent: 'space-between' },
  itemKey: { color: colors.textMuted, fontSize: font.size.xs },
  itemAge: { color: colors.textMuted, fontSize: font.size.xs },
  itemTitle: { color: colors.text, fontSize: font.size.md, marginTop: 2 },
  action: {
    marginTop: space.sm,
    padding: space.sm,
    backgroundColor: colors.surface2,
    borderRadius: radius.sm,
  },
  actionName: { color: colors.textMuted, fontSize: font.size.xs },
  actionPreview: { color: colors.text, fontSize: font.size.sm, marginTop: 2 },
  reason: { color: colors.textMuted, fontSize: font.size.sm, marginTop: space.sm },
  chipsRow: { flexDirection: 'row', flexWrap: 'wrap', gap: space.xs, marginTop: space.sm },
  evidenceChip: {
    borderRadius: radius.pill,
    backgroundColor: colors.surface2,
    paddingHorizontal: space.sm,
    paddingVertical: 2,
  },
  evidenceText: { color: colors.textMuted, fontSize: font.size.xs },
  link: { color: colors.accent, fontSize: font.size.sm, marginTop: space.sm },
  queued: { color: colors.textMuted, fontSize: font.size.xs, marginTop: space.sm },
  actions: { flexDirection: 'row', gap: space.xs, marginTop: space.md },
  btn: {
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.sm,
    paddingHorizontal: space.md,
    paddingVertical: space.xs,
  },
  btnPrimary: { backgroundColor: colors.accent, borderColor: colors.accent },
  btnDisabled: { opacity: 0.4 },
  btnText: { color: colors.text, fontSize: font.size.sm },
  btnPrimaryText: { color: colors.accentText, fontSize: font.size.sm },
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
  modalTitle: { color: colors.text, fontSize: font.size.md },
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
    gap: space.xs,
    justifyContent: 'flex-end',
  },
});
