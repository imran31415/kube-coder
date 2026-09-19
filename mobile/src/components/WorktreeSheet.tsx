/**
 * An isolated Build's changes (#701), as a full-screen sheet: its branch,
 * every file it changed (committed or not) with a diff on tap, the command
 * that gets the work out, and Remove — which never deletes the branch.
 *
 * Every tappable thing is at least 44pt (the iOS minimum) and labelled.
 */
import { Ionicons } from '@expo/vector-icons';
import * as Clipboard from 'expo-clipboard';
import React, { useCallback, useEffect, useState } from 'react';
import {
  ActivityIndicator,
  Modal,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import {
  ApiError,
  getTaskWorktree,
  getTaskWorktreeDiff,
  removeTaskWorktree,
} from '../api/client';
import type { TaskWorktreeView, WorktreeDiff, WorktreeFile } from '../api/types';
import { colors, font, radius, space } from '../theme';
import { confirmAction } from '../util/confirm';
import { formatDiffStat, removeErrorKind } from '../util/worktree';
import { Button } from './ui';

const BLOCKED: Record<string, string> = {
  live: 'Stop the Build before removing its worktree.',
  removed: 'This worktree has already been removed.',
  repo_missing: 'The repository this worktree came from is gone.',
};

export function WorktreeSheet({
  taskId,
  visible,
  onClose,
}: {
  taskId: string;
  visible: boolean;
  onClose: () => void;
}) {
  const [view, setView] = useState<TaskWorktreeView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [openFile, setOpenFile] = useState<string | null>(null);
  const [diff, setDiff] = useState<WorktreeDiff | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const load = useCallback(async (fresh = false) => {
    try {
      setView(await getTaskWorktree(taskId, fresh));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load the worktree');
    }
  }, [taskId]);

  useEffect(() => {
    if (visible) void load(true);
  }, [visible, load]);

  async function toggle(f: WorktreeFile) {
    if (openFile === f.path) {
      setOpenFile(null);
      return;
    }
    setOpenFile(f.path);
    setDiff(null);
    try {
      setDiff(await getTaskWorktreeDiff(taskId, f.path));
    } catch (e) {
      setDiff({ file: f.path, diff: e instanceof Error ? e.message : 'Could not load the diff', truncated: false, binary: false });
    }
  }

  async function copyPush() {
    if (!view?.push_command) return;
    await Clipboard.setStringAsync(view.push_command);
    setNote('Push command copied');
  }

  async function remove(force: boolean) {
    setBusy(true);
    try {
      const out = await removeTaskWorktree(taskId, force);
      setNote(`Worktree removed. Branch ${out.branch ?? view?.worktree.branch ?? ''} is kept.`);
      await load(true);
    } catch (e) {
      if (removeErrorKind(e) === 'dirty' && !force) {
        askForce();
      } else {
        setNote(e instanceof ApiError ? e.message : 'Could not remove the worktree');
      }
    } finally {
      setBusy(false);
    }
  }

  function askForce() {
    confirmAction({
      title: 'Discard uncommitted changes?',
      message: 'This worktree has changes that were never committed. Removing it deletes them '
        + `for good. Committed work on ${view?.worktree.branch ?? 'its branch'} is kept.`,
      confirmLabel: 'Discard and remove',
      destructive: true,
      onConfirm: () => void remove(true),
    });
  }

  function onRemove() {
    if (view?.remove_blocked === 'dirty') {
      askForce();
      return;
    }
    confirmAction({
      title: 'Remove this worktree?',
      message: `The folder is deleted. The branch ${view?.worktree.branch ?? ''} and its commits are kept.`,
      confirmLabel: 'Remove',
      destructive: true,
      onConfirm: () => void remove(false),
    });
  }

  const st = view?.status;
  const blocked = view?.remove_blocked ?? '';
  return (
    <Modal visible={visible} animationType="slide" onRequestClose={onClose}>
      <SafeAreaView style={styles.safe} edges={['top', 'bottom']}>
        <View style={styles.bar}>
          <Text style={styles.barTitle}>Changes</Text>
          <Pressable
            onPress={onClose}
            style={styles.iconBtn}
            accessibilityRole="button"
            accessibilityLabel="Close changes"
          >
            <Ionicons name="close" size={22} color={colors.text} />
          </Pressable>
        </View>

        {!view && !error ? (
          <ActivityIndicator style={{ marginTop: space.xl }} color={colors.accent} />
        ) : null}
        {error && !view ? <Text style={styles.error}>{error}</Text> : null}

        {view ? (
          <ScrollView contentContainerStyle={styles.body}>
            <Text style={styles.branch} selectable>⎇ {st?.branch || view.worktree.branch}</Text>
            <Text style={styles.muted}>
              from {view.worktree.base_ref || 'HEAD'} @ {view.worktree.base_sha.slice(0, 7)}
              {view.worktree.port ? ` · port ${view.worktree.port}` : ''}
              {view.live ? ' · running' : ''}
            </Text>

            {!view.exists ? (
              <Text style={styles.note}>
                This worktree has been removed.{' '}
                {view.branch_exists ? `Its branch ${view.worktree.branch} and its commits are kept.` : ''}
              </Text>
            ) : null}

            {view.status_error ? <Text style={styles.error}>{view.status_error}</Text> : null}

            {st ? (
              <>
                <Text style={styles.summary}>
                  {formatDiffStat(st)}
                  {typeof st.ahead === 'number' ? ` · ${st.ahead} commit${st.ahead === 1 ? '' : 's'}` : ''}
                  {st.dirty + st.untracked > 0 ? ` · ${st.dirty + st.untracked} uncommitted` : ''}
                </Text>
                {st.files.map((f) => (
                  <View key={f.path} style={styles.file}>
                    <Pressable
                      onPress={() => void toggle(f)}
                      style={styles.fileRow}
                      accessibilityRole="button"
                      accessibilityLabel={`Diff of ${f.path}`}
                      accessibilityState={{ expanded: openFile === f.path }}
                    >
                      <Text style={[styles.fileStatus, statusStyle(f.status)]}>{f.status}</Text>
                      <Text style={styles.filePath} numberOfLines={2}>{f.path}</Text>
                      {f.binary ? (
                        <Text style={styles.muted}>binary</Text>
                      ) : typeof f.added === 'number' ? (
                        <Text style={styles.counts}>
                          <Text style={{ color: colors.success }}>+{f.added}</Text>{' '}
                          <Text style={{ color: colors.danger }}>−{f.deleted}</Text>
                        </Text>
                      ) : null}
                    </Pressable>
                    {openFile === f.path ? (
                      <ScrollView horizontal style={styles.diffBox}>
                        {diff ? (
                          <Text style={styles.diffText} selectable>
                            {diff.binary ? 'Binary file — no text diff.' : diff.diff}
                          </Text>
                        ) : (
                          <ActivityIndicator color={colors.accent} />
                        )}
                      </ScrollView>
                    ) : null}
                  </View>
                ))}
                {st.truncated ? (
                  <Text style={styles.muted}>Showing the first {st.files.length} files.</Text>
                ) : null}
              </>
            ) : null}

            {view.push_command ? (
              <View style={styles.section}>
                <Text style={styles.label}>Get the work out</Text>
                <Text style={styles.cmd} selectable>{view.push_command}</Text>
                <Button title="Copy push command" icon="copy-outline" variant="secondary" onPress={() => void copyPush()} />
              </View>
            ) : null}

            {note ? <Text style={styles.note} accessibilityRole="alert">{note}</Text> : null}

            {view.exists ? (
              <View style={styles.section}>
                <Button
                  title="Remove worktree"
                  icon="trash-outline"
                  variant="danger"
                  onPress={onRemove}
                  loading={busy}
                  disabled={busy || blocked === 'live' || blocked === 'removed'}
                />
                {BLOCKED[blocked] ? <Text style={styles.muted}>{BLOCKED[blocked]}</Text> : null}
                <Text style={styles.muted}>Removing deletes the folder; the branch is always kept.</Text>
              </View>
            ) : null}
          </ScrollView>
        ) : null}
      </SafeAreaView>
    </Modal>
  );
}

function statusStyle(s: string) {
  if (s === 'A' || s === '?') return { color: colors.success };
  if (s === 'D') return { color: colors.danger };
  return { color: colors.warning };
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: colors.bg },
  bar: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingHorizontal: space.lg,
    borderBottomWidth: 1,
    borderBottomColor: colors.border,
  },
  barTitle: { color: colors.text, fontSize: font.size.lg, fontWeight: '600' },
  iconBtn: { minWidth: 44, minHeight: 44, alignItems: 'center', justifyContent: 'center' },
  body: { padding: space.lg, gap: space.sm, paddingBottom: space.xxl },
  branch: { color: colors.text, fontFamily: font.mono, fontSize: font.size.md, fontWeight: '600' },
  muted: { color: colors.textFaint, fontSize: font.size.sm },
  summary: { color: colors.text, fontSize: font.size.md, marginTop: space.md },
  file: {
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.md,
    backgroundColor: colors.bgElevated,
  },
  fileRow: {
    minHeight: 44,
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.sm,
    paddingHorizontal: space.md,
  },
  fileStatus: { width: 16, fontFamily: font.mono, fontWeight: '700', textAlign: 'center' },
  filePath: { flex: 1, color: colors.text, fontFamily: font.mono, fontSize: font.size.sm },
  counts: { fontFamily: font.mono, fontSize: font.size.sm },
  diffBox: {
    maxHeight: 360,
    borderTopWidth: 1,
    borderTopColor: colors.border,
    padding: space.sm,
  },
  diffText: { color: colors.text, fontFamily: font.mono, fontSize: font.size.xs, lineHeight: 16 },
  section: { marginTop: space.lg, gap: space.sm },
  label: { color: colors.textMuted, fontSize: font.size.sm, fontWeight: '600' },
  cmd: {
    color: colors.text,
    fontFamily: font.mono,
    fontSize: font.size.xs,
    backgroundColor: colors.bgElevated,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.sm,
    padding: space.sm,
  },
  note: {
    color: colors.text,
    backgroundColor: colors.bgElevated,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.md,
    padding: space.md,
    marginTop: space.md,
  },
  error: { color: colors.danger, padding: space.lg },
});
