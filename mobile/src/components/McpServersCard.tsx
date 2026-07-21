/** Settings card for user-defined MCP servers (issue #353). Lists the
 *  registry entries with their fan-out state (enabled/disabled), lets the user
 *  enable/disable or remove one (confirmAction), and opens a small modal sheet
 *  to add a new server (name / command / args / KEY=VALUE env lines). Entries
 *  are stored on the workspace disk and fanned out to every assistant's own
 *  config — new sessions pick them up. Mirrors ProviderKeysCard. */
import { Ionicons } from '@expo/vector-icons';
import React, { useEffect, useState } from 'react';
import {
  KeyboardAvoidingView,
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
  listMcpServers,
  saveMcpServer,
  deleteMcpServer,
  type McpServerEntry,
  type McpSyncResults,
} from '../api/client';
import { colors, font, radius, space } from '../theme';
import { confirmAction } from '../util/confirm';
import { Button, Card, Label } from './ui';

// Parse "KEY=VALUE" lines into an env object; returns an error for bad lines.
function parseEnvLines(text: string): { env: Record<string, string>; error: string | null } {
  const env: Record<string, string> = {};
  for (const raw of text.split('\n')) {
    const line = raw.trim();
    if (!line) continue;
    const eq = line.indexOf('=');
    if (eq <= 0) return { env, error: `Env lines must be KEY=VALUE (got "${line}")` };
    env[line.slice(0, eq).trim()] = line.slice(eq + 1);
  }
  return { env, error: null };
}

function syncWarning(sync: McpSyncResults): string | null {
  const bad = Object.entries(sync).filter(([, r]) => r.startsWith('error'));
  if (!bad.length) return null;
  return `Some assistants failed to update: ${bad.map(([p]) => p).join(', ')}`;
}

export function McpServersCard({ readOnly }: { readOnly?: boolean }) {
  const [servers, setServers] = useState<McpServerEntry[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [warning, setWarning] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);

  async function refresh() {
    try {
      setServers(await listMcpServers());
    } catch {
      // server unavailable (or older image) — leave null
    }
  }
  useEffect(() => { void refresh(); }, []);

  function applySync(sync: McpSyncResults) {
    setWarning(syncWarning(sync));
  }

  async function toggle(s: McpServerEntry) {
    setBusy(s.name);
    try {
      // Blank env values round-trip the stored secrets unchanged.
      const env: Record<string, string> = {};
      for (const k of Object.keys(s.env)) env[k] = '';
      applySync(await saveMcpServer({
        name: s.name, command: s.command, args: s.args, env, enabled: !s.enabled,
      }));
      await refresh();
    } catch {
      setWarning(`Couldn't update ${s.name}`);
    } finally {
      setBusy(null);
    }
  }

  function remove(s: McpServerEntry) {
    confirmAction({
      title: `Remove "${s.name}"?`,
      message: 'This removes it from every assistant’s config. Built-in workspace servers are unaffected.',
      confirmLabel: 'Remove',
      destructive: true,
      onConfirm: async () => {
        setBusy(s.name);
        try {
          applySync(await deleteMcpServer(s.name));
          await refresh();
        } catch {
          setWarning(`Couldn't remove ${s.name}`);
        } finally {
          setBusy(null);
        }
      },
    });
  }

  return (
    <Card style={{ gap: space.md, marginTop: space.lg }}>
      <Label>MCP servers</Label>
      <Text style={styles.help}>
        Model Context Protocol connectors for every assistant in this workspace (Claude, OpenCode,
        Ante, Codex). New sessions pick them up. Built-in workspace servers are managed for you.
      </Text>

      {servers?.map((s) => (
        <View key={s.name} style={styles.row}>
          <View style={{ flex: 1 }}>
            <Text style={styles.srvName}>{s.name}</Text>
            <Text style={[styles.srvState, { color: s.enabled ? colors.success : colors.textFaint }]}>
              {s.enabled ? 'enabled' : 'disabled'}
            </Text>
            <Text style={styles.srvCmd} numberOfLines={1}>
              {s.command} {s.args.join(' ')}
              {Object.keys(s.env).length ? ` · env: ${Object.keys(s.env).join(', ')}` : ''}
            </Text>
          </View>
          {!readOnly ? (
            <>
              <Pressable
                onPress={() => toggle(s)}
                disabled={busy === s.name}
                hitSlop={8}
                accessibilityLabel={s.enabled ? `Disable ${s.name}` : `Enable ${s.name}`}
              >
                <Ionicons
                  name={s.enabled ? 'pause-circle-outline' : 'play-circle-outline'}
                  size={20}
                  color={colors.textMuted}
                />
              </Pressable>
              <Pressable
                onPress={() => remove(s)}
                disabled={busy === s.name}
                hitSlop={8}
                accessibilityLabel={`Remove ${s.name}`}
              >
                <Ionicons name="trash-outline" size={20} color={colors.textMuted} />
              </Pressable>
            </>
          ) : null}
        </View>
      ))}
      {servers && servers.length === 0 ? (
        <Text style={styles.empty}>No custom MCP servers yet.</Text>
      ) : null}

      {warning ? <Text style={styles.warning}>{warning}</Text> : null}

      {!readOnly ? (
        <Button
          title="Add MCP server"
          variant="secondary"
          icon="add"
          onPress={() => setAdding(true)}
        />
      ) : null}

      <AddMcpServerModal
        visible={adding}
        onClose={() => setAdding(false)}
        onDone={async (sync) => {
          setAdding(false);
          applySync(sync);
          await refresh();
        }}
      />
    </Card>
  );
}

