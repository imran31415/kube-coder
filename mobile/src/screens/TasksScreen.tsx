/** Task list — the home of the app. Polls every few seconds. */
import { useNavigation } from '@react-navigation/native';
import React, { useCallback, useEffect, useState } from 'react';
import { FlatList, Pressable, RefreshControl, StyleSheet, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { listTasks } from '../api/client';
import { Card, EmptyState, Loading, StatusPill } from '../components/ui';
import type { TasksNav } from '../navigation';
import type { TaskSummary } from '../api/types';
import { colors, font, radius, space } from '../theme';
import { relativeTime } from '../util/format';

export default function TasksScreen() {
  const nav = useNavigation<TasksNav>();
  const [tasks, setTasks] = useState<TaskSummary[] | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const t = await listTasks();
      t.sort((a, b) => (b.created_at ?? 0) - (a.created_at ?? 0));
      setTasks(t);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
      setTasks((prev) => prev ?? []);
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 4000);
    return () => clearInterval(id);
  }, [load]);

  const onRefresh = async () => {
    setRefreshing(true);
    await load();
    setRefreshing(false);
  };

  return (
    <SafeAreaView style={styles.safe} edges={['top']}>
      <View style={styles.header}>
        <View>
          <Text style={styles.title}>Tasks</Text>
          {error ? <Text style={styles.err}>{error}</Text> : null}
        </View>
        <Pressable style={styles.newBtn} onPress={() => nav.navigate('NewTask')}>
          <Text style={styles.newBtnText}>+ New</Text>
        </Pressable>
      </View>

      {tasks === null ? (
        <Loading label="Loading tasks…" />
      ) : tasks.length === 0 ? (
        <EmptyState title="No tasks yet" subtitle="Tap + New to start a Claude task on your workspace." />
      ) : (
        <FlatList
          data={tasks}
          keyExtractor={(t) => t.id}
          contentContainerStyle={styles.list}
          refreshControl={
            <RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={colors.accent} />
          }
          renderItem={({ item }) => (
            <Card style={styles.taskCard} onPress={() => nav.navigate('TaskDetail', { id: item.id })}>
              <View style={styles.taskTop}>
                <StatusPill status={item.status} />
                <Text style={styles.time}>{relativeTime(item.created_at)}</Text>
              </View>
              <Text style={styles.prompt} numberOfLines={2}>
                {item.prompt}
              </Text>
              <View style={styles.metaRow}>
                <Text style={styles.meta}>{item.assistant ?? 'claude'}</Text>
                <Text style={styles.metaDot}>·</Text>
                <Text style={styles.meta} numberOfLines={1}>
                  {item.workdir ?? '/home/dev'}
                </Text>
              </View>
            </Card>
          )}
        />
      )}
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: colors.bg },
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingHorizontal: space.lg,
    paddingTop: space.md,
    paddingBottom: space.sm,
  },
  title: { color: colors.text, fontSize: font.size.xxl, fontWeight: '800' },
  err: { color: colors.danger, fontSize: font.size.xs, marginTop: 2 },
  newBtn: {
    backgroundColor: colors.accent,
    borderRadius: radius.pill,
    paddingHorizontal: space.lg,
    paddingVertical: space.sm,
  },
  newBtnText: { color: colors.accentText, fontWeight: '700', fontSize: font.size.sm },
  list: { padding: space.lg, gap: space.md },
  taskCard: { gap: space.sm },
  taskTop: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' },
  time: { color: colors.textFaint, fontSize: font.size.xs },
  prompt: { color: colors.text, fontSize: font.size.md, fontWeight: '500', lineHeight: 21 },
  metaRow: { flexDirection: 'row', alignItems: 'center', gap: 6 },
  meta: { color: colors.textMuted, fontSize: font.size.xs, flexShrink: 1 },
  metaDot: { color: colors.textFaint },
});
