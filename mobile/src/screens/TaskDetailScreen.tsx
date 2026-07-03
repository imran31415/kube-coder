/** Task detail: live-tailed output + follow-up composer. */
import { Ionicons } from '@expo/vector-icons';
import { useNavigation, useRoute, RouteProp } from '@react-navigation/native';
import React, { useCallback, useLayoutEffect, useMemo, useRef, useState } from 'react';
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
import * as Clipboard from 'expo-clipboard';
import { getTask, getTaskOutput, killTask, sendKey, sendMessage } from '../api/client';
import { Button, Loading, StatusPill } from '../components/ui';
import type { TaskDetail } from '../api/types';
import type { TasksStackParams } from '../navigation';
import { parseAnsiLines } from '../util/ansi';
import { colors, font, radius, space, statusColor } from '../theme';
import { usePolling } from '../util/usePolling';

// Mobile key bar: control keys you can't type into the composer. Paste pulls the
// clipboard into the input; the rest go straight to the live tmux session.
const KEY_BAR: { label: string; key?: string; paste?: boolean }[] = [
  { label: 'Paste', paste: true },
  { label: '⇧⇥ Mode', key: 'shift-tab' },
  { label: 'Esc', key: 'escape' },
  { label: '↑', key: 'up' },
  { label: '↓', key: 'down' },
  { label: '⏎', key: 'enter' },
  { label: '⌃C', key: 'ctrl-c' },
];
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
  const [err, setErr] = useState<string | null>(null);
  const [sendErr, setSendErr] = useState<string | null>(null);
  const scrollRef = useRef<ScrollView>(null);
  // Only auto-follow new output while the user is pinned near the bottom —
  // force-scrolling while they read scrollback makes the log unreadable.
  const pinnedToBottom = useRef(true);

  const load = useCallback(async () => {
    try {
      const [t, o] = await Promise.all([getTask(id), getTaskOutput(id)]);
      setTask(t);
      setOutput(o);
      setErr(null);
    } catch (e) {
      // Keep the last good data during polling; surface a message only so the
      // initial load doesn't hang on "Loading…" forever if it genuinely fails.
      setErr(e instanceof Error ? e.message : 'Failed to load task');
    }
  }, [id]);

  usePolling(load, 3000);

  const active = task?.status === 'running' || task?.status === 'waiting';
  const lines = useMemo(() => parseAnsiLines(output), [output]);

  async function onKeyBar(item: (typeof KEY_BAR)[number]) {
    if (item.paste) {
      const clip = await Clipboard.getStringAsync().catch(() => '');
      if (clip) setMsg((m) => (m ? `${m}${clip}` : clip));
      return;
    }
    if (item.key) {
      try {
        await sendKey(id, item.key);
        await load(); // reflect the session's reaction quickly
      } catch {
        /* transient — the next poll will catch up */
      }
    }
  }

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
    setSendErr(null);
    try {
      await sendMessage(id, msg.trim());
      setMsg('');
      await load();
    } catch (e) {
      // Keep the draft so the user can retry; a silent failure here looks
      // exactly like a hung assistant.
      setSendErr(e instanceof Error ? e.message : 'Failed to send');
    } finally {
      setSending(false);
    }
  }

  if (!task) return <Loading label={err ? "Couldn't load task — retrying…" : 'Loading task…'} />;

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
          scrollEventThrottle={64}
          onScroll={(e) => {
            const { contentOffset, contentSize, layoutMeasurement } = e.nativeEvent;
            pinnedToBottom.current =
              contentOffset.y + layoutMeasurement.height >= contentSize.height - 40;
          }}
          onContentSizeChange={() => {
            if (pinnedToBottom.current) scrollRef.current?.scrollToEnd({ animated: false });
          }}
        >
          {lines.length === 0 ? (
            <Text style={styles.termText}>(no output yet)</Text>
          ) : (
            lines.map((line, i) => (
              <Text key={i} style={styles.termText}>
                {line.length === 0
                  ? ' '
                  : line.map((seg, j) => (
                      <Text
                        key={j}
                        style={{
                          color: seg.color ?? '#c8d3df',
                          fontWeight: seg.bold ? '700' : '400',
                          opacity: seg.dim ? 0.6 : 1,
                        }}
                      >
                        {seg.text}
                      </Text>
                    ))}
              </Text>
            ))
          )}
        </ScrollView>

        {active ? (
          <View>
            <ScrollView
              horizontal
              showsHorizontalScrollIndicator={false}
              keyboardShouldPersistTaps="handled"
              contentContainerStyle={styles.keyBar}
            >
              {KEY_BAR.map((k) => (
                <Pressable key={k.label} style={styles.keyBtn} onPress={() => onKeyBar(k)}>
                  <Text style={styles.keyBtnText}>{k.label}</Text>
                </Pressable>
              ))}
            </ScrollView>
            {sendErr ? (
              <Text style={styles.sendErr} accessibilityRole="alert">
                Couldn't send: {sendErr}
              </Text>
            ) : null}
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
  keyBar: {
    flexDirection: 'row',
    gap: space.sm,
    paddingHorizontal: space.md,
    paddingTop: space.sm,
  },
  keyBtn: {
    paddingHorizontal: space.md,
    paddingVertical: 7,
    borderRadius: radius.pill,
    backgroundColor: colors.bgElevated,
    borderWidth: 1,
    borderColor: colors.border,
  },
  keyBtnText: { color: colors.text, fontSize: font.size.sm, fontWeight: '600' },
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
  sendErr: {
    color: colors.danger,
    fontSize: font.size.xs,
    paddingHorizontal: space.lg,
    paddingTop: space.sm,
  },
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
