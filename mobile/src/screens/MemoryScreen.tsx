/** Persistent memory browser (read-only). */
import React, { useEffect, useState } from 'react';
import { FlatList, StyleSheet, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { listMemory } from '../api/client';
import { Card, EmptyState, Loading, ScreenHeader } from '../components/ui';
import type { MemoryRecord } from '../api/types';
import { colors, font, radius, space } from '../theme';

export default function MemoryScreen() {
  const [items, setItems] = useState<MemoryRecord[] | null>(null);

  useEffect(() => {
    listMemory()
      .then(setItems)
      .catch(() => setItems([]));
  }, []);

  return (
    <SafeAreaView style={styles.safe} edges={['top']}>
      <ScreenHeader title="Memory" subtitle="What your workspace remembers" />
      {items === null ? (
        <Loading label="Loading memory…" />
      ) : items.length === 0 ? (
        <EmptyState
          icon="bookmark-outline"
          title="No memory entries"
          subtitle="Facts you ask the workspace to remember show up here."
        />
      ) : (
        <FlatList
          data={items}
          keyExtractor={(m) => `${m.namespace}/${m.key}`}
          contentContainerStyle={styles.list}
          showsVerticalScrollIndicator={false}
          renderItem={({ item }) => (
            <Card style={{ gap: space.sm }}>
              <Text style={styles.ns}>
                {item.namespace}
                <Text style={styles.key}>/{item.key}</Text>
              </Text>
              <Text style={styles.value}>{item.value}</Text>
              {item.tags && item.tags.length > 0 ? (
                <View style={styles.tags}>
                  {item.tags.map((t) => (
                    <View key={t} style={styles.tag}>
                      <Text style={styles.tagText}>{t}</Text>
                    </View>
                  ))}
                </View>
              ) : null}
            </Card>
          )}
        />
      )}
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: colors.bg },
  list: { paddingHorizontal: space.lg, paddingBottom: space.xl, gap: space.md },
  ns: { color: colors.accent, fontSize: font.size.sm, fontWeight: '700', fontFamily: font.mono },
  key: { color: colors.textMuted, fontWeight: '400' },
  value: { color: colors.text, fontSize: font.size.md, lineHeight: 21 },
  tags: { flexDirection: 'row', flexWrap: 'wrap', gap: 6 },
  tag: {
    backgroundColor: colors.bgElevated,
    borderRadius: radius.sm,
    paddingHorizontal: space.sm,
    paddingVertical: 3,
  },
  tagText: { color: colors.textMuted, fontSize: font.size.xs },
});
