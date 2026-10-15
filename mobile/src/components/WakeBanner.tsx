/**
 * App-wide strip for a workspace that isn't answering (#728): what we know
 * (asleep, can't reach, starting), a Wake button, and the health polling that
 * notices when it is back.
 *
 * It sits in the layout below the screens rather than floating over them, so
 * it never covers an input and stays within thumb reach. Tap-to-wake only:
 * opening the app never wakes a 4 GiB pod on its own.
 */
import React, { useEffect, useState } from 'react';
import { ActivityIndicator, AppState, StyleSheet, Text, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { getHealth, wakeViaKeeper } from '../api/client';
import { listControllerWorkspaces, startWorkspace } from '../api/controller';
import { getConfig, hasController } from '../store/config';
import { useConfig } from '../store/useConfig';
import { useWorkspaceState } from '../store/useWorkspaceState';
import { reportAwake, setReach } from '../store/workspaceState';
import { colors, font, space } from '../theme';
import {
  matchWorkspace,
  pollDelay,
  reachFromController,
  wakeWorkspace,
  type Reach,
} from '../util/workspaceReach';
import { Button, Ionicons } from './ui';

// A pod start is about a minute; past this, say so rather than spin forever.
const SLOW_WAKE_MS = 3 * 60_000;

/** Health-check with backoff while the workspace is away. Stops while the app
 *  is in the background and checks at once when it returns. */
function usePollUntilAwake(away: boolean): void {
  useEffect(() => {
    if (!away) return;
    let attempt = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let inFlight = false;
    let stopped = false;
    const schedule = () => {
      clearTimeout(timer);
      if (!stopped && AppState.currentState === 'active') {
        timer = setTimeout(tick, pollDelay(attempt++));
      }
    };
    const tick = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        await getHealth();
        reportAwake();
      } catch {
        // still away — the next tick tries again
      } finally {
        inFlight = false;
      }
      schedule();
    };
    schedule();
    const sub = AppState.addEventListener('change', (s) => {
      clearTimeout(timer);
      if (s === 'active') {
        attempt = 0;
        void tick();
      }
    });
    return () => {
      stopped = true;
      clearTimeout(timer);
      sub.remove();
    };
  }, [away]);
}

/** "Can't reach" is a guess; the controller, when connected, knows whether the
 *  Deployment is scaled to 0 (asleep) or starting. */
function useControllerLabel(reach: Reach): void {
  const cfg = useConfig();
  useEffect(() => {
    if (reach !== 'unreachable' || !hasController(cfg)) return;
    let cancelled = false;
    listControllerWorkspaces()
      .then((list) => {
        const ws = matchWorkspace(cfg.host, list);
        const known = ws && reachFromController(ws);
        if (!cancelled && known && known !== 'awake') setReach(known);
      })
      .catch(() => {
        // the label stays "can't reach" — still true
      });
    return () => {
      cancelled = true;
    };
  }, [reach, cfg]);
}

function elapsed(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

export function WakeBanner() {
  const { reach, since } = useWorkspaceState();
  const insets = useSafeAreaInsets();
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(Date.now());
  const { controllerHost, controllerToken } = useConfig();
  const away = reach !== 'awake';

  usePollUntilAwake(away);
  useControllerLabel(reach);

  // A failed wake's message is about that attempt: drop it once the workspace
  // is back, or once the controller connection it may point at has changed.
  useEffect(() => {
    setError(null);
  }, [away, controllerHost, controllerToken]);

  useEffect(() => {
    if (reach !== 'waking') return;
    setNow(Date.now());
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [reach]);

  if (!away) return null;

  const onWake = async () => {
    const before = reach;
    setError(null);
    setReach('waking');
    const c = getConfig();
    const result = await wakeWorkspace({
      host: c.host,
      viaKeeper: wakeViaKeeper,
      controller: hasController(c) ? { list: listControllerWorkspaces, start: startWorkspace } : null,
    });
    if (!result.ok) {
      setReach(before);
      setError(result.message);
    }
  };

  const waking = reach === 'waking';
  const title = waking
    ? `Starting your workspace… ${elapsed(now - since)}`
    : reach === 'asleep'
      ? 'Your workspace is asleep'
      : "Can't reach your workspace";
  const detail =
    error ??
    (waking
      ? now - since > SLOW_WAKE_MS
        ? 'Taking longer than usual — it may need an admin.'
        : 'Your data will refresh when it is back.'
      : reach === 'unreachable'
        ? 'It may be asleep or restarting.'
        : 'Wake it up to load fresh data.');

  return (
    <View
      nativeID="wake-banner"
      accessibilityRole="alert"
      style={[styles.bar, { paddingBottom: space.md + insets.bottom }]}
    >
      {waking ? (
        <ActivityIndicator size="small" color={colors.textMuted} />
      ) : (
        <Ionicons
          name={reach === 'asleep' ? 'moon-outline' : 'cloud-offline-outline'}
          size={20}
          color={colors.warning}
        />
      )}
      <View style={styles.text}>
        <Text style={styles.title}>{title}</Text>
        <Text style={[styles.detail, error ? styles.error : null]}>{detail}</Text>
      </View>
      {waking ? null : <Button title="Wake up" onPress={onWake} style={styles.button} />}
    </View>
  );
}

const styles = StyleSheet.create({
  bar: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.md,
    paddingHorizontal: space.lg,
    paddingTop: space.md,
    backgroundColor: colors.bgElevated,
    borderTopWidth: 1,
    borderTopColor: colors.border,
  },
  text: { flex: 1 },
  title: { color: colors.text, fontSize: font.size.md, fontWeight: '600' },
  detail: { color: colors.textMuted, fontSize: font.size.sm, marginTop: 2 },
  error: { color: colors.danger },
  button: { minWidth: 104 },
});
