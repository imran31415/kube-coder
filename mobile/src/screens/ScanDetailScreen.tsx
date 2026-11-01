/** One security scan (#726) — live progress, then its findings. */
import { Ionicons } from '@expo/vector-icons';
import type { RouteProp } from '@react-navigation/native';
import { useRoute } from '@react-navigation/native';
import React, { useCallback, useEffect, useState } from 'react';
import { ScrollView, StyleSheet, Text, TouchableOpacity, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { getScan, setFindingDisposition, stopScan } from '../api/client';
import { Button, Card, ErrorBanner, Loading } from '../components/ui';
import type { Finding, ScanDetail, ScanReport } from '../api/types';
import type { SecurityStackParams } from '../navigation';
import { colors, font, radius, space } from '../theme';
import { usePolling } from '../util/usePolling';
import {
  FINDING_FACTS,
  FINDING_SECTIONS,
  emptyFindingsText,
  fieldText,
  isLiveScan,
  scanTitle,
  sortFindings,
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
  usePolling(load, 6000, ['scan.']);

  // Clear the previous scan the moment the route is re-pointed at a new one.
  // A push tapped while this screen is already mounted updates `params` on
  // the existing route key rather than mounting a fresh screen, and
  // usePolling's effect is keyed on the interval only -- so the screen kept
  // rendering the old scan's status, spend and findings for up to 6s while
  // `id` had already moved, and a "Stop scan" tap in that window acted on
  // the NEW scan while describing the old one.
  useEffect(() => {
    setScan(null);
    setError(null);
    void load();
  }, [id, load]);

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
  const caveats = scan.caveats ?? [];

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
        {/* Name, state, problems — what fits a phone. The address the scanner
            used is an internal container address and is left off. */}
        <Card style={styles.head}>
          <View style={styles.titleRow}>
            <Text style={styles.title} numberOfLines={2}>
              {scanTitle(scan)}
            </Text>
            {live ? (
              <View style={styles.liveBadge} accessibilityLabel="Scanning">
                <View style={styles.liveDot} />
                <Text style={styles.liveText}>Scanning</Text>
              </View>
            ) : (
              <Text style={styles.status}>{scan.status}</Text>
            )}
          </View>
          {/* Never a bare count: a finished scan's sentence says what was
              actually done. While it runs, the badge already says so. */}
          {live ? null : <Text style={styles.summary}>{scan.summary}</Text>}
          {/* Inline, not an ErrorBanner: the banner is for a request that
              failed, and clips to two lines -- the reason is the answer. */}
          {scan.error ? (
            <View
              style={[
                styles.reason,
                scan.status === 'failed' ? styles.reasonFailed : null,
              ]}
            >
              <Text style={styles.reasonLabel}>Why it ended</Text>
              <Text style={styles.reasonText}>{scan.error}</Text>
            </View>
          ) : null}
          {/* The scanner's own account of what it never reached. Above the
              problems list, because an empty list under this means "not
              checked", not "clean". */}
          {caveats.length ? (
            <View style={styles.reason}>
              <Text style={styles.reasonLabel}>Not all of the app was checked</Text>
              {caveats.map((c) => (
                <Text key={c} style={styles.reasonText}>
                  {'• '}
                  {c}
                </Text>
              ))}
            </View>
          ) : null}
          {scan.budget_note ? (
            <Text style={styles.budgetNote}>{scan.budget_note}</Text>
          ) : null}
          {live ? (
            <Button
              title={stopping ? 'Stopping…' : 'Stop scan'}
              onPress={stop}
              disabled={stopping}
              variant="danger"
            />
          ) : null}
        </Card>

        <Text style={styles.sectionTitle}>
          Problems found{scan.counts?.total ? ` (${scan.counts.total})` : ''}
        </Text>

        {findings.length === 0 ? (
          <Text style={styles.empty}>{emptyFindingsText(scan)}</Text>
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

        {scan.report ? <ReportCard report={scan.report} /> : null}
      </ScrollView>
    </SafeAreaView>
  );
}

/**
 * The scanner's write-up — the result the scan was run for.
 *
 * Collapsed to begin with: a report runs to several screens, and a phone
 * that opens on it buries the problems list above. Shown as the scanner
 * wrote it, headings and all, rather than reformatted into something it did
 * not say. The file is named underneath because it is saved on the workspace
 * and outlives this screen.
 */
function ReportCard({ report }: { report: ScanReport }) {
  const [open, setOpen] = useState(false);
  return (
    <Card style={styles.report}>
      <TouchableOpacity
        style={styles.reportHead}
        onPress={() => setOpen(!open)}
        accessibilityRole="button"
        accessibilityState={{ expanded: open }}
      >
        <Text style={styles.reportTitle}>What the scanner reported</Text>
        <Ionicons
          name={open ? 'chevron-up' : 'chevron-down'}
          size={18}
          color={colors.textMuted}
        />
      </TouchableOpacity>
      {open ? (
        <>
          <Text style={styles.reportBody} selectable>
            {report.text}
          </Text>
          {report.location ? (
            <Text style={styles.reportFile} selectable>
              Saved in this workspace as {report.location}
            </Text>
          ) : null}
        </>
      ) : null}
    </Card>
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
    <Card
      style={dismissed ? styles.findingDismissed : styles.finding}
      accent={dismissed ? undefined : colors.danger}
    >
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
  titleRow: { flexDirection: 'row', alignItems: 'center', gap: space.sm },
  title: { flex: 1, color: colors.text, fontSize: font.size.lg, fontWeight: '700' },
  liveBadge: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 6,
    paddingVertical: 3,
    paddingHorizontal: 10,
    borderRadius: 999,
    borderWidth: 1,
    borderColor: colors.infoBorder,
    backgroundColor: colors.infoSoft,
  },
  liveDot: { width: 7, height: 7, borderRadius: 4, backgroundColor: colors.info },
  liveText: { color: colors.info, fontSize: font.size.xs, fontWeight: '600' },
  status: { color: colors.textMuted, fontSize: font.size.xs },
  summary: { color: colors.text, fontSize: font.size.sm, lineHeight: 20 },
  reason: {
    gap: 2,
    paddingLeft: space.sm,
    borderLeftWidth: 2,
    borderLeftColor: colors.warning,
  },
  reasonFailed: { borderLeftColor: colors.danger },
  reasonLabel: { color: colors.textMuted, fontSize: font.size.xs, fontWeight: '600' },
  reasonText: { color: colors.text, fontSize: font.size.sm, lineHeight: 20 },
  budgetNote: { color: colors.textMuted, fontSize: font.size.xs, lineHeight: 18 },
  report: { gap: space.sm, marginTop: space.sm },
  reportHead: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.sm,
    // 44pt so the expand target is comfortable.
    minHeight: 44,
  },
  reportTitle: { flex: 1, color: colors.text, fontSize: font.size.md, fontWeight: '700' },
  reportBody: { color: colors.text, fontSize: font.size.sm, lineHeight: 21 },
  reportFile: { color: colors.textMuted, fontSize: font.size.xs, lineHeight: 18 },
  sectionTitle: {
    color: colors.text,
    fontSize: font.size.md,
    fontWeight: '700',
    marginTop: space.sm,
  },
  empty: { color: colors.textMuted, fontSize: font.size.sm, lineHeight: 20 },
  // A problem reads as one: a soft red wash and edge (the edge comes from
  // Card's `accent`). Dismissed drops back to a plain, faded card.
  finding: {
    gap: 6,
    backgroundColor: colors.dangerSoft,
    borderColor: colors.dangerBorder,
  },
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
