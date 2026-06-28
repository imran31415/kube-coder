/** Task detail: live-tailed output + follow-up message + kill. */
import { useNavigation, useRoute, RouteProp } from '@react-navigation/native';
import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  KeyboardAvoidingView,
  Platform,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { getTask, getTaskOutput, killTask, sendMessage } from '../api/client';
import { Button, Loading, StatusPill } from '../components/ui';
import type { TaskDetail } from '../api/types';
import type { TasksStackParams } from '../navigation';
import { colors, font, radius, space } from '../theme';

export default function TaskDetailScreen() {
  const route = useRoute<RouteProp<TasksStackParams, 'TaskDetail'>>();
  const nav = useNavigation();
  const { id } = route.params;
  const [task, setTask] = useState<TaskDetail | null>(null);
  const [output, setOutput] = useState('');
  const [msg, setMsg] = useState('');
  const [sending, setSending] = useState(false);
  const scrollRef = useRef<ScrollView>(null);

  const load = useCallback(async () => {
    try {
      const [t, o] = await Promise.all([getTask(id), getTaskOutput(id)]);
      setTask(t);
      setOutput(o);
    } catch {
      /* keep last good */
    }
  }, [id]);

  useEffect(() => {
    load();
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, [load]);

  async function send() {
    if (!msg.trim()) return;
    setSending(true);
    try {
      await sendMessage(id, msg.trim());
      setMsg('');
      await load();
    } finally {
      setSending(false);
    }
  }

  async function kill() {
    await killTask(id);
    await load();
  }

  if (!task) return <Loading label="Loading task…" />;

  const active = task.status === 'running' || task.status === 'waiting';

  return (
    <SafeAreaView style={styles.safe} edges={['bottom']}>
      <KeyboardAvoidingView
        behavior={Platform.OS === 'ios' ? 'padding' : undefined}
        style={{ flex: 1 }}
        keyboardVerticalOffset={90}
      >
        <View style={styles.head}>
          <View style={styles.headTop}>
            <StatusPill status={task.status} />
            <Text style={styles.id}>#{task.id}</Text>
          </View>
          <Text style={styles.prompt}>{task.prompt}</Text>
          <Text style={styles.meta}>
            {(task.assistant ?? 'claude') + '  ·  ' + (task.workdir ?? '/home/dev')}
          </Text>
        </View>

        <ScrollView
          ref={scrollRef}
          style={styles.term}
          contentContainerStyle={styles.termContent}
          onContentSizeChange={() => scrollRef.current?.scrollToEnd({ animated: false })}
        >
          <Text style={styles.termText}>{output || '(no output yet)'}</Text>
        </ScrollView>

        <View style={styles.composer}>
          <TextInput
            value={msg}
            onChangeText={setMsg}
            placeholder={active ? 'Send a follow-up…' : 'Task finished'}
            placeholderTextColor={colors.textFaint}
            editable={active}
            style={styles.composerInput}
            multiline
          />
          <Button
            title="Send"
            onPress={send}
            loading={sending}
            disabled={!active || !msg.trim()}
            style={styles.sendBtn}
          />
        </View>

        <View style={styles.actions}>
          {active ? (
            <Button title="Kill task" variant="danger" onPress={kill} style={{ flex: 1 }} />
          ) : (
            <Button title="Back to tasks" variant="secondary" onPress={() => nav.goBack()} style={{ flex: 1 }} />
          )}
        </View>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: colors.bg },
  head: { padding: space.lg, gap: space.sm, borderBottomWidth: 1, borderBottomColor: colors.border },
  headTop: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' },
  id: { color: colors.textFaint, fontSize: font.size.sm, fontFamily: font.mono },
  prompt: { color: colors.text, fontSize: font.size.lg, fontWeight: '600', lineHeight: 24 },
  meta: { color: colors.textMuted, fontSize: font.size.xs },
  term: { flex: 1, backgroundColor: '#08090b' },
  termContent: { padding: space.lg },
  termText: { color: '#c8d3df', fontFamily: font.mono, fontSize: font.size.sm, lineHeight: 19 },
  composer: {
    flexDirection: 'row',
    alignItems: 'flex-end',
    gap: space.sm,
    padding: space.md,
    borderTopWidth: 1,
    borderTopColor: colors.border,
  },
  composerInput: {
    flex: 1,
    minHeight: 48,
    maxHeight: 120,
    backgroundColor: colors.bgElevated,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: colors.border,
    color: colors.text,
    paddingHorizontal: space.md,
    paddingTop: space.md,
    fontSize: font.size.md,
  },
  sendBtn: { paddingHorizontal: space.lg },
  actions: { flexDirection: 'row', paddingHorizontal: space.md, paddingBottom: space.sm },
});
