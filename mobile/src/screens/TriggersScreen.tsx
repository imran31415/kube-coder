/** Triggers — webhooks and crons that start builds without you (#250).
 *
 *  Mobile take on the dashboard's /triggers route: both kinds fold into one
 *  newest-first list (src/util/triggers.ts), a segmented control narrows it,
 *  and each row expands to reveal its prompt plus the actions — fire now,
 *  pause/resume (cron), edit, delete. Create/edit happens in a bottom sheet
 *  rather than a side drawer, and a webhook's signing secret is surfaced once
 *  in a copyable panel because the server only ever returns it on write. */
import { Ionicons } from '@expo/vector-icons';
import * as Clipboard from 'expo-clipboard';
import { LinearGradient } from 'expo-linear-gradient';
import React, { useCallback, useMemo, useState } from 'react';
import {
  Alert,
  FlatList,
  KeyboardAvoidingView,
  Modal,
  Platform,
  Pressable,
  RefreshControl,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { useFocusEffect } from '@react-navigation/native';
import {
  cronAction,
  deleteCron,
  deleteWebhook,
  listCrons,
  listWebhooks,
  saveCron,
  saveWebhook,
  testWebhook,
} from '../api/client';
import type { CronRecord, Trigger, TriggerKind, WebhookRecord } from '../api/types';
import { Button, EmptyState, ErrorBanner, Loading, ScreenHeader } from '../components/ui';
import { confirmAction } from '../util/confirm';
import { relativeTime } from '../util/format';
import {
  describeSchedule,
  filterTriggers,
  isValidSchedule,
  isValidTimezone,
  isValidTriggerId,
  mergeTriggers,
  triggerIdHint,
} from '../util/triggers';
import { colors, font, gradients, radius, space } from '../theme';

type Scope = 'all' | 'webhook' | 'cron';
const SCOPES: { key: Scope; label: string }[] = [
  { key: 'all', label: 'All' },
  { key: 'webhook', label: 'Webhooks' },
  { key: 'cron', label: 'Crons' },
];

/** The editor's working copy. `original` is set when editing an existing
 *  trigger — the id then locks, because the server keys on it (a changed id
 *  would silently create a second trigger instead of renaming one). */
interface Draft {
  kind: TriggerKind;
  id: string;
  prompt: string;
  schedule: string;
  timezone: string;
  workdir: string;
  original: Trigger | null;
}

const emptyDraft = (kind: TriggerKind): Draft => ({
  kind,
  id: '',
  prompt: '',
  schedule: '0 9 * * *',
  timezone: 'UTC',
  workdir: '/home/dev',
  original: null,
});

const rowKey = (t: Trigger) => `${t.kind}:${t.id}`;

export default function TriggersScreen() {
  const [webhooks, setWebhooks] = useState<WebhookRecord[] | null>(null);
  const [crons, setCrons] = useState<CronRecord[] | null>(null);
  const [scope, setScope] = useState<Scope>('all');
  const [query, setQuery] = useState('');
  const [openKey, setOpenKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [saving, setSaving] = useState(false);
  // One-time secret reveal after creating/updating a webhook.
  const [secret, setSecret] = useState<{ id: string; secret?: string; url?: string } | null>(null);

  const load = useCallback(async () => {
    try {
      const [w, c] = await Promise.all([listWebhooks(), listCrons()]);
      setWebhooks(w);
      setCrons(c);
      setError(null);
    } catch (e) {
      // A failed load must not masquerade as "no triggers".
      setError((e as Error).message);
      setWebhooks((prev) => prev ?? []);
      setCrons((prev) => prev ?? []);
    }
  }, []);

  // Crons flip suspended/active out from under us (kubectl, the dashboard),
  // so refetch whenever the screen regains focus.
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

  const loaded = webhooks !== null && crons !== null;
  const all = useMemo(
    () => (loaded ? mergeTriggers(webhooks!, crons!) : []),
    [loaded, webhooks, crons],
  );
  const visible = useMemo(() => {
    const scoped = scope === 'all' ? all : all.filter((t) => t.kind === scope);
    return filterTriggers(scoped, query);
  }, [all, scope, query]);

  // ---- row actions ---------------------------------------------------------

  const run = async (t: Trigger, fn: () => Promise<void>, failTitle: string) => {
    setBusyKey(rowKey(t));
    try {
      await fn();
      await load();
    } catch (e) {
      Alert.alert(failTitle, (e as Error).message);
    } finally {
      setBusyKey(null);
    }
  };

  const onFire = (t: Trigger) =>
    run(
      t,
      async () => {
        if (t.kind === 'cron') await cronAction(t.id, 'run');
        else await testWebhook(t.id);
        Alert.alert('Fired', `${t.id} started a build. Watch it under Builds.`);
      },
      'Could not fire',
    );

  const onToggleSuspend = (t: Trigger) =>
    run(t, () => cronAction(t.id, t.suspended ? 'resume' : 'suspend'), 'Could not update');

  const onDelete = (t: Trigger) =>
    confirmAction({
      title: `Delete ${t.id}?`,
      message:
        t.kind === 'cron'
          ? 'The CronJob and its in-cluster Secret are deleted too. This cannot be undone.'
          : 'Anything still posting to this webhook starts getting 404s. This cannot be undone.',
      confirmLabel: 'Delete',
      destructive: true,
      onConfirm: () => {
        void run(t, () => (t.kind === 'cron' ? deleteCron(t.id) : deleteWebhook(t.id)), 'Delete failed');
      },
    });

  const onEdit = (t: Trigger) => {
    const cron = t.kind === 'cron' ? crons?.find((c) => c.id === t.id) : undefined;
    setDraft({
      kind: t.kind,
      id: t.id,
      prompt: t.prompt,
      schedule: cron?.schedule ?? '0 9 * * *',
      timezone: cron?.timezone ?? 'UTC',
      workdir: t.workdir ?? '/home/dev',
      original: t,
    });
  };

  // ---- editor --------------------------------------------------------------

  const draftValid =
    !!draft &&
    isValidTriggerId(draft.kind, draft.id) &&
    draft.prompt.trim().length > 0 &&
    (draft.kind === 'webhook' || (isValidSchedule(draft.schedule) && isValidTimezone(draft.timezone)));

  const onSave = async () => {
    if (!draft || !draftValid) return;
    setSaving(true);
    try {
      if (draft.kind === 'cron') {
        const rec = await saveCron({
          id: draft.id,
          schedule: draft.schedule.trim(),
          prompt_template: draft.prompt.trim(),
          workdir: draft.workdir.trim() || '/home/dev',
          timezone: draft.timezone.trim() || 'UTC',
        });
        setDraft(null);
        if (rec.warning) Alert.alert('Saved with a warning', rec.warning);
      } else {
        const rec = await saveWebhook({
          id: draft.id,
          prompt_template: draft.prompt.trim(),
          workdir: draft.workdir.trim() || '/home/dev',
        });
        setDraft(null);
        // The secret only ever comes back here — show it before it's gone.
        if (rec.hmac_secret_once || rec.receive_url) {
          setSecret({ id: rec.id, secret: rec.hmac_secret_once, url: rec.receive_url });
        }
      }
      await load();
    } catch (e) {
      Alert.alert('Save failed', (e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const copy = async (value: string, what: string) => {
    await Clipboard.setStringAsync(value);
    Alert.alert('Copied', `${what} copied to the clipboard.`);
  };

  // ---- render --------------------------------------------------------------

  const counts = useMemo(
    () => ({
      webhook: all.filter((t) => t.kind === 'webhook').length,
      cron: all.filter((t) => t.kind === 'cron').length,
    }),
    [all],
  );

  return (
    <SafeAreaView style={styles.safe} edges={['top']}>
      <ScreenHeader
        title="Triggers"
        subtitle={
          loaded
            ? `${counts.webhook} webhook${counts.webhook === 1 ? '' : 's'} · ${counts.cron} cron${counts.cron === 1 ? '' : 's'}`
            : 'Webhooks and crons that start builds'
        }
        right={
          <Pressable
            accessibilityRole="button"
            accessibilityLabel="New trigger"
            onPress={() => setDraft(emptyDraft('cron'))}
          >
            <LinearGradient
              colors={gradients.primary}
              start={{ x: 0, y: 0 }}
              end={{ x: 1, y: 1 }}
              style={styles.newBtn}
            >
              <Ionicons name="add" size={18} color={colors.accentText} />
              <Text style={styles.newBtnText}>New</Text>
            </LinearGradient>
          </Pressable>
        }
      />

      {loaded && all.length > 0 ? (
        <>
          <View style={styles.segment}>
            {SCOPES.map((s) => (
              <Pressable
                key={s.key}
                accessibilityRole="button"
                accessibilityState={{ selected: scope === s.key }}
                style={[styles.segItem, scope === s.key && styles.segItemActive]}
                onPress={() => setScope(s.key)}
              >
                <Text style={[styles.segText, scope === s.key && styles.segTextActive]}>{s.label}</Text>
              </Pressable>
            ))}
          </View>

          <View style={styles.searchWrap}>
            <Ionicons name="search" size={16} color={colors.textFaint} />
            <TextInput
              value={query}
              onChangeText={setQuery}
              placeholder="Search triggers…"
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
        </>
      ) : null}

      {error && all.length > 0 ? <ErrorBanner message={error} /> : null}

      {!loaded ? (
        <Loading label="Loading triggers…" />
      ) : all.length === 0 ? (
        error ? (
          <EmptyState icon="cloud-offline-outline" title="Couldn't load triggers" subtitle={error} />
        ) : (
          <View style={styles.emptyWrap}>
            <EmptyState
              icon="flash-outline"
              title="No triggers yet"
              subtitle="Triggers start builds on their own — on a schedule, or when something posts to a webhook."
            />
            <Button
              title="Create a trigger"
              icon="add"
              onPress={() => setDraft(emptyDraft('cron'))}
              style={styles.emptyBtn}
            />
          </View>
        )
      ) : visible.length === 0 ? (
        <EmptyState icon="search-outline" title="No matches" subtitle="Try a different search or scope." />
      ) : (
        <FlatList
          data={visible}
          keyExtractor={rowKey}
          contentContainerStyle={styles.list}
          keyboardShouldPersistTaps="handled"
          keyboardDismissMode="on-drag"
          showsVerticalScrollIndicator={false}
          refreshControl={
            <RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={colors.accent} />
          }
          renderItem={({ item }) => (
            <TriggerRow
              t={item}
              open={openKey === rowKey(item)}
              busy={busyKey === rowKey(item)}
              onToggle={() => setOpenKey(openKey === rowKey(item) ? null : rowKey(item))}
              onFire={() => void onFire(item)}
              onSuspend={() => void onToggleSuspend(item)}
              onEdit={() => onEdit(item)}
              onDelete={() => onDelete(item)}
            />
          )}
        />
      )}

      {/* Create / edit sheet */}
      <Modal visible={draft !== null} animationType="slide" transparent onRequestClose={() => setDraft(null)}>
        <KeyboardAvoidingView
          style={styles.sheetRoot}
          behavior={Platform.OS === 'ios' ? 'padding' : undefined}
        >
          <Pressable style={styles.sheetBackdrop} onPress={() => setDraft(null)} />
          <SafeAreaView style={styles.sheet} edges={['bottom']}>
            <View style={styles.sheetHead}>
              <Text style={styles.sheetTitle}>
                {draft?.original ? `Edit ${draft.original.id}` : 'New trigger'}
              </Text>
              <Pressable hitSlop={10} onPress={() => setDraft(null)} accessibilityLabel="Close">
                <Ionicons name="close" size={22} color={colors.textMuted} />
              </Pressable>
            </View>
            {draft ? (
              <ScrollView
                contentContainerStyle={styles.form}
                keyboardShouldPersistTaps="handled"
                showsVerticalScrollIndicator={false}
              >
                {!draft.original ? (
                  <View style={styles.field}>
                    <Text style={styles.fieldLabel}>Kind</Text>
                    <View style={styles.segment}>
                      {(['cron', 'webhook'] as TriggerKind[]).map((k) => (
                        <Pressable
                          key={k}
                          accessibilityRole="button"
                          accessibilityState={{ selected: draft.kind === k }}
                          style={[styles.segItem, draft.kind === k && styles.segItemActive]}
                          onPress={() => setDraft({ ...draft, kind: k })}
                        >
                          <Text style={[styles.segText, draft.kind === k && styles.segTextActive]}>
                            {k === 'cron' ? 'Cron' : 'Webhook'}
                          </Text>
                        </Pressable>
                      ))}
                    </View>
                    <Text style={styles.hint}>
                      {draft.kind === 'cron'
                        ? 'Runs on a schedule, as a Kubernetes CronJob.'
                        : 'Runs when a signed HTTP POST arrives from another service.'}
                    </Text>
                  </View>
                ) : null}

                <View style={styles.field}>
                  <Text style={styles.fieldLabel}>ID</Text>
                  <TextInput
                    value={draft.id}
                    editable={!draft.original}
                    onChangeText={(v) => setDraft({ ...draft, id: v })}
                    placeholder={draft.kind === 'cron' ? 'nightly-tests' : 'github-ci'}
                    placeholderTextColor={colors.textFaint}
                    autoCapitalize="none"
                    autoCorrect={false}
                    style={[styles.input, !!draft.original && styles.inputLocked]}
                  />
                  {draft.original ? (
                    <Text style={styles.hint}>The id identifies the trigger and can't change.</Text>
                  ) : draft.id && !isValidTriggerId(draft.kind, draft.id) ? (
                    <Text style={styles.errText}>{triggerIdHint(draft.kind)}</Text>
                  ) : (
                    <Text style={styles.hint}>{triggerIdHint(draft.kind)}</Text>
                  )}
                </View>

                <View style={styles.field}>
                  <Text style={styles.fieldLabel}>Prompt template</Text>
                  <TextInput
                    value={draft.prompt}
                    onChangeText={(v) => setDraft({ ...draft, prompt: v })}
                    placeholder="What should the agent do when this fires?"
                    placeholderTextColor={colors.textFaint}
                    multiline
                    style={[styles.input, styles.textarea]}
                  />
                </View>

                {draft.kind === 'cron' ? (
                  <>
                    <View style={styles.field}>
                      <Text style={styles.fieldLabel}>Schedule</Text>
                      <TextInput
                        value={draft.schedule}
                        onChangeText={(v) => setDraft({ ...draft, schedule: v })}
                        placeholder="0 9 * * *"
                        placeholderTextColor={colors.textFaint}
                        autoCapitalize="none"
                        autoCorrect={false}
                        style={styles.input}
                      />
                      {isValidSchedule(draft.schedule) ? (
                        <Text style={styles.hint}>{describeSchedule(draft.schedule)}</Text>
                      ) : (
                        <Text style={styles.errText}>
                          Five fields (minute hour day month weekday) or @daily/@hourly.
                        </Text>
                      )}
                      <View style={styles.presets}>
                        {['@hourly', '0 9 * * *', '30 2 * * *', '0 9 * * 1'].map((p) => (
                          <Pressable
                            key={p}
                            style={styles.preset}
                            onPress={() => setDraft({ ...draft, schedule: p })}
                          >
                            <Text style={styles.presetText}>{p}</Text>
                          </Pressable>
                        ))}
                      </View>
                    </View>

                    <View style={styles.field}>
                      <Text style={styles.fieldLabel}>Timezone</Text>
                      <TextInput
                        value={draft.timezone}
                        onChangeText={(v) => setDraft({ ...draft, timezone: v })}
                        placeholder="UTC"
                        placeholderTextColor={colors.textFaint}
                        autoCapitalize="none"
                        autoCorrect={false}
                        style={styles.input}
                      />
                      {draft.timezone && !isValidTimezone(draft.timezone) ? (
                        <Text style={styles.errText}>An IANA name, like UTC or America/Los_Angeles.</Text>
                      ) : null}
                    </View>
                  </>
                ) : (
                  <Text style={styles.notice}>
                    {draft.original
                      ? 'Saving re-mints the signing secret — the sending service must be updated with the new one.'
                      : 'A signing secret is minted on save and shown once. Unsigned requests are rejected.'}
                  </Text>
                )}

                <View style={styles.field}>
                  <Text style={styles.fieldLabel}>Working directory</Text>
                  <TextInput
                    value={draft.workdir}
                    onChangeText={(v) => setDraft({ ...draft, workdir: v })}
                    placeholder="/home/dev"
                    placeholderTextColor={colors.textFaint}
                    autoCapitalize="none"
                    autoCorrect={false}
                    style={styles.input}
                  />
                </View>

                <View style={styles.formActions}>
                  <Button
                    title="Cancel"
                    variant="secondary"
                    onPress={() => setDraft(null)}
                    style={styles.formBtn}
                  />
                  <Button
                    title={draft.original ? 'Save' : 'Create'}
                    onPress={() => void onSave()}
                    disabled={!draftValid}
                    loading={saving}
                    style={styles.formBtn}
                  />
                </View>
              </ScrollView>
            ) : null}
          </SafeAreaView>
        </KeyboardAvoidingView>
      </Modal>

      {/* One-time webhook secret reveal */}
      <Modal visible={secret !== null} animationType="fade" transparent onRequestClose={() => setSecret(null)}>
        <View style={styles.dialogRoot}>
          <Pressable style={styles.sheetBackdrop} onPress={() => setSecret(null)} />
          <View style={styles.dialog}>
            <Text style={styles.dialogTitle}>Webhook ready</Text>
            <Text style={styles.dialogBody}>
              Copy these into the sending service now — the secret is never shown again.
            </Text>
            {secret?.url ? (
              <>
                <Text style={styles.fieldLabel}>Receive URL</Text>
                <Text style={styles.code} selectable>
                  {secret.url}
                </Text>
                <Button
                  title="Copy URL"
                  icon="copy-outline"
                  variant="secondary"
                  onPress={() => void copy(secret.url!, 'Receive URL')}
                />
              </>
            ) : null}
            {secret?.secret ? (
              <>
                <Text style={styles.fieldLabel}>Signing secret</Text>
                <Text style={styles.code} selectable>
                  {secret.secret}
                </Text>
                <Button
                  title="Copy secret"
                  icon="copy-outline"
                  variant="secondary"
                  onPress={() => void copy(secret.secret!, 'Signing secret')}
                />
              </>
            ) : null}
            <Button title="Done" onPress={() => setSecret(null)} />
          </View>
        </View>
      </Modal>
    </SafeAreaView>
  );
}

function TriggerRow({
  t,
  open,
  busy,
  onToggle,
  onFire,
  onSuspend,
  onEdit,
  onDelete,
}: {
  t: Trigger;
  open: boolean;
  busy: boolean;
  onToggle: () => void;
  onFire: () => void;
  onSuspend: () => void;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const isCron = t.kind === 'cron';
  const paused = isCron && t.suspended;
  const tint = paused ? colors.warning : isCron ? colors.accent : colors.info;
  return (
    <Pressable style={styles.row} onPress={onToggle}>
      <View style={styles.rowTop}>
        <View style={[styles.kindPill, { borderColor: tint + '59' }]}>
          <Ionicons name={isCron ? 'time-outline' : 'flash-outline'} size={12} color={tint} />
          <Text style={[styles.kindText, { color: tint }]}>{paused ? 'cron · paused' : t.kind}</Text>
        </View>
        <Text style={styles.rowId} numberOfLines={1}>
          {t.id}
        </Text>
        <Ionicons name={open ? 'chevron-up' : 'chevron-down'} size={14} color={colors.textFaint} />
      </View>

      <Text style={styles.rowMeta} numberOfLines={1}>
        {isCron
          ? `${describeSchedule(t.schedule ?? '')}${t.timezone ? ` · ${t.timezone}` : ''}`
          : t.unsigned
            ? 'unsigned — the receiver rejects everything'
            : 'signed · HMAC'}
        {t.created_at ? ` · added ${relativeTime(t.created_at)}` : ''}
      </Text>

      <Text style={[styles.rowPrompt, open && styles.rowPromptOpen]} numberOfLines={open ? undefined : 2}>
        {t.prompt}
      </Text>

      {open ? (
        <>
          {isCron && t.schedule ? <Text style={styles.rowSub}>{t.schedule}</Text> : null}
          {t.workdir ? <Text style={styles.rowSub}>{t.workdir}</Text> : null}
          <View style={styles.rowActions}>
            <RowAction icon="play-outline" label="Fire now" onPress={onFire} disabled={busy} />
            {isCron ? (
              <RowAction
                icon={paused ? 'play-circle-outline' : 'pause-outline'}
                label={paused ? 'Resume' : 'Pause'}
                onPress={onSuspend}
                disabled={busy}
              />
            ) : null}
            <RowAction icon="create-outline" label="Edit" onPress={onEdit} disabled={busy} />
            <RowAction icon="trash-outline" label="Delete" onPress={onDelete} disabled={busy} danger />
          </View>
        </>
      ) : null}
    </Pressable>
  );
}

function RowAction({
  icon,
  label,
  onPress,
  disabled,
  danger,
}: {
  icon: keyof typeof Ionicons.glyphMap;
  label: string;
  onPress: () => void;
  disabled?: boolean;
  danger?: boolean;
}) {
  const fg = danger ? colors.danger : colors.text;
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={label}
      disabled={disabled}
      onPress={onPress}
      style={({ pressed }) => [
        styles.rowAction,
        danger && { borderColor: colors.danger + '55' },
        { opacity: disabled ? 0.45 : pressed ? 0.7 : 1 },
      ]}
    >
      <Ionicons name={icon} size={14} color={fg} />
      <Text style={[styles.rowActionText, { color: fg }]}>{label}</Text>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: colors.bg },
  newBtn: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 4,
    paddingHorizontal: space.md,
    paddingVertical: space.sm,
    borderRadius: radius.md,
  },
  newBtnText: { color: colors.accentText, fontWeight: '700', fontSize: font.size.sm },

  segment: {
    flexDirection: 'row',
    marginHorizontal: space.lg,
    marginBottom: space.sm,
    padding: 2,
    backgroundColor: colors.surface2,
    borderRadius: radius.sm,
    borderWidth: 1,
    borderColor: colors.border,
  },
  segItem: { flex: 1, alignItems: 'center', paddingVertical: 7, borderRadius: radius.sm },
  segItemActive: { backgroundColor: colors.accent },
  segText: { color: colors.textMuted, fontSize: font.size.sm, fontWeight: '600' },
  segTextActive: { color: colors.accentText },

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

  emptyWrap: { flex: 1, justifyContent: 'center' },
  emptyBtn: { marginHorizontal: space.xl, marginTop: -space.md, marginBottom: space.xxl },

  list: { paddingBottom: space.xl },
  row: {
    paddingHorizontal: space.lg,
    paddingVertical: space.md,
    borderBottomWidth: 1,
    borderBottomColor: colors.border,
    gap: 4,
  },
  rowTop: { flexDirection: 'row', alignItems: 'center', gap: space.sm },
  kindPill: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 4,
    borderWidth: 1,
    borderRadius: radius.pill,
    backgroundColor: colors.surface2,
    paddingHorizontal: 8,
    paddingVertical: 3,
  },
  kindText: { fontSize: font.size.xs, fontWeight: '600', letterSpacing: 0.3 },
  rowId: { flex: 1, color: colors.text, fontSize: font.size.md, fontWeight: '700', fontFamily: font.mono },
  rowMeta: { color: colors.textFaint, fontSize: font.size.xs },
  rowPrompt: { color: colors.textMuted, fontSize: font.size.sm, lineHeight: 19, marginTop: 2 },
  rowPromptOpen: { color: colors.text },
  rowSub: { color: colors.textFaint, fontSize: font.size.xs, fontFamily: font.mono, marginTop: 2 },
  rowActions: { flexDirection: 'row', flexWrap: 'wrap', gap: space.sm, marginTop: space.sm },
  rowAction: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 5,
    paddingHorizontal: space.md,
    paddingVertical: 7,
    borderRadius: radius.sm,
    borderWidth: 1,
    borderColor: colors.border,
    backgroundColor: colors.surface2,
  },
  rowActionText: { fontSize: font.size.xs, fontWeight: '600' },

  sheetRoot: { flex: 1, justifyContent: 'flex-end' },
  sheetBackdrop: {
    position: 'absolute',
    top: 0,
    left: 0,
    right: 0,
    bottom: 0,
    backgroundColor: 'rgba(0,0,0,0.55)',
  },
  sheet: {
    maxHeight: '88%',
    backgroundColor: colors.bgElevated,
    borderTopLeftRadius: radius.xl,
    borderTopRightRadius: radius.xl,
    borderTopWidth: 1,
    borderColor: colors.border,
  },
  sheetHead: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: space.md,
    padding: space.lg,
    borderBottomWidth: 1,
    borderBottomColor: colors.border,
  },
  sheetTitle: { flex: 1, color: colors.text, fontSize: font.size.lg, fontWeight: '700' },

  form: { padding: space.lg, gap: space.lg, paddingBottom: space.xxl },
  field: { gap: 6 },
  fieldLabel: {
    color: colors.textMuted,
    fontSize: font.size.xs,
    fontWeight: '600',
    textTransform: 'uppercase',
    letterSpacing: 0.7,
  },
  input: {
    backgroundColor: colors.bg,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: colors.border,
    paddingHorizontal: space.md,
    paddingVertical: space.sm,
    color: colors.text,
    fontSize: font.size.md,
    fontFamily: font.mono,
  },
  inputLocked: { color: colors.textFaint, backgroundColor: colors.surface2 },
  textarea: { minHeight: 96, textAlignVertical: 'top' },
  hint: { color: colors.textFaint, fontSize: font.size.xs, lineHeight: 16 },
  errText: { color: colors.danger, fontSize: font.size.xs, lineHeight: 16 },
  notice: {
    color: colors.warning,
    fontSize: font.size.xs,
    lineHeight: 17,
    padding: space.md,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: colors.warning + '44',
    backgroundColor: colors.warning + '12',
  },
  presets: { flexDirection: 'row', flexWrap: 'wrap', gap: 6, marginTop: 2 },
  preset: {
    borderWidth: 1,
    borderColor: colors.border,
    backgroundColor: colors.surface2,
    borderRadius: radius.sm,
    paddingHorizontal: space.sm,
    paddingVertical: 4,
  },
  presetText: { color: colors.textMuted, fontSize: font.size.xs, fontFamily: font.mono },
  formActions: { flexDirection: 'row', gap: space.md, marginTop: space.sm },
  formBtn: { flex: 1 },

  dialogRoot: { flex: 1, alignItems: 'center', justifyContent: 'center', padding: space.xl },
  dialog: {
    width: '100%',
    maxWidth: 400,
    backgroundColor: colors.bgElevated,
    borderRadius: radius.lg,
    borderWidth: 1,
    borderColor: colors.border,
    padding: space.lg,
    gap: space.sm,
  },
  dialogTitle: { color: colors.text, fontSize: font.size.lg, fontWeight: '700' },
  dialogBody: { color: colors.textMuted, fontSize: font.size.sm, lineHeight: 19 },
  code: {
    color: colors.text,
    fontSize: font.size.xs,
    fontFamily: font.mono,
    backgroundColor: colors.bg,
    borderRadius: radius.sm,
    borderWidth: 1,
    borderColor: colors.border,
    padding: space.md,
  },
});
