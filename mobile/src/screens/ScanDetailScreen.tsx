/** One security scan (#726) — live progress, then its findings. */
import { Ionicons } from '@expo/vector-icons';
import type { RouteProp } from '@react-navigation/native';
import { useRoute } from '@react-navigation/native';
import React, { useCallback, useState } from 'react';
import { ScrollView, StyleSheet, Text, TouchableOpacity, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { getScan, setFindingDisposition, stopScan } from '../api/client';
import { Button, Card, ErrorBanner, Loading } from '../components/ui';
import type { Finding, ScanDetail } from '../api/types';
import type { SecurityStackParams } from '../navigation';
import { colors, font, radius, space } from '../theme';
import { usePolling } from '../util/usePolling';
import {
  FINDING_FACTS,
  FINDING_SECTIONS,
  fieldText,
  isLiveScan,
  sortFindings,
  spendLabel,
} from '../util/scans';

const SEVERITY_COLOR: Record<string, string> = {
  critical: colors.danger,
  high: colors.danger,
  medium: colors.warning,
  low: colors.accent,
};

export default function ScanDetailScreen() {
  const route = useRoute<RouteProp<SecurityStackParams, 'ScanDetail'>>();
  const { id } = route.params;
  const [scan, setScan] = useState<ScanDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [stopping, setStopping] = useState(false);

  const load = useCallback(async () => {
    try {
      setScan(await getScan(id));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [id]);

  // A scan rewrites its findings as it confirms them, so this fills in while
  // it runs rather than showing a frozen screen for minutes at a time.
  usePolling(load, 6000);

  if (!scan) {
    return (
      <SafeAreaView style={styles.safe} edges={['bottom']}>
        {error ? <ErrorBanner message={error} /> : <Loading label="Loading scan" />}
      </SafeAreaView>
    );
  }

  const live = isLiveScan(scan);
  const findings = sortFindings(scan.findings ?? []);
  const dispositions = scan.dispositions ?? {};

  async function stop() {
    setStopping(true);
    try {
      await stopScan(id);
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setStopping(false);
    }
  }

  return (
    <SafeAreaView style={styles.safe} edges={['bottom']}>
      <ScrollView contentContainerStyle={styles.body}>
        <Card style={styles.head}>
          <Text style={styles.title}>
            {scan.target.name || `Port ${scan.target.port}`}
          </Text>
          {/* Never a bare count: the sentence says what was actually done. */}
          <Text style={styles.summary}>{scan.summary}</Text>
          <View style={styles.stats}>
            <Stat label="Status" value={scan.status} />
            <Stat label="Depth" value={scan.mode} />
            <Stat label="Spent" value={spendLabel(scan)} />
          </View>
          <Text style={styles.address}>{scan.target.url}</Text>
          {live ? (
            <Button
              title={stopping ? 'Stopping…' : 'Stop scan'}
              onPress={stop}
              disabled={stopping}
              variant="danger"
            />
          ) : null}
        </Card>

        {scan.error ? <ErrorBanner message={scan.error} /> : null}

        <Text style={styles.sectionTitle}>
          Findings{scan.counts?.total ? ` (${scan.counts.total})` : ''}
        </Text>

        {findings.length === 0 ? (
          <Text style={styles.empty}>
            {live
              ? 'Nothing found yet. Findings appear here as the scanner confirms them.'
              : 'No findings were recorded for this scan.'}
          </Text>
        ) : (
          findings.map((f) => (
            <FindingCard
              key={f.id}
              scanId={scan.id}
              finding={f}
              dismissed={dispositions[f.id] === 'dismissed'}
              onChanged={load}
            />
          ))
        )}
      </ScrollView>
    </SafeAreaView>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <View style={styles.stat}>
      <Text style={styles.statLabel}>{label}</Text>
      <Text style={styles.statValue}>{value}</Text>
    </View>
  );
}

function FindingCard({
  scanId,
  finding,
  dismissed,
  onChanged,
}: {
  scanId: string;
  finding: Finding;
  dismissed: boolean;
  onChanged: () => void;
}) {
  // Collapsed by default: a reproduction can be pages long, and a list where
  // every finding is fully expanded cannot be read on a phone.
  const [open, setOpen] = useState(false);
  const severity = (finding.severity || '').toLowerCase();
  const facts = FINDING_FACTS.map((f) => ({ ...f, value: fieldText(finding[f.key]) }))
    .filter((f) => f.value);
  const sections = FINDING_SECTIONS.map((s) => ({ ...s, value: fieldText(finding[s.key]) }))
    .filter((s) => s.value);

  async function toggleDisposition() {
    try {
      await setFindingDisposition(scanId, finding.id, dismissed ? 'open' : 'dismissed');
      onChanged();
    } catch {
      /* the next poll re-reads the truth */
    }
  }

  return (
    <Card style={dismissed ? styles.findingDismissed : styles.finding}>
      <TouchableOpacity
        style={styles.findingHead}
        onPress={() => setOpen(!open)}
        accessibilityRole="button"
        accessibilityState={{ expanded: open }}
      >
        <View
          style={[
            styles.severity,
            { backgroundColor: SEVERITY_COLOR[severity] ?? colors.textMuted },
          ]}
        />
        <Text style={styles.findingTitle} numberOfLines={open ? undefined : 2}>
          {fieldText(finding.title) || finding.id}
        </Text>
        <Ionicons
          name={open ? 'chevron-up' : 'chevron-down'}
          size={18}
          color={colors.textMuted}
        />
      </TouchableOpacity>

      <Text style={styles.severityLabel}>{severity || 'unrated'}</Text>

      {facts.length ? (
        <View style={styles.facts}>
          {facts.map((f) => (
            <Text key={f.key} style={styles.fact}>
              {f.label}: {f.value}
            </Text>
          ))}
        </View>
      ) : null}

      {open ? (
        <View style={styles.findingBody}>
          {sections.map((s) => (
            <View key={s.key} style={styles.findingSection}>
              <Text style={styles.findingHeading}>{s.label}</Text>
              <Text style={s.code ? styles.code : styles.prose}>{s.value}</Text>
            </View>
          ))}
          {sections.length === 0 ? (
            <Text style={styles.empty}>The scanner recorded no further detail.</Text>
          ) : null}
          <Button
            title={dismissed ? 'Restore' : 'Dismiss'}
            onPress={toggleDisposition}
            variant="secondary"
          />
        </View>
      ) : null}
    </Card>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: colors.bg },
  body: { padding: space.md, gap: space.sm },
  head: { gap: space.sm },
  title: { color: colors.text, fontSize: font.size.lg, fontWeight: '700' },
  summary: { color: colors.text, fontSize: font.size.sm, lineHeight: 20 },
  stats: { flexDirection: 'row', flexWrap: 'wrap', gap: space.md },
  stat: { gap: 2 },
  statLabel: { color: colors.textMuted, fontSize: font.size.xs },
  statValue: { color: colors.text, fontSize: font.size.sm },
  address: { color: colors.textMuted, fontSize: font.size.xs },
  sectionTitle: {
    color: colors.text,
    fontSize: font.size.md,
    fontWeight: '700',
    marginTop: space.sm,
  },
  empty: { color: colors.textMuted, fontSize: font.size.sm, lineHeight: 20 },
  finding: { gap: 6 },
  // Card takes a single style object rather than an array, so the dismissed
  // variant carries the base values too.
  findingDismissed: { gap: 6, opacity: 0.55 },
  findingHead: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.sm,
    // 44pt so the expand target is comfortable.
    minHeight: 44,
  },
  severity: { width: 8, height: 8, borderRadius: 4 },
  severityLabel: { color: colors.textMuted, fontSize: font.size.xs },
  findingTitle: { flex: 1, color: colors.text, fontSize: font.size.md, fontWeight: '600' },
  facts: { gap: 2 },
  fact: { color: colors.textMuted, fontSize: font.size.xs },
  findingBody: { gap: space.sm, marginTop: space.sm },
  findingSection: { gap: 4 },
  findingHeading: { color: colors.text, fontSize: font.size.sm, fontWeight: '700' },
  prose: { color: colors.text, fontSize: font.size.sm, lineHeight: 20 },
  code: {
    color: colors.text,
    fontSize: font.size.xs,
    fontFamily: font.mono,
    backgroundColor: colors.bg,
    borderRadius: radius.sm,
    padding: space.sm,
  },
});
