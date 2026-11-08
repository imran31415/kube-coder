import { useCallback, useEffect, useRef, useState } from 'react';
import { Linking, Pressable, StyleSheet, Text, View } from 'react-native';
import { dashboardUrl, listBoardRequests } from '../api/client';
import type { BoardHandoffRequest } from '../api/types';
import { colors } from '../theme';
import { usePolling } from '../util/usePolling';

/** Same requests in the same states: nothing on screen would change. */
function sameRequests(a: BoardHandoffRequest[], b: BoardHandoffRequest[]): boolean {
  if (a.length !== b.length) return false;
  return a.every((r, i) => r.id === b[i].id && r.state === b[i].state
    && (r.error?.error ?? '') === (b[i].error?.error ?? ''));
}

/** Mobile bearer identity can observe requests, but cannot approve them.
 *
 *  Polls through `usePolling`, so it stops while the chat is out of focus or
 *  the window is hidden and refreshes on `boards.request` events when the
 *  desktop app holds the event stream (#833). State is set only when it
 *  changed, and never through a bailing-out updater: React keeps a bailed-out
 *  update (and what its closure captured) queued until the next render, which
 *  is how the chat transcript leaked in #839. */
export function BoardRequestCards({ threadId, onChange }: { threadId: string; onChange?: () => void }) {
  const [records, setRecords] = useState<BoardHandoffRequest[]>([]);
  const [error, setError] = useState('');
  const recordsRef = useRef<BoardHandoffRequest[]>([]);
  const errorRef = useRef('');
  const loading = useRef(false);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; };
  }, []);

  const showError = (message: string) => {
    if (errorRef.current === message) return;
    errorRef.current = message;
    setError(message);
  };

  const refresh = useCallback(async () => {
    if (loading.current) return;
    loading.current = true;
    try {
      const response = await listBoardRequests(threadId);
      if (!alive.current) return;
      // A server without this endpoint's shape (older or partly upgraded)
      // must not take the chat transcript down with it.
      const next = Array.isArray(response?.requests) ? response.requests : [];
      if (!sameRequests(recordsRef.current, next)) {
        recordsRef.current = next;
        setRecords(next);
      }
      showError('');
    } catch {
      if (alive.current) showError('Board status is unavailable. Open the dashboard to check.');
    } finally {
      loading.current = false;
    }
  }, [threadId]);

  usePolling(refresh, 15000, ['boards.request']);

  // Cards arrive between transcript updates, so tell the chat to follow them.
  useEffect(() => { onChange?.(); }, [records, error]);

  return <View>
    {error ? <Text accessibilityRole="alert" style={styles.message}>{error}</Text> : null}
    {/* Oldest first: the list ends the transcript, newest beside the newest message. */}
    {records.slice(0, 10).reverse().map(record => <View key={record.id} style={styles.card}>
      <Text style={styles.title}>{record.state === 'pending' ? 'Board run needs your approval' : `Board request: ${record.state}`}</Text>
      <Text style={styles.message}>{record.scope.board_name} · {record.scope.repository.replace('https://', '')}</Text>
      {record.error?.error ? <Text style={styles.message}>{record.error.error}</Text> : null}
      <Pressable accessibilityRole="link" style={styles.link} onPress={() => {
        void Linking.openURL(dashboardUrl(`/hypervisor/${encodeURIComponent(threadId)}`))
          .catch(() => showError('Could not open the browser. Open your workspace dashboard manually.'));
      }}><Text style={styles.title}>{record.state === 'pending' ? 'Open dashboard to approve' : 'Open dashboard for progress'}</Text></Pressable>
    </View>)}
  </View>;
}

const styles = StyleSheet.create({
  card: { borderColor: colors.border, borderWidth: 1, borderRadius: 12, padding: 14, marginVertical: 8 },
  title: { color: colors.text, fontSize: 14, fontWeight: '600' },
  message: { color: colors.textMuted, fontSize: 13, marginVertical: 6 },
  link: { minHeight: 44, justifyContent: 'center' },
});
