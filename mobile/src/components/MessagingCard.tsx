/** Settings card for the WhatsApp messaging gateway (issue #330 · mobile parity).
 *  Mirrors the web dashboard's MessagingSection and ProviderKeysCard: a
 *  data-driven provider form (fields come from the registry catalog), masked
 *  credential status, test-connection, a webhook helper, and pairing-code link
 *  management. Talks to the same /api/gateway/* endpoints as the web app. */
import { Ionicons } from '@expo/vector-icons';
import * as Clipboard from 'expo-clipboard';
import React, { useEffect, useRef, useState } from 'react';
import {
  KeyboardAvoidingView,
  Linking,
  Modal,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import {
  getGatewayProviders,
  getGatewayCredentials,
  putGatewayCredentials,
  deleteGatewayCredentials,
  testGatewayConnection,
  createGatewayLink,
  listGatewayLinks,
  deleteGatewayLink,
} from '../api/client';
import type {
  GatewayProviderSpec,
  GatewayCredentialField,
  GatewayCredentialsView,
  GatewayLink,
  GatewayPairingCode,
} from '../api/types';
import { useConfig } from '../store/useConfig';
import { colors, font, radius, space } from '../theme';
import { confirmAction } from '../util/confirm';
import { Button, Card, Label } from './ui';

function dialableSender(view: GatewayCredentialsView | null): string | null {
  if (!view || !view.sender_field) return null;
  const raw = view.fields[view.sender_field]?.value || '';
  if (!(raw.startsWith('whatsapp:') || raw.startsWith('+'))) return null;
  const digits = raw.replace(/^whatsapp:/, '').replace(/[^\d]/g, '');
  return digits.length >= 6 ? digits : null;
}

function fmt(sec: number): string {
  const m = Math.floor(sec / 60);
  return `${m}:${String(sec % 60).padStart(2, '0')}`;
}

export function MessagingCard({ readOnly }: { readOnly?: boolean }) {
  const cfg = useConfig();
  const [providers, setProviders] = useState<GatewayProviderSpec[] | null>(null);
  const [cred, setCred] = useState<GatewayCredentialsView | null>(null);
  const [providerId, setProviderId] = useState('');
  const [links, setLinks] = useState<GatewayLink[]>([]);
  const [editing, setEditing] = useState(false);
  const [testResult, setTestResult] = useState<{ ok: boolean; detail: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [code, setCode] = useState<GatewayPairingCode | null>(null);
  const [remaining, setRemaining] = useState(0);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  const spec = providers?.find((p) => p.id === providerId) ?? null;
  const configured = !!cred?.configured;

  async function refresh() {
    try {
      const [pr, cr] = await Promise.all([getGatewayProviders(), getGatewayCredentials()]);
      setProviders(pr);
      setCred(cr);
      setProviderId(cr.provider_id || pr[0]?.id || '');
    } catch {
      setProviders([]); // renders "unavailable" gracefully
    }
  }
  async function refreshLinks() {
    try {
      setLinks(await listGatewayLinks());
    } catch {
      /* leave prior */
    }
  }
  useEffect(() => {
    void refresh();
    void refreshLinks();
    return () => {
      if (timer.current) clearInterval(timer.current);
    };
  }, []);

  useEffect(() => {
    if (timer.current) clearInterval(timer.current);
    if (!code) return;
    timer.current = setInterval(() => {
      setRemaining((r) => {
        if (r <= 1) {
          if (timer.current) clearInterval(timer.current);
          setCode(null);
          return 0;
        }
        return r - 1;
      });
    }, 1000);
    return () => {
      if (timer.current) clearInterval(timer.current);
    };
  }, [code]);

  async function copy(text: string) {
    try {
      await Clipboard.setStringAsync(text);
    } catch {
      /* clipboard unavailable */
    }
  }

  async function onTest() {
    setBusy(true);
    setTestResult(null);
    try {
      setTestResult(await testGatewayConnection());
    } catch (e) {
      setTestResult({ ok: false, detail: (e as Error).message });
    } finally {
      setBusy(false);
    }
  }

  function onDisconnect() {
    confirmAction({
      title: 'Disconnect messaging?',
      message: 'Clears your stored provider credentials and disables the WhatsApp channel.',
      confirmLabel: 'Disconnect',
      destructive: true,
      onConfirm: async () => {
        await deleteGatewayCredentials();
        setTestResult(null);
        await refresh();
      },
    });
  }

  async function onLink() {
    setBusy(true);
    try {
      const c = await createGatewayLink();
      setCode(c);
      setRemaining(c.expires_in || 600);
      await refreshLinks();
    } catch {
      /* surface nothing — button just no-ops on failure */
    } finally {
      setBusy(false);
    }
  }

  function onUnlink(id: string) {
    confirmAction({
      title: 'Unlink this number?',
      message: 'It will stop reaching this workspace until it links again.',
      confirmLabel: 'Unlink',
      destructive: true,
      onConfirm: async () => {
        await deleteGatewayLink(id);
        await refreshLinks();
      },
    });
  }

  const webhookUrl = cfg.host ? `${cfg.host.replace(/\/$/, '')}/api/gateway/whatsapp/webhook` : '';
  const verifyToken = spec?.credential_fields.some((f) => f.key === 'verify_token')
    ? cred?.fields['verify_token']?.value || ''
    : '';
  const sender = dialableSender(cred);

  if (providers && providers.length === 0) {
    return (
      <Card style={{ gap: space.md, marginTop: space.lg }}>
        <Label>Messaging / WhatsApp</Label>
        <Text style={styles.help}>The messaging gateway is not available on this workspace.</Text>
      </Card>
    );
  }

  return (
    <Card style={{ gap: space.md, marginTop: space.lg }}>
      <Label>Messaging / WhatsApp</Label>
      <Text style={styles.help}>
        Connect your own WhatsApp provider to chat with this workspace over WhatsApp. Credentials are
        stored securely on your workspace disk.
      </Text>

      {/* Provider chooser */}
      {providers ? (
        <View style={styles.chips}>
          {providers.map((p) => (
            <Pressable
              key={p.id}
              onPress={() => { setProviderId(p.id); setTestResult(null); }}
              style={[styles.chip, providerId === p.id && styles.chipActive]}
            >
              <Text style={[styles.chipText, providerId === p.id && styles.chipTextActive]}>
                {p.display_name}
              </Text>
            </Pressable>
          ))}
        </View>
      ) : (
        <Text style={styles.help}>Loading…</Text>
      )}

      {/* Masked field status */}
      {spec
        ? [...spec.credential_fields, spec.sender_field].map((f) => {
            const st = cred?.provider_id === spec.id ? cred?.fields[f.key] : undefined;
            const isSet = !!st?.set;
            const shown = f.secret ? (isSet ? `set · ${st?.hint || '••••'}` : 'not set') : st?.value || 'not set';
            return (
              <View key={f.key} style={styles.row}>
                <Text style={styles.fieldLabel}>{f.label}</Text>
                <Text style={[styles.fieldState, { color: isSet ? colors.success : colors.textFaint }]}>
                  {shown}
                </Text>
              </View>
            );
          })
        : null}

      {/* Status + actions */}
      <View style={styles.statusRow}>
        <Text style={[styles.state, { color: configured ? colors.success : colors.textFaint }]}>
          {configured ? 'Connected' : 'Not configured'}
        </Text>
      </View>
      {!readOnly && spec ? (
        <>
          <Button title="Configure credentials" icon="key-outline" onPress={() => setEditing(true)} />
          <Button
            title="Test connection"
            variant="secondary"
            onPress={onTest}
            loading={busy}
            disabled={!configured}
            style={{ marginTop: space.sm }}
          />
          {testResult ? (
            <Text style={{ color: testResult.ok ? colors.success : colors.danger, fontSize: font.size.sm }}>
              {testResult.ok ? 'Connection ok' : 'Failed'} · {testResult.detail}
            </Text>
          ) : null}
        </>
      ) : null}

      {/* Webhook helper */}
      {spec ? (
        <>
          <View style={styles.copyRow}>
            <View style={{ flex: 1 }}>
              <Label>Webhook URL</Label>
              <Text style={styles.mono} numberOfLines={1}>{webhookUrl}</Text>
            </View>
            <Pressable onPress={() => copy(webhookUrl)} hitSlop={8} accessibilityLabel="Copy webhook URL">
              <Ionicons name="copy-outline" size={20} color={colors.textMuted} />
            </Pressable>
          </View>
          {verifyToken ? (
            <View style={styles.copyRow}>
              <View style={{ flex: 1 }}>
                <Label>Verify token</Label>
                <Text style={styles.mono} numberOfLines={1}>{verifyToken}</Text>
              </View>
              <Pressable onPress={() => copy(verifyToken)} hitSlop={8} accessibilityLabel="Copy verify token">
                <Ionicons name="copy-outline" size={20} color={colors.textMuted} />
              </Pressable>
            </View>
          ) : null}
        </>
      ) : null}

      {/* Link management */}
      {!readOnly ? (
        <Button title="Link WhatsApp" icon="link-outline" onPress={onLink} loading={busy} style={{ marginTop: space.sm }} />
      ) : null}
      {code ? (
        <View style={styles.codeBox}>
          <Text style={styles.codeText}>{code.code}</Text>
          <Text style={styles.help}>expires in {fmt(remaining)}</Text>
          {sender ? (
            <Button
              title="Open in WhatsApp"
              variant="secondary"
              icon="logo-whatsapp"
              onPress={() => void Linking.openURL(`https://wa.me/${sender}?text=${encodeURIComponent(code.code)}`).catch(() => {})}
              style={{ marginTop: space.sm }}
            />
          ) : (
            <Text style={styles.help}>Message your WhatsApp Business number with this code to link it.</Text>
          )}
        </View>
      ) : null}

      {/* Bindings */}
      {links.length > 0 ? (
        <View style={{ gap: space.sm }}>
          <Label>Linked numbers</Label>
          {links.flatMap((l) =>
            l.bindings.map((b) => (
              <View key={`${l.id}-${b.workspace}`} style={styles.linkRow}>
                <View style={{ flex: 1 }}>
                  <Text style={styles.fieldLabel}>{b.workspace}</Text>
                  <Text style={styles.help} numberOfLines={1}>{b.workspace_host || '—'}</Text>
                </View>
                {!readOnly ? (
                  <Pressable onPress={() => onUnlink(l.id)} hitSlop={8} accessibilityLabel="Unlink">
                    <Ionicons name="trash-outline" size={20} color={colors.danger} />
                  </Pressable>
                ) : null}
              </View>
            )),
          )}
        </View>
      ) : null}

      <MessagingEditModal
        visible={editing}
        spec={spec}
        cred={cred}
        onClose={() => setEditing(false)}
        onDone={async () => { setEditing(false); setTestResult(null); await refresh(); }}
        onDisconnect={onDisconnect}
      />
    </Card>
  );
}

function MessagingEditModal({
  visible,
  spec,
  cred,
  onClose,
  onDone,
  onDisconnect,
}: {
  visible: boolean;
  spec: GatewayProviderSpec | null;
  cred: GatewayCredentialsView | null;
  onClose: () => void;
  onDone: () => void;
  onDisconnect: () => void;
}) {
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Seed non-secret fields from the redacted view whenever the sheet opens.
  useEffect(() => {
    if (!visible || !spec) return;
    const next: Record<string, string> = {};
    if (cred?.provider_id === spec.id) {
      for (const f of [...spec.credential_fields, spec.sender_field]) {
        if (!f.secret) next[f.key] = cred.fields[f.key]?.value ?? '';
      }
    }
    setDrafts(next);
    setError(null);
  }, [visible, spec?.id]);

  const configured = cred?.provider_id === spec?.id && !!cred?.configured;

  async function save() {
    if (!spec) return;
    setBusy(true);
    setError(null);
    try {
      const creds: Record<string, string> = {};
      for (const f of spec.credential_fields) creds[f.key] = drafts[f.key] ?? '';
      await putGatewayCredentials({
        provider_id: spec.id,
        creds,
        sender_number: drafts[spec.sender_field.key] ?? '',
      });
      onDone();
    } catch (e) {
      setError(`Couldn't save: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  const fields = spec ? [...spec.credential_fields, spec.sender_field] : [];

  return (
    <Modal visible={visible} animationType="slide" transparent onRequestClose={onClose}>
      <View style={styles.backdrop}>
        <KeyboardAvoidingView behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
          <View style={styles.sheet}>
            <View style={styles.head}>
              <Text style={styles.title}>{spec?.display_name} credentials</Text>
              <Pressable onPress={onClose} hitSlop={8} accessibilityLabel="Close">
                <Ionicons name="close" size={22} color={colors.textMuted} />
              </Pressable>
            </View>
            <ScrollView keyboardShouldPersistTaps="handled">
              {fields.map((f: GatewayCredentialField) => {
                const st = cred?.provider_id === spec?.id ? cred?.fields[f.key] : undefined;
                return (
                  <View key={f.key} style={{ marginTop: space.md }}>
                    <Label>{f.label}</Label>
                    <TextInput
                      value={drafts[f.key] ?? ''}
                      onChangeText={(v) => setDrafts((d) => ({ ...d, [f.key]: v }))}
                      placeholder={f.secret ? (st?.set ? 'Replace…' : f.placeholder || 'Paste…') : f.placeholder}
                      placeholderTextColor={colors.textFaint}
                      autoCapitalize="none"
                      autoCorrect={false}
                      secureTextEntry={f.secret}
                      style={styles.input}
                    />
                  </View>
                );
              })}
              {error ? <Text style={styles.error}>{error}</Text> : null}
              <Button title="Save credentials" onPress={save} loading={busy} style={{ marginTop: space.lg }} />
              {configured ? (
                <Button
                  title="Disconnect"
                  variant="danger"
                  icon="trash-outline"
                  onPress={() => { onClose(); onDisconnect(); }}
                  disabled={busy}
                  style={{ marginTop: space.sm }}
                />
              ) : null}
            </ScrollView>
          </View>
        </KeyboardAvoidingView>
      </View>
    </Modal>
  );
}

const styles = StyleSheet.create({
  help: { color: colors.textMuted, fontSize: font.size.sm, lineHeight: 19 },
  chips: { flexDirection: 'row', flexWrap: 'wrap', gap: space.sm },
  chip: {
    paddingHorizontal: space.md,
    paddingVertical: space.sm,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: colors.border,
    backgroundColor: colors.card,
  },
  chipActive: { borderColor: colors.accent, backgroundColor: colors.accent },
  chipText: { color: colors.text, fontSize: font.size.sm, fontWeight: '600' },
  chipTextActive: { color: colors.accentText },
  row: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', gap: space.md },
  fieldLabel: { color: colors.text, fontSize: font.size.md },
  fieldState: { fontSize: font.size.xs, fontWeight: '700', letterSpacing: 0.3 },
  statusRow: { flexDirection: 'row', alignItems: 'center' },
  state: { fontSize: font.size.xs, fontWeight: '700', textTransform: 'uppercase', letterSpacing: 0.5 },
  copyRow: { flexDirection: 'row', alignItems: 'center', gap: space.md },
  mono: { color: colors.text, fontSize: font.size.sm, fontFamily: font.mono },
  codeBox: { alignItems: 'center', gap: 4, padding: space.md, backgroundColor: colors.card, borderRadius: radius.md },
  codeText: { color: colors.text, fontSize: font.size.xl, fontWeight: '800', letterSpacing: 4, fontFamily: font.mono },
  linkRow: { flexDirection: 'row', alignItems: 'center', gap: space.md },
  backdrop: { flex: 1, backgroundColor: '#000a', justifyContent: 'flex-end' },
  sheet: {
    backgroundColor: colors.bgElevated,
    borderTopLeftRadius: radius.xl,
    borderTopRightRadius: radius.xl,
    padding: space.xl,
    paddingBottom: space.xxl,
    maxHeight: '85%',
  },
  head: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' },
  title: { color: colors.text, fontSize: font.size.lg, fontWeight: '800' },
  input: {
    marginTop: space.xs,
    backgroundColor: colors.card,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.md,
    paddingHorizontal: space.md,
    paddingVertical: space.md,
    color: colors.text,
    fontSize: font.size.md,
  },
  error: { color: colors.danger, fontSize: font.size.sm, marginTop: space.md },
});
