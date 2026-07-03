/** Create a new Claude task. */
import { useNavigation } from '@react-navigation/native';
import React, { useState } from 'react';
import {
  KeyboardAvoidingView,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { createTask } from '../api/client';
import { Button, Label } from '../components/ui';
import type { TasksNav } from '../navigation';
import { colors, font, radius, space } from '../theme';

const ASSISTANTS = ['claude', 'ante', 'opencode-openrouter'];

export default function NewTaskScreen() {
  const nav = useNavigation<TasksNav>();
  const [prompt, setPrompt] = useState('');
  const [workdir, setWorkdir] = useState('/home/dev');
  const [assistant, setAssistant] = useState('claude');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit() {
    if (!prompt.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const t = await createTask({ prompt: prompt.trim(), workdir, assistant });
      nav.replace('TaskDetail', { id: t.id });
    } catch (e) {
      // The prompt stays in the form — surface why it didn't start instead of
      // silently doing nothing.
      setError(e instanceof Error ? e.message : 'Failed to start the task');
    } finally {
      setBusy(false);
    }
  }

  return (
    <SafeAreaView style={styles.safe} edges={['bottom']}>
      <KeyboardAvoidingView behavior={Platform.OS === 'ios' ? 'padding' : undefined} style={{ flex: 1 }}>
        <ScrollView contentContainerStyle={styles.scroll} keyboardShouldPersistTaps="handled">
          <Label>Prompt</Label>
          <TextInput
            value={prompt}
            onChangeText={setPrompt}
            placeholder="Describe the task for Claude…"
            placeholderTextColor={colors.textFaint}
            style={styles.prompt}
            multiline
            autoFocus
          />

          <Label style={{ marginTop: space.xl }}>Working directory</Label>
          <TextInput
            value={workdir}
            onChangeText={setWorkdir}
            autoCapitalize="none"
            autoCorrect={false}
            style={styles.input}
          />

          <Label style={{ marginTop: space.xl }}>Assistant</Label>
          <View style={styles.chips}>
            {ASSISTANTS.map((a) => (
              <Pressable
                key={a}
                onPress={() => setAssistant(a)}
                style={[styles.chip, assistant === a && styles.chipActive]}
              >
                <Text style={[styles.chipText, assistant === a && styles.chipTextActive]}>{a}</Text>
              </Pressable>
            ))}
          </View>

          {error ? (
            <Text style={styles.error} accessibilityRole="alert">
              {error}
            </Text>
          ) : null}

          <Button
            title="Start task"
            icon="rocket-outline"
            onPress={submit}
            loading={busy}
            disabled={!prompt.trim()}
            style={{ marginTop: error ? space.md : space.xxl }}
          />
        </ScrollView>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: colors.bg },
  scroll: { padding: space.lg },
  prompt: {
    backgroundColor: colors.bgElevated,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.md,
    color: colors.text,
    fontSize: font.size.md,
    padding: space.lg,
    minHeight: 130,
    textAlignVertical: 'top',
    lineHeight: 22,
  },
  input: {
    backgroundColor: colors.bgElevated,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.md,
    color: colors.text,
    fontSize: font.size.md,
    paddingHorizontal: space.lg,
    height: 50,
    fontFamily: font.mono,
  },
  chips: { flexDirection: 'row', flexWrap: 'wrap', gap: space.sm },
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
  error: { color: colors.danger, fontSize: font.size.sm, marginTop: space.xl, lineHeight: 19 },
});
