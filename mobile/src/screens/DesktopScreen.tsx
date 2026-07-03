/** Desktop: the same customizable launcher grid as the web dashboard's
 *  Desktop tab, backed by the same /api/desktop config (desktop.json on the
 *  workspace PVC) — icons that start a task, open a URL, or run a shell
 *  command. Tap to launch; long-press to edit/move/delete; + to add. */
import { Ionicons } from '@expo/vector-icons';
import { useNavigation } from '@react-navigation/native';
import React, { useCallback, useState } from 'react';
import {
  Alert,
  FlatList,
  Linking,
  Pressable,
  RefreshControl,
  StyleSheet,
  Text,
  View,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import {
  createDesktopItem,
  dashboardUrl,
  deleteDesktopItem,
  launchDesktopItem,
  listDesktop,
  reorderDesktop,
  updateDesktopItem,
} from '../api/client';
import { DesktopEditorSheet } from '../components/DesktopEditorSheet';
import { EmptyState, ErrorBanner, Loading, ScreenHeader } from '../components/ui';
import type { DesktopItem, DesktopItemDraft } from '../api/types';
import { colors, font, radius, shadow, space } from '../theme';
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

export default function DesktopScreen() {
  // Cross-tab navigation (launched tasks open in the Tasks stack).
  const nav = useNavigation<{ navigate: (tab: string, opts?: object) => void }>();
  const [items, setItems] = useState<DesktopItem[] | null>(null);
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
  }, []);

  // The desktop is shared with the web dashboard — pick up edits made there.
  usePolling(load, 15000);

  const onRefresh = async () => {
    setRefreshing(true);
    await load();
    setRefreshing(false);
  };

  async function launch(item: DesktopItem) {
    // URL icons open directly, same as the web (no server roundtrip). Relative
    // URLs are dashboard routes (the web seeds '/tasks', '/settings', …) —
    // resolve them against the workspace's oauth-authenticated dashboard.
    if (item.action.type === 'url') {
      const u = item.action.url;
      void Linking.openURL(u.startsWith('/') ? dashboardUrl(u) : u);
      return;
    }
    setLaunching(item.id);
    try {
      const r = await launchDesktopItem(item.id);
      if (r.kind === 'task') {
        nav.navigate('Tasks', { screen: 'TaskDetail', params: { id: r.task_id } });
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

  return (
    <SafeAreaView style={styles.safe} edges={['top']}>
      <ScreenHeader
        title="Desktop"
        subtitle="One-tap launchers, shared with the web dashboard"
        right={
          <Pressable
            onPress={() => { setEditing(null); setEditorOpen(true); }}
            hitSlop={8}
            accessibilityRole="button"
            accessibilityLabel="New icon"
            style={styles.addBtn}
          >
            <Ionicons name="add" size={22} color={colors.accent} />
          </Pressable>
        }
      />

      {error && items !== null && items.length > 0 ? <ErrorBanner message={error} /> : null}

      {items === null ? (
        <Loading label="Loading desktop…" />
      ) : items.length === 0 ? (
        error ? (
          <EmptyState icon="cloud-offline-outline" title="Couldn't load the desktop" subtitle={error} />
        ) : (
          <EmptyState
            icon="grid-outline"
            title="No icons yet"
            subtitle="Pin a build prompt, a URL, or a shell command for one-tap launch. Tap + to create your first icon."
          />
        )
      ) : (
        <FlatList
          data={items}
          key="grid-3"
          numColumns={3}
          keyExtractor={(i) => i.id}
          contentContainerStyle={styles.grid}
          columnWrapperStyle={styles.gridRow}
          showsVerticalScrollIndicator={false}
          refreshControl={
            <RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={colors.accent} />
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
  addBtn: {
    width: 38,
    height: 38,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: colors.border,
    backgroundColor: colors.bgElevated,
    alignItems: 'center',
    justifyContent: 'center',
  },
  grid: { paddingHorizontal: space.lg, paddingBottom: space.xl, gap: space.md },
  gridRow: { gap: space.md },
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
});
