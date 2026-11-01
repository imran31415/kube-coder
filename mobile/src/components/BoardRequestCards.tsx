import { useEffect, useState } from 'react';
import { Linking, Pressable, StyleSheet, Text, View } from 'react-native';
import { dashboardUrl, listBoardRequests } from '../api/client';
import type { BoardHandoffRequest } from '../api/types';
import { colors } from '../theme';

/** Mobile bearer identity can observe requests, but cannot approve them. */
export function BoardRequestCards({ threadId }: { threadId: string }) {
  const [records, setRecords] = useState<BoardHandoffRequest[]>([]);
  const [error, setError] = useState('');
  useEffect(() => {
    let alive = true;
    let loading = false;
    setRecords([]);
    async function refresh() {
      if (loading) return;
      loading = true;
      try {
        const response = await listBoardRequests(threadId);
        if (alive) { setRecords(response.requests); setError(''); }
      } catch { if (alive) setError('Board status is unavailable. Open the dashboard to check.'); }
      finally { loading = false; }
    }
    void refresh();
    const timer = setInterval(() => void refresh(), 15000);
    return () => { alive = false; clearInterval(timer); };
  }, [threadId]);
  return <View>
    {error ? <Text accessibilityRole="alert" style={styles.message}>{error}</Text> : null}
    {records.slice(0, 10).map(record => <View key={record.id} style={styles.card}>
      <Text style={styles.title}>{record.state === 'pending' ? 'Board run needs your approval' : `Board request: ${record.state}`}</Text>
      <Text style={styles.message}>{record.scope.board_name} · {record.scope.repository.replace('https://', '')}</Text>
      {record.error?.error ? <Text style={styles.message}>{record.error.error}</Text> : null}
      <Pressable accessibilityRole="link" style={styles.link} onPress={() => {
        void Linking.openURL(dashboardUrl(`/hypervisor/${encodeURIComponent(threadId)}`))
          .catch(() => setError('Could not open the browser. Open your workspace dashboard manually.'));
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
