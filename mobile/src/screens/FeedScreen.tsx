/**
 * Feed (#471) — native parity for the web /feed page. One calm, day-grouped
 * stream of briefings, news, activity, and decisions over GET /api/feed. Kind
 * rules, filter chips, typed-ref actions, and the "Discuss with CTO" handoff.
 * Polled like the other list screens (no SSE on mobile).
 */
import { useNavigation } from '@react-navigation/native';
import React, { useCallback, useMemo, useState } from 'react';
import { FlatList, Linking, Pressable, RefreshControl, StyleSheet, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { listFeed, markFeedRead, dismissFeedItem } from '../api/client';
import { Card, EmptyState, ErrorBanner, Loading, ScreenHeader } from '../components/ui';
import { Markdown } from '../components/Markdown';
import { Ionicons } from '@expo/vector-icons';
import type { FeedItem, FeedKind, FeedLink } from '../api/types';
import { colors, font, radius, space } from '../theme';
import { relativeTime } from '../util/format';
import { usePolling } from '../util/usePolling';
import { groupByDay, resolveFeedRef, feedSourceLabel, discussPrefix } from '../util/feed';

type Nav = { navigate: (tab: string, opts?: object) => void };

const KIND_FILTERS: { value: FeedKind | 'all'; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'briefing', label: 'Briefings' },
  { value: 'news', label: 'News' },
  { value: 'activity', label: 'Activity' },
  { value: 'decision', label: 'Decisions' },
];

const KIND_RULE: Record<FeedKind, string> = {
  briefing: colors.accent,
  news: colors.info,
  activity: colors.success,
  decision: colors.border,
};

type Row = { type: 'header'; key: string; label: string } | { type: 'item'; key: string; item: FeedItem };

