/** Docs — a browsable, phone-shaped view of the workspace documentation (#250).
 *
 *  The dashboard's /docs route is a sidebar + article split; a phone gets the
 *  two halves as two steps instead: this list (manifest sections → pages, with
 *  an instant title/summary filter), then DocsArticleScreen for the markdown.
 *  Same /api/docs endpoints, no VS Code detour. */
import { Ionicons } from '@expo/vector-icons';
import { useNavigation } from '@react-navigation/native';
import React, { useCallback, useMemo, useState } from 'react';
import { Pressable, RefreshControl, ScrollView, StyleSheet, Text, TextInput, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { useFocusEffect } from '@react-navigation/native';
import { getDocsManifest } from '../api/client';
import type { DocsManifest } from '../api/types';
import { EmptyState, ErrorBanner, Loading, ScreenHeader } from '../components/ui';
import type { DocsNav } from '../navigation';
import { filterDocs, flattenDocs, groupDocs } from '../util/docs';
import { colors, font, radius, space } from '../theme';

export default function DocsScreen() {
  const nav = useNavigation<DocsNav>();
  const [manifest, setManifest] = useState<DocsManifest | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [query, setQuery] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async () => {
    try {
      setManifest(await getDocsManifest());
      setError(null);
    } catch (e) {
      // A failed load must not masquerade as "no docs".
      setError((e as Error).message);
    } finally {
      setLoaded(true);
    }
  }, []);

  // The manifest is edited on disk (docs/_manifest.json); refetch on focus.
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

  const rows = useMemo(() => flattenDocs(manifest), [manifest]);
  const sections = useMemo(() => groupDocs(filterDocs(rows, query)), [rows, query]);

  return (
    <SafeAreaView style={styles.safe} edges={['top']}>
      <ScreenHeader
        title="Docs"
        subtitle={
          loaded && rows.length
            ? `${rows.length} page${rows.length === 1 ? '' : 's'} · ${manifest?.sections.length ?? 0} sections`
            : 'How this workspace works'
        }
      />

      {rows.length > 0 ? (
        <View style={styles.searchWrap}>
          <Ionicons name="search" size={16} color={colors.textFaint} />
          <TextInput
            value={query}
            onChangeText={setQuery}
            placeholder="Search docs…"
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

      {error && rows.length > 0 ? <ErrorBanner message={error} /> : null}

      {!loaded ? (
        <Loading label="Loading docs…" />
      ) : rows.length === 0 ? (
        error ? (
          <EmptyState icon="cloud-offline-outline" title="Couldn't load docs" subtitle={error} />
        ) : (
          <EmptyState
            icon="book-outline"
            title="No docs published"
            subtitle="Pages come from docs/_manifest.json in the workspace's kube-coder clone."
          />
        )
      ) : sections.length === 0 ? (
        <EmptyState icon="search-outline" title="No matches" subtitle={`Nothing matches “${query.trim()}”.`} />
      ) : (
        <ScrollView
          contentContainerStyle={styles.list}
          keyboardShouldPersistTaps="handled"
          keyboardDismissMode="on-drag"
          showsVerticalScrollIndicator={false}
          refreshControl={
            <RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={colors.accent} />
          }
        >
          {sections.map((section) => (
            <View key={section.id}>
              <Text style={styles.sectionTitle}>{section.title}</Text>
              {section.pages.map((page) => (
                <Pressable
                  key={page.id}
                  style={({ pressed }) => [styles.row, pressed && styles.rowPressed]}
                  accessibilityRole="button"
                  accessibilityLabel={`Open ${page.title}`}
                  onPress={() => nav.navigate('DocsArticle', { id: page.id, title: page.title })}
                >
                  <View style={styles.rowText}>
                    <Text style={styles.rowTitle} numberOfLines={1}>
                      {page.title}
                    </Text>
                    {page.summary ? (
                      <Text style={styles.rowSummary} numberOfLines={2}>
                        {page.summary}
                      </Text>
                    ) : null}
                  </View>
                  <Ionicons name="chevron-forward" size={16} color={colors.textFaint} />
                </Pressable>
              ))}
            </View>
          ))}
        </ScrollView>
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
  sectionTitle: {
    color: colors.textFaint,
    fontSize: font.size.xs,
    fontWeight: '700',
    textTransform: 'uppercase',
    letterSpacing: 0.8,
    paddingHorizontal: space.lg,
    paddingTop: space.lg,
    paddingBottom: space.sm,
  },
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.md,
    paddingHorizontal: space.lg,
    paddingVertical: space.md,
    borderBottomWidth: 1,
    borderBottomColor: colors.border,
  },
  rowPressed: { backgroundColor: colors.accentSoft },
  rowText: { flex: 1, gap: 3 },
  rowTitle: { color: colors.text, fontSize: font.size.md, fontWeight: '600' },
  rowSummary: { color: colors.textMuted, fontSize: font.size.sm, lineHeight: 18 },
});
