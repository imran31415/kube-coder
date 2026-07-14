/** Settings card for the workspace's git identity + SSH key. Mirrors the web
 *  dashboard's GitSection (charts/workspace/web/src/routes/settings/GitSection):
 *  shows SSH-key / gh-CLI status pills, edits git user.name + user.email, and
 *  generates an ed25519 key whose public half can be copied into GitHub.
 *  Backend: /api/github/status, POST /api/github/config, POST /api/github/ssh/generate. */
import { Ionicons } from '@expo/vector-icons';
import * as Clipboard from 'expo-clipboard';
import React, { useEffect, useState } from 'react';
import { Pressable, StyleSheet, Text, TextInput, View } from 'react-native';
import { getGithubStatus, setGitConfig, generateSshKey, type GithubStatus } from '../api/client';
import { colors, font, radius, space } from '../theme';
import { confirmAction } from '../util/confirm';
import { Button, Card, Label } from './ui';

function StatePill({ ok, label }: { ok: boolean; label: string }) {
  const tone = ok ? colors.success : colors.warning;
  return (
    <View style={[styles.pill, { borderColor: tone + '59', backgroundColor: tone + '14' }]}>
      <Text style={[styles.pillText, { color: tone }]}>{label}</Text>
    </View>
  );
}

export function GitIdentityCard({ readOnly }: { readOnly?: boolean }) {
  const [status, setStatus] = useState<GithubStatus | null>(null);
  const [name, setName] = useState('');
  const [email, setEmail] = useState('');
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null);
  const [showKey, setShowKey] = useState(false);

  async function refresh() {
    try {
      const s = await getGithubStatus();
      setStatus(s);
      setName(s.git_config?.user_name ?? '');
      setEmail(s.git_config?.user_email ?? '');
    } catch {
      // server unavailable / unauthorized — leave status null; the card still
      // renders the (empty) identity form so the user can set it.
    }
  }
  useEffect(() => { void refresh(); }, []);

  async function saveIdentity() {
    if (!name.trim() || !email.trim()) {
      setNote({ kind: 'err', text: 'Enter both a name and an email.' });
      return;
    }
    setBusy(true);
    setNote(null);
    try {
      await setGitConfig(name.trim(), email.trim());
      setNote({ kind: 'ok', text: 'Git identity saved.' });
      await refresh();
    } catch (e) {
      setNote({ kind: 'err', text: (e as Error).message || 'Save failed.' });
    } finally {
      setBusy(false);
    }
  }

  function genKey() {
    if (!email.trim()) {
      setNote({ kind: 'err', text: 'Set your git email first.' });
      return;
    }
    confirmAction({
      title: status?.ssh?.configured ? 'Replace SSH key?' : 'Generate SSH key?',
      message: status?.ssh?.configured
        ? 'This overwrites the existing key pair on the workspace. Any service still trusting the old key stops working until you add the new one.'
        : 'Generates a new ed25519 key pair on the workspace. Add the public key to GitHub afterwards.',
      confirmLabel: status?.ssh?.configured ? 'Replace key' : 'Generate',
      destructive: !!status?.ssh?.configured,
      onConfirm: async () => {
        setBusy(true);
        setNote(null);
        try {
          await generateSshKey(email.trim());
          setNote({ kind: 'ok', text: 'SSH key generated. Add the public key to GitHub.' });
          setShowKey(true);
          await refresh();
        } catch (e) {
          setNote({ kind: 'err', text: (e as Error).message || 'ssh-keygen failed.' });
        } finally {
          setBusy(false);
        }
      },
    });
  }

  async function copyKey() {
    const key = status?.ssh?.public_key;
    if (!key) return;
    await Clipboard.setStringAsync(key);
    setNote({ kind: 'ok', text: 'Public key copied.' });
  }

  const sshOk = !!status?.ssh?.configured;
  const ghOk = !!status?.gh_cli?.authenticated;
  const ghUser = status?.gh_cli?.username?.trim();

  return (
    <Card style={{ gap: space.md, marginTop: space.lg }}>
      <Label>GitHub &amp; SSH</Label>

      <View style={styles.pillRow}>
        <StatePill ok={sshOk} label={sshOk ? 'SSH key ✓' : 'SSH key missing'} />
        <StatePill ok={ghOk} label={ghOk ? `gh CLI ✓${ghUser ? ` ${ghUser}` : ''}` : 'gh CLI not signed in'} />
      </View>

      <View>
        <Label>git user.name</Label>
        <TextInput
          value={name}
          onChangeText={setName}
          editable={!readOnly && !busy}
          placeholder="Imran Hassanali"
          placeholderTextColor={colors.textFaint}
          autoCapitalize="words"
          autoCorrect={false}
          style={[styles.input, readOnly && styles.inputDisabled]}
        />
      </View>
      <View>
        <Label>git user.email</Label>
        <TextInput
          value={email}
          onChangeText={setEmail}
          editable={!readOnly && !busy}
          placeholder="you@example.com"
          placeholderTextColor={colors.textFaint}
          keyboardType="email-address"
          autoCapitalize="none"
          autoCorrect={false}
          style={[styles.input, readOnly && styles.inputDisabled]}
        />
      </View>

      {note ? (
        <Text style={[styles.note, { color: note.kind === 'ok' ? colors.success : colors.danger }]}>
          {note.text}
        </Text>
      ) : null}

      {!readOnly ? (
        <>
          <Button title="Save identity" icon="checkmark-outline" onPress={saveIdentity} loading={busy} />
          <Button
            title={sshOk ? 'Replace SSH key' : 'Generate SSH key'}
            variant="secondary"
            icon="key-outline"
            onPress={genKey}
            disabled={busy}
          />
        </>
      ) : null}

      {status?.ssh?.public_key ? (
        <View style={styles.keyBox}>
          <View style={styles.keyHead}>
            <Text style={styles.keyLabel}>SSH public key</Text>
            <View style={styles.keyActions}>
              <Pressable onPress={() => setShowKey((v) => !v)} hitSlop={8}>
                <Text style={styles.keyToggle}>{showKey ? 'Hide' : 'Show'}</Text>
              </Pressable>
              <Pressable onPress={copyKey} hitSlop={8} accessibilityLabel="Copy public key">
                <Ionicons name="copy-outline" size={16} color={colors.textMuted} />
              </Pressable>
            </View>
          </View>
          {showKey ? <Text style={styles.keyText} selectable>{status.ssh.public_key}</Text> : null}
        </View>
      ) : null}
    </Card>
  );
}

