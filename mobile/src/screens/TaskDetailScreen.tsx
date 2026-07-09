/** Task detail: the live ttyd session (or archived output) + follow-up composer. */
import { Ionicons } from '@expo/vector-icons';
import { useNavigation, useRoute, RouteProp } from '@react-navigation/native';
import { LinearGradient } from 'expo-linear-gradient';
import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import {
  ActivityIndicator,
  Image,
  KeyboardAvoidingView,
  LayoutAnimation,
  PanResponder,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  UIManager,
  View,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import * as Clipboard from 'expo-clipboard';
import * as ImagePicker from 'expo-image-picker';
import { getTask, getTaskOutput, killTask, sendKey, sendMessage, uploadTaskImage } from '../api/client';
import { AppEmbed } from '../components/AppEmbed';
import { AppPickerSheet } from '../components/AppPickerSheet';
import { Loading, StatusPill } from '../components/ui';
import { TerminalView } from '../components/TerminalView';
import { getItem, setItem } from '../store/storage';
import type { TaskDetail } from '../api/types';
import type { TasksStackParams } from '../navigation';
import { colors, font, gradients, radius, space, statusColor } from '../theme';
import { confirmAction } from '../util/confirm';
import { relativeTime } from '../util/format';
import { usePolling } from '../util/usePolling';

if (Platform.OS === 'android' && UIManager.setLayoutAnimationEnabledExperimental) {
  UIManager.setLayoutAnimationEnabledExperimental(true);
}

// Control keys you can't type into the composer — tucked behind the keypad
// toggle beside Send so they never crowd the screen. Paste pulls the clipboard
// into the input; the rest go straight to the live tmux session.
const KEY_TRAY: { label: string; key?: string; paste?: boolean; hint: string }[] = [
  { label: 'Paste', paste: true, hint: 'Paste clipboard into the composer' },
  { label: '⇧⇥', key: 'shift-tab', hint: 'Cycle assistant mode' },
  { label: 'Esc', key: 'escape', hint: 'Escape' },
  { label: '↑', key: 'up', hint: 'Arrow up' },
  { label: '↓', key: 'down', hint: 'Arrow down' },
  { label: '⏎', key: 'enter', hint: 'Enter' },
  { label: '⌃C', key: 'ctrl-c', hint: 'Interrupt' },
];

/** One image attached to the composer, tracked from pick → upload. */
interface Attachment {
  id: string;
  /** Local file URI for the thumbnail preview. */
  uri: string;
  /** Saved absolute path Claude Code will read; set once uploaded. */
  path?: string;
  status: 'uploading' | 'ready' | 'error';
}

// Last app shown in the split pane, remembered across tasks/restarts so the
// toggle is one tap once you've picked your dev server.
const SPLIT_APP_KEY = 'kc.splitApp';
interface SplitApp {
  port: number;
  name: string;
}

// Divider position for the task/app split: fraction of the screen given to
// the task pane. Draggable via the app pane's header bar; persisted so the
// preferred balance survives restarts. Clamped so neither pane can collapse.
const SPLIT_RATIO_KEY = 'kc.splitRatio';
const SPLIT_MIN = 0.25;
const SPLIT_MAX = 0.75;

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
  // Images attached to the pending follow-up (issue #179). Each is uploaded to
  // the task's attachments dir; its saved path is appended to the prompt on send.
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [promptExpanded, setPromptExpanded] = useState(false);
  // The control-key tray, hidden behind the keypad button beside Send.
  const [keysOpen, setKeysOpen] = useState(false);
  // Split view: watch an app run in the lower half while the task streams in
  // the upper half. null = off. The picker chooses which app/port.
  const [splitApp, setSplitApp] = useState<SplitApp | null>(null);
  const [pickerOpen, setPickerOpen] = useState(false);

  // Draggable split: fraction of the vertical space the task pane keeps.
  // Dragging the app pane's header bar moves the divider; only vertical
  // moves claim the gesture, so the bar's buttons still tap normally.
  const [splitRatio, setSplitRatio] = useState(0.5);
  const splitRatioRef = useRef(0.5);
  const dragStartRatio = useRef(0.5);
  const splitAreaH = useRef(0);
  useEffect(() => {
    void (async () => {
      const saved = parseFloat((await getItem(SPLIT_RATIO_KEY)) ?? '');
      if (saved >= SPLIT_MIN && saved <= SPLIT_MAX) {
        splitRatioRef.current = saved;
        setSplitRatio(saved);
      }
    })();
  }, []);
  const persistRatio = () => {
    void setItem(SPLIT_RATIO_KEY, splitRatioRef.current.toFixed(3));
  };
  const splitPan = useRef(
    PanResponder.create({
      onMoveShouldSetPanResponder: (_e, g) =>
        Math.abs(g.dy) > 6 && Math.abs(g.dy) > Math.abs(g.dx),
      onPanResponderGrant: () => {
        dragStartRatio.current = splitRatioRef.current;
      },
      onPanResponderMove: (_e, g) => {
        const h = splitAreaH.current;
        if (h <= 0) return;
        // Finger down (+dy) drags the bar down → the task pane grows.
        const next = Math.min(
          SPLIT_MAX,
          Math.max(SPLIT_MIN, dragStartRatio.current + g.dy / h),
        );
        splitRatioRef.current = next;
        setSplitRatio(next);
      },
      onPanResponderRelease: persistRatio,
      onPanResponderTerminate: persistRatio,
    }),
  ).current;

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

  function toggleKeys() {
    LayoutAnimation.configureNext(LayoutAnimation.Presets.easeInEaseOut);
    setKeysOpen((v) => !v);
  }

  async function onTrayKey(item: (typeof KEY_TRAY)[number]) {
    if (item.paste) {
      const clip = await Clipboard.getStringAsync().catch(() => '');
      if (clip) setMsg((m) => (m ? `${m}${clip}` : clip));
      return;
    }
    if (item.key) {
      try {
        await sendKey(id, item.key);
      } catch {
        /* transient — the live terminal shows the session's real state */
      }
    }
  }

  async function toggleSplit() {
    if (splitApp) {
      setSplitApp(null);
      return;
    }
    // One-tap re-open with the remembered app; picker only on first use.
    const saved = await getItem(SPLIT_APP_KEY);
    if (saved) {
      try {
        const parsed = JSON.parse(saved) as SplitApp;
        if (parsed && typeof parsed.port === 'number') {
          setSplitApp(parsed);
          return;
        }
      } catch {
        /* fall through to picker */
      }
    }
    setPickerOpen(true);
  }

  function pickApp(port: number, name: string) {
    const app = { port, name };
    setSplitApp(app);
    setPickerOpen(false);
    void setItem(SPLIT_APP_KEY, JSON.stringify(app));
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
  // the bottom so it can't be hit by accident. The split toggle sits beside it.
  useLayoutEffect(() => {
    nav.setOptions({
      headerRight: () => (
        <View style={styles.headerBtns}>
          <Pressable
            onPress={() => void toggleSplit()}
            hitSlop={10}
            accessibilityRole="button"
            accessibilityLabel={splitApp ? 'Close app pane' : 'Show an app alongside'}
            style={styles.headerBtn}
          >
            <Ionicons
              name={splitApp ? 'contract-outline' : 'browsers-outline'}
              size={22}
              color={splitApp ? colors.accent : colors.text}
            />
          </Pressable>
          {active ? (
            <Pressable
              onPress={promptKill}
              hitSlop={10}
              accessibilityRole="button"
              accessibilityLabel="Kill task"
              style={styles.headerBtn}
            >
              <Ionicons name="stop-circle-outline" size={24} color={colors.danger} />
            </Pressable>
          ) : null}
        </View>
      ),
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nav, active, promptKill, splitApp]);

  // Pick one or more images from the library and upload each into the task's
  // attachments dir. Chips flip uploading → ready|error independently.
  async function pickImages() {
    const perm = await ImagePicker.requestMediaLibraryPermissionsAsync();
    if (!perm.granted) {
      setSendErr('Photo access is off — enable it in Settings to attach images.');
      return;
    }
    const result = await ImagePicker.launchImageLibraryAsync({
      mediaTypes: ['images'],
      allowsMultipleSelection: true,
      quality: 0.9,
    });
    if (result.canceled) return;
    setSendErr(null);
    for (const asset of result.assets) {
      const localId = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;
      setAttachments((a) => [...a, { id: localId, uri: asset.uri, status: 'uploading' }]);
      void (async () => {
        try {
          const path = await uploadTaskImage(id, asset);
          setAttachments((a) =>
            a.map((x) => (x.id === localId ? { ...x, path, status: 'ready' } : x)),
          );
        } catch (e) {
          setAttachments((a) =>
            a.map((x) => (x.id === localId ? { ...x, status: 'error' } : x)),
          );
          setSendErr(e instanceof Error ? e.message : 'Image upload failed');
        }
      })();
    }
  }

  function removeAttachment(aid: string) {
    setAttachments((a) => a.filter((x) => x.id !== aid));
  }

  async function send() {
    const text = msg.trim();
    const ready = attachments.filter((a) => a.status === 'ready' && a.path);
    // Hold send until uploads settle so their paths make it into the prompt.
    if (attachments.some((a) => a.status === 'uploading')) return;
    if (!text && ready.length === 0) return;
    setSending(true);
    setSendErr(null);
    // Append each uploaded image's absolute path on its own line — Claude Code
    // detects the path and reads the image as vision input.
    const finalText = [text, ...ready.map((a) => a.path as string)].filter(Boolean).join('\n');
    try {
      await sendMessage(id, finalText);
      setMsg('');
      setAttachments([]);
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
  const uploading = attachments.some((a) => a.status === 'uploading');
  const readyCount = attachments.filter((a) => a.status === 'ready').length;
  const canSend = (!!msg.trim() || readyCount > 0) && !sending && !uploading;

  return (
    <SafeAreaView style={styles.safe} edges={['bottom']}>
      <KeyboardAvoidingView
        behavior={Platform.OS === 'ios' ? 'padding' : undefined}
        style={{ flex: 1 }}
        keyboardVerticalOffset={90}
        onLayout={(e) => {
          splitAreaH.current = e.nativeEvent.layout.height;
        }}
      >
        {/* Top pane: the task itself. With the split open, the divider ratio
            says how much of the screen it keeps; drag the app pane's header
            bar to adjust. */}
        <View style={{ flex: splitApp ? splitRatio : 1 }}>
          <View style={styles.head}>
            <View style={styles.headTop}>
              <StatusPill status={task.status} />
              <View style={styles.headTopRight}>
                <Text style={styles.time}>{relativeTime(task.created_at)}</Text>
                <Text style={styles.id}>#{task.id}</Text>
              </View>
            </View>
            <Pressable
              onPress={() => setPromptExpanded((v) => !v)}
              hitSlop={4}
              accessibilityRole="button"
              accessibilityLabel={promptExpanded ? 'Collapse prompt' : 'Expand prompt'}
            >
              <Text style={styles.prompt} numberOfLines={promptExpanded ? undefined : 2}>
                {task.prompt}
              </Text>
            </Pressable>
            <View style={styles.metaRow}>
              <View style={styles.assistantChip}>
                <Ionicons name="hardware-chip-outline" size={11} color={colors.accent} />
                <Text style={styles.assistantText}>{task.assistant ?? 'claude'}</Text>
              </View>
              <Text style={styles.workdir} numberOfLines={1}>
                {task.workdir ?? '/home/dev'}
              </Text>
            </View>
          </View>

          {/* The real workspace terminal (ttyd/xterm.js) — the same session the
              web dashboard renders — with an archived-output fallback for
              finished tasks whose tmux session is gone. */}
          <TerminalView taskId={id} output={output} />

          {active ? (
            <View style={styles.composerZone}>
              {keysOpen ? (
                <ScrollView
                  horizontal
                  showsHorizontalScrollIndicator={false}
                  keyboardShouldPersistTaps="handled"
                  contentContainerStyle={styles.keyTray}
                >
                  {KEY_TRAY.map((k) => (
                    <Pressable
                      key={k.label}
                      onPress={() => void onTrayKey(k)}
                      accessibilityRole="button"
                      accessibilityLabel={k.hint}
                      style={({ pressed }) => [styles.keyChip, pressed && styles.keyChipPressed]}
                    >
                      <Text style={styles.keyChipText}>{k.label}</Text>
                    </Pressable>
                  ))}
                </ScrollView>
              ) : null}

              {attachments.length > 0 ? (
                <ScrollView
                  horizontal
                  showsHorizontalScrollIndicator={false}
                  keyboardShouldPersistTaps="handled"
                  contentContainerStyle={styles.attachTray}
                >
                  {attachments.map((a) => (
                    <View key={a.id} style={styles.attachChip}>
                      <Image source={{ uri: a.uri }} style={styles.attachThumb} />
                      {a.status !== 'ready' ? (
                        <View style={styles.attachOverlay}>
                          {a.status === 'uploading' ? (
                            <ActivityIndicator size="small" color={colors.accentText} />
                          ) : (
                            <Ionicons name="alert-circle" size={18} color={colors.danger} />
                          )}
                        </View>
                      ) : null}
                      <Pressable
                        onPress={() => removeAttachment(a.id)}
                        hitSlop={6}
                        accessibilityRole="button"
                        accessibilityLabel="Remove image"
                        style={styles.attachRemove}
                      >
                        <Ionicons name="close" size={12} color={colors.accentText} />
                      </Pressable>
                    </View>
                  ))}
                </ScrollView>
              ) : null}

              {sendErr ? (
                <Text style={styles.sendErr} accessibilityRole="alert">
                  Couldn't send: {sendErr}
                </Text>
              ) : null}

              <View style={styles.composer}>
                <Pressable
                  onPress={toggleKeys}
                  accessibilityRole="button"
                  accessibilityLabel={keysOpen ? 'Hide control keys' : 'Show control keys'}
                  accessibilityState={{ expanded: keysOpen }}
                  style={[styles.keysBtn, keysOpen && styles.keysBtnActive]}
                >
                  <Ionicons
                    name={keysOpen ? 'keypad' : 'keypad-outline'}
                    size={20}
                    color={keysOpen ? colors.accent : colors.textMuted}
                  />
                </Pressable>
                <Pressable
                  onPress={() => void pickImages()}
                  accessibilityRole="button"
                  accessibilityLabel="Attach image"
                  style={styles.keysBtn}
                >
                  <Ionicons name="image-outline" size={20} color={colors.textMuted} />
                </Pressable>
                <TextInput
                  value={msg}
                  onChangeText={setMsg}
                  placeholder="Send a follow-up…"
                  placeholderTextColor={colors.textFaint}
                  style={styles.composerInput}
                  multiline
                />
                <Pressable
                  onPress={() => void send()}
                  disabled={!canSend}
                  accessibilityRole="button"
                  accessibilityLabel="Send follow-up"
                  style={({ pressed }) => [
                    styles.sendWrap,
                    { opacity: !canSend ? 0.35 : pressed ? 0.85 : 1 },
                    pressed && canSend ? { transform: [{ scale: 0.94 }] } : null,
                  ]}
                >
                  <LinearGradient
                    colors={gradients.primary}
                    start={{ x: 0, y: 0 }}
                    end={{ x: 1, y: 1 }}
                    style={styles.sendBtn}
                  >
                    {sending ? (
                      <ActivityIndicator size="small" color={colors.accentText} />
                    ) : (
                      <Ionicons name="arrow-up" size={20} color={colors.accentText} />
                    )}
                  </LinearGradient>
                </Pressable>
              </View>
            </View>
          ) : (
            <View
              style={[styles.finishedBar, { backgroundColor: statusColor(task.status) + '14' }]}
            >
              <Ionicons name={note.icon} size={17} color={statusColor(task.status)} />
              <Text style={styles.finishedText}>{note.text}</Text>
            </View>
          )}
        </View>

        {splitApp ? (
          <View style={[styles.appPane, { flex: 1 - splitRatio }]}>
            <View
              style={styles.paneBar}
              {...splitPan.panHandlers}
              accessibilityHint="Drag up or down to resize the panes"
            >
              <View style={styles.paneGrip} pointerEvents="none" />
              <Ionicons name="globe-outline" size={14} color={colors.accent} />
              <Text style={styles.paneTitle} numberOfLines={1}>
                {splitApp.name}
                <Text style={styles.panePort}>  :{splitApp.port}</Text>
              </Text>
              <Pressable
                onPress={() => setPickerOpen(true)}
                hitSlop={8}
                accessibilityRole="button"
                accessibilityLabel="Change app"
                style={styles.paneBtn}
              >
                <Ionicons name="swap-horizontal" size={16} color={colors.textMuted} />
              </Pressable>
              <Pressable
                onPress={() => setSplitApp(null)}
                hitSlop={8}
                accessibilityRole="button"
                accessibilityLabel="Close app pane"
                style={styles.paneBtn}
              >
                <Ionicons name="close" size={16} color={colors.textMuted} />
              </Pressable>
            </View>
            <AppEmbed compact port={splitApp.port} name={splitApp.name} />
          </View>
        ) : null}

        <AppPickerSheet
          visible={pickerOpen}
          onPick={pickApp}
          onClose={() => setPickerOpen(false)}
        />
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: colors.bg },
  headerBtns: { flexDirection: 'row', alignItems: 'center' },
  headerBtn: { paddingHorizontal: space.sm, paddingVertical: 2 },

  // ---- task summary zone ----
  head: {
    paddingHorizontal: space.lg,
    paddingTop: space.md,
    paddingBottom: space.md,
    gap: space.sm,
    borderBottomWidth: 1,
    borderBottomColor: colors.border,
  },
  headTop: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' },
  headTopRight: { flexDirection: 'row', alignItems: 'center', gap: space.sm },
  time: { color: colors.textFaint, fontSize: font.size.xs },
  id: { color: colors.textFaint, fontSize: font.size.xs, fontFamily: font.mono },
  prompt: { color: colors.text, fontSize: 16, fontWeight: '600', lineHeight: 22 },
  metaRow: { flexDirection: 'row', alignItems: 'center', gap: space.sm },
  assistantChip: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 4,
    backgroundColor: colors.accent + '14',
    borderRadius: radius.pill,
    paddingHorizontal: space.sm,
    paddingVertical: 3,
  },
  assistantText: { color: colors.accent, fontSize: font.size.xs, fontWeight: '700' },
  workdir: { flex: 1, color: colors.textFaint, fontSize: font.size.xs, fontFamily: font.mono },

  // ---- composer ----
  composerZone: { borderTopWidth: 1, borderTopColor: colors.border, backgroundColor: colors.bg },
  keyTray: {
    flexGrow: 1,
    justifyContent: 'center',
    flexDirection: 'row',
    gap: space.sm,
    paddingHorizontal: space.lg,
    paddingTop: space.sm,
  },
  keyChip: {
    height: 34,
    borderRadius: 17,
    paddingHorizontal: space.md,
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: colors.bgElevated,
    borderWidth: 1,
    borderColor: colors.border,
  },
  keyChipPressed: { backgroundColor: colors.cardHover, borderColor: colors.borderStrong },
  keyChipText: { color: colors.text, fontSize: font.size.sm, fontWeight: '600' },
  // ---- attached-image chips ----
  attachTray: {
    flexDirection: 'row',
    gap: space.sm,
    paddingHorizontal: space.lg,
    paddingTop: space.sm,
  },
  attachChip: {
    width: 56,
    height: 56,
    borderRadius: 10,
    overflow: 'hidden',
    backgroundColor: colors.bgElevated,
    borderWidth: 1,
    borderColor: colors.border,
  },
  attachThumb: { width: '100%', height: '100%' },
  attachOverlay: {
    position: 'absolute',
    top: 0,
    left: 0,
    right: 0,
    bottom: 0,
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: 'rgba(0,0,0,0.35)',
  },
  attachRemove: {
    position: 'absolute',
    top: 2,
    right: 2,
    width: 18,
    height: 18,
    borderRadius: 9,
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: 'rgba(0,0,0,0.6)',
  },
  sendErr: {
    color: colors.danger,
    fontSize: font.size.xs,
    paddingHorizontal: space.lg,
    paddingTop: space.sm,
  },
  composer: {
    flexDirection: 'row',
    alignItems: 'flex-end',
    gap: space.sm,
    paddingHorizontal: space.md,
    paddingVertical: space.md,
  },
  keysBtn: {
    width: 44,
    height: 44,
    borderRadius: 22,
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: colors.bgElevated,
    borderWidth: 1,
    borderColor: colors.border,
  },
  keysBtnActive: { backgroundColor: colors.accent + '1f', borderColor: colors.accent + '66' },
  composerInput: {
    flex: 1,
    minHeight: 44,
    maxHeight: 120,
    backgroundColor: colors.bgElevated,
    borderRadius: 22,
    borderWidth: 1,
    borderColor: colors.border,
    color: colors.text,
    paddingHorizontal: space.lg,
    paddingTop: 12,
    paddingBottom: 12,
    fontSize: font.size.md,
    lineHeight: 20,
  },
  sendWrap: { borderRadius: 22 },
  sendBtn: {
    width: 44,
    height: 44,
    borderRadius: 22,
    alignItems: 'center',
    justifyContent: 'center',
  },

  // ---- finished ----
  finishedBar: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
    gap: space.sm,
    paddingVertical: space.lg,
    borderTopWidth: 1,
    borderTopColor: colors.border,
  },
  finishedText: { color: colors.textMuted, fontSize: font.size.sm, fontWeight: '600' },

  // ---- split view ----
  appPane: { borderTopWidth: 2, borderTopColor: colors.borderStrong },
  paneBar: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.sm,
    paddingHorizontal: space.md,
    paddingTop: 10,
    paddingBottom: 6,
    backgroundColor: colors.bgElevated,
    borderBottomWidth: 1,
    borderBottomColor: colors.border,
  },
  // Centered pill signalling the bar doubles as the split's drag handle.
  paneGrip: {
    position: 'absolute',
    top: 3,
    left: '50%',
    marginLeft: -16,
    width: 32,
    height: 3,
    borderRadius: 2,
    backgroundColor: colors.borderStrong,
  },
  paneTitle: { flex: 1, color: colors.text, fontSize: font.size.sm, fontWeight: '700' },
  panePort: { color: colors.textFaint, fontWeight: '400', fontFamily: font.mono },
  paneBtn: { padding: 4 },
});
