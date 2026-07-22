/** Settings card for user-settable model-provider API keys. Each provider shows
 *  its masked status (set · …cc18 / not set) and opens a small modal to paste a
 *  new key or clear it. Keys are stored on the workspace disk and used the next
 *  time an assistant runs — no redeploy. Mirrors ControllerConnectModal. */
import { Ionicons } from '@expo/vector-icons';
import React, { useEffect, useState } from 'react';
import {
  KeyboardAvoidingView,
  Modal,
  Platform,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import {
  listProviderKeys,
  setProviderKey,
  deleteProviderKey,
  type ProviderVar,
  type ProviderKeysView,
} from '../api/client';
import { colors, font, radius, space } from '../theme';
import { Button, Card, Label } from './ui';

const PROVIDERS: { var: ProviderVar; label: string; hint: string }[] = [
  { var: 'OPENROUTER_API_KEY', label: 'OpenRouter', hint: 'OpenCode + OpenRouter-backed models' },
  { var: 'DEEPSEEK_API_KEY', label: 'DeepSeek', hint: 'DeepSeek API' },
  { var: 'ANTHROPIC_API_KEY', label: 'Anthropic', hint: 'Overrides the Claude oauth default' },
  { var: 'OPENAI_API_KEY', label: 'OpenAI', hint: 'Whisper transcription for the voice mic' },
];

export function ProviderKeysCard({ readOnly }: { readOnly?: boolean }) {
  const [view, setView] = useState<ProviderKeysView | null>(null);
  const [editing, setEditing] = useState<ProviderVar | null>(null);

  async function refresh() {
    try {
      setView(await listProviderKeys());
    } catch {
      // server unavailable — leave null
    }
  }
  useEffect(() => { void refresh(); }, []);

  const active = PROVIDERS.find((p) => p.var === editing) ?? null;

  return (
    <Card style={{ gap: space.md, marginTop: space.lg }}>
      <Label>Provider API keys</Label>
      <Text style={styles.help}>
        Set your own model-provider keys for this workspace. Used the next time an assistant runs — no
        redeploy. Leave unset to use the workspace default.
      </Text>
      {PROVIDERS.map((p) => {
        const st = view?.[p.var];
        return (
          <View key={p.var} style={styles.row}>
            <View style={{ flex: 1 }}>
              <Text style={styles.provName}>{p.label}</Text>
              <Text style={[styles.provState, { color: st?.set ? colors.success : colors.textFaint }]}>
                {st?.set ? `set · ${st.hint}` : 'not set'}
              </Text>
            </View>
            {!readOnly ? (
              <Pressable onPress={() => setEditing(p.var)} hitSlop={8} accessibilityLabel={`Edit ${p.label} key`}>
                <Ionicons name="create-outline" size={20} color={colors.textMuted} />
              </Pressable>
            ) : null}
          </View>
        );
      })}

      <ProviderKeyModal
        provider={active}
        isSet={active ? !!view?.[active.var]?.set : false}
        onClose={() => setEditing(null)}
        onDone={async () => { setEditing(null); await refresh(); }}
      />
    </Card>
  );
}

function ProviderKeyModal({
  provider,
  isSet,
  onClose,
  onDone,
}: {
  provider: { var: ProviderVar; label: string; hint: string } | null;
  isSet: boolean;
  onClose: () => void;
  onDone: () => void;
}) {
  const [key, setKey] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Reset the draft whenever the target provider changes.
  useEffect(() => { setKey(''); setError(null); }, [provider?.var]);

  async function save() {
    if (!provider) return;
    if (!key.trim()) {
      setError('Paste a key');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await setProviderKey(provider.var, key.trim());
      onDone();
    } catch (e) {
      setError(`Couldn't save: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  async function clear() {
    if (!provider) return;
    setBusy(true);
    setError(null);
    try {
      await deleteProviderKey(provider.var);
      onDone();
    } catch (e) {
      setError(`Couldn't clear: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal visible={!!provider} animationType="slide" transparent onRequestClose={onClose}>
      <View style={styles.backdrop}>
        <KeyboardAvoidingView behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
          <View style={styles.sheet}>
            <View style={styles.head}>
              <Text style={styles.title}>{provider?.label} key</Text>
              <Pressable onPress={onClose} hitSlop={8} accessibilityLabel="Close">
                <Ionicons name="close" size={22} color={colors.textMuted} />
              </Pressable>
            </View>
            <Text style={styles.help}>{provider?.hint}</Text>

            <Label style={{ marginTop: space.md }}>API key</Label>
            <TextInput
              value={key}
              onChangeText={setKey}
              placeholder={isSet ? 'Replace current key…' : 'Paste key…'}
              placeholderTextColor={colors.textFaint}
              autoCapitalize="none"
              autoCorrect={false}
              secureTextEntry
              style={styles.input}
            />

            {error ? <Text style={styles.error}>{error}</Text> : null}

            <Button title="Save key" onPress={save} loading={busy} style={{ marginTop: space.lg }} />
            {isSet ? (
              <Button
                title="Clear key"
                variant="secondary"
                icon="trash-outline"
                onPress={clear}
                disabled={busy}
                style={{ marginTop: space.sm }}
              />
            ) : null}
          </View>
        </KeyboardAvoidingView>
      </View>
    </Modal>
  );
}

const styles = StyleSheet.create({
  help: { color: colors.textMuted, fontSize: font.size.sm, lineHeight: 19 },
  row: { flexDirection: 'row', alignItems: 'center', gap: space.md },
  provName: { color: colors.text, fontSize: font.size.md },
  provState: { fontSize: font.size.xs, fontWeight: '700', marginTop: 2, letterSpacing: 0.3 },
  backdrop: { flex: 1, backgroundColor: '#000a', justifyContent: 'flex-end' },
  sheet: {
    backgroundColor: colors.bgElevated,
    borderTopLeftRadius: radius.xl,
    borderTopRightRadius: radius.xl,
    padding: space.xl,
    paddingBottom: space.xxl,
    gap: 2,
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
