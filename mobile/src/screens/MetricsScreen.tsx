/** Workspace metrics + service health. */
import React, { useCallback, useEffect, useState } from 'react';
import { ScrollView, StyleSheet, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { getHealth, getMetrics } from '../api/client';
import { Card, Loading } from '../components/ui';
import type { Health, Metrics } from '../api/types';
import { colors, font, radius, space } from '../theme';

function Bar({ pct, color }: { pct: number; color: string }) {
  return (
    <View style={styles.barTrack}>
      <View style={[styles.barFill, { width: `${Math.min(100, Math.max(0, pct))}%`, backgroundColor: color }]} />
    </View>
  );
}

function Gauge({ label, used, total, unit, pct, color }: {
  label: string;
  used: number;
  total: number;
  unit: string;
  pct: number;
  color: string;
}) {
  return (
    <Card style={{ gap: space.sm }}>
      <View style={styles.gaugeTop}>
        <Text style={styles.gaugeLabel}>{label}</Text>
        <Text style={styles.gaugeVal}>
          {used}
          <Text style={styles.gaugeTotal}>
            {' '}
            / {total} {unit}
          </Text>
        </Text>
      </View>
      <Bar pct={pct} color={color} />
      <Text style={[styles.pct, { color }]}>{Math.round(pct)}%</Text>
    </Card>
  );
}

export default function MetricsScreen() {
  const [m, setM] = useState<Metrics | null>(null);
  const [h, setH] = useState<Health | null>(null);

  const load = useCallback(async () => {
    const [metrics, health] = await Promise.all([
      getMetrics().catch(() => null),
      getHealth().catch(() => null),
    ]);
    if (metrics) setM(metrics);
    if (health) setH(health);
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, [load]);

  if (!m) return <Loading label="Loading metrics…" />;

  const memPct = (m.memory_used_mb / m.memory_total_mb) * 100;
  const diskPct = (m.disk_used_gb / m.disk_total_gb) * 100;

  const services: [string, boolean | undefined][] = [
    ['VS Code', h?.vscode],
    ['Terminal', h?.terminal],
    ['Browser', h?.browser],
  ];

  return (
    <SafeAreaView style={styles.safe} edges={['top']}>
      <View style={styles.header}>
        <Text style={styles.title}>Metrics</Text>
      </View>
      <ScrollView contentContainerStyle={styles.list}>
        <Gauge label="CPU" used={Math.round(m.cpu_percent)} total={100} unit="%" pct={m.cpu_percent} color={colors.accent} />
        <Gauge label="Memory" used={Math.round(m.memory_used_mb)} total={Math.round(m.memory_total_mb)} unit="MB" pct={memPct} color={colors.warning} />
        <Gauge label="Disk" used={Math.round(m.disk_used_gb)} total={Math.round(m.disk_total_gb)} unit="GB" pct={diskPct} color={colors.success} />

        <Card style={{ gap: space.md, marginTop: space.sm }}>
          <Text style={styles.gaugeLabel}>Services</Text>
          {services.map(([name, up]) => (
            <View key={name} style={styles.svcRow}>
              <View style={[styles.svcDot, { backgroundColor: up ? colors.success : colors.killed }]} />
              <Text style={styles.svcName}>{name}</Text>
              <Text style={[styles.svcState, { color: up ? colors.success : colors.textFaint }]}>
                {up ? 'up' : 'down'}
              </Text>
            </View>
          ))}
        </Card>
      </ScrollView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: colors.bg },
  header: { paddingHorizontal: space.lg, paddingTop: space.md, paddingBottom: space.sm },
  title: { color: colors.text, fontSize: font.size.xxl, fontWeight: '800' },
  list: { padding: space.lg, gap: space.md },
  gaugeTop: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'baseline' },
  gaugeLabel: { color: colors.textMuted, fontSize: font.size.sm, fontWeight: '600', textTransform: 'uppercase', letterSpacing: 0.5 },
  gaugeVal: { color: colors.text, fontSize: font.size.lg, fontWeight: '700' },
  gaugeTotal: { color: colors.textFaint, fontSize: font.size.sm, fontWeight: '400' },
  barTrack: { height: 8, borderRadius: radius.pill, backgroundColor: colors.bgElevated, overflow: 'hidden' },
  barFill: { height: 8, borderRadius: radius.pill },
  pct: { fontSize: font.size.xs, fontWeight: '700', alignSelf: 'flex-end' },
  svcRow: { flexDirection: 'row', alignItems: 'center', gap: space.md },
  svcDot: { width: 9, height: 9, borderRadius: 5 },
  svcName: { color: colors.text, fontSize: font.size.md, flex: 1 },
  svcState: { fontSize: font.size.sm, fontWeight: '600' },
});
