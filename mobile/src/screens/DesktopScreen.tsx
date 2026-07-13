/** Desktop: the workspace home. A clean identity masthead, a one-tap composer
 *  that starts a Hypervisor chat by default (with a "start a build instead"
 *  toggle), the customizable launcher grid (shared with the web dashboard's
 *  Desktop tab via /api/desktop), and an activity feed of live builds + recent
 *  chats. Tap an icon to launch; long-press to edit/move/delete; + to add. */
import { Ionicons } from '@expo/vector-icons';
import { useNavigation } from '@react-navigation/native';
import { LinearGradient } from 'expo-linear-gradient';
import React, { useCallback, useMemo, useState } from 'react';
import {
  ActivityIndicator,
  Alert,
  FlatList,
  Linking,
  Pressable,
  RefreshControl,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import {
  createDesktopItem,
  createTask,
  dashboardUrl,
  deleteDesktopItem,
  githubDisplayName,
  launchDesktopItem,
  listDesktop,
  listTasks,
  listThreads,
  reorderDesktop,
  updateDesktopItem,
} from '../api/client';
import { DesktopEditorSheet } from '../components/DesktopEditorSheet';
import { getConfig } from '../store/config';
import { EmptyState, ErrorBanner, Loading, MenuButton, StatusPill } from '../components/ui';
import type { DesktopItem, DesktopItemDraft, HypervisorThread, TaskSummary } from '../api/types';
import { colors, font, gradients, radius, shadow, space } from '../theme';
import { relativeTime } from '../util/format';
import { usePolling } from '../util/usePolling';

// The server stores "icon:NAME" for the web SPA's line-icon set; map the
// common names onto Ionicons equivalents so both clients render the same
// config. Unknown names fall back to a generic glyph, and anything without
// the prefix renders as literal text (emoji).
const ICON_MAP: Record<string, keyof typeof Ionicons.glyphMap> = {
  chat: 'chatbubbles-outline',
  tasks: 'layers-outline',
  memory: 'bookmark-outline',
  settings: 'settings-outline',
  terminal: 'terminal-outline',
  globe: 'globe-outline',
  url: 'globe-outline',
  docs: 'document-text-outline',
  files: 'folder-outline',
  metrics: 'stats-chart-outline',
  apps: 'grid-outline',
  play: 'play-outline',
  build: 'construct-outline',
};

function ItemIcon({ icon }: { icon: string }) {
  if (icon.startsWith('icon:')) {
    const name = ICON_MAP[icon.slice(5)] ?? 'apps-outline';
    return <Ionicons name={name} size={26} color={colors.accent} />;
  }
  return <Text style={styles.cellEmoji}>{icon}</Text>;
}

function actionSubtitle(item: DesktopItem): string {
  const a = item.action;
  if (a.type === 'task') return a.prompt;
  if (a.type === 'url') return a.url.replace(/^https?:\/\//, '');
  return a.command;
}

/** Where a url-icon should land INSIDE the app, or null for the browser.
 *
 * Desktop icons are shared with the web dashboard, whose seeds store its own
 * routes ('/tasks', '/memory', …). On mobile those pages ARE tabs — bouncing
 * the user out to Safari for them is jarring — so dashboard routes (relative,
 * or absolute on the connected workspace host, with or without the /oauth
 * prefix) map to the matching tab. Anything unmapped (external links, or web
 * routes with no mobile equivalent like /files, /docs, /triggers) still opens
 * in the browser.
 */
function inAppTarget(url: string): { tab: string; params?: object } | null {
  let path = url;
  if (!path.startsWith('/')) {
    try {
      const u = new URL(url);
      const workspace = new URL(getConfig().host);
      if (u.host !== workspace.host) return null;
      path = u.pathname;
    } catch {
      return null;
    }
  }
  path = path.replace(/^\/oauth(?=\/|$)/, '');
  const [head, sub] = path.split('/').filter(Boolean);
  switch (head) {
    case undefined:
      return { tab: 'Tasks' }; // '/' — the dashboard home
    case 'tasks':
      return sub
        ? // initial: false keeps TaskList mounted beneath so the detail header
          // has a back button even when the Tasks tab was never visited.
          { tab: 'Tasks', params: { screen: 'TaskDetail', params: { id: sub }, initial: false } }
        : { tab: 'Tasks', params: { screen: 'TaskList' } };
    case 'memory':
      return { tab: 'Memory' };
    case 'apps':
      return { tab: 'Apps', params: { screen: 'AppList' } };
    case 'desktop':
      return { tab: 'Desktop' };
    case 'settings':
      return { tab: 'Settings' };
    default:
      return null;
  }
}

type Nav = { navigate: (tab: string, opts?: object) => void };

/** Identity masthead: gradient monogram + "AI Workspace" over the operator's
 *  GitHub handle. Degrades to a neutral label until the name resolves. */
function DesktopHero({ name }: { name: string | null }) {
  const display = name ?? 'Workspace';
  const initial = display.charAt(0).toUpperCase();
  return (
    <View style={styles.hero}>
      <MenuButton />
      <LinearGradient
        colors={gradients.brand}
        start={{ x: 0, y: 0 }}
        end={{ x: 1, y: 1 }}
        style={styles.heroAvatar}
      >
        <Text style={styles.heroGlyph}>{initial}</Text>
      </LinearGradient>
      <View style={{ flexShrink: 1 }}>
        <Text style={styles.heroEyebrow}>AI Workspace</Text>
        <View style={styles.heroNameRow}>
          <Text style={styles.heroName} numberOfLines={1}>
            {display}
          </Text>
          {name ? <Ionicons name="logo-github" size={15} color={colors.textFaint} /> : null}
        </View>
      </View>
    </View>
  );
}

/** One-tap composer. Starts a Hypervisor chat by default; a quiet toggle flips
 *  it to the classic build path. Owns its own prompt state so list re-renders
 *  (from polling) never steal focus or clear the text.
 *
 *  Chat mode hands off to the Hypervisor tab (which creates the thread and shows
 *  the live turn); build mode creates the task inline and opens it. */
function Composer({
  onStartBuild,
  onStartChat,
}: {
  onStartBuild: (id: string) => void;
  onStartChat: (message: string) => void;
}) {
  const [prompt, setPrompt] = useState('');
  const [busy, setBusy] = useState(false);
  const [mode, setMode] = useState<'chat' | 'build'>('chat');
  const isChat = mode === 'chat';
  const canSend = prompt.trim().length > 0 && !busy;

  async function submit() {
    const text = prompt.trim();
    if (!text || busy) return;
    if (isChat) {
      // Hand the message to the Hypervisor tab; it owns thread creation and the
      // live view. Clear immediately — there's no inline async to await here.
      setPrompt('');
      onStartChat(text);
      return;
    }
    setBusy(true);
    try {
      const t = await createTask({ prompt: text, workdir: '/home/dev' });
      setPrompt('');
      onStartBuild(t.id);
    } catch (e) {
      Alert.alert("Couldn't start the build", (e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <View style={styles.composer}>
      <View style={styles.composerRow}>
        <View style={styles.composerGlyph}>
          <Ionicons
            name={isChat ? 'chatbubbles-outline' : 'construct-outline'}
            size={17}
            color={colors.accent}
          />
        </View>
        <TextInput
          value={prompt}
          onChangeText={setPrompt}
          placeholder={isChat ? 'Ask your workspace anything…' : 'Describe a build to run…'}
          placeholderTextColor={colors.textFaint}
          style={styles.composerInput}
          multiline
          editable={!busy}
        />
        <Pressable
          onPress={submit}
          disabled={!canSend}
          accessibilityRole="button"
          accessibilityLabel={isChat ? 'Start chat' : 'Start build'}
          style={({ pressed }) => [{ opacity: pressed ? 0.85 : 1 }]}
        >
          {canSend ? (
            <LinearGradient
              colors={gradients.primary}
              start={{ x: 0, y: 0 }}
              end={{ x: 1, y: 1 }}
              style={styles.composerSend}
            >
              {busy ? (
                <ActivityIndicator color={colors.accentText} size="small" />
              ) : (
                <Ionicons name="arrow-up" size={18} color={colors.accentText} />
              )}
            </LinearGradient>
          ) : (
            <View style={[styles.composerSend, styles.composerSendOff]}>
              {busy ? (
                <ActivityIndicator color={colors.textFaint} size="small" />
              ) : (
                <Ionicons name="arrow-up" size={18} color={colors.textFaint} />
              )}
            </View>
          )}
        </Pressable>
      </View>
      <View style={styles.composerFoot}>
        <Text style={styles.composerHint}>
          {isChat
            ? 'Starts a chat in /home/dev'
            : 'Starts a build in /home/dev and opens it live'}
        </Text>
        <Pressable
          onPress={() => setMode((m) => (m === 'chat' ? 'build' : 'chat'))}
          disabled={busy}
          hitSlop={8}
          accessibilityRole="button"
          style={({ pressed }) => [styles.composerToggle, pressed && { opacity: 0.6 }]}
        >
          <Ionicons
            name={isChat ? 'construct-outline' : 'chatbubbles-outline'}
            size={12}
            color={colors.accent}
          />
          <Text style={styles.composerToggleText}>
            {isChat ? 'Start a build instead' : 'Start a chat instead'}
          </Text>
        </Pressable>
      </View>
    </View>
  );
}

/** A clean uppercase section label with an optional right-side action. */
function SectionLabel({ title, action }: { title: string; action?: React.ReactNode }) {
  return (
    <View style={styles.sectionRow}>
      <Text style={styles.sectionLabel}>{title}</Text>
      {action}
    </View>
  );
}

export default function DesktopScreen() {
  // Cross-tab navigation (launched tasks open in the Tasks stack).
  const nav = useNavigation<Nav>();
  const [items, setItems] = useState<DesktopItem[] | null>(null);
  const [live, setLive] = useState<TaskSummary[]>([]);
  const [chats, setChats] = useState<HypervisorThread[]>([]);
  const [name, setName] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [editorOpen, setEditorOpen] = useState(false);
  const [editing, setEditing] = useState<DesktopItem | null>(null);
  const [launching, setLaunching] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setItems(await listDesktop());
      setError(null);
    } catch (e) {
      setError((e as Error).message);
      setItems((prev) => prev ?? []);
    }
    // Live builds for the Activity feed — best-effort, never blocks the grid.
    try {
      const tasks = await listTasks();
      setLive(
        tasks
          .filter((t) => t.status === 'running' || t.status === 'waiting' || t.waiting_for_input)
          .sort((a, b) => (b.created_at ?? 0) - (a.created_at ?? 0))
          .slice(0, 3),
      );
    } catch {
      /* keep last-good */
    }
    // Recent chats — also best-effort. listThreads() is newest-first already.
    try {
      const threads = await listThreads();
      setChats(threads.slice(0, 3));
    } catch {
      /* keep last-good */
    }
  }, []);

  // The desktop is shared with the web dashboard — pick up edits made there.
  usePolling(load, 15000);
  // Identity is stable; fetch once.
  React.useEffect(() => {
    let ok = true;
    void githubDisplayName().then((n) => { if (ok) setName(n); });
    return () => { ok = false; };
  }, []);

  const onRefresh = async () => {
    setRefreshing(true);
    await load();
    setRefreshing(false);
  };

  function openTask(id: string) {
    // initial: false → TaskList stays beneath the detail screen, so it opens
    // with a back button instead of becoming the stack's only (trapped) route.
    nav.navigate('Tasks', { screen: 'TaskDetail', params: { id }, initial: false });
  }

  // Chat composer + Activity chat rows hand off to the Hypervisor tab: an
  // initialMessage seeds a brand-new chat, an openThreadId reopens an existing
  // one. HypervisorScreen consumes the param once and clears it.
  function startChat(message: string) {
    nav.navigate('Hypervisor', { initialMessage: message });
  }
  function openChat(id: string) {
    nav.navigate('Hypervisor', { openThreadId: id });
  }

  async function launch(item: DesktopItem) {
    // URL icons open directly, same as the web (no server roundtrip). Dashboard
    // routes with a mobile tab stay in-app; everything else goes to the
    // browser (relative routes resolved against the oauth'd dashboard).
    if (item.action.type === 'url') {
      const u = item.action.url;
      const target = inAppTarget(u);
      if (target) {
        nav.navigate(target.tab, target.params);
        return;
      }
      void Linking.openURL(u.startsWith('/') ? dashboardUrl(u) : u);
      return;
    }
    setLaunching(item.id);
    try {
      const r = await launchDesktopItem(item.id);
      if (r.kind === 'task') {
        openTask(r.task_id);
      } else if (r.kind === 'shell') {
        const ok = r.exit_code === 0;
        const tail = (ok ? r.stdout : r.stderr || r.stdout).trim().split('\n').slice(-6).join('\n');
        Alert.alert(
          ok ? `${item.label} — exit 0` : `${item.label} — exit ${r.exit_code}`,
          tail || '(no output)',
        );
      }
    } catch (e) {
      Alert.alert(`Couldn't launch ${item.label}`, (e as Error).message);
    } finally {
      setLaunching(null);
    }
  }

  function itemMenu(item: DesktopItem) {
    if (!items) return;
    const idx = items.findIndex((i) => i.id === item.id);
    Alert.alert(item.label, actionSubtitle(item), [
      { text: 'Edit', onPress: () => { setEditing(item); setEditorOpen(true); } },
      ...(idx > 0
        ? [{ text: 'Move up', onPress: () => void move(item.id, -1) }]
        : []),
      ...(idx >= 0 && idx < items.length - 1
        ? [{ text: 'Move down', onPress: () => void move(item.id, 1) }]
        : []),
      {
        text: 'Delete',
        style: 'destructive' as const,
        onPress: () =>
          Alert.alert('Delete icon?', `Remove "${item.label}"? You can re-create it any time.`, [
            { text: 'Cancel', style: 'cancel' },
            { text: 'Delete', style: 'destructive', onPress: () => void remove(item.id) },
          ]),
      },
      { text: 'Cancel', style: 'cancel' },
    ]);
  }

  async function move(id: string, dir: -1 | 1) {
    if (!items) return;
    const order = items.map((i) => i.id);
    const idx = order.indexOf(id);
    const to = idx + dir;
    if (idx < 0 || to < 0 || to >= order.length) return;
    [order[idx], order[to]] = [order[to], order[idx]];
    try {
      setItems(await reorderDesktop(order));
    } catch (e) {
      Alert.alert("Couldn't reorder", (e as Error).message);
    }
  }

  async function remove(id: string) {
    try {
      await deleteDesktopItem(id);
      await load();
    } catch (e) {
      Alert.alert("Couldn't delete", (e as Error).message);
    }
  }

  async function save(draft: DesktopItemDraft): Promise<string | null> {
    try {
      if (editing) await updateDesktopItem(editing.id, draft);
      else await createDesktopItem(draft);
      setEditorOpen(false);
      setEditing(null);
      await load();
      return null;
    } catch (e) {
      return (e as Error).message;
    }
  }

  const newBtn = (
    <Pressable
      onPress={() => { setEditing(null); setEditorOpen(true); }}
      hitSlop={8}
      accessibilityRole="button"
      accessibilityLabel="New icon"
      style={({ pressed }) => [styles.newPill, pressed && { opacity: 0.7 }]}
    >
      <Ionicons name="add" size={15} color={colors.textMuted} />
      <Text style={styles.newPillText}>New</Text>
    </Pressable>
  );

  // Header (masthead + composer + Shortcuts label) is passed as an ELEMENT so
  // it reconciles across polls instead of remounting — the composer keeps its
  // text + focus. Memoized on the values it actually reads.
  const header = useMemo(
    () => (
      <View>
        <DesktopHero name={name} />
        <SectionLabel title="Start a chat" />
        <Composer onStartBuild={openTask} onStartChat={startChat} />
        {error && items && items.length > 0 ? <ErrorBanner message={error} /> : null}
        {items && items.length > 0 ? <SectionLabel title="Shortcuts" action={newBtn} /> : null}
      </View>
    ),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [name, error, items?.length],
  );

  const footer =
    live.length > 0 || chats.length > 0 ? (
      <View style={styles.activity}>
        <SectionLabel title="Activity" />
        {live.length > 0 && (
          <>
            <Text style={styles.activityGroup}>LIVE BUILDS</Text>
            {live.map((t) => (
              <Pressable
                key={t.id}
                onPress={() => openTask(t.id)}
                style={({ pressed }) => [styles.activityRow, pressed && { opacity: 0.7 }]}
              >
                <StatusPill status={t.status} />
                <Text style={styles.activityTitle} numberOfLines={1}>
                  {t.prompt || t.id}
                </Text>
                <Text style={styles.activityTime}>{relativeTime(t.created_at)}</Text>
              </Pressable>
            ))}
          </>
        )}
        {chats.length > 0 && (
          <>
            <Text style={[styles.activityGroup, live.length > 0 && { marginTop: space.md }]}>
              RECENT CHATS
            </Text>
            {chats.map((c) => (
              <Pressable
                key={c.id}
                onPress={() => openChat(c.id)}
                style={({ pressed }) => [styles.activityRow, pressed && { opacity: 0.7 }]}
              >
                <View style={styles.activityChatIcon}>
                  <Ionicons name="chatbubbles-outline" size={15} color={colors.accent} />
                </View>
                <Text style={styles.activityTitle} numberOfLines={1}>
                  {c.title || 'New chat'}
                </Text>
                <Text style={styles.activityTime}>
                  {relativeTime(c.updated_at ?? c.created_at ?? undefined)}
                </Text>
              </Pressable>
            ))}
          </>
        )}
      </View>
    ) : null;

  return (
    <SafeAreaView style={styles.safe} edges={['top']}>
      {items === null ? (
        <Loading label="Loading workspace…" />
      ) : (
        <FlatList
          data={items}
          key="grid-3"
          numColumns={3}
          keyExtractor={(i) => i.id}
          ListHeaderComponent={header}
          ListFooterComponent={footer}
          contentContainerStyle={styles.grid}
          columnWrapperStyle={styles.gridRow}
          showsVerticalScrollIndicator={false}
          refreshControl={
            <RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={colors.accent} />
          }
          ListEmptyComponent={
            error ? (
              <EmptyState icon="cloud-offline-outline" title="Couldn't load the desktop" subtitle={error} />
            ) : (
              <EmptyState
                icon="grid-outline"
                title="No shortcuts yet"
                subtitle="Pin a build prompt, a URL, or a shell command for one-tap launch. Tap + to create your first icon."
              />
            )
          }
          renderItem={({ item }) => (
            <Pressable
              style={({ pressed }) => [styles.cell, pressed && styles.cellPressed]}
              onPress={() => void launch(item)}
              onLongPress={() => itemMenu(item)}
              delayLongPress={350}
              accessibilityRole="button"
              accessibilityLabel={`Launch ${item.label}`}
              accessibilityHint="Long press for edit, move and delete"
            >
              <View style={styles.cellIcon}>
                {launching === item.id ? (
                  <Ionicons name="hourglass-outline" size={26} color={colors.textMuted} />
                ) : (
                  <ItemIcon icon={item.icon} />
                )}
              </View>
              <Text style={styles.cellLabel} numberOfLines={1}>
                {item.label}
              </Text>
              <Text style={styles.cellSub} numberOfLines={1}>
                {actionSubtitle(item)}
              </Text>
            </Pressable>
          )}
        />
      )}

      <DesktopEditorSheet
        visible={editorOpen}
        initial={editing}
        onSave={save}
        onClose={() => { setEditorOpen(false); setEditing(null); }}
      />
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: colors.bg },

  // Masthead
  hero: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.md,
    paddingHorizontal: space.lg,
    paddingTop: space.md,
    paddingBottom: space.lg,
  },
  heroAvatar: {
    width: 48,
    height: 48,
    borderRadius: 15,
    alignItems: 'center',
    justifyContent: 'center',
    ...shadow.card,
  },
  heroGlyph: { color: colors.accentText, fontSize: 20, fontWeight: '800' },
  heroEyebrow: {
    color: colors.textFaint,
    fontSize: 10.5,
    fontWeight: '700',
    textTransform: 'uppercase',
    letterSpacing: 1,
  },
  heroNameRow: { flexDirection: 'row', alignItems: 'center', gap: 6 },
  heroName: { color: colors.text, fontSize: font.size.xl, fontWeight: '800', letterSpacing: -0.4 },

  // Section labels
  sectionRow: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingHorizontal: space.lg,
    marginBottom: space.sm,
  },
  sectionLabel: {
    color: colors.textMuted,
    fontSize: font.size.xs,
    fontWeight: '700',
    textTransform: 'uppercase',
    letterSpacing: 0.8,
  },
  newPill: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 3,
    paddingVertical: 4,
    paddingHorizontal: 10,
    borderRadius: radius.pill,
    borderWidth: 1,
    borderColor: colors.border,
    backgroundColor: colors.bgElevated,
  },
  newPillText: { color: colors.textMuted, fontSize: font.size.xs, fontWeight: '600' },

  // Composer
  composer: {
    marginHorizontal: space.lg,
    marginBottom: space.xl,
    backgroundColor: colors.card,
    borderRadius: radius.lg,
    borderWidth: 1,
    borderColor: colors.border,
    paddingHorizontal: space.md,
    paddingVertical: space.md,
    gap: space.sm,
    ...shadow.card,
  },
  composerRow: { flexDirection: 'row', alignItems: 'flex-end', gap: space.sm },
  composerGlyph: {
    width: 32,
    height: 32,
    borderRadius: 10,
    backgroundColor: colors.accent + '1f',
    alignItems: 'center',
    justifyContent: 'center',
    marginBottom: 2,
  },
  composerInput: {
    flex: 1,
    color: colors.text,
    fontSize: font.size.md,
    minHeight: 34,
    maxHeight: 130,
    paddingTop: 7,
    paddingBottom: 4,
  },
  composerSend: { width: 38, height: 38, borderRadius: 19, alignItems: 'center', justifyContent: 'center' },
  composerSendOff: { backgroundColor: colors.bgElevated, borderWidth: 1, borderColor: colors.border },
  composerFoot: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingLeft: 40,
    gap: space.sm,
  },
  composerHint: { color: colors.textFaint, fontSize: font.size.xs, flexShrink: 1 },
  composerToggle: { flexDirection: 'row', alignItems: 'center', gap: 4 },
  composerToggleText: { color: colors.accent, fontSize: font.size.xs, fontWeight: '600' },

  // Grid
  grid: { paddingBottom: space.xl, gap: space.md },
  gridRow: { gap: space.md, paddingHorizontal: space.lg },
  cell: {
    flex: 1,
    maxWidth: '31.5%',
    backgroundColor: colors.card,
    borderRadius: radius.lg,
    borderWidth: 1,
    borderColor: colors.border,
    paddingVertical: space.lg,
    paddingHorizontal: space.sm,
    alignItems: 'center',
    gap: 6,
    ...shadow.card,
  },
  cellPressed: { opacity: 0.7, transform: [{ scale: 0.97 }] },
  cellIcon: {
    width: 52,
    height: 52,
    borderRadius: radius.md,
    backgroundColor: colors.accent + '14',
    alignItems: 'center',
    justifyContent: 'center',
  },
  cellEmoji: { fontSize: 26 },
  cellLabel: { color: colors.text, fontSize: font.size.sm, fontWeight: '700' },
  cellSub: { color: colors.textFaint, fontSize: font.size.xs, maxWidth: '95%' },

  // Activity
  activity: { marginTop: space.lg, paddingHorizontal: space.lg },
  activityGroup: {
    color: colors.textFaint,
    fontSize: 10.5,
    fontWeight: '700',
    letterSpacing: 0.8,
    marginBottom: space.sm,
  },
  activityChatIcon: {
    width: 30,
    height: 30,
    borderRadius: 9,
    backgroundColor: colors.accent + '1f',
    alignItems: 'center',
    justifyContent: 'center',
  },
  activityRow: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.md,
    paddingVertical: space.md,
    paddingHorizontal: space.md,
    marginBottom: space.sm,
    backgroundColor: colors.card,
    borderRadius: radius.lg,
    borderWidth: 1,
    borderColor: colors.border,
  },
  activityTitle: { flex: 1, color: colors.text, fontSize: font.size.sm, fontWeight: '600' },
  activityTime: { color: colors.textFaint, fontSize: font.size.xs },
});