export default function FeedScreen() {
  const nav = useNavigation<Nav>();
  const [items, setItems] = useState<FeedItem[] | null>(null);
  const [filter, setFilter] = useState<FeedKind | 'all'>('all');
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async () => {
    try {
      const next = await listFeed(filter === 'all' ? {} : { kinds: [filter] });
      setItems(next);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
      setItems((prev) => prev ?? []);
    }
  }, [filter]);

  usePolling(load, 15000);

  const onRefresh = async () => {
    setRefreshing(true);
    await load();
    setRefreshing(false);
  };

  const rows = useMemo<Row[] | null>(() => {
    if (!items) return null;
    const out: Row[] = [];
    for (const g of groupByDay(items)) {
      out.push({ type: 'header', key: `h:${g.key}`, label: g.label });
      for (const it of g.items) out.push({ type: 'item', key: it.id, item: it });
    }
    return out;
  }, [items]);

  const openLink = useCallback(
    (item: FeedItem, link: FeedLink) => {
      void markFeedRead(item.id).catch(() => {});
      setItems((prev) => prev?.map((i) => (i.id === item.id ? { ...i, read: true } : i)) ?? prev);
      const target = resolveFeedRef(link);
      if (target.kind === 'task') nav.navigate('Tasks', { screen: 'TaskDetail', params: { id: target.id }, initial: false });
      else if (target.kind === 'thread') nav.navigate('Cto', {});
      else if (target.kind === 'memory') nav.navigate('Memory', {});
      else if (target.kind === 'external') void Linking.openURL(target.url).catch(() => {});
    },
    [nav],
  );

  const discuss = useCallback(
    (item: FeedItem) => {
      void markFeedRead(item.id).catch(() => {});
      nav.navigate('Cto', { discussProject: item.project_id, discussText: discussPrefix(item) });
    },
    [nav],
  );

  const dismiss = useCallback(async (item: FeedItem) => {
    setItems((prev) => prev?.filter((i) => i.id !== item.id) ?? prev);
    try {
      await dismissFeedItem(item.id);
    } catch {
      void load();
    }
  }, [load]);

  const renderRow = useCallback(
    ({ item: row }: { item: Row }) => {
      if (row.type === 'header') {
        return <Text style={styles.dayLabel}>{row.label}</Text>;
      }
      const item = row.item;
      return (
        <Card style={styles.itemCard}>
          <View style={[styles.rule, { backgroundColor: item.waiting ? colors.warning : KIND_RULE[item.kind] }]} />
          <View style={styles.itemBody}>
            <View style={styles.metaRow}>
              {!item.read && <View style={styles.unreadDot} />}
              <Text style={styles.meta} numberOfLines={1}>
                {[item.kind, feedSourceLabel(item.source), item.project_id].filter(Boolean).join(' · ').toUpperCase()}
              </Text>
              <Text style={styles.metaTime}>{item.ts ? relativeTime(item.ts) : ''}</Text>
            </View>
            <Text style={styles.title}>{item.title}</Text>
            {!!item.body_md && <Markdown text={item.body_md} />}
            <View style={styles.actions}>
              {item.links.map((l, i) => (
                <Pressable key={l.ref || l.href || String(i)} style={styles.chip} onPress={() => openLink(item, l)}>
                  <Text style={styles.chipText}>{l.label}{l.href ? ' ↗' : ''}</Text>
                </Pressable>
              ))}
              <Pressable style={[styles.chip, styles.chipPrimary]} onPress={() => discuss(item)}>
                <Text style={styles.chipText}>Discuss with CTO</Text>
              </Pressable>
              <Pressable style={styles.dismiss} onPress={() => void dismiss(item)} accessibilityLabel="Dismiss">
                <Ionicons name="close" size={15} color={colors.textFaint} />
              </Pressable>
            </View>
          </View>
        </Card>
      );
    },
    [openLink, discuss, dismiss],
  );

  return (
    <SafeAreaView style={styles.container} edges={['top']}>
      <ScreenHeader title="Feed" subtitle="What changed · what matters" />
      <View style={styles.filters}>
        {KIND_FILTERS.map((f) => (
          <Pressable
            key={f.value}
            onPress={() => setFilter(f.value)}
            style={[styles.filter, filter === f.value && styles.filterOn]}
          >
            <Text style={[styles.filterText, filter === f.value && styles.filterTextOn]}>{f.label}</Text>
          </Pressable>
        ))}
      </View>
      {error && items && items.length > 0 ? <ErrorBanner message={error} /> : null}
      {!rows ? (
        <Loading label="Loading feed…" />
      ) : rows.length === 0 ? (
        <EmptyState
          icon="newspaper-outline"
          title="Nothing yet"
          subtitle="Your CTO and workspace will post here as things happen."
        />
      ) : (
        <FlatList
          data={rows}
          keyExtractor={(r) => r.key}
          renderItem={renderRow}
          contentContainerStyle={styles.list}
          refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={colors.accent} />}
        />
      )}
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: colors.bg },
  filters: { flexDirection: 'row', flexWrap: 'wrap', gap: 6, paddingHorizontal: space.lg, paddingBottom: space.sm },
  filter: { paddingVertical: 4, paddingHorizontal: 10, borderRadius: radius.pill, borderWidth: 1, borderColor: colors.border, backgroundColor: colors.card },
  filterOn: { borderColor: colors.accent, backgroundColor: colors.accentSoft },
  filterText: { color: colors.textMuted, fontSize: font.size.xs },
  filterTextOn: { color: colors.text },
  list: { paddingHorizontal: space.lg, paddingBottom: space.xxl },
  dayLabel: { color: colors.textFaint, fontSize: font.size.xs, fontWeight: '600', letterSpacing: 1, textTransform: 'uppercase', marginTop: space.md, marginBottom: space.xs },
  itemCard: { flexDirection: 'row', padding: 0, overflow: 'hidden', marginBottom: space.sm },
  rule: { width: 3 },
  itemBody: { flex: 1, padding: space.md },
  metaRow: { flexDirection: 'row', alignItems: 'center', gap: 6 },
  unreadDot: { width: 6, height: 6, borderRadius: 3, backgroundColor: colors.accent },
  meta: { flex: 1, color: colors.textFaint, fontSize: 10, fontWeight: '600', letterSpacing: 0.5 },
  metaTime: { color: colors.textFaint, fontSize: 10 },
  title: { color: colors.text, fontSize: font.size.sm, fontWeight: '600', marginTop: 4, lineHeight: 19 },
  actions: { flexDirection: 'row', flexWrap: 'wrap', alignItems: 'center', gap: 6, marginTop: space.md },
  chip: { paddingVertical: 5, paddingHorizontal: 10, borderRadius: radius.md, borderWidth: 1, borderColor: colors.borderStrong, backgroundColor: colors.card },
  chipPrimary: { borderColor: colors.accent },
  chipText: { color: colors.text, fontSize: font.size.xs },
  dismiss: { marginLeft: 'auto', padding: 4 },
});
