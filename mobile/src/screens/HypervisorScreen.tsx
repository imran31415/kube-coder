/**
 * Hypervisor — the workspace-aware chat tab, on mobile. Talks to the same
 * /api/hypervisor facade as the dashboard SPA: a thread is a structured agent
 * session and the server returns a canonical event stream (no terminal, no
 * scraping), which buildTurns() folds into user bubbles + agent turns (prose +
 * expandable tool-activity chips). See charts/workspace/hypervisor_session.py.
 */
import { type ReactNode, useCallback, useEffect, useRef, useState } from 'react';
import {
  Image,
  KeyboardAvoidingView,
  Linking,
  Modal,
  type NativeScrollEvent,
  type NativeSyntheticEvent,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import { useNavigation, useRoute } from '@react-navigation/native';
import { SafeAreaView, useSafeAreaInsets } from 'react-native-safe-area-context';
import * as ImagePicker from 'expo-image-picker';
import { useVideoPlayer, VideoView } from 'expo-video';
import {
  authHeaders,
  createThread,
  deleteThread,
  fileDownloadBrowserUrl,
  fileRawUrl,
  fileViewUrl,
  getHypervisorConfig,
  getThreadDetail,
  listDeletedThreads,
  listThreads,
  listWorkspaceDirs,
  previewFile,
  restoreThread,
  renameThread,
  sendThreadMessage,
  stopThread,
  uploadTaskImage,
} from '../api/client';
import { AppEmbed } from '../components/AppEmbed';
import { WebView } from '../components/PlatformWebView';
import { Markdown } from '../components/Markdown';
import type { FilePreview, HvEvent, HypervisorConfig, HypervisorThread, WorkdirOption } from '../api/types';
import { buildTurns, type HvBlock } from '../util/hvTranscript';
import { EmptyState, ErrorBanner, ScreenHeader } from '../components/ui';
import { confirmAction } from '../util/confirm';
import { relativeTime } from '../util/format';
import { useKeyboardVisible } from '../util/useKeyboard';
import { colors, font, radius, space } from '../theme';

const SUGGESTIONS = [
  "What's running and how much CPU am I using?",
  'Spin up a task to run the tests',
  'Remember that I deploy with `make ship`',
];

/** A picked image being uploaded so the agent can read it — its saved absolute
 *  path is appended to the outgoing message (same as the Build tab). */
interface Attachment {
  id: string;
  uri: string;
  path?: string;
  status: 'uploading' | 'ready' | 'error';
}

/** Params other screens (the Desktop composer, its Activity feed) can pass when
 *  navigating to the Hypervisor tab: seed + auto-send a first message, or open
 *  an existing thread. Consumed once, then cleared. */
type HvParams = { initialMessage?: string; openThreadId?: string } | undefined;

export default function HypervisorScreen() {
  const insets = useSafeAreaInsets();
  const navigation = useNavigation();
  const route = useRoute();
  const keyboardVisible = useKeyboardVisible();
  const [config, setConfig] = useState<HypervisorConfig | null>(null);
  const [threads, setThreads] = useState<HypervisorThread[]>([]);
  // "Recently deleted" — soft-deleted threads (issue #260). Loaded lazily the
  // first time the trash section is opened in the chats sheet.
  const [deletedThreads, setDeletedThreads] = useState<HypervisorThread[]>([]);
  const [trashOpen, setTrashOpen] = useState(false);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [events, setEvents] = useState<HvEvent[]>([]);
  const [status, setStatus] = useState<string>('');
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [stopping, setStopping] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [chatsOpen, setChatsOpen] = useState(false);
  // Assistant chosen for the NEXT new chat (existing threads keep their own).
  const [selectedAssistant, setSelectedAssistant] = useState<string | undefined>(undefined);
  // Folder the NEXT new chat starts in (#370, parity with web #345/#368) —
  // seeded from config.workdir so the picker shows the real server default.
  // '' omits workdir on create, letting the server default apply. A thread
  // keeps the folder it was created in for life.
  const [selectedWorkdir, setSelectedWorkdir] = useState('');
  const [dirs, setDirs] = useState<WorkdirOption[]>([]);
  const scrollRef = useRef<ScrollView | null>(null);
  const optimisticSeq = useRef(-1);
  // Whether the view is pinned to the bottom. We only auto-scroll on new events
  // while pinned — so scrolling up to read history isn't yanked back down by the
  // 2s poll. Starts true; onScroll flips it as the user scrolls.
  const pinnedRef = useRef(true);

  const refreshThreads = useCallback(async () => {
    try {
      setThreads(await listThreads());
    } catch {
      /* keep last-good */
    }
  }, []);

  const refreshDeletedThreads = useCallback(async () => {
    try {
      setDeletedThreads(await listDeletedThreads());
    } catch {
      /* keep last-good */
    }
  }, []);

  useEffect(() => {
    void getHypervisorConfig()
      .then((c) => {
        setConfig(c);
        // Seed the new-chat assistant picker with the workspace default.
        setSelectedAssistant((prev) => prev ?? c.defaultAssistant);
        // Seed the folder picker with the server default (HYPERVISOR_WORKDIR).
        setSelectedWorkdir((prev) => prev || c.workdir || '');
      })
      .catch((e) => setError(e instanceof Error ? e.message : 'Failed to load config'));
    // Folder choices for new chats; an empty list falls back to free text.
    void listWorkspaceDirs()
      .then(setDirs)
      .catch(() => setDirs([]));
    void refreshThreads();
  }, [refreshThreads]);

  // Poll the open thread's canonical events while it's active.
  useEffect(() => {
    if (!activeId) return;
    let alive = true;
    const tick = async () => {
      try {
        const d = await getThreadDetail(activeId, 0);
        if (!alive || d.thread.id !== activeId) return;
        setEvents(d.events);
        setStatus(d.thread.status);
      } catch {
        /* transient */
      }
    };
    void tick();
    const timer = setInterval(tick, 2000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [activeId]);

  useEffect(() => {
    if (!pinnedRef.current) return;
    const t = setTimeout(() => scrollRef.current?.scrollToEnd({ animated: true }), 60);
    return () => clearTimeout(t);
  }, [events]);

  // A freshly opened thread starts pinned to the bottom.
  useEffect(() => {
    pinnedRef.current = true;
  }, [activeId]);

  // Pinned when within ~80px of the bottom. Scrolling up unpins (so polls stop
  // pulling down); scrolling back to the bottom re-pins.
  function onTranscriptScroll(e: NativeSyntheticEvent<NativeScrollEvent>) {
    const { contentOffset, contentSize, layoutMeasurement } = e.nativeEvent;
    pinnedRef.current = contentSize.height - contentOffset.y - layoutMeasurement.height < 80;
  }

  // Deep-link handling: the Desktop composer navigates here with an
  // `initialMessage` to seed a fresh chat, and its Activity feed with an
  // `openThreadId` to reopen an existing one. Consume the params exactly once
  // (clear them) so switching back to this tab later doesn't resend.
  useEffect(() => {
    const params = route.params as HvParams;
    if (!params) return;
    if (params.openThreadId) {
      openThread(params.openThreadId);
    } else if (params.initialMessage) {
      void send(params.initialMessage);
    }
    if (params.openThreadId || params.initialMessage) {
      // The tab navigator has no typed params for this screen; clear the
      // consumed ones so a later tab switch doesn't replay them.
      (navigation.setParams as (p: HvParams) => void)({
        initialMessage: undefined,
        openThreadId: undefined,
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [route.params]);

  function openThread(id: string) {
    setActiveId(id);
    setEvents([]);
    setStatus('');
    setError(null);
    setChatsOpen(false);
  }

  function newChat() {
    setActiveId(null);
    setEvents([]);
    setStatus('');
    setError(null);
    setDraft('');
    setChatsOpen(false);
  }

  async function removeThread(id: string) {
    try {
      await deleteThread(id);
    } catch {
      /* best effort */
    }
    if (activeId === id) newChat();
    void refreshThreads();
    // Keep the trash section in sync only if it's already been loaded.
    if (deletedThreads.length > 0 || trashOpen) void refreshDeletedThreads();
  }

  // Optimistically patch the in-memory list so the sheet + header update
  // instantly, then confirm against the server (rolling back on failure).
  async function renameThreadTitle(id: string, title: string) {
    const next = title.trim();
    if (!next) return;
    const prev = threads;
    setThreads((list) => list.map((t) => (t.id === id ? { ...t, title: next } : t)));
    try {
      await renameThread(id, next);
      void refreshThreads();
    } catch (e) {
      setThreads(prev);
      setError(e instanceof Error ? e.message : 'Failed to rename');
    }
  }

  function confirmRemove(t: HypervisorThread) {
    confirmAction({
      title: 'Delete chat?',
      message: t.title || 'New chat',
      confirmLabel: 'Delete',
      destructive: true,
      onConfirm: () => void removeThread(t.id),
    });
  }

  async function reviveThread(id: string) {
    try {
      await restoreThread(id);
    } catch {
      /* best effort */
    }
    void Promise.all([refreshThreads(), refreshDeletedThreads()]);
  }

  function toggleTrash() {
    setTrashOpen((v) => {
      const next = !v;
      if (next) void refreshDeletedThreads();
      return next;
    });
  }

  function uploadAssets(assets: ImagePicker.ImagePickerAsset[]) {
    for (const asset of assets) {
      const id = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
      setAttachments((a) => [...a, { id, uri: asset.uri, status: 'uploading' }]);
      // Reuse the Build tab's uploader; 'hypervisor' → .claude-tasks/hypervisor/attachments.
      uploadTaskImage('hypervisor', asset)
        .then((path) =>
          setAttachments((a) => a.map((x) => (x.id === id ? { ...x, path, status: 'ready' } : x))),
        )
        .catch(() =>
          setAttachments((a) => a.map((x) => (x.id === id ? { ...x, status: 'error' } : x))),
        );
    }
  }

  async function pickImage() {
    const perm = await ImagePicker.requestMediaLibraryPermissionsAsync();
    if (!perm.granted) {
      setError('Photo access is off — enable it in Settings to attach images.');
      return;
    }
    const result = await ImagePicker.launchImageLibraryAsync({
      mediaTypes: ['images'],
      allowsMultipleSelection: true,
      quality: 0.9,
    });
    if (!result.canceled) uploadAssets(result.assets);
  }

  function removeAttachment(id: string) {
    setAttachments((a) => a.filter((x) => x.id !== id));
  }

  async function send(text?: string) {
    if (sending || attachments.some((a) => a.status === 'uploading')) return;
    const msg = (text ?? draft).trim();
    const paths = attachments
      .filter((a) => a.status === 'ready' && a.path)
      .map((a) => a.path as string);
    if (!msg && paths.length === 0) return;
    // Append each uploaded image's absolute path on its own line — Claude reads
    // the image by path (same as the Build tab composer).
    const finalText = [msg, ...paths].filter(Boolean).join('\n');
    pinnedRef.current = true; // sending your own message re-pins to the bottom
    setSending(true);
    setError(null);
    setDraft('');
    setAttachments([]);
    setStatus('running');
    // Optimistic user turn (negative seq so it never collides with server seqs).
    setEvents((prev) => [
      ...prev,
      { seq: optimisticSeq.current--, ts: Date.now() / 1000, role: 'user', type: 'message', text: finalText },
    ]);
    try {
      if (!activeId) {
        const thread = await createThread(
          finalText,
          selectedAssistant || config?.defaultAssistant,
          selectedWorkdir.trim() || undefined,
        );
        await refreshThreads();
        openThread(thread.id);
      } else {
        await sendThreadMessage(activeId, finalText);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to send');
    } finally {
      setSending(false);
    }
  }

  async function stop() {
    if (!activeId || stopping) return;
    setStopping(true);
    try {
      await stopThread(activeId);
      // Reflect the halt immediately rather than waiting for the next poll.
      const d = await getThreadDetail(activeId, 0);
      if (d.thread.id === activeId) {
        setEvents(d.events);
        setStatus(d.thread.status);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to stop');
    } finally {
      setStopping(false);
    }
  }

  if (config && config.enabled === false) {
    return (
      <SafeAreaView style={styles.root} edges={['top']}>
        <ScreenHeader title="Hypervisor" />
        <EmptyState icon="hardware-chip-outline" title="Hypervisor is disabled" subtitle="Enable it in the workspace chart (hypervisor.enabled)." />
      </SafeAreaView>
    );
  }

  const turns = buildTurns(events);
  const agentName = config?.defaultAssistant || 'claude';
  const activeThread = threads.find((t) => t.id === activeId) || null;
  const working = status === 'running';
  const blocked = sending || working;
  const canSend = !!draft.trim() || attachments.some((a) => a.status === 'ready');
  const empty = !activeId && events.length === 0;

  return (
    <SafeAreaView style={styles.root} edges={['top']}>
      <ScreenHeader
        title={activeThread ? activeThread.title || 'Chat' : 'Hypervisor'}
        subtitle={activeThread ? `via ${activeThread.assistant || agentName}` : 'Talk to your workspace'}
        right={
          <View style={styles.headerActions}>
            {threads.length > 0 && (
              <Pressable
                onPress={() => setChatsOpen(true)}
                hitSlop={8}
                accessibilityRole="button"
                accessibilityLabel={`Past chats, ${threads.length}`}
                style={({ pressed }) => [styles.chatsBtn, pressed && { opacity: 0.6 }]}
              >
                <Ionicons name="chatbubbles-outline" size={16} color={colors.text} />
                <Text style={styles.chatsBtnText}>{threads.length}</Text>
              </Pressable>
            )}
            <Pressable onPress={newChat} hitSlop={8} style={({ pressed }) => [styles.newBtn, pressed && { opacity: 0.9 }]}>
              <Ionicons name="add" size={18} color={colors.accentText} />
              <Text style={styles.newBtnText}>New</Text>
            </Pressable>
          </View>
        }
      />

      <ChatsSheet
        visible={chatsOpen}
        threads={threads}
        activeId={activeId}
        onClose={() => setChatsOpen(false)}
        onOpen={openThread}
        onDelete={confirmRemove}
        onRename={renameThreadTitle}
        onNew={newChat}
        deletedThreads={deletedThreads}
        trashOpen={trashOpen}
        onToggleTrash={toggleTrash}
        onRestore={(t) => void reviveThread(t.id)}
      />

      <KeyboardAvoidingView
        style={styles.flex}
        behavior={Platform.OS === 'ios' ? 'padding' : undefined}
        keyboardVerticalOffset={0}
      >
        <ScrollView
          ref={scrollRef}
          style={styles.flex}
          contentContainerStyle={styles.transcript}
          onScroll={onTranscriptScroll}
          scrollEventThrottle={100}
        >
          {empty ? (
            <View style={styles.welcome}>
              <EmptyState
                icon="hardware-chip-outline"
                title="Kube-Coder"
                subtitle="Ask about your workspace or tell it what to do — it reads live state and acts on it through your tools."
              />
              <View style={styles.suggests}>
                {SUGGESTIONS.map((s) => (
                  <Pressable key={s} onPress={() => void send(s)} style={styles.suggest} disabled={blocked}>
                    <Text style={styles.suggestText}>{s}</Text>
                  </Pressable>
                ))}
              </View>
            </View>
          ) : (
            turns.map((turn, i) =>
              turn.role === 'user' ? (
                <View key={i} style={styles.userRow}>
                  <View style={styles.userBubble}>
                    <Text style={styles.userText}>{turn.text}</Text>
                  </View>
                </View>
              ) : (
                <View key={i} style={styles.agentRow}>
                  <View style={styles.agentHead}>
                    <Ionicons name="hardware-chip-outline" size={13} color={colors.textMuted} />
                    <Text style={styles.agentName}>Kube-Coder</Text>
                    <Text style={styles.agentVia}>via {agentName}</Text>
                    {working && i === turns.length - 1 && <Text style={styles.working}>· working…</Text>}
                  </View>
                  {turn.blocks.map((b, j) => (
                    <Block
                      key={j}
                      block={b}
                      id={`${i}:${j}`}
                      expanded={expanded}
                      setExpanded={setExpanded}
                      interactive={i === turns.length - 1 && !working}
                      onChoose={(t) => void send(t)}
                    />
                  ))}
                </View>
              ),
            )
          )}
          {!empty && working && turns[turns.length - 1]?.role !== 'agent' && (
            <View style={styles.agentRow}>
              <View style={styles.agentHead}>
                <Ionicons name="hardware-chip-outline" size={13} color={colors.textMuted} />
                <Text style={styles.agentName}>Kube-Coder</Text>
                <Text style={styles.working}>· working…</Text>
              </View>
            </View>
          )}
        </ScrollView>

        {error && <ErrorBanner message={error} />}

        {attachments.length > 0 && (
          <ScrollView
            horizontal
            showsHorizontalScrollIndicator={false}
            style={styles.attachStrip}
            contentContainerStyle={styles.attachStripInner}
          >
            {attachments.map((a) => (
              <View key={a.id} style={[styles.attachThumb, a.status === 'error' && styles.attachThumbErr]}>
                <Image source={{ uri: a.uri }} style={styles.attachImg} />
                {a.status === 'uploading' && <View style={styles.attachDim} />}
                <Pressable style={styles.attachX} onPress={() => removeAttachment(a.id)} hitSlop={6}>
                  <Ionicons name="close" size={11} color="#fff" />
                </Pressable>
              </View>
            ))}
          </ScrollView>
        )}

        {/* New-chat assistant picker — parity with the web Hypervisor agent
            select. Only shown when starting a NEW chat (no active thread) and
            more than one assistant is available; existing threads keep the
            assistant they were created with. */}
        {!activeThread && (config?.assistants?.length ?? 0) > 1 && (
          <ScrollView
            horizontal
            showsHorizontalScrollIndicator={false}
            style={styles.asstRow}
            contentContainerStyle={styles.asstRowContent}
            keyboardShouldPersistTaps="handled"
          >
            {config!.assistants.map((a) => {
              const on = (selectedAssistant || config?.defaultAssistant) === a.id;
              return (
                <Pressable
                  key={a.id}
                  onPress={() => setSelectedAssistant(a.id)}
                  style={[styles.asstChip, on && styles.asstChipOn]}
                >
                  <Text style={[styles.asstChipText, on && styles.asstChipTextOn]}>{a.label || a.id}</Text>
                </Pressable>
              );
            })}
          </ScrollView>
        )}

        {/* New-chat folder picker (#370) — parity with the web sidebar Folder
            select (#345/#368). Only shown when starting a NEW chat; an open
            thread keeps the folder it was created in. The server default
            (config.workdir) is offered as the first chip since /api/workspace/
            dirs only lists folders UNDER it; when the list is empty entirely,
            fall back to free text like the web picker and NewTaskScreen. */}
        {!activeThread &&
          (dirs.length > 0 ? (
            <ScrollView
              horizontal
              showsHorizontalScrollIndicator={false}
              style={styles.asstRow}
              contentContainerStyle={styles.asstRowContent}
              keyboardShouldPersistTaps="handled"
            >
              <Ionicons name="folder-outline" size={14} color={colors.textFaint} />
              {config?.workdir && !dirs.some((d) => d.path === config.workdir) && (
                <Pressable
                  onPress={() => setSelectedWorkdir(config.workdir)}
                  style={[styles.asstChip, selectedWorkdir === config.workdir && styles.asstChipOn]}
                >
                  <Text
                    style={[styles.asstChipText, selectedWorkdir === config.workdir && styles.asstChipTextOn]}
                  >
                    {config.workdir}
                  </Text>
                </Pressable>
              )}
              {dirs.map((d) => {
                const on = selectedWorkdir === d.path;
                return (
                  <Pressable
                    key={d.path}
                    onPress={() => setSelectedWorkdir(d.path)}
                    style={[styles.asstChip, on && styles.asstChipOn]}
                  >
                    <Text style={[styles.asstChipText, on && styles.asstChipTextOn]}>
                      {(d.label ?? d.path) + (d.is_git_repo ? ' (git)' : '')}
                    </Text>
                  </Pressable>
                );
              })}
            </ScrollView>
          ) : (
            <View style={styles.workdirRow}>
              <Ionicons name="folder-outline" size={14} color={colors.textFaint} />
              <TextInput
                style={styles.workdirInput}
                value={selectedWorkdir}
                onChangeText={setSelectedWorkdir}
                autoCapitalize="none"
                autoCorrect={false}
                placeholder={config?.workdir || '/home/dev'}
                placeholderTextColor={colors.textFaint}
                accessibilityLabel="Folder for new chats"
              />
            </View>
          ))}

        <View
          style={[
            styles.composer,
            // Keyboard up → it already covers the home indicator, so drop the
            // safe-area inset that would otherwise leave a gap under the input.
            { paddingBottom: keyboardVisible ? space.sm : Math.max(insets.bottom, space.sm) },
          ]}
        >
          <Pressable
            onPress={() => void pickImage()}
            disabled={blocked}
            style={[styles.attachBtn, blocked && styles.sendBtnOff]}
            hitSlop={6}
          >
            <Ionicons name="image-outline" size={20} color={colors.textMuted} />
          </Pressable>
          <TextInput
            style={styles.input}
            value={draft}
            onChangeText={setDraft}
            placeholder={working ? 'Kube-Coder is working…' : 'Message Kube-Coder…'}
            placeholderTextColor={colors.textFaint}
            multiline
            // Lock input for the whole turn, not just the send request, so the
            // user can't queue a message the server would reject (409).
            editable={!blocked}
          />
          {working ? (
            <Pressable
              onPress={() => void stop()}
              disabled={stopping}
              style={[styles.sendBtn, styles.stopBtn, stopping && styles.sendBtnOff]}
            >
              <Ionicons name="stop" size={18} color={colors.accentText} />
            </Pressable>
          ) : (
            <Pressable
              onPress={() => void send()}
              disabled={blocked || !canSend}
              style={[styles.sendBtn, (blocked || !canSend) && styles.sendBtnOff]}
            >
              <Ionicons name="arrow-up" size={20} color={colors.accentText} />
            </Pressable>
          )}
        </View>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

/**
 * Past-chats sheet — the workspace's chat history as a first-class, discoverable
 * surface. Slides up from the bottom; each row switches on tap and deletes via
 * an explicit trash button (with a confirm), replacing the old undiscoverable
 * long-press on a cramped chip row.
 */
function ChatsSheet({
  visible,
  threads,
  activeId,
  onClose,
  onOpen,
  onDelete,
  onRename,
  onNew,
  deletedThreads,
  trashOpen,
  onToggleTrash,
  onRestore,
}: {
  visible: boolean;
  threads: HypervisorThread[];
  activeId: string | null;
  onClose: () => void;
  onOpen: (id: string) => void;
  onDelete: (t: HypervisorThread) => void;
  onRename: (id: string, title: string) => void;
  onNew: () => void;
  deletedThreads: HypervisorThread[];
  trashOpen: boolean;
  onToggleTrash: () => void;
  onRestore: (t: HypervisorThread) => void;
}) {
  const insets = useSafeAreaInsets();
  // Inline rename: the row being edited plus its draft text.
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [draftTitle, setDraftTitle] = useState('');

  function startRename(t: HypervisorThread) {
    setRenamingId(t.id);
    setDraftTitle(t.title || '');
  }
  function cancelRename() {
    setRenamingId(null);
    setDraftTitle('');
  }
  function commitRename(id: string) {
    const next = draftTitle.trim();
    if (next) onRename(id, next);
    cancelRename();
  }
  return (
    <Modal
      visible={visible}
      animationType="slide"
      transparent
      onRequestClose={() => {
        cancelRename();
        onClose();
      }}
    >
      <Pressable
        style={styles.sheetScrim}
        onPress={() => {
          cancelRename();
          onClose();
        }}
      />
      <View style={[styles.sheet, { paddingBottom: Math.max(insets.bottom, space.md) }]}>
        <View style={styles.sheetGrip} />
        <View style={styles.sheetHead}>
          <Text style={styles.sheetTitle}>Chats</Text>
          <Pressable onPress={onNew} hitSlop={8} style={({ pressed }) => [styles.sheetNew, pressed && { opacity: 0.9 }]}>
            <Ionicons name="add" size={16} color={colors.accentText} />
            <Text style={styles.newBtnText}>New</Text>
          </Pressable>
        </View>
        <ScrollView style={styles.sheetList} contentContainerStyle={styles.sheetListInner}>
          {threads.map((t) => {
            const on = t.id === activeId;
            if (renamingId === t.id) {
              return (
                <View key={t.id} style={[styles.chatRow, styles.chatRowEditing, on && styles.chatRowOn]}>
                  <TextInput
                    style={styles.chatRenameInput}
                    value={draftTitle}
                    onChangeText={setDraftTitle}
                    autoFocus
                    maxLength={80}
                    returnKeyType="done"
                    onSubmitEditing={() => commitRename(t.id)}
                    placeholder="Chat name"
                    placeholderTextColor={colors.textFaint}
                    accessibilityLabel="Chat name"
                  />
                  <Pressable
                    onPress={() => commitRename(t.id)}
                    hitSlop={8}
                    accessibilityRole="button"
                    accessibilityLabel="Save name"
                    style={({ pressed }) => [styles.chatDel, pressed && { opacity: 0.6 }]}
                  >
                    <Ionicons name="checkmark" size={18} color={colors.accent} />
                  </Pressable>
                  <Pressable
                    onPress={cancelRename}
                    hitSlop={8}
                    accessibilityRole="button"
                    accessibilityLabel="Cancel rename"
                    style={({ pressed }) => [styles.chatDel, pressed && { opacity: 0.6 }]}
                  >
                    <Ionicons name="close" size={18} color={colors.textFaint} />
                  </Pressable>
                </View>
              );
            }
            return (
              <View key={t.id} style={[styles.chatRow, on && styles.chatRowOn]}>
                <Pressable onPress={() => onOpen(t.id)} onLongPress={() => startRename(t)} style={styles.chatRowMain}>
                  <View style={[styles.dot, { backgroundColor: t.status === 'running' ? colors.running : colors.killed }]} />
                  <View style={styles.chatRowBody}>
                    <Text numberOfLines={1} style={[styles.chatRowTitle, on && { color: colors.text }]}>
                      {t.title || 'New chat'}
                    </Text>
                    <Text numberOfLines={1} style={styles.chatRowMeta}>
                      {t.assistant || 'agent'}
                      {t.updated_at ? ` · ${relativeTime(t.updated_at)}` : ''}
                    </Text>
                  </View>
                </Pressable>
                <Pressable
                  onPress={() => startRename(t)}
                  hitSlop={8}
                  accessibilityRole="button"
                  accessibilityLabel="Rename chat"
                  style={({ pressed }) => [styles.chatDel, pressed && { opacity: 0.6 }]}
                >
                  <Ionicons name="pencil" size={16} color={colors.textFaint} />
                </Pressable>
                <Pressable
                  onPress={() => onDelete(t)}
                  hitSlop={8}
                  accessibilityRole="button"
                  accessibilityLabel="Delete chat"
                  style={({ pressed }) => [styles.chatDel, pressed && { opacity: 0.6 }]}
                >
                  <Ionicons name="trash-outline" size={17} color={colors.textFaint} />
                </Pressable>
              </View>
            );
          })}
        </ScrollView>

        {/* Recently deleted — a collapsible trash so an accidental delete is
            recoverable (issue #260). Soft-deleted threads keep their files;
            Restore clears the tombstone. Server-side GC hard-purges old ones. */}
        <Pressable
          onPress={onToggleTrash}
          style={styles.trashToggle}
          accessibilityRole="button"
          accessibilityState={{ expanded: trashOpen }}
        >
          <Ionicons name={trashOpen ? 'chevron-down' : 'chevron-forward'} size={13} color={colors.textFaint} />
          <Text style={styles.trashToggleText}>Recently deleted</Text>
          {deletedThreads.length > 0 && (
            <View style={styles.trashCount}>
              <Text style={styles.trashCountText}>{deletedThreads.length}</Text>
            </View>
          )}
        </Pressable>
        {trashOpen && (
          <ScrollView style={styles.trashList} contentContainerStyle={styles.sheetListInner}>
            {deletedThreads.length === 0 && <Text style={styles.trashEmpty}>Nothing here.</Text>}
            {deletedThreads.map((t) => (
              <View key={t.id} style={styles.chatRow}>
                <View style={styles.chatRowMain}>
                  <View style={[styles.dot, { backgroundColor: colors.textFaint }]} />
                  <View style={styles.chatRowBody}>
                    <Text numberOfLines={1} style={[styles.chatRowTitle, styles.chatRowTitleDeleted]}>
                      {t.title || 'New chat'}
                    </Text>
                    <Text numberOfLines={1} style={styles.chatRowMeta}>
                      {t.assistant || 'agent'}
                    </Text>
                  </View>
                </View>
                <Pressable
                  onPress={() => onRestore(t)}
                  hitSlop={8}
                  accessibilityRole="button"
                  accessibilityLabel="Restore chat"
                  style={({ pressed }) => [styles.restoreBtn, pressed && { opacity: 0.6 }]}
                >
                  <Text style={styles.restoreBtnText}>Restore</Text>
                </Pressable>
              </View>
            ))}
          </ScrollView>
        )}
      </View>
    </Modal>
  );
}

function Block({
  block,
  id,
  expanded,
  setExpanded,
  interactive,
  onChoose,
}: {
  block: HvBlock;
  id: string;
  expanded: Set<string>;
  setExpanded: (s: Set<string>) => void;
  interactive: boolean;
  onChoose: (text: string) => void;
}) {
  if (block.kind === 'prose') {
    return <Markdown text={block.text} />;
  }
  if (block.kind === 'embed') {
    return <EmbedBlock port={block.port} title={block.title} height={block.height} />;
  }
  if (block.kind === 'media') {
    return <MediaBlock block={block} />;
  }
  if (block.kind === 'file') {
    return <FileBlock block={block} />;
  }
  if (block.kind === 'choice') {
    return <ChoiceBlock question={block.question} options={block.options} interactive={interactive} onChoose={onChoose} />;
  }
  const open = expanded.has(id);
  const toggle = () => {
    const next = new Set(expanded);
    open ? next.delete(id) : next.add(id);
    setExpanded(next);
  };
  return (
    <View style={[styles.activity, block.error && styles.activityErr]}>
      <Pressable onPress={toggle} style={styles.activityHead}>
        <Ionicons name="terminal-outline" size={13} color={block.error ? colors.danger : colors.textMuted} />
        <Text style={[styles.activityLabel, block.error && { color: colors.danger }]} numberOfLines={1}>
          {block.label}
        </Text>
        <Ionicons name={open ? 'chevron-up' : 'chevron-down'} size={14} color={colors.textFaint} />
      </Pressable>
      {open && block.detail ? <Text style={styles.activityDetail}>{block.detail}</Text> : null}
    </View>
  );
}

/** A multiple-choice prompt (a ```choice block the backend parsed into a choice
 *  event). Each option is a tappable button that sends it as the next message —
 *  no typing "1". Only the latest turn's picker is interactive; older ones are
 *  disabled. The composer stays open to type a different answer. */
function ChoiceBlock({
  question,
  options,
  interactive,
  onChoose,
}: {
  question?: string;
  options: string[];
  interactive: boolean;
  onChoose: (text: string) => void;
}) {
  return (
    <View style={styles.choice}>
      {question ? <Text style={styles.choiceQ}>{question}</Text> : null}
      {options.map((o, i) => (
        <Pressable
          key={i}
          onPress={() => interactive && onChoose(o)}
          disabled={!interactive}
          style={({ pressed }) => [styles.choiceOpt, pressed && interactive && styles.choiceOptOn, !interactive && styles.choiceOptOff]}
        >
          <Text style={styles.choiceNum}>{i + 1}</Text>
          <Text style={styles.choiceText}>{o}</Text>
        </Pressable>
      ))}
      {interactive ? <Text style={styles.choiceHint}>Or type your own answer below.</Text> : null}
    </View>
  );
}

/** Live app preview: the existing AppEmbed WebView in a fixed-height box so it
 *  lays out inside the scrolling transcript. */
function EmbedBlock({ port, title, height }: { port: number; title?: string; height?: number }) {
  const h = height && height >= 120 ? height : 260;
  return (
    <View style={styles.embed}>
      <View style={styles.embedHead}>
        <Ionicons name="globe-outline" size={12} color={colors.textMuted} />
        <Text style={styles.embedTitle} numberOfLines={1}>
          {title || `App on :${port}`}
        </Text>
      </View>
      <View style={{ height: h }}>
        <AppEmbed port={port} name={title || `App :${port}`} compact />
      </View>
    </View>
  );
}

/** An inline image or video. Workspace files go through the authed
 *  /api/files/raw endpoint (Bearer header); external URLs are used directly. */
function MediaBlock({ block }: { block: Extract<HvBlock, { kind: 'media' }> }) {
  const src = block.url || (block.path ? fileRawUrl(block.path) : '');
  if (!src) return null;
  const headers = block.url ? undefined : authHeaders();
  const h = block.height && block.height >= 80 ? block.height : 320;
  if (block.mediaKind === 'video') {
    return <VideoBlock uri={src} headers={headers} height={h} title={block.title} />;
  }
  return (
    <View>
      <Image source={{ uri: src, headers }} style={[styles.mediaImg, { height: h }]} resizeMode="contain" />
      {block.title ? <Text style={styles.mediaCap}>{block.title}</Text> : null}
    </View>
  );
}

function VideoBlock({
  uri,
  headers,
  height,
  title,
}: {
  uri: string;
  headers?: Record<string, string>;
  height: number;
  title?: string;
}) {
  const player = useVideoPlayer({ uri, headers }, (p) => {
    p.loop = false;
  });
  return (
    <View>
      <VideoView player={player} style={[styles.mediaImg, { height }]} contentFit="contain" nativeControls />
      {title ? <Text style={styles.mediaCap}>{title}</Text> : null}
    </View>
  );
}

const MARKDOWN_RE = /\.(md|markdown|mdx)$/i;
// MIME types /api/files/view serves inline in a WebView (PDF direct; the rest
// CSP-sandboxed server-side). Kept in sync with the web FileBlock.
const FRAME_MIME = ['text/html', 'application/xhtml+xml', 'image/svg+xml', 'text/xml', 'application/xml'];

/** A document/file the agent asked to show (via show_file). We classify it with
 *  /api/files/preview and render inline: markdown formatted, text/code in a
 *  scrollable mono box, image/video streamed from /api/files/raw, and PDF/HTML/
 *  SVG/XML in a WebView pointed at /api/files/view. Anything else (or an Android
 *  PDF, which WebView can't render inline) falls back to an open/download card —
 *  parity with the web tab's FileBlock. */
function FileBlock({ block }: { block: Extract<HvBlock, { kind: 'file' }> }) {
  const { path, title, height } = block;
  const [preview, setPreview] = useState<FilePreview | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setPreview(null);
    setError(null);
    previewFile(path)
      .then((p) => alive && setPreview(p))
      .catch((e) => alive && setError(e instanceof Error ? e.message : 'Failed to load'));
    return () => {
      alive = false;
    };
  }, [path]);

  const name = path.split('/').pop() || path;
  const mime = preview?.mime || '';
  const isPdf = mime === 'application/pdf';
  const isFrame = isPdf || FRAME_MIME.includes(mime);
  const frameH = height && height >= 80 ? height : 420;
  // Android's system WebView has no built-in PDF viewer, so an inline <iframe>
  // to a PDF renders blank; fall back to the open/download card there (and on
  // any WebView load error) rather than showing an empty box.
  const androidPdf = isPdf && Platform.OS === 'android';

  const openExternally = () => void Linking.openURL(fileDownloadBrowserUrl(path)).catch(() => {});

  let body: ReactNode;
  if (error) {
    body = <Text style={styles.fileMsgErr}>Couldn’t load {name}: {error}</Text>;
  } else if (!preview) {
    body = <Text style={styles.fileMsg}>Loading {name}…</Text>;
  } else if (preview.kind === 'image') {
    body = (
      <Image
        source={{ uri: fileRawUrl(path), headers: authHeaders() }}
        style={[styles.mediaImg, { height: frameH }]}
        resizeMode="contain"
      />
    );
  } else if (preview.kind === 'video') {
    body = <VideoBlock uri={fileRawUrl(path)} headers={authHeaders()} height={frameH} title={title} />;
  } else if (isFrame && !androidPdf) {
    body = (
      <View style={[styles.fileFrame, { height: frameH }]}>
        <WebView
          source={{ uri: fileViewUrl(path), headers: authHeaders() }}
          style={styles.fileFrameWeb}
          originWhitelist={['*']}
          // A single self-contained doc: the Bearer header rides the top-level
          // request, so — unlike AppEmbed — no app-session cookie bootstrap is
          // needed. On a load failure, degrade to the open/download card.
          onError={() => setError('This file can’t be shown inline.')}
          startInLoadingState={false}
        />
      </View>
    );
  } else if (preview.kind === 'text' && (MARKDOWN_RE.test(path) || mime === 'text/markdown')) {
    body = (
      <ScrollView style={styles.fileScroll} contentContainerStyle={styles.fileScrollInner} nestedScrollEnabled>
        <Markdown text={preview.content} />
      </ScrollView>
    );
  } else if (preview.kind === 'text') {
    body = (
      <ScrollView style={styles.fileScroll} contentContainerStyle={styles.fileScrollInner} nestedScrollEnabled>
        <Text style={styles.fileCode}>{preview.content}</Text>
      </ScrollView>
    );
  } else {
    // binary (or an Android PDF) → an info card with an open/download action.
    body = (
      <View style={styles.fileFallback}>
        <Ionicons name="document-outline" size={32} color={colors.textFaint} />
        <Text style={styles.fileMsg}>
          {androidPdf ? 'PDFs open in your browser on Android.' : `${name} is a binary file.`}
        </Text>
        <Pressable onPress={openExternally} style={styles.fileOpenBtn}>
          <Ionicons name="open-outline" size={14} color={colors.accent} />
          <Text style={styles.fileOpenText}>Open / download</Text>
        </Pressable>
      </View>
    );
  }

  const truncated = preview?.kind === 'text' && preview.truncated;
  return (
    <View style={styles.embed}>
      <View style={styles.embedHead}>
        <Ionicons name="document-text-outline" size={12} color={colors.textMuted} />
        <Text style={styles.embedTitle} numberOfLines={1}>
          {title || name}
        </Text>
        <Pressable onPress={openExternally} hitSlop={8} style={({ pressed }) => pressed && { opacity: 0.6 }}>
          <Ionicons name="download-outline" size={15} color={colors.textMuted} />
        </Pressable>
      </View>
      {body}
      {truncated ? <Text style={styles.fileNote}>Preview truncated — open for the full file.</Text> : null}
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: colors.bg },
  flex: { flex: 1 },
  newBtn: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 3,
    backgroundColor: colors.accent,
    paddingHorizontal: space.md,
    paddingVertical: space.xs + 2,
    borderRadius: radius.md,
  },
  newBtnText: { color: colors.accentText, fontWeight: '700', fontSize: font.size.sm },
  headerActions: { flexDirection: 'row', alignItems: 'center', gap: space.sm },
  chatsBtn: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 5,
    paddingHorizontal: space.md,
    height: 34,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: colors.border,
    backgroundColor: colors.card,
  },
  chatsBtnText: { color: colors.text, fontWeight: '600', fontSize: font.size.sm },
  dot: { width: 7, height: 7, borderRadius: 4 },
  // Past-chats sheet
  sheetScrim: { flex: 1, backgroundColor: 'rgba(0,0,0,0.5)' },
  sheet: {
    backgroundColor: colors.bgElevated,
    borderTopLeftRadius: radius.xl,
    borderTopRightRadius: radius.xl,
    borderTopWidth: 1,
    borderColor: colors.border,
    paddingTop: space.sm,
    maxHeight: '75%',
  },
  sheetGrip: {
    alignSelf: 'center',
    width: 36,
    height: 4,
    borderRadius: radius.pill,
    backgroundColor: colors.borderStrong,
    marginBottom: space.sm,
  },
  sheetHead: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingHorizontal: space.lg,
    paddingBottom: space.sm,
  },
  sheetTitle: { color: colors.text, fontSize: font.size.lg, fontWeight: '700' },
  sheetNew: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 3,
    backgroundColor: colors.accent,
    paddingHorizontal: space.md,
    paddingVertical: space.xs + 1,
    borderRadius: radius.md,
  },
  sheetList: { flexGrow: 0 },
  sheetListInner: { paddingHorizontal: space.md, paddingBottom: space.sm, gap: space.xs },
  chatRow: {
    flexDirection: 'row',
    alignItems: 'center',
    borderRadius: radius.md,
  },
  chatRowOn: { backgroundColor: colors.accentSoft },
  chatRowEditing: { paddingLeft: space.sm, paddingVertical: space.xs },
  chatRenameInput: {
    flex: 1,
    minWidth: 0,
    color: colors.text,
    fontSize: font.size.md,
    fontWeight: '500',
    backgroundColor: colors.surface2,
    borderWidth: 1,
    borderColor: colors.accent,
    borderRadius: radius.sm,
    paddingHorizontal: space.sm,
    paddingVertical: space.xs + 2,
  },
  chatRowMain: {
    flex: 1,
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.sm,
    paddingVertical: space.sm + 2,
    paddingHorizontal: space.sm,
    minWidth: 0,
  },
  chatRowBody: { flex: 1, minWidth: 0, gap: 2 },
  chatRowTitle: { color: colors.textMuted, fontSize: font.size.md, fontWeight: '500' },
  chatRowMeta: { color: colors.textFaint, fontSize: font.size.xs },
  chatDel: {
    width: 40,
    height: 40,
    alignItems: 'center',
    justifyContent: 'center',
  },
  trashToggle: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 6,
    paddingHorizontal: space.lg,
    paddingVertical: space.sm,
    borderTopWidth: 1,
    borderTopColor: colors.border,
  },
  trashToggleText: {
    color: colors.textFaint,
    fontSize: font.size.xs,
    fontWeight: '700',
    textTransform: 'uppercase',
    letterSpacing: 0.4,
  },
  trashCount: {
    minWidth: 18,
    height: 18,
    borderRadius: 9,
    paddingHorizontal: 5,
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: colors.surface2,
  },
  trashCountText: { color: colors.textMuted, fontSize: 10, fontWeight: '700' },
  trashList: { flexGrow: 0, maxHeight: 220 },
  trashEmpty: { color: colors.textFaint, fontSize: font.size.sm, paddingHorizontal: space.md, paddingVertical: space.sm },
  chatRowTitleDeleted: { textDecorationLine: 'line-through', color: colors.textFaint },
  restoreBtn: {
    paddingHorizontal: space.md,
    paddingVertical: 6,
    marginRight: space.sm,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: colors.border,
  },
  restoreBtnText: { color: colors.textMuted, fontSize: font.size.xs, fontWeight: '600' },
  transcript: { padding: space.lg, gap: space.md },
  welcome: { paddingTop: space.xxl, gap: space.lg },
  suggests: { gap: space.sm, paddingHorizontal: space.lg },
  suggest: {
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.md,
    paddingHorizontal: space.md,
    paddingVertical: space.md,
    backgroundColor: colors.card,
  },
  suggestText: { color: colors.textMuted, fontSize: font.size.sm },
  userRow: { alignItems: 'flex-end' },
  userBubble: {
    maxWidth: '86%',
    backgroundColor: colors.surface2,
    borderRadius: radius.lg,
    paddingHorizontal: space.md,
    paddingVertical: space.sm,
  },
  userText: { color: colors.text, fontSize: font.size.md, lineHeight: 21 },
  agentRow: { gap: space.sm },
  agentHead: { flexDirection: 'row', alignItems: 'center', gap: 6 },
  agentName: { color: colors.text, fontWeight: '700', fontSize: font.size.sm },
  agentVia: { color: colors.textFaint, fontSize: font.size.xs },
  working: { color: colors.textFaint, fontSize: font.size.xs, fontStyle: 'italic' },
  choice: { gap: space.sm, marginTop: space.xs },
  choiceQ: { color: colors.text, fontSize: font.size.md, fontWeight: '600', lineHeight: 21 },
  choiceOpt: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.sm,
    minHeight: 44,
    paddingHorizontal: space.md,
    paddingVertical: space.sm,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.md,
    backgroundColor: colors.card,
  },
  choiceOptOn: { borderColor: colors.accent, backgroundColor: colors.accentSoft },
  choiceOptOff: { opacity: 0.55 },
  choiceNum: {
    width: 20,
    height: 20,
    borderRadius: 10,
    textAlign: 'center',
    lineHeight: 20,
    fontSize: font.size.xs,
    fontWeight: '700',
    color: colors.textMuted,
    backgroundColor: colors.surface2,
    overflow: 'hidden',
  },
  choiceText: { flex: 1, color: colors.text, fontSize: font.size.md },
  choiceHint: { color: colors.textFaint, fontSize: font.size.xs, fontStyle: 'italic' },
  activity: {
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.md,
    backgroundColor: colors.card,
    overflow: 'hidden',
  },
  activityErr: { borderColor: colors.danger },
  activityHead: { flexDirection: 'row', alignItems: 'center', gap: 8, padding: space.sm },
  activityLabel: { flex: 1, color: colors.textMuted, fontSize: font.size.sm, fontWeight: '600' },
  activityDetail: {
    color: colors.textMuted,
    fontSize: font.size.xs,
    fontFamily: Platform.OS === 'ios' ? 'Menlo' : 'monospace',
    padding: space.sm,
    borderTopWidth: 1,
    borderTopColor: colors.border,
  },
  asstRow: {
    maxHeight: 44,
    borderTopWidth: 1,
    borderTopColor: colors.border,
    backgroundColor: colors.bgElevated,
  },
  asstRowContent: {
    alignItems: 'center',
    gap: space.sm,
    paddingHorizontal: space.md,
    paddingVertical: space.sm,
  },
  asstChip: {
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.pill,
    paddingHorizontal: space.md,
    paddingVertical: 6,
  },
  asstChipOn: { backgroundColor: colors.accent + '22', borderColor: colors.accent },
  asstChipText: { color: colors.textMuted, fontSize: font.size.sm, fontWeight: '500' },
  asstChipTextOn: { color: colors.accent, fontWeight: '700' },
  // Free-text folder fallback when /api/workspace/dirs returns nothing.
  workdirRow: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.sm,
    paddingHorizontal: space.md,
    paddingVertical: space.xs + 2,
    borderTopWidth: 1,
    borderTopColor: colors.border,
    backgroundColor: colors.bgElevated,
  },
  workdirInput: {
    flex: 1,
    color: colors.text,
    fontSize: font.size.sm,
    backgroundColor: colors.surface2,
    borderRadius: radius.md,
    paddingHorizontal: space.md,
    paddingVertical: 6,
  },
  composer: {
    flexDirection: 'row',
    alignItems: 'flex-end',
    gap: space.sm,
    paddingHorizontal: space.md,
    paddingTop: space.sm,
    borderTopWidth: 1,
    borderTopColor: colors.border,
    backgroundColor: colors.bgElevated,
  },
  input: {
    flex: 1,
    maxHeight: 120,
    color: colors.text,
    fontSize: font.size.md,
    backgroundColor: colors.surface2,
    borderRadius: radius.md,
    paddingHorizontal: space.md,
    paddingVertical: space.sm,
  },
  sendBtn: {
    width: 40,
    height: 40,
    borderRadius: radius.md,
    backgroundColor: colors.accent,
    alignItems: 'center',
    justifyContent: 'center',
  },
  sendBtnOff: { opacity: 0.4 },
  stopBtn: { backgroundColor: colors.danger },
  attachBtn: {
    width: 40,
    height: 40,
    borderRadius: radius.md,
    alignItems: 'center',
    justifyContent: 'center',
    borderWidth: 1,
    borderColor: colors.border,
    backgroundColor: colors.surface2,
  },
  attachStrip: {
    maxHeight: 72,
    borderTopWidth: 1,
    borderTopColor: colors.border,
    backgroundColor: colors.bgElevated,
  },
  attachStripInner: { gap: space.sm, padding: space.sm },
  attachThumb: {
    width: 56,
    height: 56,
    borderRadius: radius.sm,
    overflow: 'hidden',
    borderWidth: 1,
    borderColor: colors.border,
    backgroundColor: colors.surface2,
  },
  attachThumbErr: { borderColor: colors.danger },
  attachImg: { width: '100%', height: '100%' },
  attachDim: {
    position: 'absolute',
    top: 0,
    left: 0,
    right: 0,
    bottom: 0,
    backgroundColor: 'rgba(0,0,0,0.4)',
  },
  attachX: {
    position: 'absolute',
    top: 2,
    right: 2,
    width: 16,
    height: 16,
    borderRadius: 8,
    backgroundColor: 'rgba(0,0,0,0.6)',
    alignItems: 'center',
    justifyContent: 'center',
  },
  embed: {
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.md,
    overflow: 'hidden',
    backgroundColor: colors.card,
  },
  embedHead: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 6,
    paddingHorizontal: space.md,
    paddingVertical: 6,
    borderBottomWidth: 1,
    borderBottomColor: colors.border,
    backgroundColor: colors.surface2,
  },
  embedTitle: { flex: 1, color: colors.textMuted, fontSize: font.size.sm, fontWeight: '600' },
  mediaImg: {
    width: '100%',
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.md,
    backgroundColor: colors.surface2,
  },
  mediaCap: { marginTop: 4, color: colors.textFaint, fontSize: font.size.xs },
  // show_file blocks — reuse the embed frame chrome above, plus these bodies.
  fileFrame: { backgroundColor: '#fff' },
  fileFrameWeb: { flex: 1, backgroundColor: '#fff' },
  fileScroll: { maxHeight: 380, backgroundColor: colors.bg },
  fileScrollInner: { padding: space.md },
  fileCode: { color: colors.text, fontSize: font.size.xs, fontFamily: font.mono, lineHeight: 17 },
  fileMsg: { color: colors.textMuted, fontSize: font.size.sm, textAlign: 'center', padding: space.md },
  fileMsgErr: { color: colors.danger, fontSize: font.size.sm, padding: space.md },
  fileFallback: { alignItems: 'center', gap: space.sm, paddingVertical: space.xl },
  fileOpenBtn: { flexDirection: 'row', alignItems: 'center', gap: 5 },
  fileOpenText: { color: colors.accent, fontSize: font.size.sm, fontWeight: '600' },
  fileNote: {
    color: colors.textFaint,
    fontSize: font.size.xs,
    paddingHorizontal: space.md,
    paddingVertical: space.xs,
    borderTopWidth: 1,
    borderTopColor: colors.border,
  },
});
