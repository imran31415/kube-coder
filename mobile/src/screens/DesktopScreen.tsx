/** Desktop: the workspace home (#433). A short greeting over a centered
 *  composer (starts a Hypervisor chat by default, with a build-mode pill),
 *  a live Mission Control strip fed by /api/missioncontrol/queue, and a
 *  compact bottom dock of launcher icons (shared with the web dashboard via
 *  /api/desktop). Tap a dock icon to launch; long-press to edit/move/delete;
 *  + to add. */
import { Ionicons } from '@expo/vector-icons';
import { useNavigation } from '@react-navigation/native';
import { LinearGradient } from 'expo-linear-gradient';
import React, { useCallback, useState } from 'react';
import {
  ActivityIndicator,
  Alert,
  FlatList,
  Linking,
  Pressable,
  RefreshControl,
  ScrollView,
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
  getMissionQueue,
  githubDisplayName,
  launchDesktopItem,
  listDesktop,
  reorderDesktop,
  updateDesktopItem,
} from '../api/client';
import { DesktopEditorSheet } from '../components/DesktopEditorSheet';
import { getConfig } from '../store/config';
import { EmptyState, ErrorBanner, Loading, MenuButton, StatusPill } from '../components/ui';
import type { DesktopItem, DesktopItemDraft, MissionCard, MissionPulse } from '../api/types';
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
    return <Ionicons name={name} size={22} color={colors.accent} />;
  }
  return <Text style={styles.dockEmoji}>{icon}</Text>;
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
    case 'mission':
      return { tab: 'MissionControl' };
    case 'settings':
      return { tab: 'Settings' };
    default:
      return null;
  }
}

type Nav = { navigate: (tab: string, opts?: object) => void };

/** Greeting masthead — one calm line instead of the old identity card. */
function DesktopGreeting({ name }: { name: string | null }) {
  return (
    <View style={styles.hero}>
      <MenuButton />
      <View style={{ flexShrink: 1, flex: 1 }}>
        <Text style={styles.heroTitle}>
          What are we building{name ? `, ${name}` : ''}?
        </Text>
      </View>
    </View>
  );
}

/** One-tap composer in the "new chat" shape: growing input on top, then a
 *  control row inside the box (mode pill left, send right). Starts a
 *  Hypervisor chat by default; the pill flips it to the classic build path.
 *  Owns its own prompt state so list re-renders (from polling) never steal
 *  focus or clear the text. */
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
      <TextInput
        value={prompt}
        onChangeText={setPrompt}
        placeholder={isChat ? 'Ask anything or start a build…' : 'Describe a build to run…'}
        placeholderTextColor={colors.textFaint}
        style={styles.composerInput}
        multiline
        editable={!busy}
      />
      <View style={styles.composerControls}>
        <View style={styles.composerPill}>
          <Ionicons name="folder-outline" size={12} color={colors.textFaint} />
          <Text style={styles.composerPillText}>/home/dev</Text>
        </View>
        <Pressable
          onPress={() => setMode((m) => (m === 'chat' ? 'build' : 'chat'))}
          disabled={busy}
          hitSlop={8}
          accessibilityRole="button"
          accessibilityLabel={isChat ? 'Mode: chat — switch to build' : 'Mode: build — switch to chat'}
          style={({ pressed }) => [styles.composerPill, pressed && { opacity: 0.6 }]}
        >
          <Ionicons
            name={isChat ? 'chatbubbles-outline' : 'construct-outline'}
            size={12}
            color={colors.accent}
          />
          <Text style={[styles.composerPillText, { color: colors.accent }]}>
            {isChat ? 'Chat' : 'Build'}
          </Text>
          <Ionicons name="chevron-down" size={10} color={colors.textFaint} />
        </Pressable>
        <View style={{ flex: 1 }} />
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
                <Ionicons name="arrow-up" size={17} color={colors.accentText} />
              )}
            </LinearGradient>
          ) : (
            <View style={[styles.composerSend, styles.composerSendOff]}>
              {busy ? (
                <ActivityIndicator color={colors.textFaint} size="small" />
              ) : (
                <Ionicons name="arrow-up" size={17} color={colors.textFaint} />
              )}
            </View>
          )}
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

/** State-appropriate age for a mission card, mirroring the web strip. */
function missionTime(card: MissionCard): string {
  if (card.state === 'waiting') {
    // relativeTime says "14m ago"; a waiting card reads better as "14m waiting".
    const since = card.waiting_since ?? card.updated_at;
    return since ? relativeTime(since).replace(' ago', ' waiting') : '';
  }
  if (card.state === 'running') return relativeTime(card.created_at ?? undefined);
  return relativeTime(card.finished_at ?? card.updated_at ?? undefined);
}

/** Condensed Mission Control strip: pulse row + the top few cards (the queue
 *  arrives pre-sorted waiting → running → done). Tapping anything lands on
 *  the Mission Control tab, where quick replies and kill live. */
