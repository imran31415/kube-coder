/** Settings card for self-serve workspace updates. Mirrors the web dashboard's
 *  UpdatesSection (charts/workspace/web/src/routes/settings/UpdatesSection): read
 *  current vs latest version, then broker a "restart & pull latest" to the
 *  controller. While the pod rolls (~1 min) it shows a restarting state that
 *  polls /health until the fresh pod answers again, then refreshes.
 *
 *  Renders nothing when self-serve updates aren't configured (available:false),
 *  so default deployments don't show a dead section — same as the SPA. */
import React, { useEffect, useState } from 'react';
import { ActivityIndicator, StyleSheet, Text, View } from 'react-native';
import { getHealth, getWorkspaceVersion, updateWorkspace, type WorkspaceVersion } from '../api/client';
import { colors, font, radius, space } from '../theme';
import { confirmAction } from '../util/confirm';
import { Button, Card, Label } from './ui';

export function UpdatesCard({ readOnly }: { readOnly?: boolean }) {
  const [info, setInfo] = useState<WorkspaceVersion | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [restarting, setRestarting] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  async function refresh() {
    try {
      setInfo(await getWorkspaceVersion());
    } catch {
      // server unavailable — leave info null; the card stays hidden.
    } finally {
      setLoaded(true);
    }
  }
  useEffect(() => { void refresh(); }, []);

  // Once the update rolls the pod, poll /health until it goes DOWN and then
  // comes back UP, then drop the restarting state and refresh the version.
  // Unlike the web (which reloads the SPA), the native app keeps running — its
  // API calls simply fail during the ~1 min Recreate window and recover.
  useEffect(() => {
    if (!restarting) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let sawDown = false;
    const poll = async () => {
      try {
        const h = await getHealth();
        if (cancelled) return;
        if (h.ok) {
          if (sawDown) {
            setRestarting(false);
            setNote('Workspace updated and back online.');
            await refresh();
            return;
          }
        } else {
          sawDown = true;
        }
      } catch {
        sawDown = true; // pod down / network blip during the rollout
      }
      if (!cancelled) timer = setTimeout(() => void poll(), 4000);
    };
    // Give the old pod a moment to begin terminating before the first probe.
    timer = setTimeout(() => void poll(), 6000);
    // Safety net: never strand the user on the restarting state forever.
    const fallback = setTimeout(() => {
      if (!cancelled) { setRestarting(false); void refresh(); }
    }, 5 * 60 * 1000);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
      clearTimeout(fallback);
    };
  }, [restarting]);

  // Self-serve disabled (or not yet loaded) => render nothing.
  if (!loaded || !info || !info.available) return null;

  const current = info.version ?? 'unknown';
  const latest = info.latestVersion ?? null;
  const canUpdate = !!info.updateAvailable;

  function onUpdate() {
    confirmAction({
      title: `Restart & update to ${latest ?? 'the latest release'}?`,
      message:
        'The pod restarts — running processes, terminal sessions and unsaved ' +
        'in-memory state are lost. Your /home/dev disk is preserved.',
      confirmLabel: 'Restart & update',
      destructive: true,
      onConfirm: async () => {
        setBusy(true);
        setNote(null);
        try {
          const r = await updateWorkspace();
          if (r.error) throw new Error(r.error);
          if (r.rolled) {
            setRestarting(true);
          } else {
            setNote(`Already up to date (${r.toVersion ?? current}).`);
            setTimeout(() => void refresh(), 1000);
          }
        } catch (e) {
          setNote((e as Error).message || 'Update failed.');
        } finally {
          setBusy(false);
        }
      },
    });
  }

  return (
    <Card style={{ gap: space.md, marginTop: space.lg }}>
      <Label>Updates</Label>

      <View style={styles.verRow}>
        <View style={[styles.pill, { borderColor: (canUpdate ? colors.warning : colors.success) + '59' }]}>
          <Text style={[styles.pillText, { color: canUpdate ? colors.warning : colors.success }]}>{current}</Text>
        </View>
        {canUpdate && latest ? (
          <>
            <Text style={styles.arrow}>→</Text>
            <View style={[styles.pill, { borderColor: colors.success + '59' }]}>
              <Text style={[styles.pillText, { color: colors.success }]}>{latest}</Text>
            </View>
          </>
        ) : null}
      </View>

      <Text style={styles.help}>
        {restarting
          ? 'Applying the update and bringing your workspace back online. This usually takes about a minute.'
          : canUpdate
            ? `A newer release (${latest}) is available. Updating restarts the pod onto the new image.`
            : latest
              ? `Running the latest release (${latest}).`
              : 'Latest-release lookup is currently unavailable.'}
      </Text>

      {note ? <Text style={styles.note}>{note}</Text> : null}

      {restarting ? (
        <View style={styles.restartRow}>
          <ActivityIndicator color={colors.accent} />
          <Text style={styles.restartText}>Restarting…</Text>
        </View>
      ) : !readOnly ? (
        <Button
          title={busy ? 'Updating…' : canUpdate ? 'Restart & update' : 'Up to date'}
          icon={canUpdate ? 'cloud-download-outline' : 'checkmark-outline'}
          variant={canUpdate ? 'primary' : 'secondary'}
          onPress={onUpdate}
          disabled={busy || !canUpdate}
        />
      ) : null}
    </Card>
  );
}

const styles = StyleSheet.create({
  verRow: { flexDirection: 'row', alignItems: 'center', gap: space.sm },
  pill: {
    alignSelf: 'flex-start',
    borderRadius: radius.pill,
    borderWidth: 1,
    paddingVertical: 3,
    paddingHorizontal: 10,
  },
  pillText: { fontSize: font.size.xs, fontWeight: '700', fontFamily: font.mono },
  arrow: { color: colors.textFaint, fontSize: font.size.md },
  help: { color: colors.textMuted, fontSize: font.size.sm, lineHeight: 19 },
  note: { color: colors.text, fontSize: font.size.sm, lineHeight: 19 },
  restartRow: { flexDirection: 'row', alignItems: 'center', gap: space.sm },
  restartText: { color: colors.textMuted, fontSize: font.size.sm, fontWeight: '600' },
});
