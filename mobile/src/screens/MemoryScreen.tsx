/** Persistent memory browser (read-only) — searchable, compact rows. */
import { Ionicons } from '@expo/vector-icons';
import React, { useEffect, useMemo, useState } from 'react';
import { FlatList, Pressable, StyleSheet, Text, TextInput, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { listMemory } from '../api/client';
import { EmptyState, Loading, ScreenHeader } from '../components/ui';
import type { MemoryRecord } from '../api/types';
import { colors, font, radius, space } from '../theme';

const rowKey = (m: MemoryRecord) => `${m.namespace}/${m.key}`;

export default function MemoryScreen() {
  const [items, setItems] = useState<MemoryRecord[] | null>(null);
  const [query, setQuery] = useState('');
  const [openKey, setOpenKey] = useState<string | null>(null);

  useEffect(() => {
    listMemory()
      .then(setItems)
      .catch(() => setItems([]));
  }, []);

  const filtered = useMemo(() => {
    if (!items) return null;
    const q = query.trim().toLowerCase();
    if (!q) return items;
    return items.filter((m) =>
      `${m.namespace} ${m.key} ${m.value} ${(m.tags ?? []).join(' ')}`.toLowerCase().includes(q),
    );
  }, [items, query]);

  return (
    <SafeAreaView style={styles.safe} edges={['top']}>
      <ScreenHeader title="Memory" subtitle="What your workspace remembers" />

      {items !== null && items.length > 0 ? (
        <View style={styles.searchWrap}>
          <Ionicons name="search" size={16} color={colors.textFaint} />
          <TextInput
            value={query}
            onChangeText={setQuery}
            placeholder="Search memory…"
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

      {filtered === null ? (
        <Loading label="Loading memory…" />
      ) : items && items.length === 0 ? (
        <EmptyState
          icon="bookmark-outline"
          title="No memory entries"
          subtitle="Facts you ask the workspace to remember show up here."
        />
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
          ListHeaderComponent={
            <Text style={styles.count}>
              {filtered.length} {filtered.length === 1 ? 'entry' : 'entries'}
            </Text>
          }
          renderItem={({ item }) => {
            const open = openKey === rowKey(item);
            return (
              <Pressable style={styles.row} onPress={() => setOpenKey(open ? null : rowKey(item))}>
                <View style={styles.rowTop}>
                  <Text style={styles.ns} numberOfLines={1}>
                    {item.namespace}
                    <Text style={styles.key}>/{item.key}</Text>
                  </Text>
                  <Ionicons
                    name={open ? 'chevron-up' : 'chevron-down'}
                    size={14}
                    color={colors.textFaint}
                  />
                </View>
                <Text style={[styles.value, open && styles.valueOpen]} numberOfLines={open ? undefined : 1}>
                  {item.value}
                </Text>
                {open && item.tags && item.tags.length > 0 ? (
                  <View style={styles.tags}>
                    {item.tags.map((t) => (
                      <View key={t} style={styles.tag}>
                        <Text style={styles.tagText}>{t}</Text>
                      </View>
                    ))}
                  </View>
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
  ns: { flex: 1, color: colors.accent, fontSize: font.size.sm, fontWeight: '700', fontFamily: font.mono },
  key: { color: colors.textMuted, fontWeight: '400' },
  value: { color: colors.textMuted, fontSize: font.size.sm, lineHeight: 19 },
  valueOpen: { color: colors.text },
  tags: { flexDirection: 'row', flexWrap: 'wrap', gap: 6, marginTop: 4 },
  tag: {
    backgroundColor: colors.bgElevated,
    borderRadius: radius.sm,
    paddingHorizontal: space.sm,
    paddingVertical: 3,
  },
  tagText: { color: colors.textMuted, fontSize: font.size.xs },
});