function AddMcpServerModal({
  visible,
  onClose,
  onDone,
}: {
  visible: boolean;
  onClose: () => void;
  onDone: (sync: McpSyncResults) => void;
}) {
  const [name, setName] = useState('');
  const [command, setCommand] = useState('');
  const [args, setArgs] = useState('');
  const [envText, setEnvText] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Reset the draft whenever the sheet reopens.
  useEffect(() => {
    if (visible) {
      setName(''); setCommand(''); setArgs(''); setEnvText(''); setError(null);
    }
  }, [visible]);

  async function save() {
    if (!name.trim() || !command.trim()) {
      setError('Name and command are required');
      return;
    }
    const { env, error: envError } = parseEnvLines(envText);
    if (envError) {
      setError(envError);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const sync = await saveMcpServer({
        name: name.trim(),
        command: command.trim(),
        args: args.trim() ? args.trim().split(/\s+/) : [],
        env,
      });
      onDone(sync);
    } catch (e) {
      setError(`Couldn't save: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal visible={visible} animationType="slide" transparent onRequestClose={onClose}>
      <View style={styles.backdrop}>
        <KeyboardAvoidingView behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
          <View style={styles.sheet}>
            <View style={styles.head}>
              <Text style={styles.title}>Add MCP server</Text>
              <Pressable onPress={onClose} hitSlop={8} accessibilityLabel="Close">
                <Ionicons name="close" size={22} color={colors.textMuted} />
              </Pressable>
            </View>
            <ScrollView keyboardShouldPersistTaps="handled">
              <Label style={{ marginTop: space.md }}>Name</Label>
              <TextInput
                value={name}
                onChangeText={setName}
                placeholder="e.g. github"
                placeholderTextColor={colors.textFaint}
                autoCapitalize="none"
                autoCorrect={false}
                style={styles.input}
              />
              <Label style={{ marginTop: space.md }}>Command</Label>
              <TextInput
                value={command}
                onChangeText={setCommand}
                placeholder="e.g. npx"
                placeholderTextColor={colors.textFaint}
                autoCapitalize="none"
                autoCorrect={false}
                style={styles.input}
              />
              <Label style={{ marginTop: space.md }}>Arguments</Label>
              <TextInput
                value={args}
                onChangeText={setArgs}
                placeholder="e.g. -y @modelcontextprotocol/server-github"
                placeholderTextColor={colors.textFaint}
                autoCapitalize="none"
                autoCorrect={false}
                style={styles.input}
              />
              <Label style={{ marginTop: space.md }}>Env vars (one per line)</Label>
              <TextInput
                value={envText}
                onChangeText={setEnvText}
                placeholder={'GITHUB_TOKEN=ghp_…'}
                placeholderTextColor={colors.textFaint}
                autoCapitalize="none"
                autoCorrect={false}
                multiline
                numberOfLines={2}
                style={[styles.input, styles.multiline]}
              />

              {error ? <Text style={styles.error}>{error}</Text> : null}

              <Button title="Add server" onPress={save} loading={busy} style={{ marginTop: space.lg }} />
            </ScrollView>
          </View>
        </KeyboardAvoidingView>
      </View>
    </Modal>
  );
}

const styles = StyleSheet.create({
  help: { color: colors.textMuted, fontSize: font.size.sm, lineHeight: 19 },
  row: { flexDirection: 'row', alignItems: 'center', gap: space.md },
  srvName: { color: colors.text, fontSize: font.size.md },
  srvState: { fontSize: font.size.xs, fontWeight: '700', marginTop: 2, letterSpacing: 0.3 },
  srvCmd: { color: colors.textFaint, fontSize: font.size.xs, marginTop: 2 },
  empty: { color: colors.textFaint, fontSize: font.size.sm },
  warning: { color: colors.warning, fontSize: font.size.sm },
  backdrop: { flex: 1, backgroundColor: '#000a', justifyContent: 'flex-end' },
  sheet: {
    backgroundColor: colors.bgElevated,
    borderTopLeftRadius: radius.xl,
    borderTopRightRadius: radius.xl,
    padding: space.xl,
    paddingBottom: space.xxl,
    gap: 2,
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
  multiline: { minHeight: 64, textAlignVertical: 'top' },
  error: { color: colors.danger, fontSize: font.size.sm, marginTop: space.md },
});