const styles = StyleSheet.create({
  pillRow: { flexDirection: 'row', flexWrap: 'wrap', gap: space.sm },
  pill: {
    alignSelf: 'flex-start',
    borderRadius: radius.pill,
    borderWidth: 1,
    paddingVertical: 3,
    paddingHorizontal: 10,
  },
  pillText: { fontSize: font.size.xs, fontWeight: '700', fontFamily: font.mono },
  input: {
    marginTop: space.xs,
    backgroundColor: colors.surface2,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.md,
    paddingHorizontal: space.md,
    paddingVertical: space.md,
    color: colors.text,
    fontSize: font.size.md,
  },
  inputDisabled: { opacity: 0.6 },
  note: { fontSize: font.size.sm, lineHeight: 19 },
  keyBox: {
    backgroundColor: colors.surface2,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.md,
    padding: space.md,
    gap: space.sm,
  },
  keyHead: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' },
  keyLabel: { color: colors.textMuted, fontSize: font.size.xs, fontWeight: '600', letterSpacing: 0.5, textTransform: 'uppercase' },
  keyActions: { flexDirection: 'row', alignItems: 'center', gap: space.md },
  keyToggle: { color: colors.info, fontSize: font.size.sm, fontWeight: '600' },
  keyText: { color: colors.text, fontSize: font.size.xs, fontFamily: font.mono, lineHeight: 17 },
});