function MissionStrip({
  cards,
  pulse,
  onOpen,
}: {
  cards: MissionCard[];
  pulse: MissionPulse | null;
  onOpen: () => void;
}) {
  if (cards.length === 0) return null;
  const waiting = pulse?.waiting ?? 0;
  return (
    <View style={styles.mission}>
      <SectionLabel
        title="Mission Control"
        action={
          <Pressable onPress={onOpen} hitSlop={8} accessibilityRole="button">
            <Text style={styles.missionViewAll}>View all →</Text>
          </Pressable>
        }
      />
      {pulse ? (
        <Text style={styles.missionPulse}>
          <Text style={styles.missionPulseNum}>{pulse.running}</Text> running
          <Text style={styles.missionPulseDot}> · </Text>
          <Text style={[styles.missionPulseNum, waiting > 0 && { color: colors.warning }]}>
            {waiting}
          </Text>
          <Text style={waiting > 0 ? { color: colors.warning } : undefined}> waiting on you</Text>
          <Text style={styles.missionPulseDot}> · </Text>
          <Text style={styles.missionPulseNum}>{pulse.done_today}</Text> done today
        </Text>
      ) : null}
      {cards.map((c) => (
        <Pressable
          key={c.id}
          onPress={onOpen}
          accessibilityRole="button"
          accessibilityLabel={`Open Mission Control — ${c.title}`}
          style={({ pressed }) => [
            styles.missionRow,
            c.state === 'waiting' && styles.missionRowWaiting,
            pressed && { opacity: 0.7 },
          ]}
        >
          <StatusPill status={c.state === 'review' ? 'done' : c.state} />
          <View style={{ flex: 1, minWidth: 0 }}>
            <Text style={styles.missionTitle} numberOfLines={1}>
              {c.title}
            </Text>
            {c.headline ? (
              <Text style={styles.missionHeadline} numberOfLines={1}>
                {c.headline}
              </Text>
            ) : null}
          </View>
          <Text style={styles.missionTime}>{missionTime(c)}</Text>
        </Pressable>
      ))}
    </View>
  );
}

