/** Task list — the home of the app. Polls every few seconds. */
import { Ionicons } from '@expo/vector-icons';
import { useNavigation } from '@react-navigation/native';
import { LinearGradient } from 'expo-linear-gradient';
import React, { useCallback, useEffect, useState } from 'react';
import { FlatList, Pressable, RefreshControl, StyleSheet, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { listTasks } from '../api/client';
import { Card, EmptyState, Loading, ScreenHeader, StatusPill } from '../components/ui';
import { getConfig } from '../store/config';
import type { TasksNav } from '../navigation';
import type { TaskSummary } from '../api/types';
import { colors, font, gradients, radius, space, statusColor } from '../theme';
import { relativeTime } from '../util/format';

function hostLabel(): string {
  const h = getConfig().host;
  if (!h) return 'workspace';
  try {
    return new URL(h).host;
  } catch {
    return h.replace(/^https?:\/\//, '');
  }
}

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

  const running = tasks?.filter((t) => t.status === 'running' || t.status === 'waiting').length ?? 0;

  return (
    <SafeAreaView style={styles.safe} edges={['top']}>
      <ScreenHeader
        brand
        title="kube-coder"
        subtitle={error ? error : `${hostLabel()}${running ? `  ·  ${running} active` : ''}`}
        right={
          <Pressable onPress={() => nav.navigate('NewTask')}>
            <LinearGradient
              colors={gradients.primary}
              start={{ x: 0, y: 0 }}
              end={{ x: 1, y: 1 }}
              style={styles.newBtn}
            >
              <Ionicons name="add" size={18} color={colors.accentText} />
              <Text style={styles.newBtnText}>New</Text>
            </LinearGradient>
          </Pressable>
        }
      />

      {tasks === null ? (
        <Loading label="Loading tasks…" />
      ) : tasks.length === 0 ? (
        <EmptyState
          icon="rocket-outline"
          title="No tasks yet"
          subtitle="Tap New to start a Claude task on your workspace — it runs remotely and you can follow along here."
        />
      ) : (
        <FlatList
          data={tasks}
          keyExtractor={(t) => t.id}
          contentContainerStyle={styles.list}
          showsVerticalScrollIndicator={false}
          refreshControl={
            <RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={colors.accent} />
          }
          renderItem={({ item }) => {
            const active = item.status === 'running' || item.status === 'waiting';
            return (
              <Card
                style={styles.taskCard}
                accent={active ? statusColor(item.status) : undefined}
                onPress={() => nav.navigate('TaskDetail', { id: item.id })}
              >
                <View style={styles.taskTop}>
                  <StatusPill status={item.status} />
                  <Text style={styles.time}>{relativeTime(item.created_at)}</Text>
                </View>
                <Text style={styles.prompt} numberOfLines={2}>
                  {item.prompt}
                </Text>
                <View style={styles.metaRow}>
                  <Ionicons name="hardware-chip-outline" size={13} color={colors.textFaint} />
                  <Text style={styles.meta}>{item.assistant ?? 'claude'}</Text>
                  <Text style={styles.metaDot}>·</Text>
                  <Ionicons name="folder-outline" size={13} color={colors.textFaint} />
                  <Text style={styles.meta} numberOfLines={1}>
                    {item.workdir ?? '/home/dev'}
                  </Text>
                </View>
              </Card>
            );
          }}
        />
      )}
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: colors.bg },
  newBtn: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 4,
    borderRadius: radius.pill,
    paddingLeft: space.md,
    paddingRight: space.lg,
    paddingVertical: 9,
  },
  newBtnText: { color: colors.accentText, fontWeight: '700', fontSize: font.size.sm },
  list: { paddingHorizontal: space.lg, paddingBottom: space.xl, gap: space.md },
  taskCard: { gap: space.md },
  taskTop: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' },
  time: { color: colors.textFaint, fontSize: font.size.xs },
  prompt: { color: colors.text, fontSize: font.size.md, fontWeight: '600', lineHeight: 22 },
  metaRow: { flexDirection: 'row', alignItems: 'center', gap: 5 },
  meta: { color: colors.textMuted, fontSize: font.size.xs, flexShrink: 1 },
  metaDot: { color: colors.textFaint, marginHorizontal: 2 },
});
