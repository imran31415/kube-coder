/** One documentation page, rendered natively (#250).
 *
 *  The markdown comes from /api/docs/<id> and goes through the app's existing
 *  dependency-free renderer (src/components/Markdown.tsx) — the same one the
 *  Hypervisor chat uses — so headings, code fences and lists read as native
 *  text instead of raw syntax. The stack header supplies the back button. */
import { useNavigation, useRoute, type RouteProp } from '@react-navigation/native';
import React, { useEffect, useLayoutEffect, useState } from 'react';
import { RefreshControl, ScrollView, StyleSheet, Text, View } from 'react-native';
import { getDocsPage } from '../api/client';
import type { DocsPage } from '../api/types';
import { Markdown } from '../components/Markdown';
import { EmptyState, Loading } from '../components/ui';
import type { DocsStackParams } from '../navigation';
import { stripLeadingTitle } from '../util/docs';
import { relativeTime } from '../util/format';
import { colors, font, space } from '../theme';

export default function DocsArticleScreen() {
  const route = useRoute<RouteProp<DocsStackParams, 'DocsArticle'>>();
  const nav = useNavigation();
  const { id, title } = route.params;
  const [page, setPage] = useState<DocsPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  // Show the title from the manifest immediately, before the body lands.
  useLayoutEffect(() => {
    nav.setOptions({ title });
  }, [nav, title]);

  const load = async () => {
    try {
      setPage(await getDocsPage(id));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  const onRefresh = async () => {
    setRefreshing(true);
    await load();
    setRefreshing(false);
  };

  if (!page) {
    return (
      <View style={styles.fill}>
        {error ? (
          <EmptyState icon="cloud-offline-outline" title="Couldn't load this page" subtitle={error} />
        ) : (
          <Loading label="Loading page…" />
        )}
      </View>
    );
  }

  return (
    <ScrollView
      style={styles.fill}
      contentContainerStyle={styles.content}
      refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={colors.accent} />}
    >
      <Text style={styles.crumb}>{page.section_title}</Text>
      <Text style={styles.title}>{page.title}</Text>
      {page.summary ? <Text style={styles.summary}>{page.summary}</Text> : null}
      <Text style={styles.meta}>
        {page.file}
        {page.edited_at ? ` · updated ${relativeTime(page.edited_at)}` : ''}
      </Text>
      <View style={styles.divider} />
      <Markdown text={stripLeadingTitle(page.markdown, page.title)} />
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  fill: { flex: 1, backgroundColor: colors.bg },
  content: { padding: space.lg, paddingBottom: space.xxl },
  crumb: {
    color: colors.textFaint,
    fontSize: font.size.xs,
    fontWeight: '700',
    textTransform: 'uppercase',
    letterSpacing: 0.8,
  },
  title: { color: colors.text, fontSize: font.size.xxl, fontWeight: '800', letterSpacing: -0.4, marginTop: 4 },
  summary: { color: colors.textMuted, fontSize: font.size.md, lineHeight: 22, marginTop: space.sm },
  meta: { color: colors.textFaint, fontSize: font.size.xs, fontFamily: font.mono, marginTop: space.sm },
  divider: { height: 1, backgroundColor: colors.border, marginVertical: space.lg },
});