export default function DesktopScreen() {
  // Cross-tab navigation (launched tasks open in the Tasks stack).
  const nav = useNavigation<Nav>();
  const [items, setItems] = useState<DesktopItem[] | null>(null);
  const [mission, setMission] = useState<MissionCard[]>([]);
  const [pulse, setPulse] = useState<MissionPulse | null>(null);
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
    // Mission Control strip — best-effort, never blocks the dock. The queue is
    // pre-sorted waiting → running → done, so the top slice is the priority cut.
    try {
      const q = await getMissionQueue();
      setMission(q.cards.slice(0, 3));
      setPulse(q.pulse);
    } catch {
      /* keep last-good */
    }
  }, []);

  // The desktop is shared with the web dashboard — pick up edits made there;
  // 10s keeps the mission strip fresh too.
  usePolling(load, 10000);
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

  // Chat composer hands off to the Hypervisor tab: an initialMessage seeds a
  // brand-new chat. HypervisorScreen consumes the param once and clears it.
  function startChat(message: string) {
    nav.navigate('Hypervisor', { initialMessage: message });
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
        ? [{ text: 'Move left', onPress: () => void move(item.id, -1) }]
        : []),
      ...(idx >= 0 && idx < items.length - 1
        ? [{ text: 'Move right', onPress: () => void move(item.id, 1) }]
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

  return (
    <SafeAreaView style={styles.safe} edges={['top']}>
      {items === null ? (
        <Loading label="Loading workspace…" />
      ) : (
        <>
          <ScrollView
            contentContainerStyle={styles.scroll}
            showsVerticalScrollIndicator={false}
            refreshControl={
              <RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={colors.accent} />
            }
          >
            <DesktopGreeting name={name} />
            {error ? <ErrorBanner message={error} /> : null}
            <Composer onStartBuild={openTask} onStartChat={startChat} />
            <MissionStrip
              cards={mission}
              pulse={pulse}
              onOpen={() => nav.navigate('MissionControl')}
            />
            {items.length === 0 && !error ? (
              <EmptyState
                icon="grid-outline"
                title="No shortcuts yet"
                subtitle="Pin a build prompt, a URL, or a shell command for one-tap launch. Tap + in the dock below to create your first icon."
              />
            ) : null}
          </ScrollView>

          {/* Bottom dock — compact icons, label beneath, + at the end.
              Long-press keeps the edit/move/delete sheet. */}
          <View style={styles.dock}>
            <FlatList
              horizontal
              data={items}
              keyExtractor={(i) => i.id}
              contentContainerStyle={styles.dockRow}
              showsHorizontalScrollIndicator={false}
              renderItem={({ item }) => (
                <Pressable
                  style={({ pressed }) => [styles.dockItem, pressed && styles.dockItemPressed]}
                  onPress={() => void launch(item)}
                  onLongPress={() => itemMenu(item)}
                  delayLongPress={350}
                  accessibilityRole="button"
                  accessibilityLabel={`Launch ${item.label}`}
                  accessibilityHint="Long press for edit, move and delete"
                >
                  <View style={styles.dockIcon}>
                    {launching === item.id ? (
                      <Ionicons name="hourglass-outline" size={22} color={colors.textMuted} />
                    ) : (
                      <ItemIcon icon={item.icon} />
                    )}
                  </View>
                  <Text style={styles.dockLabel} numberOfLines={1}>
                    {item.label}
                  </Text>
                </Pressable>
              )}
              ListFooterComponent={
                <Pressable
                  onPress={() => { setEditing(null); setEditorOpen(true); }}
                  accessibilityRole="button"
                  accessibilityLabel="Add icon"
                  style={({ pressed }) => [styles.dockItem, pressed && styles.dockItemPressed]}
                >
                  <View style={[styles.dockIcon, styles.dockIconAdd]}>
                    <Ionicons name="add" size={22} color={colors.textMuted} />
                  </View>
                  <Text style={styles.dockLabel} numberOfLines={1}>
                    Add
                  </Text>
                </Pressable>
              }
            />
          </View>
        </>
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
  scroll: { paddingBottom: space.xl },

  // Greeting masthead
  hero: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.md,
    paddingHorizontal: space.lg,
    paddingTop: space.md,
    paddingBottom: space.lg,
  },
  heroTitle: {
    color: colors.text,
    fontSize: font.size.xl,
    fontWeight: '800',
    letterSpacing: -0.4,
  },

  // Section labels
  sectionRow: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    marginBottom: space.sm,
  },
  sectionLabel: {
    color: colors.textMuted,
    fontSize: font.size.xs,
    fontWeight: '700',
    textTransform: 'uppercase',
    letterSpacing: 0.8,
  },

  // Composer — "new chat" shape: input on top, pills + send inside the box.
  composer: {
    marginHorizontal: space.lg,
    marginBottom: space.xl,
    backgroundColor: colors.card,
    borderRadius: radius.lg,
    borderWidth: 1,
    borderColor: colors.border,
    paddingHorizontal: space.md,
    paddingTop: space.sm,
    paddingBottom: space.sm,
    gap: space.sm,
    ...shadow.card,
  },
  composerInput: {
    color: colors.text,
    fontSize: font.size.md,
    minHeight: 44,
    maxHeight: 130,
    paddingTop: 8,
    paddingBottom: 4,
    paddingHorizontal: 2,
  },
  composerControls: { flexDirection: 'row', alignItems: 'center', gap: space.sm },
  composerPill: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 4,
    paddingVertical: 4,
    paddingHorizontal: 9,
    borderRadius: radius.pill,
    borderWidth: 1,
    borderColor: colors.border,
  },
  composerPillText: { color: colors.textMuted, fontSize: font.size.xs, fontWeight: '600' },
  composerSend: { width: 34, height: 34, borderRadius: 17, alignItems: 'center', justifyContent: 'center' },
  composerSendOff: { backgroundColor: colors.bgElevated, borderWidth: 1, borderColor: colors.border },

  // Mission Control strip
  mission: { paddingHorizontal: space.lg, marginBottom: space.lg },
  missionViewAll: { color: colors.accent, fontSize: font.size.xs, fontWeight: '600' },
  missionPulse: { color: colors.textMuted, fontSize: font.size.sm, marginBottom: space.md },
  missionPulseNum: { color: colors.text, fontWeight: '700' },
  missionPulseDot: { color: colors.textFaint },
  missionRow: {
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
  missionRowWaiting: { borderLeftWidth: 2, borderLeftColor: colors.warning },
  missionTitle: { color: colors.text, fontSize: font.size.sm, fontWeight: '600' },
  missionHeadline: { color: colors.textFaint, fontSize: font.size.xs, marginTop: 1 },
  missionTime: { color: colors.textFaint, fontSize: font.size.xs },

  // Bottom dock
  dock: {
    borderTopWidth: StyleSheet.hairlineWidth,
    borderTopColor: colors.border,
    backgroundColor: colors.bgElevated,
    paddingVertical: space.sm,
  },
  dockRow: {
    paddingHorizontal: space.md,
    gap: space.sm,
    flexGrow: 1,
    justifyContent: 'center',
  },
  dockItem: { alignItems: 'center', width: 62, gap: 3 },
  dockItemPressed: { opacity: 0.7, transform: [{ scale: 0.95 }] },
  dockIcon: {
    width: 46,
    height: 46,
    borderRadius: 13,
    backgroundColor: colors.accent + '14',
    alignItems: 'center',
    justifyContent: 'center',
  },
  dockIconAdd: {
    backgroundColor: 'transparent',
    borderWidth: 1,
    borderStyle: 'dashed',
    borderColor: colors.border,
  },
  dockEmoji: { fontSize: 22 },
  dockLabel: { color: colors.textMuted, fontSize: 10.5, fontWeight: '600', maxWidth: 60 },
});
