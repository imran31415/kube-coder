/** Bottom-sheet editor for a Desktop icon — mirrors the web dashboard's
 *  DesktopEditor: label + icon (emoji or "icon:NAME") + one action of type
 *  task / url / shell. Hotkeys are a web-keyboard concept, so mobile preserves
 *  an existing hotkey untouched rather than editing it. */
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
import type { DesktopAction, DesktopActionType, DesktopItem, DesktopItemDraft } from '../api/types';
import { colors, font, radius, space } from '../theme';
import { Button, Label } from './ui';

const ACTION_TYPES: { key: DesktopActionType; label: string; hint: string }[] = [
  { key: 'task', label: 'Task', hint: 'Start an assistant task' },
  { key: 'url', label: 'URL', hint: 'Open a link' },
  { key: 'shell', label: 'Shell', hint: 'Run a command' },
];

export function DesktopEditorSheet({
  visible,
  initial,
  onSave,
  onClose,
}: {
  visible: boolean;
  /** Existing item to edit, or null for a new icon. */
  initial: DesktopItem | null;
  onSave: (draft: DesktopItemDraft) => Promise<string | null>; // -> error message or null
  onClose: () => void;
}) {
  const [label, setLabel] = useState('');
  const [icon, setIcon] = useState('✨');
  const [type, setType] = useState<DesktopActionType>('task');
  const [prompt, setPrompt] = useState('');
  const [workdir, setWorkdir] = useState('/home/dev');
  const [url, setUrl] = useState('');
  const [command, setCommand] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Re-seed the form whenever the sheet opens for a different item.
  useEffect(() => {
    if (!visible) return;
    setError(null);
    setLabel(initial?.label ?? '');
    setIcon(initial?.icon ?? '✨');
    const a = initial?.action;
    setType(a?.type ?? 'task');
    setPrompt(a?.type === 'task' ? a.prompt : '');
    setWorkdir(a?.type === 'task' ? a.workdir ?? '/home/dev' : '/home/dev');
    setUrl(a?.type === 'url' ? a.url : '');
    setCommand(a?.type === 'shell' ? a.command : '');
  }, [visible, initial]);

  function buildAction(): DesktopAction | null {
    if (type === 'task') {
      if (!prompt.trim()) return null;
      return { type: 'task', prompt: prompt.trim(), workdir: workdir.trim() || undefined };
    }
    if (type === 'url') {
      if (!/^https?:\/\//.test(url.trim())) return null;
      return { type: 'url', url: url.trim(), target: 'blank' };
    }
    if (!command.trim()) return null;
    return { type: 'shell', command: command.trim() };
  }

  const action = buildAction();
  const valid = !!label.trim() && !!icon.trim() && !!action;

  async function save() {
    if (!valid || !action) return;
    setBusy(true);
    setError(null);
    const err = await onSave({
      label: label.trim(),
      icon: icon.trim(),
      // Hotkeys only fire on web; keep whatever is set rather than dropping it.
      hotkey: initial?.hotkey,
      action,
    });
    setBusy(false);
    if (err) setError(err);
  }

  return (
    <Modal visible={visible} transparent animationType="slide" onRequestClose={onClose}>
      <KeyboardAvoidingView
        behavior={Platform.OS === 'ios' ? 'padding' : undefined}
        style={styles.backdropWrap}
      >
        <Pressable style={styles.backdrop} onPress={onClose} />
        <View style={styles.sheet}>
          <View style={styles.grabber} />
          <Text style={styles.title}>{initial ? 'Edit icon' : 'New icon'}</Text>
          <ScrollView
            keyboardShouldPersistTaps="handled"
            showsVerticalScrollIndicator={false}
            contentContainerStyle={{ gap: space.md }}
          >
            <View style={styles.rowTwo}>
              <View style={{ flex: 1 }}>
                <Label>Label</Label>
                <TextInput
                  value={label}
                  onChangeText={setLabel}
                  placeholder="Run tests"
                  placeholderTextColor={colors.textFaint}
                  style={styles.input}
                />
              </View>
              <View style={{ width: 96 }}>
                <Label>Icon</Label>
                <TextInput
                  value={icon}
                  onChangeText={setIcon}
                  placeholder="✨"
                  placeholderTextColor={colors.textFaint}
                  autoCapitalize="none"
                  style={[styles.input, { textAlign: 'center' }]}
                />
              </View>
            </View>
            <Text style={styles.hint}>Any emoji, or icon:NAME for a line icon (icon:terminal, icon:chat…)</Text>

            <Label style={{ marginTop: space.sm }}>Action</Label>
            <View style={styles.chips}>
              {ACTION_TYPES.map((a) => (
                <Pressable
                  key={a.key}
                  onPress={() => setType(a.key)}
                  style={[styles.chip, type === a.key && styles.chipActive]}
                >
                  <Text style={[styles.chipText, type === a.key && styles.chipTextActive]}>
                    {a.label}
                  </Text>
                </Pressable>
              ))}
            </View>
            <Text style={styles.hint}>{ACTION_TYPES.find((a) => a.key === type)?.hint}</Text>

            {type === 'task' ? (
              <>
                <Label>Prompt</Label>
                <TextInput
                  value={prompt}
                  onChangeText={setPrompt}
                  placeholder="Describe the task…"
                  placeholderTextColor={colors.textFaint}
                  multiline
                  style={[styles.input, styles.multiline]}
                />
                <Label style={{ marginTop: space.sm }}>Working directory</Label>
                <TextInput
                  value={workdir}
                  onChangeText={setWorkdir}
                  autoCapitalize="none"
                  autoCorrect={false}
                  style={[styles.input, { fontFamily: font.mono }]}
                />
              </>
            ) : type === 'url' ? (
              <>
                <Label>URL</Label>
                <TextInput
                  value={url}
                  onChangeText={setUrl}
                  placeholder="https://…"
                  placeholderTextColor={colors.textFaint}
                  autoCapitalize="none"
                  autoCorrect={false}
                  keyboardType="url"
                  style={styles.input}
                />
              </>
            ) : (
              <>
                <Label>Command</Label>
                <TextInput
                  value={command}
                  onChangeText={setCommand}
                  placeholder="make test"
                  placeholderTextColor={colors.textFaint}
                  autoCapitalize="none"
                  autoCorrect={false}
                  multiline
                  style={[styles.input, styles.multiline, { fontFamily: font.mono }]}
                />
              </>
            )}

            {error ? (
              <Text style={styles.error} accessibilityRole="alert">
                {error}
              </Text>
            ) : null}

            <View style={styles.btnRow}>
              <Button title="Cancel" variant="secondary" onPress={onClose} style={{ flex: 1 }} />
              <Button
                title={initial ? 'Save' : 'Create'}
                onPress={() => void save()}
                loading={busy}
                disabled={!valid}
                style={{ flex: 1 }}
              />
            </View>
          </ScrollView>
        </View>
      </KeyboardAvoidingView>
    </Modal>
  );
}

const styles = StyleSheet.create({
  backdropWrap: { flex: 1, justifyContent: 'flex-end' },
  backdrop: {
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
    borderWidth: 1,
    borderColor: colors.border,
    padding: space.xl,
    paddingBottom: space.xxl,
  },
  grabber: {
    alignSelf: 'center',
    width: 36,
    height: 4,
    borderRadius: 2,
    backgroundColor: colors.borderStrong,
    marginBottom: space.md,
  },
  title: { color: colors.text, fontSize: font.size.lg, fontWeight: '800', marginBottom: space.md },
  rowTwo: { flexDirection: 'row', gap: space.md },
  input: {
    backgroundColor: colors.card,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.md,
    color: colors.text,
    fontSize: font.size.md,
    paddingHorizontal: space.md,
    height: 46,
  },
  multiline: { height: undefined, minHeight: 80, paddingTop: space.md, textAlignVertical: 'top' },
  hint: { color: colors.textFaint, fontSize: font.size.xs, lineHeight: 16 },
  chips: { flexDirection: 'row', gap: space.sm },
  chip: {
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.pill,
    paddingHorizontal: space.lg,
    paddingVertical: space.sm,
  },
  chipActive: { backgroundColor: colors.accent + '22', borderColor: colors.accent },
  chipText: { color: colors.textMuted, fontSize: font.size.sm, fontWeight: '500' },
  chipTextActive: { color: colors.accent, fontWeight: '700' },
  error: { color: colors.danger, fontSize: font.size.sm },
  btnRow: { flexDirection: 'row', gap: space.md, marginTop: space.md },
});
