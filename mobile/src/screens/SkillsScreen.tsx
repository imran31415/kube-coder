/** Multi-harness skills browser (read-only) — searchable, expandable rows.
 *  Shows every SKILL.md-defined capability discovered across the workspace's
 *  agent harnesses (Claude Code, OpenCode, …) with per-system badges. */
import { Ionicons } from '@expo/vector-icons';
import React, { useCallback, useMemo, useState } from 'react';
import { FlatList, Pressable, RefreshControl, StyleSheet, Text, TextInput, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { useFocusEffect } from '@react-navigation/native';
import { listSkills } from '../api/client';
import { EmptyState, ErrorBanner, Loading, ScreenHeader } from '../components/ui';
import type { SkillRecord } from '../api/types';
import { colors, font, radius, space } from '../theme';

const rowKey = (s: SkillRecord) => `${s.name}:${s.systems.join(',')}`;

export default function SkillsScreen() {
  const [items, setItems] = useState<SkillRecord[] | null>(null);
  const [query, setQuery] = useState('');
  const [openKey, setOpenKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async () => {
    try {
      setItems(await listSkills());
      setError(null);
    } catch (e) {
      // A failed load must not masquerade as "no skills".
      setError((e as Error).message);
      setItems((prev) => prev ?? []);
    }
  }, []);

  // Skills change when files change on disk; refetch on tab focus.
  useFocusEffect(
    useCallback(() => {
      void load();
    }, [load]),
  );

  const onRefresh = async () => {
    setRefreshing(true);
    await load();
    setRefreshing(false);
  };

  const filtered = useMemo(() => {
    if (!items) return null;
    const q = query.trim().toLowerCase();
    if (!q) return items;
    return items.filter((s) =>
      `${s.name} ${s.description} ${s.systems.join(' ')} ${s.scope}`.toLowerCase().includes(q),
    );
  }, [items, query]);

  return (
    <SafeAreaView style={styles.safe} edges={['top']}>
      <ScreenHeader title="Skills" subtitle="Agent capabilities across every harness" />

      {items !== null && items.length > 0 ? (
        <View style={styles.searchWrap}>
          <Ionicons name="search" size={16} color={colors.textFaint} />
          <TextInput
            value={query}
            onChangeText={setQuery}
            placeholder="Search skills…"
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
      ) : null}

      {error && items !== null && items.length > 0 ? <ErrorBanner message={error} /> : null}

      {filtered === null ? (
        <Loading label="Loading skills…" />
      ) : items && items.length === 0 ? (
        error ? (
          <EmptyState icon="cloud-offline-outline" title="Couldn't load skills" subtitle={error} />
        ) : (
          <EmptyState
            icon="sparkles-outline"
            title="No skills found"
            subtitle="Skills are SKILL.md folders under .claude/skills/ and other harness directories."
          />
        )
      ) : filtered.length === 0 ? (
        <EmptyState icon="search-outline" title="No matches" subtitle={`Nothing matches “${query.trim()}”.`} />
      ) : (
        <FlatList
          data={filtered}
          keyExtractor={rowKey}
          contentContainerStyle={styles.list}
          keyboardShouldPersistTaps="handled"
          keyboardDismissMode="on-drag"
          showsVerticalScrollIndicator={false}
          refreshControl={
            <RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={colors.accent} />
          }
          ListHeaderComponent={
            <Text style={styles.count}>
              {filtered.length} {filtered.length === 1 ? 'skill' : 'skills'}
            </Text>
          }
          renderItem={({ item }) => {
            const open = openKey === rowKey(item);
            return (
              <Pressable style={styles.row} onPress={() => setOpenKey(open ? null : rowKey(item))}>
                <View style={styles.rowTop}>
                  <Text style={styles.name} numberOfLines={1}>
                    /{item.name}
                    <Text style={styles.scope}>  {item.scope}</Text>
                  </Text>
                  <Ionicons
                    name={open ? 'chevron-up' : 'chevron-down'}
                    size={14}
                    color={colors.textFaint}
                  />
                </View>
                <Text style={[styles.desc, open && styles.descOpen]} numberOfLines={open ? undefined : 2}>
                  {item.description || 'No description'}
                </Text>
                <View style={styles.badges}>
                  {item.systems.map((sys) => (
                    <View key={sys} style={styles.badge}>
                      <Text style={styles.badgeText}>{sys}</Text>
                    </View>
                  ))}
                  {item.user_invocable ? (
                    <Text style={styles.invocable}>user-invocable</Text>
                  ) : null}
                </View>
                {open && item.body ? (
                  <Text style={styles.body}>{item.body}</Text>
                ) : null}
                {open && item.allowed_tools && item.allowed_tools.length > 0 ? (
                  <Text style={styles.tools}>Tools: {item.allowed_tools.join(', ')}</Text>
                ) : null}
              </Pressable>
            );
          }}
        />
      )}
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: colors.bg },
  searchWrap: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.sm,
    marginHorizontal: space.lg,
    marginBottom: space.sm,
    paddingHorizontal: space.md,
    height: 40,
    backgroundColor: colors.bgElevated,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: colors.border,
  },
  search: { flex: 1, color: colors.text, fontSize: font.size.md, padding: 0 },
  list: { paddingBottom: space.xl },
  count: {
    color: colors.textFaint,
    fontSize: font.size.xs,
    paddingHorizontal: space.lg,
    paddingBottom: space.sm,
  },
  row: {
    paddingHorizontal: space.lg,
    paddingVertical: space.md,
    borderBottomWidth: 1,
    borderBottomColor: colors.border,
    gap: 4,
  },
  rowTop: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', gap: space.sm },
  name: { flex: 1, color: colors.accent, fontSize: font.size.sm, fontWeight: '700', fontFamily: font.mono },
  scope: { color: colors.textFaint, fontWeight: '400', fontSize: font.size.xs },
  desc: { color: colors.textMuted, fontSize: font.size.sm, lineHeight: 19 },
  descOpen: { color: colors.text },
  badges: { flexDirection: 'row', flexWrap: 'wrap', alignItems: 'center', gap: 6, marginTop: 4 },
  badge: {
    backgroundColor: colors.bgElevated,
    borderRadius: radius.sm,
    paddingHorizontal: space.sm,
    paddingVertical: 3,
    borderWidth: 1,
    borderColor: colors.border,
  },
  badgeText: { color: colors.accent, fontSize: font.size.xs, fontFamily: font.mono },
  invocable: { color: colors.textFaint, fontSize: font.size.xs },
  body: {
    color: colors.textMuted,
    fontSize: font.size.xs,
    fontFamily: font.mono,
    lineHeight: 17,
    marginTop: 6,
    padding: space.md,
    backgroundColor: colors.bgElevated,
    borderRadius: radius.sm,
  },
  tools: { color: colors.textFaint, fontSize: font.size.xs, marginTop: 4 },
});
