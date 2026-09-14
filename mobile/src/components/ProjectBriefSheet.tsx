/**
 * The deterministic project brief as a bottom sheet (#471, rehomed in #683).
 *
 * It used to live inside the AI CTO screen, which is the only place it could be
 * reached. The brief is aggregated server-side with zero LLM calls, so it is
 * useful to any chat filed into a project — which is exactly what the web
 * gained when the brief became a pane of Chat. This is the phone's version of
 * that: a sheet the chat's header opens whenever the open chat has a project.
 *
 * It loads the brief itself (and re-loads when the project changes) so the
 * screen embedding it needs no brief plumbing of its own.
 */
import React, { useEffect, useState } from 'react';
import { Modal, Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import { getProjectBrief } from '../api/client';
import type { ProjectBrief } from '../api/types';
import { EmptyState, Loading } from './ui';
import { colors, font, radius, space } from '../theme';

type Nav = { navigate: (tab: string, opts?: object) => void };

export function ProjectBriefSheet({
  open,
  projectId,
  onClose,
  nav,
}: {
  open: boolean;
  /** The project the open chat is filed into; null → nothing to brief on. */
  projectId: string | null;
  onClose: () => void;
  nav: Nav;
}) {
  const [brief, setBrief] = useState<ProjectBrief | null>(null);
  const [loading, setLoading] = useState(false);

  // Fetch only while the sheet is actually open: a chat filed into a project
  // shouldn't pay for a brief nobody asked to see.
  useEffect(() => {
    if (!open || !projectId) return;
    let alive = true;
    setLoading(true);
    setBrief(null);
    (async () => {
      try {
        const b = await getProjectBrief(projectId);
        if (alive) setBrief(b);
      } catch {
        /* the empty state below says so */
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [open, projectId]);

  return (
    <Modal visible={open} animationType="slide" transparent onRequestClose={onClose}>
      <Pressable style={styles.sheetScrim} onPress={onClose} />
      <View style={styles.sheet}>
        <View style={styles.sheetHandle} />
        {loading ? (
          <Loading label="Loading brief…" />
        ) : !brief ? (
          <EmptyState
            title="No brief"
            subtitle="This chat isn't filed into a project with a brief yet."
            icon="reader-outline"
          />
        ) : (
          <ScrollView contentContainerStyle={styles.sheetBody}>
            <Text style={styles.sheetTitle}>{brief.project.name}</Text>
            {!!brief.project.north_star && (
              <>
                <Text style={styles.sheetH}>NORTH STAR</Text>
                <Text style={styles.sheetText}>{brief.project.north_star}</Text>
              </>
            )}
            <Text style={styles.sheetH}>NOW</Text>
            <Text style={styles.sheetText}>
              {brief.tasks.running} running · {brief.tasks.waiting} waiting ·{' '}
              {brief.tasks.total} tasks
            </Text>
            {brief.goals.length > 0 && (
              <>
                <Text style={styles.sheetH}>GOALS ({brief.counts.goals})</Text>
                {brief.goals.map((g) => (
                  <Text key={g.key} style={styles.sheetText}>
                    • {g.value}
                  </Text>
                ))}
              </>
            )}
            {brief.decisions.length > 0 && (
              <>
                <Text style={styles.sheetH}>DECISION LOG ({brief.counts.decisions})</Text>
                {brief.decisions.map((d) => (
                  <Text key={d.key} style={styles.sheetText}>
                    • {d.value}
                  </Text>
                ))}
              </>
            )}
            {brief.tasks.recent.length > 0 && (
              <>
                <Text style={styles.sheetH}>RECENT TASKS</Text>
                {brief.tasks.recent.map((t) => (
                  <Pressable
                    key={t.task_id}
                    onPress={() => {
                      onClose();
                      nav.navigate('Tasks', {
                        screen: 'TaskDetail',
                        params: { id: t.task_id },
                        initial: false,
                      });
                    }}
                  >
                    <Text style={styles.sheetLink} numberOfLines={1}>
                      → {t.prompt || t.task_id}
                    </Text>
                  </Pressable>
                ))}
              </>
            )}
          </ScrollView>
        )}
      </View>
    </Modal>
  );
}

const styles = StyleSheet.create({
  sheetScrim: { flex: 1, backgroundColor: 'rgba(0,0,0,0.5)' },
  sheet: {
    backgroundColor: colors.card,
    borderTopLeftRadius: radius.xl,
    borderTopRightRadius: radius.xl,
    maxHeight: '75%',
    paddingBottom: space.xl,
  },
  sheetHandle: {
    alignSelf: 'center',
    width: 36,
    height: 4,
    borderRadius: 2,
    backgroundColor: colors.borderStrong,
    marginTop: space.sm,
    marginBottom: space.xs,
  },
  sheetBody: { padding: space.lg, gap: 6 },
  sheetTitle: { color: colors.text, fontSize: font.size.lg, fontWeight: '700' },
  sheetH: {
    color: colors.textFaint,
    fontSize: font.size.xs,
    letterSpacing: 1,
    marginTop: space.md,
  },
  sheetText: { color: colors.text, fontSize: font.size.sm, lineHeight: 20 },
  sheetLink: { color: colors.accent, fontSize: font.size.sm, lineHeight: 20 },
});
