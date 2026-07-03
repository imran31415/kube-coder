/** Task list — the home of the app. Polls every few seconds. */
import { Ionicons } from '@expo/vector-icons';
import { useNavigation } from '@react-navigation/native';
import { LinearGradient } from 'expo-linear-gradient';
import React, { useCallback, useMemo, useState } from 'react';
import { FlatList, Pressable, RefreshControl, StyleSheet, Text, TextInput, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { listTasks } from '../api/client';
import { Card, EmptyState, ErrorBanner, Loading, ScreenHeader, StatusPill } from '../components/ui';
import { getConfig } from '../store/config';
import type { TasksNav } from '../navigation';
import type { TaskSummary } from '../api/types';
import { colors, font, gradients, radius, space, statusColor } from '../theme';
import { relativeTime } from '../util/format';
import { usePolling } from '../util/usePolling';

function hostLabel(): string {
  const h = getConfig().host;
  if (!h) return 'workspace';
  try {
    return new URL(h).host;
  } catch {
    return h.replace(/^https?:\/\//, '');
  }
}

/** Active = needs attention now; everything else is history. */
const isActive = (t: TaskSummary) => t.status === 'running' || t.status === 'waiting';

type Segment = 'active' | 'done';

export default function TasksScreen() {
  const nav = useNavigation<TasksNav>();
  const [tasks, setTasks] = useState<TaskSummary[] | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Default to Active: the everyday view is "what's running right now" —
  // finished tasks live behind the Done segment instead of cluttering it.
  const [segment, setSegment] = useState<Segment>('active');
  const [query, setQuery] = useState('');

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

  usePolling(load, 4000);

  const onRefresh = async () => {
    setRefreshing(true);
    await load();
    setRefreshing(false);
  };

  const running = tasks?.filter(isActive).length ?? 0;
  const doneCount = (tasks?.length ?? 0) - running;

  const q = query.trim().toLowerCase();
  const visible = useMemo(() => {
    if (!tasks) return null;
    return tasks.filter((t) => {
      if (segment === 'active' ? !isActive(t) : isActive(t)) return false;
      if (!q) return true;
      return `${t.prompt} ${t.assistant ?? ''} ${t.workdir ?? ''}`.toLowerCase().includes(q);
    });
  }, [tasks, segment, q]);

  return (
    <SafeAreaView style={styles.safe} edges={['top']}>
      <ScreenHeader
        brand
        title="kube-coder"
        subtitle={`${hostLabel()}${running ? `  ·  ${running} active` : ''}`}
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

      {error && tasks !== null && tasks.length > 0 ? <ErrorBanner message={error} /> : null}

      {tasks !== null && tasks.length > 0 ? (
        <View style={styles.filters}>
          <View style={styles.searchWrap}>
            <Ionicons name="search" size={16} color={colors.textFaint} />
            <TextInput
              value={query}
              onChangeText={setQuery}
              placeholder="Search tasks…"
              placeholderTextColor={colors.textFaint}
              autoCapitalize="none"
              autoCorrect={false}
              style={styles.search}
            />
            {query ? (
              <Pressable onPress={() => setQuery('')} hitSlop={10}>
                <Ionicons name="close-circle" size={16} color={colors.textFaint} />
              </Pressable>
            ) : null}
          </View>
          <View style={styles.segments} accessibilityRole="tablist">
            {(
              [
                ['active', `Active${running ? ` ${running}` : ''}`],
                ['done', `Done${doneCount ? ` ${doneCount}` : ''}`],
              ] as [Segment, string][]
            ).map(([key, label]) => (
              <Pressable
                key={key}
                onPress={() => setSegment(key)}
                accessibilityRole="tab"
                accessibilityState={{ selected: segment === key }}
                style={[styles.segment, segment === key && styles.segmentOn]}
              >
                <Text style={[styles.segmentText, segment === key && styles.segmentTextOn]}>
                  {label}
                </Text>
              </Pressable>
            ))}
          </View>
        </View>
      ) : null}

      {tasks === null ? (
        <Loading label="Loading tasks…" />
      ) : tasks.length === 0 ? (
        error ? (
          <EmptyState icon="cloud-offline-outline" title="Couldn't reach your workspace" subtitle={error} />
        ) : (
          <EmptyState
            icon="rocket-outline"
            title="No tasks yet"
            subtitle="Tap New to start a Claude task on your workspace — it runs remotely and you can follow along here."
          />
        )
      ) : visible && visible.length === 0 ? (
        q ? (
          <EmptyState icon="search-outline" title="No matches" subtitle={`Nothing in ${segment === 'active' ? 'Active' : 'Done'} matches “${query.trim()}”.`} />
        ) : segment === 'active' ? (
          <EmptyState
            icon="cafe-outline"
            title="Nothing running"
            subtitle={
              doneCount
                ? `All quiet. ${doneCount} finished ${doneCount === 1 ? 'task is' : 'tasks are'} under Done — or tap New to start something.`
                : 'Tap New to start a Claude task on your workspace.'
            }
          />
        ) : (
          <EmptyState icon="checkmark-done-outline" title="No finished tasks" subtitle="Tasks that complete, error, or get killed land here." />
        )
      ) : (
        <FlatList
          data={visible}
          keyExtractor={(t) => t.id}
          contentContainerStyle={styles.list}
          showsVerticalScrollIndicator={false}
          keyboardShouldPersistTaps="handled"
          keyboardDismissMode="on-drag"
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
  filters: { paddingHorizontal: space.lg, paddingBottom: space.md, gap: space.sm },
  searchWrap: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.sm,
    paddingHorizontal: space.md,
    height: 40,
    backgroundColor: colors.bgElevated,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: colors.border,
  },
  search: { flex: 1, color: colors.text, fontSize: font.size.md, padding: 0 },
  segments: {
    flexDirection: 'row',
    backgroundColor: colors.bgElevated,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: colors.border,
    padding: 3,
    gap: 3,
  },
  segment: {
    flex: 1,
    alignItems: 'center',
    paddingVertical: 7,
    borderRadius: radius.sm,
  },
  segmentOn: { backgroundColor: colors.card, borderWidth: 1, borderColor: colors.borderStrong },
  segmentText: { color: colors.textMuted, fontSize: font.size.sm, fontWeight: '600' },
  segmentTextOn: { color: colors.text, fontWeight: '700' },
  taskCard: { gap: space.md },
  taskTop: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' },
  time: { color: colors.textFaint, fontSize: font.size.xs },
  prompt: { color: colors.text, fontSize: font.size.md, fontWeight: '600', lineHeight: 22 },
  metaRow: { flexDirection: 'row', alignItems: 'center', gap: 5 },
  meta: { color: colors.textMuted, fontSize: font.size.xs, flexShrink: 1 },
  metaDot: { color: colors.textFaint, marginHorizontal: 2 },
});
