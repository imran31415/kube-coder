/** Applications: web apps running in the workspace, embeddable in-app. */
import { Ionicons } from '@expo/vector-icons';
import { useNavigation } from '@react-navigation/native';
import React, { useCallback, useState } from 'react';
import { FlatList, RefreshControl, StyleSheet, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { listApps } from '../api/client';
import { Card, EmptyState, ErrorBanner, Loading, ScreenHeader } from '../components/ui';
import type { AppsNav } from '../navigation';
import type { AppEntry } from '../api/types';
import { colors, font, radius, space } from '../theme';
import { usePolling } from '../util/usePolling';

const STATUS_META: Record<AppEntry['status'], { color: string; label: string }> = {
  running: { color: colors.success, label: 'running' },
  stopped: { color: colors.killed, label: 'stopped' },
  blocked: { color: colors.warning, label: 'reserved' },
};

function appTitle(app: AppEntry): string {
  return app.name || `Port ${app.port}`;
}

export default function AppsScreen() {
  const nav = useNavigation<AppsNav>();
  const [apps, setApps] = useState<AppEntry[] | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const list = await listApps();
      // Running first, then pinned-but-stopped; stable by port within a group.
      list.sort(
        (a, b) =>
          Number(b.status === 'running') - Number(a.status === 'running') || a.port - b.port,
      );
      setApps(list);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
      setApps((prev) => prev ?? []);
    }
  }, []);

  // Listeners come and go as the user's dev servers start/stop; poll gently
  // while this tab is focused (usePolling also fires immediately on focus).
  usePolling(load, 6000);

  const onRefresh = async () => {
    setRefreshing(true);
    await load();
    setRefreshing(false);
  };

  return (
    <SafeAreaView style={styles.safe} edges={['top']}>
      <ScreenHeader title="Apps" subtitle="Web apps running in your workspace" />

      {error && apps !== null && apps.length > 0 ? <ErrorBanner message={error} /> : null}

      {apps === null ? (
        <Loading label="Finding running apps…" />
      ) : apps.length === 0 ? (
        error ? (
          <EmptyState
            icon="cloud-offline-outline"
            title="Couldn't load apps"
            subtitle={error}
          />
        ) : (
          <EmptyState
            icon="globe-outline"
            title="No apps running"
            subtitle="Start a dev server in your workspace (e.g. on port 3000) and it shows up here, ready to preview."
          />
        )
      ) : (
        <FlatList
          data={apps}
          keyExtractor={(a) => String(a.port)}
          contentContainerStyle={styles.list}
          showsVerticalScrollIndicator={false}
          refreshControl={
            <RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={colors.accent} />
          }
          renderItem={({ item }) => {
            const meta = STATUS_META[item.status] ?? STATUS_META.stopped;
            const openable = item.status === 'running';
            return (
              <Card
                style={styles.row}
                onPress={
                  openable
                    ? () => nav.navigate('AppView', { port: item.port, name: appTitle(item) })
                    : undefined
                }
              >
                <View style={[styles.appIcon, !openable && { opacity: 0.5 }]}>
                  <Ionicons name="globe-outline" size={20} color={colors.accent} />
                </View>
                <View style={styles.rowMain}>
                  <View style={styles.rowTitle}>
                    <Text style={[styles.name, !openable && { color: colors.textMuted }]} numberOfLines={1}>
                      {appTitle(item)}
                    </Text>
                    {item.pinned ? (
                      <Ionicons name="pin" size={12} color={colors.textFaint} />
                    ) : null}
                  </View>
                  <View style={styles.rowMeta}>
                    <View style={[styles.dot, { backgroundColor: meta.color }]} />
                    <Text style={[styles.metaText, { color: meta.color }]}>{meta.label}</Text>
                    <Text style={styles.metaDim}>·</Text>
                    <Text style={styles.metaDim}>
                      {item.addr}:{item.port}
                    </Text>
                  </View>
                </View>
                {openable ? (
                  <Ionicons name="chevron-forward" size={18} color={colors.textFaint} />
                ) : null}
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
  list: { paddingHorizontal: space.lg, paddingBottom: space.xl, gap: space.md },
  row: { flexDirection: 'row', alignItems: 'center', gap: space.md },
  appIcon: {
    width: 42,
    height: 42,
    borderRadius: radius.md,
    backgroundColor: colors.accent + '1a',
    alignItems: 'center',
    justifyContent: 'center',
  },
  rowMain: { flex: 1, gap: 3 },
  rowTitle: { flexDirection: 'row', alignItems: 'center', gap: 6 },
  name: { color: colors.text, fontSize: font.size.md, fontWeight: '700', flexShrink: 1 },
  rowMeta: { flexDirection: 'row', alignItems: 'center', gap: 6 },
  dot: { width: 7, height: 7, borderRadius: 4 },
  metaText: { fontSize: font.size.xs, fontWeight: '700', textTransform: 'uppercase', letterSpacing: 0.5 },
  metaDim: { color: colors.textFaint, fontSize: font.size.xs, fontFamily: font.mono },
});
