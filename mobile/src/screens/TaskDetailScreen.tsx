/** Task detail: live-tailed output + follow-up composer. */
import { Ionicons } from '@expo/vector-icons';
import { useNavigation, useRoute, RouteProp } from '@react-navigation/native';
import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
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
import { getTask, getTaskOutput, killTask, sendMessage } from '../api/client';
import { Button, Loading, StatusPill } from '../components/ui';
import type { TaskDetail } from '../api/types';
import type { TasksStackParams } from '../navigation';
import { colors, font, radius, space, statusColor } from '../theme';
import { confirmAction } from '../util/confirm';

function finishedNote(status: string): { icon: keyof typeof Ionicons.glyphMap; text: string } {
  switch (status) {
    case 'error':
      return { icon: 'alert-circle', text: 'This task ended with an error' };
    case 'killed':
      return { icon: 'stop-circle', text: 'This task was stopped' };
    default:
      return { icon: 'checkmark-circle', text: 'This task has finished' };
  }
}

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

  const active = task?.status === 'running' || task?.status === 'waiting';

  const promptKill = useCallback(() => {
    confirmAction({
      title: 'Kill this task?',
      message: 'The task will be stopped immediately. This cannot be undone.',
      confirmLabel: 'Kill task',
      destructive: true,
      onConfirm: async () => {
        await killTask(id);
        await load();
      },
    });
  }, [id, load]);

  // Destructive action lives in the header — far from the compose/Send area at
  // the bottom so it can't be hit by accident.
  useLayoutEffect(() => {
    nav.setOptions({
      headerRight: active
        ? () => (
            <Pressable
              onPress={promptKill}
              hitSlop={10}
              accessibilityRole="button"
              accessibilityLabel="Kill task"
              style={styles.headerBtn}
            >
              <Ionicons name="stop-circle-outline" size={24} color={colors.danger} />
            </Pressable>
          )
        : undefined,
    });
  }, [nav, active, promptKill]);

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

  if (!task) return <Loading label="Loading task…" />;

  const note = finishedNote(task.status);

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

        {active ? (
          <View style={styles.composer}>
            <TextInput
              value={msg}
              onChangeText={setMsg}
              placeholder="Send a follow-up…"
              placeholderTextColor={colors.textFaint}
              style={styles.composerInput}
              multiline
            />
            <Button
              title="Send"
              icon="arrow-up"
              onPress={send}
              loading={sending}
              disabled={!msg.trim()}
              style={styles.sendBtn}
            />
          </View>
        ) : (
          <View style={styles.finishedBar}>
            <Ionicons name={note.icon} size={18} color={statusColor(task.status)} />
            <Text style={styles.finishedText}>{note.text}</Text>
          </View>
        )}
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: colors.bg },
  headerBtn: { paddingHorizontal: space.sm, paddingVertical: 2 },
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
    minHeight: 50,
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
  finishedBar: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
    gap: space.sm,
    paddingVertical: space.lg,
    borderTopWidth: 1,
    borderTopColor: colors.border,
  },
  finishedText: { color: colors.textMuted, fontSize: font.size.sm, fontWeight: '500' },
});
