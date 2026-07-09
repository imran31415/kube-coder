/** Controller: the admin plane on mobile. Lists every workspace with live
 *  state, start/stop, and a cluster-capacity summary — authenticated by the
 *  controller admin token (a second connection, separate from the workspace).
 *  Mirrors the web console's core actions (list / start-stop / capacity);
 *  provisioning + resource edits stay web-only for now. */
import { Ionicons } from '@expo/vector-icons';
import React, { useCallback, useState } from 'react';
import { Alert, FlatList, Pressable, RefreshControl, StyleSheet, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import {
  getControllerCapacity,
  listControllerWorkspaces,
  startWorkspace,
  stopWorkspace,
} from '../api/controller';
import { EmptyState, ErrorBanner, Loading, ScreenHeader } from '../components/ui';
import { getConfig } from '../store/config';
import type { ControllerCapacity, ControllerWorkspace } from '../api/types';
import { colors, font, radius, shadow, space } from '../theme';
import { usePolling } from '../util/usePolling';

const WS_STATE: Record<string, { color: string; label: string }> = {
  running: { color: colors.success, label: 'Running' },
  stopped: { color: colors.textFaint, label: 'Stopped' },
  transitioning: { color: colors.warning, label: 'Working' },
  degraded: { color: colors.danger, label: 'Degraded' },
};

function StatePill({ state }: { state: string }) {
  const s = WS_STATE[state] ?? { color: colors.textMuted, label: state };
  return (
    <View style={[styles.pill, { backgroundColor: s.color + '1f', borderColor: s.color + '4d' }]}>
      <View style={[styles.dot, { backgroundColor: s.color }]} />
      <Text style={[styles.pillText, { color: s.color }]}>{s.label}</Text>
    </View>
  );
}

function pct(v: number | null | undefined): string {
  return v == null ? '—' : `${Math.round(v)}%`;
}

function CapacityCard({ cap }: { cap: ControllerCapacity | null }) {
  if (!cap) return null;
  const status = cap.status;
  const tint =
    status === 'critical' ? colors.danger : status === 'warn' ? colors.warning : status === 'ok' ? colors.success : colors.textFaint;
  return (
    <View style={styles.capCard}>
      <View style={styles.capHead}>
        <View style={[styles.dot, { backgroundColor: tint, width: 9, height: 9, borderRadius: 5 }]} />
        <Text style={styles.capTitle}>Cluster</Text>
        <Text style={[styles.capStatus, { color: tint }]}>{status.toUpperCase()}</Text>
      </View>
      {cap.metricsError ? (
        <Text style={styles.capErr}>Metrics unavailable</Text>
      ) : (
        <View style={styles.capStats}>
          <Stat label="Nodes" value={cap.cluster ? String(cap.cluster.nodeCount) : '—'} />
          <Stat label="CPU" value={pct(cap.cluster?.cpu.clusterPct)} />
          <Stat label="Memory" value={pct(cap.cluster?.memory.clusterPct)} />
        </View>
      )}
    </View>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <View style={styles.stat}>
      <Text style={styles.statValue}>{value}</Text>
      <Text style={styles.statLabel}>{label}</Text>
    </View>
  );
}

export default function ControllerScreen() {
  const [items, setItems] = useState<ControllerWorkspace[] | null>(null);
  const [cap, setCap] = useState<ControllerCapacity | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const ws = await listControllerWorkspaces();
      ws.sort((a, b) => a.user.localeCompare(b.user));
      setItems(ws);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
      setItems((prev) => prev ?? []);
    }
    try {
      setCap(await getControllerCapacity());
    } catch {
      /* capacity is best-effort — the list is the primary content */
    }
  }, []);

  usePolling(load, 5000);

  const onRefresh = async () => {
    setRefreshing(true);
    await load();
    setRefreshing(false);
  };

  async function toggle(ws: ControllerWorkspace) {
    const stopping = ws.state !== 'stopped';
    const run = async () => {
      setBusy(ws.user);
      try {
        if (stopping) await stopWorkspace(ws.user);
        else await startWorkspace(ws.user);
        await load();
      } catch (e) {
        Alert.alert(`Couldn't ${stopping ? 'stop' : 'start'} ${ws.user}`, (e as Error).message);
      } finally {
        setBusy(null);
      }
    };
    if (stopping) {
      Alert.alert('Stop workspace?', `Scale ${ws.user} to zero. Its data is preserved and it can be started again.`, [
        { text: 'Cancel', style: 'cancel' },
        { text: 'Stop', style: 'destructive', onPress: () => void run() },
      ]);
    } else {
      void run();
    }
  }

  const host = getConfig().controllerHost.replace(/^https?:\/\//, '');
  const count = items?.length ?? 0;

  return (
    <SafeAreaView style={styles.safe} edges={['top']}>
      <ScreenHeader
        brand
        title="Controller"
        subtitle={host ? `${host} · ${count} workspace${count === 1 ? '' : 's'}` : 'Admin plane'}
      />
      {error && items && items.length > 0 ? <ErrorBanner message={error} /> : null}

      {items === null ? (
        <Loading label="Loading workspaces…" />
      ) : (
        <FlatList
          data={items}
          keyExtractor={(w) => w.deployment}
          contentContainerStyle={styles.list}
          showsVerticalScrollIndicator={false}
          refreshControl={
            <RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={colors.accent} />
          }
          ListHeaderComponent={<CapacityCard cap={cap} />}
          ListEmptyComponent={
            error ? (
              <EmptyState icon="cloud-offline-outline" title="Couldn't reach the controller" subtitle={error} />
            ) : (
              <EmptyState icon="server-outline" title="No workspaces" subtitle="This controller has no workspaces yet." />
            )
          }
          renderItem={({ item }) => {
            const stopped = item.state === 'stopped';
            const pending = item.state === 'transitioning' || busy === item.user;
            return (
              <View style={styles.card}>
                <View style={styles.cardMain}>
                  <View style={styles.cardTop}>
                    <Text style={styles.wsName} numberOfLines={1}>
                      {item.user}
                    </Text>
                    <StatePill state={item.state} />
                    {item.updateAvailable ? (
                      <View style={styles.updatePill}>
                        <Text style={styles.updateText}>UPDATE</Text>
                      </View>
                    ) : null}
                  </View>
                  <Text style={styles.wsMeta} numberOfLines={1}>
                    {item.namespace} · {item.detail}
                    {item.version ? ` · ${item.version}` : ''}
                  </Text>
                </View>
                <Pressable
                  onPress={() => toggle(item)}
                  disabled={pending}
                  style={({ pressed }) => [
                    styles.actionBtn,
                    stopped ? styles.startBtn : styles.stopBtn,
                    (pending || pressed) && { opacity: 0.6 },
                  ]}
                >
                  {pending ? (
                    <Ionicons name="hourglass-outline" size={15} color={colors.textMuted} />
                  ) : (
                    <Text style={[styles.actionText, { color: stopped ? colors.success : colors.danger }]}>
                      {stopped ? 'Start' : 'Stop'}
                    </Text>
                  )}
                </Pressable>
              </View>
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

  // Capacity summary
  capCard: {
    backgroundColor: colors.card,
    borderRadius: radius.lg,
    borderWidth: 1,
    borderColor: colors.border,
    padding: space.lg,
    marginBottom: space.xs,
    ...shadow.card,
  },
  capHead: { flexDirection: 'row', alignItems: 'center', gap: space.sm },
  capTitle: { color: colors.text, fontSize: font.size.md, fontWeight: '700', flex: 1 },
  capStatus: { fontSize: font.size.xs, fontWeight: '800', letterSpacing: 0.6 },
  capErr: { color: colors.textMuted, fontSize: font.size.sm, marginTop: space.sm },
  capStats: { flexDirection: 'row', marginTop: space.md, gap: space.lg },
  stat: { flex: 1 },
  statValue: { color: colors.text, fontSize: font.size.xl, fontWeight: '800', letterSpacing: -0.5 },
  statLabel: {
    color: colors.textFaint,
    fontSize: font.size.xs,
    fontWeight: '600',
    textTransform: 'uppercase',
    letterSpacing: 0.5,
    marginTop: 2,
  },

  // Workspace row
  card: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.md,
    backgroundColor: colors.card,
    borderRadius: radius.lg,
    borderWidth: 1,
    borderColor: colors.border,
    paddingVertical: space.md,
    paddingHorizontal: space.lg,
    ...shadow.card,
  },
  cardMain: { flex: 1, minWidth: 0, gap: 4 },
  cardTop: { flexDirection: 'row', alignItems: 'center', gap: space.sm, flexWrap: 'wrap' },
  wsName: { color: colors.text, fontSize: font.size.md, fontWeight: '700', flexShrink: 1 },
  wsMeta: { color: colors.textFaint, fontSize: font.size.xs },

  pill: {
    flexDirection: 'row',
    alignItems: 'center',
    alignSelf: 'flex-start',
    borderRadius: radius.pill,
    borderWidth: 1,
    paddingVertical: 3,
    paddingHorizontal: 8,
  },
  dot: { width: 7, height: 7, borderRadius: 4, marginRight: 5 },
  pillText: { fontSize: font.size.xs, fontWeight: '700', textTransform: 'uppercase', letterSpacing: 0.4 },

  updatePill: {
    borderRadius: radius.pill,
    paddingVertical: 3,
    paddingHorizontal: 8,
    backgroundColor: colors.accent + '22',
  },
  updateText: { color: colors.accent, fontSize: font.size.xs, fontWeight: '700', letterSpacing: 0.4 },

  actionBtn: {
    minWidth: 68,
    height: 34,
    alignItems: 'center',
    justifyContent: 'center',
    borderRadius: radius.md,
    borderWidth: 1,
  },
  startBtn: { borderColor: colors.success + '80' },
  stopBtn: { borderColor: colors.danger + '66' },
  actionText: { fontSize: font.size.sm, fontWeight: '700' },
});
