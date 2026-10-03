/** Security scans (#726) — the list, and starting one from the phone. */
import { Ionicons } from '@expo/vector-icons';
import { useNavigation, useRoute } from '@react-navigation/native';
import React, { useCallback, useEffect, useState } from 'react';
import {
  FlatList,
  RefreshControl,
  StyleSheet,
  Text,
  TextInput,
  TouchableOpacity,
  View,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import {
  ApiError,
  createScan,
  getScanConnection,
  listScanTargets,
  listScans,
} from '../api/client';
import {
  Button,
  Card,
  EmptyState,
  ErrorBanner,
  Label,
  Loading,
  ScreenHeader,
} from '../components/ui';
import type {
  ScanConnection,
  ScanMode,
  ScanSummary,
  ScanTarget,
} from '../api/types';
import type { SecurityNav, SecurityStackParams } from '../navigation';
import { colors, font, radius, space } from '../theme';
import { usePolling } from '../util/usePolling';
import {
  outcomeLabel,
  spendLabel,
  scanAgeLabel,
  parseBudget,
  MODE_LABELS,
} from '../util/scans';

const STATUS_COLOR: Record<string, string> = {
  running: colors.accent,
  done: colors.success,
  stopped: colors.warning,
  interrupted: colors.warning,
  failed: colors.danger,
};

export default function SecurityScreen() {
  const nav = useNavigation<SecurityNav>();
  const [scans, setScans] = useState<ScanSummary[] | null>(null);
  const [targets, setTargets] = useState<ScanTarget[]>([]);
  const [connection, setConnection] = useState<ScanConnection | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [composing, setComposing] = useState(false);
  const [disabled, setDisabled] = useState(false);
  const route = useRoute();
  const startPort = (route.params as SecurityStackParams['ScanList'])?.startPort;

  const load = useCallback(async () => {
    try {
      const [list, tgts, conn] = await Promise.all([
        listScans(),
        listScanTargets(),
        getScanConnection(),
      ]);
      setScans(list);
      setTargets(tgts);
      setConnection(conn);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
      // `code` is the server's stable signal (see ApiError) -- the message
      // text this used to regex can be reworded at any time, and when it is,
      // the dedicated "switched off" state silently degrades into the
      // misleading "No scans yet" empty state below.
      if (e instanceof ApiError && e.code === 'disabled') setDisabled(true);
      setScans((prev) => prev ?? null);
    }
  }, []);

  // A scan runs for minutes to hours and rewrites its findings as it goes, so
  // the list is worth refreshing while this tab is in front — but no faster
  // than a person would notice.
  usePolling(load, 10000);

  // Open the start form on the app whose Scan button was pressed.
  useEffect(() => {
    if (startPort !== undefined) setComposing(true);
  }, [startPort]);

  const onRefresh = async () => {
    setRefreshing(true);
    await load();
    setRefreshing(false);
  };


  return (
    <SafeAreaView style={styles.safe} edges={['top']}>
      <ScreenHeader title="Security" subtitle="Check an app for security holes" />

      {disabled ? (
        <EmptyState
          title="Scanning is switched off here"
          subtitle="Scanning runs its tools in a container, so it has to be turned on when the workspace is deployed."
        />
      ) : (
        <>
          {error && scans && scans.length > 0 ? (
            <ErrorBanner message={error} />
          ) : null}

          {connection && !connection.configured ? (
            <Card style={styles.notice}>
              <Text style={styles.noticeTitle}>Connect a model first</Text>
              <Text style={styles.noticeBody}>
                Scanning is done by an AI model you choose and pay for. Set it up
                on the Security page in the dashboard, or in the workspace
                terminal.
              </Text>
            </Card>
          ) : composing ? (
            <StartScanForm
              targets={targets}
              startPort={startPort}
              onCancel={() => setComposing(false)}
              onStarted={async (id) => {
                setComposing(false);
                await load();
                nav.navigate('ScanDetail', { id });
              }}
            />
          ) : (
            <View style={styles.startRow}>
              <Button
                title="New scan"
                onPress={() => setComposing(true)}
                disabled={targets.length === 0}
              />
              {targets.length === 0 ? (
                <Text style={styles.hint}>Start an app first.</Text>
              ) : null}
            </View>
          )}

          {scans === null && error ? (
            /* An empty list after a FAILED read used to render as "No scans
               yet — Scan an app you are running to see what an intruder
               would find.", with the error invisible. On this surface an
               error must never be able to read as an all-clear. Same shape
               AppsScreen already uses. */
            <EmptyState
              icon="cloud-offline-outline"
              title="Couldn't load scans"
              subtitle={error}
            />
          ) : scans === null ? (
            <Loading label="Loading scans" />
          ) : scans.length === 0 ? (
            <EmptyState
              title="No scans yet"
              subtitle="Scan an app you are running to see what an intruder would find."
            />
          ) : (
            <FlatList
              data={scans}
              keyExtractor={(s) => s.id}
              contentContainerStyle={styles.list}
              refreshControl={
                <RefreshControl refreshing={refreshing} onRefresh={onRefresh} />
              }
              renderItem={({ item }) => (
                <ScanRow scan={item} onPress={() => nav.navigate('ScanDetail', { id: item.id })} />
              )}
            />
          )}
        </>
      )}
    </SafeAreaView>
  );
}

function ScanRow({ scan, onPress }: { scan: ScanSummary; onPress: () => void }) {
  return (
    <TouchableOpacity style={styles.row} onPress={onPress} accessibilityRole="button">
      <View style={styles.rowMain}>
        <View style={styles.rowTitleLine}>
          <View
            style={[styles.dot, { backgroundColor: STATUS_COLOR[scan.status] ?? colors.textMuted }]}
          />
          <Text style={styles.rowTitle} numberOfLines={1}>
            {scan.target.name || `Port ${scan.target.port}`}
          </Text>
        </View>
        {/* Outcome, never a bare count: an empty findings list means something
            completely different depending on how the scan ended. */}
        <Text style={styles.rowOutcome}>{outcomeLabel(scan)}</Text>
        <Text style={styles.rowMeta}>
          {scan.mode} · {spendLabel(scan)} · {scanAgeLabel(scan.started_at)}
        </Text>
      </View>
      <Ionicons name="chevron-forward" size={18} color={colors.textMuted} />
    </TouchableOpacity>
  );
}

function StartScanForm({
  targets,
  startPort,
  onCancel,
  onStarted,
}: {
  targets: ScanTarget[];
  startPort?: number;
  onCancel: () => void;
  onStarted: (id: string) => void;
}) {
  // The app the Scan button was pressed on wins over "first reachable".
  const [port, setPort] = useState<number>(
    (startPort !== undefined && targets.some((t) => t.port === startPort)
      ? startPort
      : (targets.find((t) => t.reachable) ?? targets[0])?.port) ?? 0,
  );
  const [mode, setMode] = useState<ScanMode>('quick');
  const [budget, setBudget] = useState('5');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const chosen = targets.find((t) => t.port === port) ?? null;
  const parsedBudget = parseBudget(budget);
  const budgetInvalid = parsedBudget === 'invalid';

  async function start() {
    if (parsedBudget === 'invalid') return;
    setBusy(true);
    setErr(null);
    try {
      const id = await createScan({
        port,
        mode,
        budget_usd: parsedBudget,
      });
      onStarted(id);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card style={styles.form}>
      <Label>App</Label>
      <View style={styles.choices}>
        {targets.map((t) => (
          <TouchableOpacity
            key={t.port}
            style={[styles.choice, port === t.port && styles.choiceOn]}
            onPress={() => setPort(t.port)}
          >
            <Text style={[styles.choiceText, port === t.port && styles.choiceTextOn]}>
              {t.name || `Port ${t.port}`}
            </Text>
          </TouchableOpacity>
        ))}
      </View>
      {chosen && !chosen.reachable ? (
        <Text style={styles.warn}>{chosen.reason}</Text>
      ) : null}

      <Label>How deep</Label>
      <View style={styles.choices}>
        {(Object.keys(MODE_LABELS) as ScanMode[]).map((m) => (
          <TouchableOpacity
            key={m}
            style={[styles.choice, mode === m && styles.choiceOn]}
            onPress={() => setMode(m)}
          >
            <Text style={[styles.choiceText, mode === m && styles.choiceTextOn]}>
              {MODE_LABELS[m]}
            </Text>
          </TouchableOpacity>
        ))}
      </View>

      <Label>Spending limit (US$)</Label>
      <TextInput
        style={styles.input}
        value={budget}
        onChangeText={setBudget}
        keyboardType="decimal-pad"
        placeholder="No limit"
        placeholderTextColor={colors.textMuted}
      />
      <Text style={budgetInvalid || budget.trim() === '' ? styles.warn : styles.hint}>
        {budgetInvalid
          ? 'Enter an amount like 5 or 2.50, or clear the field for no limit.'
          : budget.trim() === ''
            ? 'With no limit the scan runs until it finishes, and spends accordingly.'
            : 'The scan stops cleanly when it reaches this much.'}
      </Text>

      {err ? <ErrorBanner message={err} /> : null}

      <View style={styles.formActions}>
        <Button
          title={busy ? 'Starting…' : 'Start scan'}
          onPress={start}
          disabled={busy || !port || budgetInvalid}
        />
        <Button title="Cancel" onPress={onCancel} variant="secondary" />
      </View>
      <Text style={styles.fineprint}>
        The scan sends real attack traffic to the app you pick. Only scan
        something you own.
      </Text>
    </Card>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: colors.bg },
  list: { padding: space.md, gap: space.sm },
  notice: { margin: space.md, gap: 6 },
  noticeTitle: { color: colors.text, fontSize: font.size.md, fontWeight: '700' },
  noticeBody: { color: colors.textMuted, fontSize: font.size.sm, lineHeight: 20 },
  startRow: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.sm,
    paddingHorizontal: space.md,
    paddingBottom: space.sm,
  },
  hint: { color: colors.textMuted, fontSize: font.size.sm, flexShrink: 1 },
  warn: { color: colors.warning, fontSize: font.size.sm, lineHeight: 19 },
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.sm,
    // 44pt keeps the whole row a comfortable touch target.
    minHeight: 44,
    padding: space.md,
    borderRadius: radius.md,
    backgroundColor: colors.bgElevated,
  },
  rowMain: { flex: 1, gap: 4 },
  rowTitleLine: { flexDirection: 'row', alignItems: 'center', gap: 8 },
  dot: { width: 8, height: 8, borderRadius: 4 },
  rowTitle: { color: colors.text, fontSize: font.size.md, fontWeight: '600', flexShrink: 1 },
  rowOutcome: { color: colors.text, fontSize: font.size.sm },
  rowMeta: { color: colors.textMuted, fontSize: font.size.xs },
  form: { margin: space.md, gap: space.sm },
  choices: { flexDirection: 'row', flexWrap: 'wrap', gap: 8 },
  choice: {
    minHeight: 44,
    justifyContent: 'center',
    paddingHorizontal: space.md,
    borderRadius: radius.sm,
    borderWidth: 1,
    borderColor: colors.border,
  },
  choiceOn: { borderColor: colors.accent, backgroundColor: colors.bgElevated },
  choiceText: { color: colors.textMuted, fontSize: font.size.sm },
  choiceTextOn: { color: colors.text },
  input: {
    minHeight: 44,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.sm,
    paddingHorizontal: space.md,
    color: colors.text,
    fontSize: font.size.md,
  },
  formActions: { flexDirection: 'row', gap: space.sm, marginTop: space.sm },
  fineprint: { color: colors.textMuted, fontSize: font.size.xs, lineHeight: 17 },
});
