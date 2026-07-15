/** Files — browse /home/dev, preview a file, and (server-gated) rename or
 *  delete. Tapping a directory descends; tapping a file opens a preview sheet
 *  (text inline + copy, images inline, binary → info). The write actions call
 *  the same endpoints as the dashboard Files route; the server enforces the
 *  path-traversal guard and the read-only gate. */
import { Ionicons } from '@expo/vector-icons';
import * as Clipboard from 'expo-clipboard';
import React, { useCallback, useState } from 'react';
import {
  Alert,
  FlatList,
  Image,
  Modal,
  Pressable,
  RefreshControl,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { useFocusEffect } from '@react-navigation/native';
import {
  authHeaders,
  deleteFile,
  fileRawUrl,
  listFiles,
  previewFile,
  renameFile,
} from '../api/client';
import type { FileEntry, FilePreview } from '../api/types';
import { Button, EmptyState, ErrorBanner, Loading, ScreenHeader } from '../components/ui';
import { confirmAction } from '../util/confirm';
import { colors, font, radius, space } from '../theme';

function fmtSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(1)} GB`;
}

export default function FilesScreen() {
  const [path, setPath] = useState('');
  const [entries, setEntries] = useState<FileEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  // Preview sheet target (rel path) + fetched descriptor.
  const [selected, setSelected] = useState<FileEntry | null>(null);
  const [preview, setPreview] = useState<FilePreview | null>(null);
  const [previewErr, setPreviewErr] = useState<string | null>(null);
  // Rename modal.
  const [renaming, setRenaming] = useState<FileEntry | null>(null);
  const [renameValue, setRenameValue] = useState('');

  const rel = useCallback((name: string) => (path ? `${path}/${name}` : name), [path]);

  const load = useCallback(async (p: string) => {
    try {
      const r = await listFiles(p);
      setEntries(r.entries);
      setPath(r.path ?? p);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
      setEntries((prev) => prev ?? []);
    }
  }, []);

  // Re-list on focus so external changes (a running task writing files) show up.
  useFocusEffect(
    useCallback(() => {
      void load(path);
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [load]),
  );

  const onRefresh = async () => {
    setRefreshing(true);
    await load(path);
    setRefreshing(false);
  };

  const enter = (name: string) => {
    setEntries(null);
    void load(rel(name));
  };

  const goUp = () => {
    if (!path) return;
    const parts = path.split('/').filter(Boolean);
    parts.pop();
    setEntries(null);
    void load(parts.join('/'));
  };

  const openPreview = async (e: FileEntry) => {
    setSelected(e);
    setPreview(null);
    setPreviewErr(null);
    try {
      setPreview(await previewFile(rel(e.name)));
    } catch (err) {
      setPreviewErr((err as Error).message);
    }
  };

  const closePreview = () => {
    setSelected(null);
    setPreview(null);
    setPreviewErr(null);
  };

  const onDelete = (e: FileEntry) => {
    confirmAction({
      title: `Delete ${e.name}?`,
      message:
        e.kind === 'dir'
          ? 'The folder must be empty. This cannot be undone.'
          : 'This permanently removes the file.',
      confirmLabel: 'Delete',
      destructive: true,
      onConfirm: async () => {
        try {
          await deleteFile(rel(e.name));
          if (selected?.name === e.name) closePreview();
          await load(path);
        } catch (err) {
          Alert.alert('Delete failed', (err as Error).message);
        }
      },
    });
  };

  const startRename = (e: FileEntry) => {
    setRenaming(e);
    setRenameValue(e.name);
  };

  const commitRename = async () => {
    const target = renaming;
    const next = renameValue.trim();
    setRenaming(null);
    if (!target || !next || next === target.name) return;
    try {
      await renameFile(rel(target.name), rel(next));
      if (selected?.name === target.name) closePreview();
      await load(path);
    } catch (err) {
      Alert.alert('Rename failed', (err as Error).message);
    }
  };

  const sorted = entries
    ? [...entries].sort((a, b) => {
        if (a.kind !== b.kind) return a.kind === 'dir' ? -1 : 1;
        return a.name.localeCompare(b.name);
      })
    : null;

  return (
    <SafeAreaView style={styles.safe} edges={['top']}>
      <ScreenHeader title="Files" subtitle={path ? `/home/dev/${path}` : '/home/dev'} />

      {path ? (
        <Pressable style={styles.upRow} onPress={goUp}>
          <Ionicons name="arrow-up" size={16} color={colors.accent} />
          <Text style={styles.upText}>..</Text>
        </Pressable>
      ) : null}

      {error && sorted && sorted.length > 0 ? <ErrorBanner message={error} /> : null}

      {sorted === null ? (
        <Loading label="Loading files…" />
      ) : sorted.length === 0 ? (
        error ? (
          <EmptyState icon="cloud-offline-outline" title="Couldn't load files" subtitle={error} />
        ) : (
          <EmptyState icon="folder-open-outline" title="Empty folder" subtitle="Nothing here yet." />
        )
      ) : (
        <FlatList
          data={sorted}
          keyExtractor={(e) => `${e.kind}:${e.name}`}
          contentContainerStyle={styles.list}
          showsVerticalScrollIndicator={false}
          refreshControl={
            <RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={colors.accent} />
          }
          renderItem={({ item }) => (
            <View style={styles.row}>
              <Pressable
                style={styles.rowMain}
                onPress={() => (item.kind === 'dir' ? enter(item.name) : openPreview(item))}
              >
                <Ionicons
                  name={item.kind === 'dir' ? 'folder' : 'document-text-outline'}
                  size={18}
                  color={item.kind === 'dir' ? colors.accent : colors.textMuted}
                />
                <View style={styles.rowText}>
                  <Text style={styles.rowName} numberOfLines={1}>
                    {item.name}
                    {item.kind === 'dir' ? '/' : ''}
                  </Text>
                  <Text style={styles.rowMeta}>
                    {item.kind === 'file' ? fmtSize(item.size) : 'folder'} ·{' '}
                    {new Date(item.mtime * 1000).toLocaleDateString()}
                  </Text>
                </View>
              </Pressable>
              <View style={styles.rowActions}>
                <Pressable hitSlop={8} style={styles.actionBtn} onPress={() => startRename(item)}>
                  <Ionicons name="create-outline" size={18} color={colors.textMuted} />
                </Pressable>
                <Pressable hitSlop={8} style={styles.actionBtn} onPress={() => onDelete(item)}>
                  <Ionicons name="trash-outline" size={18} color={colors.danger} />
                </Pressable>
              </View>
            </View>
          )}
        />
      )}

      {/* Preview sheet */}
      <Modal
        visible={selected !== null}
        animationType="slide"
        transparent
        onRequestClose={closePreview}
      >
        <View style={styles.sheetRoot}>
          <Pressable style={styles.sheetBackdrop} onPress={closePreview} />
          <SafeAreaView style={styles.sheet} edges={['bottom']}>
            <View style={styles.sheetHead}>
              <Text style={styles.sheetTitle} numberOfLines={1}>
                {selected?.name}
              </Text>
              <Pressable hitSlop={10} onPress={closePreview}>
                <Ionicons name="close" size={22} color={colors.textMuted} />
              </Pressable>
            </View>
            <View style={styles.sheetBody}>
              {previewErr ? (
                <ErrorBanner message={previewErr} />
              ) : !preview ? (
                <Loading label="Loading preview…" />
              ) : preview.kind === 'text' ? (
                <>
                  {preview.truncated ? (
                    <Text style={styles.truncNote}>
                      Showing the first {fmtSize(preview.content.length)} of {fmtSize(preview.size)}.
                    </Text>
                  ) : null}
                  <ScrollView style={styles.textScroll} contentContainerStyle={styles.textInner}>
                    <Text style={styles.textBody}>{preview.content}</Text>
                  </ScrollView>
                  <Button
                    title="Copy contents"
                    icon="copy-outline"
                    variant="secondary"
                    onPress={async () => {
                      await Clipboard.setStringAsync(preview.content);
                      Alert.alert('Copied', 'File contents copied to the clipboard.');
                    }}
                  />
                </>
              ) : preview.kind === 'image' ? (
                <Image
                  source={{ uri: fileRawUrl(preview.path), headers: authHeaders() }}
                  style={styles.image}
                  resizeMode="contain"
                />
              ) : (
                <View style={styles.binary}>
                  <Ionicons name="document-outline" size={40} color={colors.textFaint} />
                  <Text style={styles.binaryText}>
                    {preview.kind === 'video'
                      ? 'Video file — open on the dashboard to play.'
                      : preview.reason === 'too_large'
                        ? 'File is too large to preview.'
                        : 'Binary file — no inline preview.'}
                  </Text>
                  <Text style={styles.binaryMeta}>{fmtSize(preview.size)}</Text>
                </View>
              )}
            </View>
          </SafeAreaView>
        </View>
      </Modal>

      {/* Rename modal */}
      <Modal visible={renaming !== null} animationType="fade" transparent onRequestClose={() => setRenaming(null)}>
        <View style={styles.dialogRoot}>
          <Pressable style={styles.sheetBackdrop} onPress={() => setRenaming(null)} />
          <View style={styles.dialog}>
            <Text style={styles.dialogTitle}>Rename</Text>
            <Text style={styles.dialogBody}>{renaming?.name}</Text>
            <TextInput
              value={renameValue}
              onChangeText={setRenameValue}
              autoFocus
              autoCapitalize="none"
              autoCorrect={false}
              placeholder="new-name"
              placeholderTextColor={colors.textFaint}
              style={styles.dialogInput}
              onSubmitEditing={commitRename}
            />
            <View style={styles.dialogActions}>
              <Button title="Cancel" variant="secondary" onPress={() => setRenaming(null)} style={styles.dialogBtn} />
              <Button
                title="Rename"
                onPress={commitRename}
                disabled={!renameValue.trim() || renameValue.trim() === renaming?.name}
                style={styles.dialogBtn}
              />
            </View>
          </View>
        </View>
      </Modal>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: colors.bg },
  upRow: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.sm,
    paddingHorizontal: space.lg,
    paddingBottom: space.sm,
  },
  upText: { color: colors.accent, fontSize: font.size.md, fontFamily: font.mono },
  list: { paddingBottom: space.xl },
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    paddingHorizontal: space.lg,
    paddingVertical: space.md,
    borderBottomWidth: 1,
    borderBottomColor: colors.border,
    gap: space.sm,
  },
  rowMain: { flex: 1, flexDirection: 'row', alignItems: 'center', gap: space.md },
  rowText: { flex: 1 },
  rowName: { color: colors.text, fontSize: font.size.md, fontWeight: '600' },
  rowMeta: { color: colors.textFaint, fontSize: font.size.xs, marginTop: 2, fontFamily: font.mono },
  rowActions: { flexDirection: 'row', alignItems: 'center', gap: space.md },
  actionBtn: { padding: 2 },

  sheetRoot: { flex: 1, justifyContent: 'flex-end' },
  sheetBackdrop: {
    position: 'absolute',
    top: 0,
    left: 0,
    right: 0,
    bottom: 0,
    backgroundColor: 'rgba(0,0,0,0.55)',
  },
  sheet: {
    maxHeight: '80%',
    backgroundColor: colors.bgElevated,
    borderTopLeftRadius: radius.lg,
    borderTopRightRadius: radius.lg,
    borderTopWidth: 1,
    borderColor: colors.border,
  },
  sheetHead: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: space.md,
    padding: space.lg,
    borderBottomWidth: 1,
    borderBottomColor: colors.border,
  },
  sheetTitle: { flex: 1, color: colors.text, fontSize: font.size.lg, fontWeight: '700', fontFamily: font.mono },
  sheetBody: { padding: space.lg, gap: space.md, minHeight: 160 },
  truncNote: { color: colors.textFaint, fontSize: font.size.xs },
  textScroll: {
    maxHeight: 380,
    backgroundColor: colors.bg,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: colors.border,
  },
  textInner: { padding: space.md },
  textBody: { color: colors.text, fontSize: font.size.xs, fontFamily: font.mono, lineHeight: 17 },
  image: { width: '100%', height: 360, borderRadius: radius.md, backgroundColor: colors.bg },
  binary: { alignItems: 'center', gap: space.sm, paddingVertical: space.xl },
  binaryText: { color: colors.textMuted, fontSize: font.size.sm, textAlign: 'center' },
  binaryMeta: { color: colors.textFaint, fontSize: font.size.xs, fontFamily: font.mono },

  dialogRoot: { flex: 1, alignItems: 'center', justifyContent: 'center', padding: space.xl },
  dialog: {
    width: '100%',
    maxWidth: 380,
    backgroundColor: colors.bgElevated,
    borderRadius: radius.lg,
    borderWidth: 1,
    borderColor: colors.border,
    padding: space.lg,
    gap: space.md,
  },
  dialogTitle: { color: colors.text, fontSize: font.size.lg, fontWeight: '700' },
  dialogBody: { color: colors.textMuted, fontSize: font.size.sm, fontFamily: font.mono },
  dialogInput: {
    backgroundColor: colors.bg,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: colors.border,
    paddingHorizontal: space.md,
    paddingVertical: space.sm,
    color: colors.text,
    fontSize: font.size.md,
    fontFamily: font.mono,
  },
  dialogActions: { flexDirection: 'row', gap: space.md },
  dialogBtn: { flex: 1 },
});
