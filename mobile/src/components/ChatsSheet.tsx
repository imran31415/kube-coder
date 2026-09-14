import { useEffect, useState } from 'react';
import { KeyboardAvoidingView, Modal, Platform, Pressable, ScrollView, StyleSheet, Text, TextInput, useWindowDimensions, View } from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import type { HypervisorThread } from '../api/types';
import { useKeyboardHeight } from '../util/useKeyboard';
import { sheetMaxHeight } from '../util/sheetSizing';
import { relativeTime } from '../util/format';
import { colors, font, radius, space } from '../theme';

export function ChatsSheet({
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
  archiveLabel = 'Delete',
  busy = false,
  readOnly = false,
  loading = false,
  error,
  notice,
  onUndo,
}: {
  archiveLabel?: 'Delete' | 'Archive';
  busy?: boolean;
  readOnly?: boolean;
  loading?: boolean;
  error?: string | null;
  notice?: string | null;
  onUndo?: () => void;
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
  useEffect(() => {
    if (!visible) { setRenamingId(null); setDraftTitle(''); }
  }, [visible]);
  const insets = useSafeAreaInsets();
  // Inline rename: the row being edited plus its draft text.
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [draftTitle, setDraftTitle] = useState('');
  // Renaming autofocuses a field inside this sheet, so the keyboard comes up
  // over a sheet sized against the full screen (#662).
  const { height: screenHeight } = useWindowDimensions();
  const keyboardHeight = useKeyboardHeight();
  const sheetHeight = sheetMaxHeight(screenHeight, keyboardHeight);

  function startRename(t: HypervisorThread) {
    if (busy || readOnly) return;
    setRenamingId(t.id);
    setDraftTitle(t.title || '');
  }
  function cancelRename() {
    setRenamingId(null);
    setDraftTitle('');
  }
  function commitRename(id: string) {
    if (busy || readOnly) return;
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
      {/* Inside the Modal — the screen's KeyboardAvoidingView wraps the chat,
          not this sheet's separate native view hierarchy (#662). */}
      <KeyboardAvoidingView behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
      <View
        style={[
          styles.sheet,
          { maxHeight: sheetHeight, paddingBottom: Math.max(insets.bottom, space.md) },
        ]}
      >
        <View style={styles.sheetGrip} />
        <View style={styles.sheetHead}>
          <Text style={styles.sheetTitle}>Chats</Text>
          <Pressable disabled={busy || readOnly} accessibilityRole="button" accessibilityLabel="New chat" onPress={onNew} hitSlop={8} style={({ pressed }) => [styles.sheetNew, pressed && { opacity: 0.9 }]}>
            <Ionicons name="add" size={16} color={colors.accentText} />
            <Text style={styles.newBtnText}>New chat</Text>
          </Pressable>
        </View>
        {error && <Text accessibilityRole="alert" style={styles.trashEmpty}>{error}</Text>}
        {notice && <View style={styles.chatRow}><Text style={styles.trashEmpty}>{notice}</Text>
          {onUndo && <Pressable disabled={busy || readOnly} onPress={onUndo} accessibilityRole="button" accessibilityLabel="Undo archive"><Text style={styles.restoreBtnText}>Undo</Text></Pressable>}
        </View>}
        {loading && <Text style={styles.trashEmpty}>Loading chats...</Text>}
        {!loading && threads.length === 0 && <Text style={styles.trashEmpty}>No chats yet.</Text>}
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
                    disabled={busy || readOnly} onPress={() => commitRename(t.id)}
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
                <Pressable disabled={busy} accessibilityRole="button" accessibilityState={{ selected: on }} onPress={() => onOpen(t.id)} onLongPress={() => startRename(t)} style={styles.chatRowMain}>
                  <View style={[styles.dot, { backgroundColor: t.status === 'running' ? colors.running : colors.killed }]} />
                  <View style={styles.chatRowBody}>
                    <Text numberOfLines={1} style={[styles.chatRowTitle, on && { color: colors.text }]}>
                      {t.title || 'New chat'}
                    </Text>
                    <Text numberOfLines={1} style={styles.chatRowMeta}>
                      {t.assistant || 'agent'}
                      {t.updated_at ? ` · ${relativeTime(t.updated_at)}` : ''}
                      {t.status === 'running' ? ' · Running' : ''}
                    </Text>
                  </View>
                </Pressable>
                <Pressable
                  disabled={busy || readOnly} onPress={() => startRename(t)}
                  hitSlop={8}
                  accessibilityRole="button"
                  accessibilityLabel="Rename chat"
                  style={({ pressed }) => [styles.chatDel, pressed && { opacity: 0.6 }]}
                >
                  <Ionicons name="pencil" size={16} color={colors.textFaint} />
                </Pressable>
                <Pressable
                  disabled={busy || readOnly || (archiveLabel === 'Archive' && t.status === 'running')} onPress={() => onDelete(t)}
                  hitSlop={8}
                  accessibilityRole="button"
                  accessibilityLabel={`${archiveLabel} chat`}
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
                  disabled={busy || readOnly} onPress={() => onRestore(t)}
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
      </KeyboardAvoidingView>
    </Modal>
  );
}


const styles = StyleSheet.create({
  newBtnText: { color: colors.accentText, fontWeight: '700', fontSize: font.size.sm },
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
});
